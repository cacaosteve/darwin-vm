#!/usr/bin/env python3
"""Run one Home swipe and one gated Settings tap, with HMP/OCR evidence."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time


REPO = Path(__file__).resolve().parents[2]
SWIPE = (16371, 32563, 16371, 6411)
SETTINGS_TAP = (20622, 25500)


def write_row(path, row):
    row["host_time"] = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as output:
        output.write(json.dumps(row, sort_keys=True) + "\n")
        output.flush()
    print("SETTINGS_APP_DRIVER " + json.dumps(row, sort_keys=True), flush=True)


class QMP:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(20)
        self.sock.connect(str(path))
        self.buffer = b""
        self._next()
        self.execute("qmp_capabilities")

    def _next(self):
        while b"\n" not in self.buffer:
            self.buffer += self.sock.recv(65536)
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def execute(self, command, arguments=None):
        self.sock.sendall(json.dumps({"execute": command,
                                     "arguments": arguments or {}}).encode() + b"\n")
        while True:
            result = self._next()
            if "event" in result:
                continue
            if "error" in result:
                raise RuntimeError("QMP %s failed: %s" % (command, result["error"]))
            return result.get("return")

    def close(self):
        self.sock.close()


def capture(sock, rec, name):
    path = rec / "frames" / name
    result = subprocess.run([sys.executable, str(REPO / "tools/hmp.py"),
                             str(sock), "screendump", str(path), "-f", "png"],
                            text=True, capture_output=True, timeout=45)
    row = {"label": name, "path": str(path), "returncode": result.returncode,
           "stderr": result.stderr[-1000:], "stdout": result.stdout[-1000:]}
    if path.exists():
        data = path.read_bytes()
        row["sha256"] = hashlib.sha256(data).hexdigest()
        try:
            from PIL import Image
            with Image.open(path) as image:
                row.update({"width": image.width, "height": image.height,
                            "nonblack_pixels": sum(max(px) > 8 for px in
                                                    image.convert("RGB").getdata())})
        except Exception as error:
            row["image_error"] = type(error).__name__
        tesseract = subprocess.run(["/opt/homebrew/bin/tesseract", str(path),
                                    "stdout", "--psm", "6"],
                                   text=True, capture_output=True, timeout=45)
        row["ocr"] = tesseract.stdout.strip()
        row["ocr_returncode"] = tesseract.returncode
        (rec / "frames" / (name + ".ocr.txt")).write_text(row["ocr"] + "\n")
    write_row(rec / "input" / "driver.jsonl", row)
    return row


def wait_for_ready(serial, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = serial.read_text(errors="replace")
        except OSError:
            text = ""
        if "DVM_INPUT_READY protocol=1" in text:
            return True
        time.sleep(1)
    return False


def ack_counts(path):
    sent = acked = failed = 0
    last_ack = None
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return sent, acked, failed, last_ack
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("event") == "sent":
            sent += 1
        elif row.get("event") == "ack":
            acked += 1
            failed += not row.get("success", False)
            last_ack = row
    return sent, acked, failed, last_ack


def send_swipe(qmp, rec, relay_log, timeout):
    x1, y1, x2, y2 = SWIPE
    start = time.time()
    write_row(rec / "input" / "driver.jsonl", {
        "stage": "swipe_start", "start": [x1, y1], "end": [x2, y2],
        "steps": 32, "relay_log": str(relay_log)})
    for i in range(33):
        y = round(y1 + (y2 - y1) * i / 32)
        events = [
            {"type": "abs", "data": {"axis": "x", "value": x1}},
            {"type": "abs", "data": {"axis": "y", "value": y}},
        ]
        if i == 0:
            events.append({"type": "btn", "data": {"button": "left", "down": True}})
        qmp.execute("input-send-event", {"events": events})
        if i < 32:
            time.sleep(0.05)
    qmp.execute("input-send-event", {"events": [
        {"type": "abs", "data": {"axis": "x", "value": x2}},
        {"type": "abs", "data": {"axis": "y", "value": y2}},
        {"type": "btn", "data": {"button": "left", "down": False}},
    ]})
    qmp_states = len(relay_log.parent.joinpath("qemu-touch.jsonl").read_text(
        errors="replace").splitlines())
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        sent, acked, failed, last_ack = ack_counts(relay_log)
        if failed:
            raise RuntimeError("one or more swipe input records failed")
        terminal_release_acked = bool(
            last_ack and last_ack.get("success") is True and
            last_ack.get("down") is False and
            [last_ack.get("x"), last_ack.get("y")] == [x2, y2])
        if sent > 0 and sent == acked and terminal_release_acked:
            result = {"stage": "swipe_ack_complete", "sent": sent,
                      "acked": acked, "failed": failed,
                      "qmp_pointer_states": qmp_states,
                      "relay_coalesced_motion": sent < qmp_states,
                      "terminal_release_acked": terminal_release_acked,
                      "elapsed_s": time.time() - start}
            write_row(rec / "input" / "driver.jsonl", result)
            return
        time.sleep(0.5)
    sent, acked, failed, last_ack = ack_counts(relay_log)
    raise TimeoutError("swipe ACK bound; sent=%d acked=%d failed=%d" %
                       (sent, acked, failed))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--record", type=Path, required=True)
    parser.add_argument("--ready-timeout", type=float, default=3600)
    parser.add_argument("--swipe-ack-timeout", type=float, default=600)
    parser.add_argument("--relay-pid-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--tap-marker", type=Path, required=True)
    args = parser.parse_args()
    root = Path("/tmp/dvm")
    serial = root / "probe" / (args.tag + ".serial.log")
    event_dir = root / (args.tag + ".events")
    event_dir.mkdir(parents=True, exist_ok=True)
    touch_log = event_dir / "qemu-touch.jsonl"
    touch_log.touch(exist_ok=True)
    qmp = QMP(root / (args.tag + ".qmp.sock"))
    try:
        if not wait_for_ready(serial, args.ready_timeout):
            capture(root / (args.tag + ".sock"), args.record, "input-not-ready.png")
            write_row(args.record / "input" / "driver.jsonl", {
                "stage": "input_ready_timeout", "timeout_s": args.ready_timeout,
                "serial_path": str(serial)})
            args.stop_file.write_text("settings-probe-input-ready-timeout\n")
            return 2
        write_row(args.record / "input" / "driver.jsonl", {
            "stage": "input_ready", "serial_path": str(serial),
            "qmp_status": qmp.execute("query-status")})
        capture(root / (args.tag + ".sock"), args.record, "before-swipe.png")
        send_swipe(qmp, args.record,
                   args.record / "input" / "recap-relay.jsonl",
                   args.swipe_ack_timeout)
        for name, delay in (("home-after-swipe-5s.png", 5),
                            ("home-after-swipe-30s.png", 25),
                            ("home-after-swipe-60s.png", 30)):
            time.sleep(delay)
            row = capture(root / (args.tag + ".sock"), args.record, name)
        ocr = row.get("ocr", "").lower()
        home_gate = "search" in ocr and "swipe up to open" not in ocr
        write_row(args.record / "input" / "driver.jsonl", {
            "stage": "home_visual_gate", "accepted_by": "OCR plus the previously validated P548 home position",
            "search_text_seen": "search" in ocr,
            "lock_prompt_seen": "swipe up to open" in ocr,
            "gate_passed": home_gate,
        })
        if not home_gate:
            args.stop_file.write_text("settings-probe-home-frame-not-confirmed\n")
            return 3

        # Open the LLDB-side gate immediately before the single native tap.
        args.tap_marker.parent.mkdir(parents=True, exist_ok=True)
        args.tap_marker.write_text(json.dumps({"time": time.time(),
                                               "normalized": SETTINGS_TAP,
                                               "action": "single Settings icon tap"}) + "\n")
        try:
            relay_pid = int(args.relay_pid_file.read_text().strip())
            os.kill(relay_pid, 15)
        except (OSError, ValueError):
            pass
        time.sleep(1)
        tap = subprocess.run([
            sys.executable, str(REPO / "tools/input/relay.py"),
            "--events", str(touch_log),
            "--uart", str(root / (args.tag + ".uart.sock")),
            "--log", str(args.record / "input" / "settings-native-tap.jsonl"),
            "--ack-timeout", "120", "--tap", str(SETTINGS_TAP[0]),
            str(SETTINGS_TAP[1]),
        ], text=True, capture_output=True, timeout=150)
        write_row(args.record / "input" / "driver.jsonl", {
            "stage": "settings_tap_result", "returncode": tap.returncode,
            "stdout": tap.stdout[-3000:], "stderr": tap.stderr[-3000:],
            "normalized": SETTINGS_TAP})
        if tap.returncode:
            args.stop_file.write_text("settings-probe-native-tap-failed\n")
            return tap.returncode

        for name, delay in (("settings-after-tap-5s.png", 5),
                            ("settings-after-tap-20s.png", 15),
                            ("settings-after-tap-55s.png", 35)):
            time.sleep(delay)
            capture(root / (args.tag + ".sock"), args.record, name)
        args.stop_file.write_text("settings-probe-observation-complete\n")
        return 0
    except Exception as error:
        write_row(args.record / "input" / "driver.jsonl", {
            "stage": "driver_error", "error": repr(error)})
        args.stop_file.write_text("settings-probe-driver-error\n")
        return 1
    finally:
        qmp.close()


if __name__ == "__main__":
    sys.exit(main())

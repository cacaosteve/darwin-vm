#!/usr/bin/env python3
"""Drive the validated v1 guest input path after the real shell returns.

The current guest helper accepts DVMINPUT1, while the DVMI2 trigger does not
interoperate with that helper version. This companion waits for the real shell,
gesture observer, and guest input service, sends one ordinary Home event, then
waits for SpringBoard to create its Home entity. It sends one Recap bottom-up
swipe, requires a pan-confirmed lock dismissal, and allows a bounded render
sample before asking the runner to freeze and archive the child.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from PIL import Image


REPO = Path(__file__).resolve().parents[2]


class LogFollower:
    def __init__(self, path):
        self.path = Path(path)
        self.offset = 0
        self.carry = b""

    def read_lines(self):
        try:
            with self.path.open("rb") as stream:
                stream.seek(self.offset)
                data = stream.read()
                self.offset = stream.tell()
        except OSError:
            return []
        lines = (self.carry + data).split(b"\n")
        self.carry = lines.pop()
        return [line.decode("utf-8", "replace") for line in lines]


def wait_file(path, deadline, description):
    path = Path(path)
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(1)
    print("LEGACY_INPUT_TIMEOUT " + json.dumps({
        "waiting_for": description, "path": str(path),
    }, sort_keys=True), flush=True)
    return False


def wait_log_marker(follower, marker, deadline):
    while time.monotonic() < deadline:
        for line in follower.read_lines():
            if marker in line:
                print("LEGACY_INPUT_MARKER " + json.dumps({
                    "marker": marker, "line": line.strip(),
                }, sort_keys=True), flush=True)
                return True
        time.sleep(1)
    print("LEGACY_INPUT_TIMEOUT " + json.dumps({
        "waiting_for": marker,
    }, sort_keys=True), flush=True)
    return False


def run_relay(args, events, uart, log, *relay_args):
    command = [sys.executable, str(REPO / "tools/input/relay.py"),
               "--events", str(events), "--uart", str(uart),
               "--log", str(log), "--ack-timeout", str(args.ack_timeout),
               *relay_args]
    result = subprocess.run(command, text=True, capture_output=True,
                           timeout=args.relay_timeout)
    print("LEGACY_INPUT_RELAY " + json.dumps({
        "command": command, "returncode": result.returncode,
        "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:],
    }, sort_keys=True), flush=True)
    return result.returncode == 0


def stop_runner(path, reason):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".legacy-input.tmp")
    temporary.write_text(reason + "\n")
    os.replace(temporary, path)


def capture_post_transition_frame(tag, sock, marker, observe_secs):
    """Capture the framebuffer immediately before the helper stops the VM.

    The periodic sampler can otherwise miss the end of the post-transition
    window when its next interval falls after the stop file is written.
    """
    output = Path("/tmp/dvm") / f"{tag}.frames"
    output.mkdir(parents=True, exist_ok=True)
    path = output / ("post-transition-%04ds.png" % round(observe_secs))
    command = [sys.executable, str(REPO / "tools/hmp.py"), str(sock),
               "screendump", str(path), "-f", "png"]
    row = {
        "label": "post-transition-final",
        "time": time.time(),
        "marker": marker,
        "path": str(path),
        "capture_ok": False,
        "capture_error": "",
    }
    try:
        result = subprocess.run(command, text=True, capture_output=True,
                                timeout=40)
        row["capture_ok"] = result.returncode == 0 and path.exists()
        row["capture_error"] = (result.stderr.strip() or result.stdout.strip())
        if row["capture_ok"]:
            image = Image.open(path).convert("RGB")
            nonblack = sum(max(pixel) > 8 for pixel in image.getdata())
            total = image.width * image.height
            row.update({
                "width": image.width,
                "height": image.height,
                "nonblack_gt8": nonblack,
                "nonblack_fraction": nonblack / total,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
    except (OSError, subprocess.SubprocessError) as exc:
        row["capture_error"] = repr(exc)
    manifest = output / "frames.jsonl"
    with manifest.open("a") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")
        stream.flush()
    print("LEGACY_INPUT_FRAME " + json.dumps(row, sort_keys=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--timeout", type=float, default=4050.0,
                        help="maximum wait for a usable Home/input window")
    parser.add_argument("--post-transition-secs", type=float, default=180.0)
    parser.add_argument("--ack-timeout", type=float, default=60.0)
    parser.add_argument("--relay-timeout", type=float, default=240.0)
    args = parser.parse_args()
    if min(args.timeout, args.ack_timeout, args.relay_timeout) <= 0:
        parser.error("timeouts must be positive")
    if args.post_transition_secs < 0:
        parser.error("post-transition-secs must be non-negative")

    root = Path("/tmp/dvm")
    event_dir = root / (args.tag + ".events")
    lldb_log = root / (args.tag + ".lldb.log")
    serial_log = root / "probe" / (args.tag + ".serial.log")
    events = root / (args.tag + ".input-events.jsonl")
    uart = root / (args.tag + ".uart.sock")
    stop_file = root / (args.tag + ".stop")
    record_dir = (Path.home() / "dvm-artifacts/bp-patched-dev6/boot1" /
                  "recovery-records" / args.tag)
    relay_log = record_dir / "legacy-input-relay.log"
    record_dir.mkdir(parents=True, exist_ok=True)
    events.touch(exist_ok=True)

    deadline = time.monotonic() + args.timeout
    shell_marker = event_dir / "progress.SHELL_SERVICE_RETURN.json"
    if not wait_file(shell_marker, deadline, "SHELL_SERVICE_RETURN"):
        return 2
    follower = LogFollower(lldb_log)
    if not wait_log_marker(follower, "POST_SHELL_HOME_GESTURE_READY", deadline):
        return 2
    if not wait_file(uart, deadline, "legacy guest input UART"):
        return 2
    if not wait_log_marker(LogFollower(serial_log), "DVM_INPUT_READY protocol=1",
                           deadline):
        # This ready line normally predates the shell marker; search the full
        # short serial file once before reporting a protocol mismatch.
        try:
            serial_text = serial_log.read_text(errors="replace")
        except OSError:
            serial_text = ""
        if "DVM_INPUT_READY protocol=1" not in serial_text:
            print("LEGACY_INPUT_PROTOCOL_MISS", flush=True)
            return 2

    if not run_relay(args, events, uart, relay_log, "--home"):
        return 3
    if not wait_log_marker(LogFollower(lldb_log),
                           '"label": "SB_HOME_ENTITY_READY"', deadline):
        return 2
    time.sleep(8)
    # relay.py and dvm_input.c use 0..32767 normalized digitizer coordinates,
    # not framebuffer pixels. For the 1179x2556 panel, this is approximately
    # the center x and a bottom-to-upper-middle swipe.
    accepted_path = event_dir / "accepted-lockscreen-gesture-transition.json"
    # A Home-button event can itself cause an unconfirmed lock-manager
    # transition. Require a fresh pan-linked marker from the swipe below.
    try:
        accepted_path.unlink()
    except FileNotFoundError:
        pass
    if not run_relay(args, events, uart, relay_log, "--swipe",
                     "16371", "32563", "16371", "6411", "--backend", "recap"):
        # Recap queues the path until lift. If a point is rejected, submit one
        # matching Recap lift to avoid leaving a half-open synthetic touch.
        if not run_relay(args, events, uart, relay_log, "--release",
                         "16371", "6411", "--backend", "recap"):
            return 3

    if not wait_file(accepted_path, min(deadline, time.monotonic() + 60),
                     "accepted user lock dismissal"):
        print("LEGACY_INPUT_RESULT " + json.dumps({
            "home_key_acked": True, "swipe_acked": True,
            "accepted_transition": False,
            "read_only_ui_diagnostics": True,
        }, sort_keys=True), flush=True)
        stop_runner(stop_file, "input-window-ended-without-accepted-transition")
        return 4
    try:
        accepted = json.loads(accepted_path.read_text())
    except (OSError, json.JSONDecodeError):
        accepted = {}
    if (accepted.get("for_user_gesture") != 1 or
            accepted.get("gesture_sequence_confirmed") is not True or
            accepted.get("dismissal_gesture", {}).get("confirmed") is not True):
        print("LEGACY_INPUT_BAD_TRANSITION " + json.dumps(accepted, sort_keys=True),
              flush=True)
        stop_runner(stop_file, "input-transition-was-not-user-gesture")
        return 4

    started = time.monotonic()
    while time.monotonic() - started < args.post_transition_secs:
        time.sleep(min(1.0, args.post_transition_secs -
                       (time.monotonic() - started)))
    capture_post_transition_frame(args.tag, root / (args.tag + ".sock"),
                                  accepted, args.post_transition_secs)
    stop_runner(stop_file,
                "accepted-lock-dismissal-plus-%.0fs-post-transition-render-observation" %
                args.post_transition_secs)
    print("LEGACY_INPUT_RESULT " + json.dumps({
        "home_key_acked": True, "swipe_acked": True,
        "accepted_transition": accepted,
        "post_transition_observe_s": args.post_transition_secs,
        "stop_file": str(stop_file),
    }, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

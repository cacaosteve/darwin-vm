#!/usr/bin/env python3
"""Send one ordinary upward swipe after helper and SpringBoard readiness."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[2]


def tail_has(path: Path, pattern: str) -> bool:
    try:
        size = path.stat().st_size
        with path.open("r", errors="replace") as stream:
            # Readiness may be a one-shot startup line in a multi-megabyte
            # serial log. Search a bounded prefix and the recent tail so that
            # later power chatter cannot evict a helper-ready marker.
            head = stream.read(2_000_000)
            if re.search(pattern, head):
                return True
            if size > 2_000_000:
                stream.seek(max(0, size - 2_000_000))
                return re.search(pattern, stream.read()) is not None
            return False
    except OSError:
        return False


def home_entity_ready(path: Path) -> bool:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    # Check the textual event form directly. The LLDB log can contain command
    # echo/terminal control bytes around callback output, so parsing the whole
    # line as a standalone JSON value is not a reliable readiness gate.
    for line in reversed(text.splitlines()):
        # The compact module emits ENTRY_RETURN; the combined compositor
        # observer emits SB_HOME_ENTITY_INIT_RETURN. Both are the validated
        # return boundary for the same non-null home entity.
        index = line.find("SB_HOME_ENTITY_INIT_ENTRY_RETURN")
        if index < 0:
            index = line.find("SB_HOME_ENTITY_INIT_RETURN")
        if index < 0:
            continue
        suffix = line[index:]
        if re.search(r'"x0"\s*:\s*(?:0x[0-9a-fA-F]+|[1-9][0-9]*)', suffix):
            return True
    return False


def atomic_json(path: Path, row: dict[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--timeout", type=int, default=5400)
    parser.add_argument("--surface-timeout", type=int, default=240)
    parser.add_argument("--event-dir", type=Path)
    parser.add_argument("--backend", choices=("direct", "recap"),
                        default="direct",
                        help="native HID direct packets or buffered Recap playback")
    parser.add_argument("--home-first", action="store_true",
                        help="ACK one native Home-button event before the swipe")
    parser.add_argument("--home-entity-timeout", type=int, default=180,
                        help="seconds to wait for a nonnull Home entity after Home")
    parser.add_argument("--swipe-after-home-timeout", action="store_true",
                        help="send the ordinary swipe if Home was ACKed but the entity stays absent")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", args.tag):
        parser.error("unsafe tag")
    if args.timeout < 1 or args.surface_timeout < 1:
        parser.error("timeouts must be positive")

    root = Path("/tmp/dvm")
    serial = root / "probe" / f"{args.tag}.serial.log"
    lldb_log = root / f"{args.tag}.lldb.log"
    uart = root / f"{args.tag}.uart.sock"
    events = root / f"{args.tag}.events.jsonl"
    event_dir = args.event_dir or root / f"{args.tag}.events"
    result_path = event_dir / "host-unlock-trigger.json"
    start_file = event_dir / "capture-after-input"
    event_dir.mkdir(parents=True, exist_ok=True)
    events.touch(exist_ok=True)

    deadline = time.monotonic() + args.timeout
    ready = None
    while time.monotonic() < deadline:
        helper = tail_has(serial, r"DVM_INPUT_READY protocol=1\b")
        probe = (tail_has(lldb_log, r"POST_SHELL_UNLOCK_READY ") or
                 tail_has(lldb_log,
                          r"POST_SHELL_HOME_UNLOCK_RENDER_READY ") or
                 tail_has(lldb_log,
                          r"POST_SHELL_HOME_TRANSITION_PRESENT_READY "))
        entity_ready = home_entity_ready(lldb_log)
        if helper and probe and (entity_ready or args.home_first):
            ready = {"helper_ready": True, "probe_ready": True,
                     "springboard_nonnull_home_entity": True,
                     "observed_at": time.time()}
            break
        time.sleep(1)
    if ready is None:
        row = {"result": "readiness-timeout",
               "helper_ready": tail_has(serial, r"DVM_INPUT_READY protocol=1\b"),
               "probe_ready": (tail_has(lldb_log, r"POST_SHELL_UNLOCK_READY ") or
                               tail_has(lldb_log,
                                        r"POST_SHELL_HOME_UNLOCK_RENDER_READY ") or
                               tail_has(lldb_log,
                                        r"POST_SHELL_HOME_TRANSITION_PRESENT_READY ")),
               "springboard_nonnull_home_entity": home_entity_ready(lldb_log),
               "serial_log": str(serial), "lldb_log": str(lldb_log),
               "time": time.time()}
        atomic_json(result_path, row)
        print(json.dumps(row, sort_keys=True), flush=True)
        return 2

    home_result = None
    if args.home_first:
        home_log = event_dir / "home-button-input.jsonl"
        home_command = [sys.executable, str(REPO / "tools/input/relay.py"),
                        "--events", str(events), "--uart", str(uart),
                        "--log", str(home_log), "--backend", args.backend,
                        "--home"]
        try:
            home_completed = subprocess.run(
                home_command, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as error:
            row = {**ready, "result": "home-relay-error",
                   "error": repr(error), "relay_command": home_command,
                   "time": time.time()}
            atomic_json(result_path, row)
            print(json.dumps(row, sort_keys=True), flush=True)
            return 3
        try:
            home_rows = [json.loads(line) for line in home_log.read_text().splitlines()
                         if line.strip()]
        except (OSError, json.JSONDecodeError):
            home_rows = []
        home_acks = [row for row in home_rows if row.get("event") == "ack"]
        home_ok = (home_completed.returncode == 0 and len(home_acks) == 2 and
                   all(row.get("success") is True for row in home_acks))
        home_result = {"returncode": home_completed.returncode,
                       "ack_count": len(home_acks),
                       "all_acked": home_ok,
                       "output_tail": (home_completed.stdout or "")[-2000:]}
        if not home_ok:
            row = {**ready, "result": "home-not-acked",
                   "home": home_result, "relay_command": home_command,
                   "time": time.time()}
            atomic_json(result_path, row)
            print(json.dumps(row, sort_keys=True), flush=True)
            return 4
        home_deadline = time.monotonic() + args.home_entity_timeout
        while time.monotonic() < home_deadline and not home_entity_ready(lldb_log):
            time.sleep(1)
        if not home_entity_ready(lldb_log) and not args.swipe_after_home_timeout:
            row = {**ready, "result": "home-acked-no-entity",
                   "home": home_result, "time": time.time()}
            atomic_json(result_path, row)
            print(json.dumps(row, sort_keys=True), flush=True)
            return 5
        time.sleep(2)

    input_log = event_dir / "upward-swipe-input.jsonl"
    before_surfaces = set(event_dir.glob("posthome-iosurface-*.json"))
    before_surfaces.update(event_dir.glob("unlock-surface-*.json"))
    command = [sys.executable, str(REPO / "tools/input/relay.py"),
               "--events", str(events), "--uart", str(uart),
               "--log", str(input_log), "--backend", args.backend,
               "--swipe", "16384", "31500", "16384", "5000"]
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True,
                                   timeout=120, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        row = {**ready, "result": "relay-error", "error": repr(error),
               "relay_command": command, "time": time.time()}
        atomic_json(result_path, row)
        print(json.dumps(row, sort_keys=True), flush=True)
        return 3

    output = completed.stdout or ""
    try:
        input_rows = [json.loads(line) for line in input_log.read_text().splitlines()
                      if line.strip()]
    except (OSError, json.JSONDecodeError):
        input_rows = []
    acks = [row for row in input_rows if row.get("event") == "ack"]
    successful = bool(acks) and all(row.get("success") is True for row in acks)
    if completed.returncode == 0 and successful:
        start_file.write_text("ordinary upward swipe ACKed by helper\n")

    surface_deadline = time.monotonic() + args.surface_timeout
    surface_path = None
    while successful and time.monotonic() < surface_deadline:
        candidates = sorted(event_dir.glob("posthome-iosurface-*.json"))
        candidates += sorted(event_dir.glob("unlock-surface-*.json"))
        surface_path = next((path for path in candidates
                             if path not in before_surfaces), None)
        if surface_path:
            break
        time.sleep(1)
    surface = None
    if surface_path and surface_path.exists():
        try:
            surface = json.loads(surface_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    row = {**ready,
           "result": ("surface-captured" if surface else
                      "gesture-acked-no-surface-within-bound" if successful else
                      "gesture-not-acked"),
           "home_first": home_result,
           "home_entity_after_home": home_entity_ready(lldb_log),
           "gesture": "ordinary-upward-swipe",
           "backend": args.backend,
           "relay_returncode": completed.returncode,
           "relay_ack_count": len(acks),
           "relay_output_tail": output[-4000:],
           "surface_capture_gate": str(start_file),
           "surface_event": str(surface_path) if surface else None,
           "surface": surface, "time": time.time()}
    atomic_json(result_path, row)
    print(json.dumps(row, sort_keys=True), flush=True)
    return 0 if successful else 4


if __name__ == "__main__":
    raise SystemExit(main())

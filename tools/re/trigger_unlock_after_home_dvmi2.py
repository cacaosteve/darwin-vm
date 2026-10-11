#!/usr/bin/env python3
"""Send one guarded DVMI2 upward swipe after SpringBoard home is ready.

The trigger waits for QEMU's DVMI2 helper state, the combined post-shell
observer, and a non-null SpringBoard home entity. It sends one gesture only;
an ACK is recorded as input delivery, not as a successful unlock. No passcode
or other text is ever entered.
"""

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
        return re.search(pattern, path.read_text(errors="replace")[-2_000_000:]) is not None
    except OSError:
        return False


def home_entity_ready(path: Path) -> bool:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        index = line.find("SB_HOME_ENTITY_INIT_ENTRY_RETURN")
        if index < 0:
            index = line.find("SB_HOME_ENTITY_INIT_RETURN")
        if index >= 0 and re.search(
                r'"x0"\s*:\s*(?:0x[0-9a-fA-F]+|[1-9][0-9]*)', line[index:]):
            return True
    return False


def read_status(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def unlock_observer_ready(path: Path) -> bool:
    # This trigger's success decision needs the lock gesture callbacks and
    # input-gated IOSurface sampler, not merely the render-only observer.
    return tail_has(path, r"POST_SHELL_HOME_UNLOCK_RENDER_READY ")


def atomic_json(path: Path, row: dict) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    os.replace(temporary, path)


def stop_probe(event_dir: Path, reason: str, result_path: Path) -> None:
    """Ask setup_gate_probe's watcher to freeze after this one-shot attempt."""
    atomic_json(event_dir / "instrumentation-stop.json", {
        "reason": reason,
        "result_path": str(result_path),
        "time": time.time(),
    })


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--timeout", type=int, default=5400)
    parser.add_argument("--surface-timeout", type=int, default=300)
    parser.add_argument("--dispatch-timeout", type=int, default=300)
    parser.add_argument("--event-dir", type=Path)
    parser.add_argument("--x", type=int, default=589)
    parser.add_argument("--start-y", type=int, default=2450)
    parser.add_argument("--end-y", type=int, default=650)
    parser.add_argument("--hold-ms", type=int, default=650)
    parser.add_argument("--steps", type=int, default=32)
    parser.add_argument("--dispatch-each-step", action="store_true",
                        help="wait for each DVMI2 motion to reach HID before sending the next point")
    parser.add_argument("--dispatch-step-timeout", type=float, default=30)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", args.tag):
        parser.error("unsafe tag")
    if min(args.timeout, args.surface_timeout, args.dispatch_timeout,
           args.hold_ms, args.steps, args.dispatch_step_timeout) <= 0:
        parser.error("timeouts and hold duration must be positive")
    if args.steps < 2:
        parser.error("--steps must be at least 2")
    if not (0 <= args.x < 1179 and 0 <= args.start_y < 2556 and
            0 <= args.end_y < 2556 and args.start_y != args.end_y):
        parser.error("swipe coordinates must be distinct framebuffer pixels")

    root = Path("/tmp/dvm")
    run = root / f"{args.tag}.input"
    status_path = run / "input-status.json"
    lldb_log = root / f"{args.tag}.lldb.log"
    event_dir = args.event_dir or root / f"{args.tag}.events"
    event_dir.mkdir(parents=True, exist_ok=True)
    result_path = event_dir / "host-unlock-trigger.json"
    start_file = event_dir / "capture-after-input"
    frame_prefix = event_dir / "dvmi2-swipe"
    input_log = event_dir / "dvmi2-swipe.jsonl"
    before_surfaces = set(event_dir.glob("posthome-iosurface-*.json"))
    before_surfaces.update(event_dir.glob("unlock-surface-*.json"))

    deadline = time.monotonic() + args.timeout
    while time.monotonic() < deadline:
        status = read_status(status_path)
        helper_ready = bool(status and status.get("guest_state") == "R")
        observer_ready = unlock_observer_ready(lldb_log)
        home_ready = home_entity_ready(lldb_log)
        if helper_ready and observer_ready and home_ready:
            gate = {
                "helper_state": status.get("guest_state"),
                "helper_pid": status.get("guest_pid"),
                "input_epoch": status.get("epoch"),
                "observer_ready": observer_ready,
                "springboard_nonnull_home_entity": home_ready,
                "observed_at": time.time(),
            }
            break
        time.sleep(1)
    else:
        row = {
            "result": "readiness-timeout",
            "helper_status": read_status(status_path),
            "observer_ready": unlock_observer_ready(lldb_log),
            "springboard_nonnull_home_entity": home_entity_ready(lldb_log),
            "lldb_log": str(lldb_log),
            "time": time.time(),
            "input_sent": False,
        }
        atomic_json(result_path, row)
        stop_probe(event_dir, "DVMI2 unlock trigger readiness timeout", result_path)
        print(json.dumps(row, sort_keys=True), flush=True)
        return 2

    command = [
        sys.executable, str(REPO / "tools/input/native_input.py"),
        "--run", str(run), "--px", "--hold-ms", str(args.hold_ms),
        "--steps", str(args.steps), "--ready-timeout", "5",
        "--ack-timeout", str(args.dispatch_timeout),
        "--frame-wait", "0", "--settle", "1.5",
        "--frames", str(frame_prefix), "--log", str(input_log),
        "swipe", str(args.x), str(args.start_y), str(args.x), str(args.end_y),
    ]
    if args.dispatch_each_step:
        command[command.index("--steps"):command.index("--steps")] = [
            "--dispatch-each-step", "--dispatch-step-timeout",
            str(args.dispatch_step_timeout)]
    try:
        completed = subprocess.run(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            timeout=args.dispatch_timeout + 60, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        row = {**gate, "result": "input-command-error", "error": repr(error),
               "command": command, "input_sent": "ambiguous", "time": time.time()}
        atomic_json(result_path, row)
        stop_probe(event_dir, "DVMI2 unlock input command returned ambiguously", result_path)
        print(json.dumps(row, sort_keys=True), flush=True)
        return 3

    output = completed.stdout or ""
    try:
        input_row = json.loads(output)
    except json.JSONDecodeError:
        input_row = None
    acknowledged = bool(
        completed.returncode == 0 and isinstance(input_row, dict) and
        input_row.get("ok") is True and input_row.get("records_sent", 0) > 0 and
        input_row.get("records_acked", 0) == input_row.get("records_sent") and
        input_row.get("dispatch_failed", 0) == 0 and
        input_row.get("dispatch_note") is None)
    if acknowledged:
        start_file.write_text("one ordinary upward swipe ACKed and dispatched\n")

    surface_deadline = time.monotonic() + args.surface_timeout
    surface_path = None
    while acknowledged and time.monotonic() < surface_deadline:
        candidates = sorted(event_dir.glob("posthome-iosurface-*.json"))
        candidates += sorted(event_dir.glob("unlock-surface-*.json"))
        surface_path = next((path for path in candidates if path not in before_surfaces), None)
        if surface_path:
            break
        time.sleep(1)
    surface = None
    if surface_path:
        try:
            surface = json.loads(surface_path.read_text())
        except (OSError, json.JSONDecodeError):
            pass
    row = {
        **gate,
        "result": ("surface-captured" if surface else
                   "gesture-dispatched-no-surface-within-bound" if acknowledged else
                   "gesture-not-confirmed"),
        "gesture": "one-ordinary-upward-swipe",
        "coordinates_pixels": [[args.x, args.start_y], [args.x, args.end_y]],
        "hold_ms": args.hold_ms,
        "steps": args.steps,
        "dispatch_each_step": args.dispatch_each_step,
        "dispatch_step_timeout": args.dispatch_step_timeout,
        "native_input_command_returncode": completed.returncode,
        "native_input": input_row,
        "native_input_output_tail": output[-4000:],
        "surface_capture_gate": str(start_file),
        "surface_event": str(surface_path) if surface else None,
        "surface": surface,
        "input_sent": True,
        "time": time.time(),
    }
    atomic_json(result_path, row)
    stop_probe(event_dir, "DVMI2 one-swipe observation complete", result_path)
    print(json.dumps(row, sort_keys=True), flush=True)
    return 0 if acknowledged else 4


if __name__ == "__main__":
    raise SystemExit(main())

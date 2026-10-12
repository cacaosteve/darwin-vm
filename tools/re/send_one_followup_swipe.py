#!/usr/bin/env python3
"""Send exactly one ordinary upward swipe after an accepted lock transition.

This is a bounded sidecar for the legacy DVMINPUT1 runner. It waits for the
first positive gesture marker, waits a configurable settling interval, clears
the marker so a later write is distinguishable, sends one Recap/direct swipe,
and records whether SpringBoard accepts a second gesture. It never enters a
passcode and never retries the swipe.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time


REPO = Path(__file__).resolve().parents[2]
ROOT = Path("/tmp/dvm")
ART = Path.home() / "dvm-artifacts/bp-patched-dev6/boot1/recovery-records"
ACCEPTED_NAME = "accepted-lockscreen-gesture-transition.json"
GESTURE_LOG_NAME = "lock-gesture.jsonl"
TRANSITION_PREFIX = "LOCK_GESTURE_TRANSITION_MARKER "


def wait_path(path, deadline):
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.5)
    return False


def is_confirmed_transition(value):
    """Require the observed cover-sheet gesture sequence, not file existence."""
    dismissal = value.get("dismissal_gesture") if isinstance(value, dict) else None
    return bool(
        isinstance(value, dict) and value.get("for_user_gesture") == 1 and
        value.get("gesture_sequence_confirmed") is True and
        isinstance(dismissal, dict) and dismissal.get("confirmed") is True)


def read_confirmed_transition(active):
    """Read the active marker or immutable callback-retained first marker."""
    candidates = [active]
    if active.suffix:
        candidates.append(active.with_name(active.stem + ".first" + active.suffix))
    for candidate in candidates:
        try:
            value = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if is_confirmed_transition(value):
            return value
    return None


def transition_records_since(path, offset):
    """Read complete transition-marker records appended after ``offset``.

    The JSONL stream is append-only. Unlike the active marker file, it cannot
    be replaced by a later unrelated lock-manager transition while this
    sidecar is waiting for a second gesture.
    """
    try:
        with path.open("rb") as source:
            source.seek(offset)
            data = source.read()
    except OSError:
        return [], offset
    complete = data.splitlines(keepends=True)
    consumed = 0
    records = []
    for raw in complete:
        if not raw.endswith(b"\n"):
            break
        consumed += len(raw)
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line.startswith(TRANSITION_PREFIX):
            continue
        try:
            records.append(json.loads(line[len(TRANSITION_PREFIX):]))
        except json.JSONDecodeError:
            continue
    return records, offset + consumed


def wait_for_confirmed_transition(path, offset, deadline):
    """Wait for a fresh, confirmed transition in the append-only event log."""
    observed = []
    while time.monotonic() < deadline:
        records, offset = transition_records_since(path, offset)
        observed.extend(records)
        for record in records:
            if is_confirmed_transition(record):
                return record, observed
        time.sleep(0.25)
    records, _ = transition_records_since(path, offset)
    observed.extend(records)
    for record in records:
        if is_confirmed_transition(record):
            return record, observed
    return None, observed


def capture(sock, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, str(REPO / "tools/hmp.py"), str(sock),
               "screendump", str(path), "-f", "png"]
    result = subprocess.run(command, text=True, capture_output=True,
                            timeout=40)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout[-1000:],
        "stderr": result.stderr[-1000:],
        "exists": path.exists(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--delay", type=float, default=90,
                        help="settling seconds after the first accepted swipe")
    parser.add_argument("--accepted-timeout", type=float, default=240)
    parser.add_argument("--ack-timeout", type=float, default=300)
    parser.add_argument("--relay-timeout", type=float, default=300)
    parser.add_argument("--max-wait", type=float, default=4500,
                        help="maximum wait for the first accepted transition")
    parser.add_argument("--backend", choices=("recap", "direct"),
                        default="recap")
    args = parser.parse_args()
    if min(args.delay, args.accepted_timeout, args.ack_timeout,
           args.relay_timeout) < 0 or args.max_wait <= 0:
        parser.error("timeouts must be non-negative")
    event_dir = ROOT / (args.tag + ".events")
    accepted = event_dir / ACCEPTED_NAME
    gesture_log = event_dir / GESTURE_LOG_NAME
    uart = ROOT / (args.tag + ".uart.sock")
    sock = ROOT / (args.tag + ".sock")
    log_dir = ART / args.tag
    log_dir.mkdir(parents=True, exist_ok=True)
    output = log_dir / "followup-swipe.json"

    row = {
        "tag": args.tag,
        "started_unix": time.time(),
        "delay_seconds": args.delay,
        "backend": args.backend,
        "passcode_entered": False,
        "swipe_attempts": 0,
        "result": "waiting-for-first-accepted-transition",
    }
    deadline = time.monotonic() + args.max_wait
    first_transition = None
    while time.monotonic() < deadline:
        first_transition = read_confirmed_transition(accepted)
        if first_transition is not None:
            break
        time.sleep(0.5)
    if first_transition is None:
        row["result"] = "first-transition-marker-timeout"
        output.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
        print(json.dumps(row, sort_keys=True), flush=True)
        return 2
    row["first_transition"] = first_transition
    row["before_capture"] = capture(
        sock, log_dir / "frames" / "before-followup-swipe.png")
    time.sleep(args.delay)

    # Do not clear or overwrite either marker. The callback's .first copy is
    # also the gate for downstream surface sampling. Record an append-only
    # event-log cursor instead, so only a fresh confirmed marker can close
    # this follow-up attempt.
    try:
        gesture_offset = gesture_log.stat().st_size
    except OSError:
        gesture_offset = 0
    if not wait_path(uart, time.monotonic() + 30):
        row["result"] = "uart-not-present"
        output.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
        print(json.dumps(row, sort_keys=True), flush=True)
        return 3

    relay_log = log_dir / "followup-swipe-relay.log"
    command = [sys.executable, str(REPO / "tools/input/relay.py"),
               "--events", str(ROOT / (args.tag + ".input-events.jsonl")),
               "--uart", str(uart), "--log", str(relay_log),
               "--ack-timeout", str(args.ack_timeout),
               "--swipe", "16371", "32563", "16371", "6411",
               "--backend", args.backend]
    result = subprocess.run(command, text=True, capture_output=True,
                            timeout=args.relay_timeout)
    row["swipe_attempts"] = 1
    row["relay"] = {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout[-4000:],
        "stderr": result.stderr[-4000:],
    }
    row["result"] = "swipe-sent-awaiting-gesture-marker"
    row["post_swipe_transition_observations"] = []
    if result.returncode != 0:
        # Send one matching Recap release when the acknowledged path was not
        # completed, so the synthetic touch cannot be left pressed.
        release = [sys.executable, str(REPO / "tools/input/relay.py"),
                   "--events", str(ROOT / (args.tag + ".input-events.jsonl")),
                   "--uart", str(uart), "--log", str(relay_log),
                   "--ack-timeout", str(args.ack_timeout),
                   "--release", "16371", "6411", "--backend", args.backend]
        rel = subprocess.run(release, text=True, capture_output=True,
                             timeout=args.relay_timeout)
        row["release"] = {"returncode": rel.returncode,
                          "stdout": rel.stdout[-2000:],
                          "stderr": rel.stderr[-2000:]}
        row["result"] = "swipe-ack-failed-release-attempted"

    second_transition, observations = wait_for_confirmed_transition(
        gesture_log, gesture_offset,
        time.monotonic() + args.accepted_timeout)
    row["post_swipe_transition_observations"] = observations
    if second_transition is not None:
        row["second_transition"] = second_transition
        row["result"] = "fresh-second-transition-marker"
    elif result.returncode == 0:
        row["result"] = "swipe-acked-no-second-transition-marker-in-bound"
    row["after_capture"] = capture(
        sock, log_dir / "frames" / "after-followup-swipe.png")
    row["completed_unix"] = time.time()
    output.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    print(json.dumps(row, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

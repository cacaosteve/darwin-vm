#!/usr/bin/env python3
"""Wait for the trusted DVMI2 helper, then send Home and one ordinary swipe.

This performs only physical-style Home/swipe input.  It never types a
passcode.  Readiness requires the helper's R state and this boot's SpringBoard
home entity; the run is stopped after a bounded post-input observation.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[2]


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def observer_ready(path: Path) -> bool:
    try:
        # Both the full render observer and the compact input observer emit a
        # one-time readiness marker after their callbacks are installed.
        # Inspect the log prefix so later output cannot evict that marker.
        with path.open("r", errors="replace") as stream:
            prefix = stream.read(2_000_000)
            return ("POSTHOME_PRESENT_READY " in prefix or
                    "POST_SHELL_HOME_INPUT_READY " in prefix or
                    "POST_SHELL_HOME_GESTURE_READY " in prefix or
                    "POST_SHELL_HOME_GESTURE_STAGED_READY " in prefix or
                    "POST_SHELL_HOME_UNLOCK_DIAG_READY " in prefix)
    except OSError:
        return False


def home_entity_ready(path: Path) -> bool:
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return False
    for line in reversed(lines):
        marker = "POST_SHELL_HOME_MIN "
        if marker not in line:
            continue
        try:
            row = json.loads(line.split(marker, 1)[1])
        except json.JSONDecodeError:
            continue
        if row.get("label") in ("SB_HOME_ENTITY_INIT_RETURN",
                                 "SB_HOME_ENTITY_INIT_ENTRY_RETURN"):
            value = row.get("x0")
            if isinstance(value, str):
                try:
                    value = int(value, 0)
                except ValueError:
                    value = 0
            if isinstance(value, int) and value:
                return True
    return False


def shell_service_return(path: Path) -> dict | None:
    """Return this run's explicit BKS shell-service return marker, if present."""
    try:
        row = read_json(path)
    except OSError:
        return None
    if row.get("label") == "SHELL_SERVICE_RETURN":
        return row
    return None


def hotplug_state_one(path: Path, after_time: float) -> dict | None:
    """Return a newly observed QuartzCore display-state row with byte 1 set."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        marker = "POSTHOME_HOTPLUG_STATE "
        if marker not in line:
            continue
        try:
            row = json.loads(line.split(marker, 1)[1])
        except json.JSONDecodeError:
            continue
        values = row.get("state_bytes")
        if (row.get("label") == "HOTPLUG_STATE_BYTES_WRITE" and
                float(row.get("time", 0)) >= after_time and
                isinstance(values, list) and len(values) >= 2 and
                values[1] == 1):
            return row
    return None


def stop_probe(event_dir: Path, reason: str, result_path: Path) -> None:
    path = event_dir / "instrumentation-stop.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"reason": reason,
                                     "result_path": str(result_path),
                                     "time": time.time()}, sort_keys=True) + "\n")
    os.replace(temporary, path)


def run_input(module, run: Path, args: list[str], prefix: Path,
              timeout: int, options: list[str] | None = None) -> tuple[int, dict]:
    output = io.StringIO()
    old_argv = sys.argv
    sys.argv = [str(REPO / "tools/input/native_input.py"), "--run", str(run),
                "--px", "--ready-timeout", "5", "--ack-timeout", "300",
                "--frame-wait", "0", "--settle", "1.0", "--frames",
                str(prefix), *(options or []), *args]
    original_execute = module.QMP.execute
    qmp_instances = []
    left_contact = set()
    contact_cleanup = {"attempted": False, "released": False,
                       "errors": []}

    def retry_execute(self, command, **kwargs):
        if self not in qmp_instances:
            qmp_instances.append(self)
        if command != "input-send-event":
            return original_execute(self, command, **kwargs)
        deadline = time.monotonic() + 90
        while True:
            try:
                status = original_execute(self, "query-status") or {}
                if status.get("status") != "running":
                    if time.monotonic() >= deadline:
                        raise TimeoutError("QEMU did not return to running state")
                    time.sleep(0.1)
                    continue
                result = original_execute(self, command, **kwargs)
                events = kwargs.get("events", [])
                for event in events:
                    if event.get("type") == "btn":
                        data = event.get("data", {})
                        if data.get("button") == "left":
                            if data.get("down"):
                                left_contact.add(self)
                            else:
                                left_contact.discard(self)
                return result
            except RuntimeError as error:
                if ("VM not running" not in str(error) and
                        "paused" not in str(error).lower()) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.1)

    def best_effort_release():
        # If the guest pauses between the down and up records, wait briefly
        # for it to resume and send one release at the last known absolute
        # position. Never leave a host-side touch held merely because an
        # intermediate gesture record hit a transient debug pause.
        for qmp in list(left_contact):
            contact_cleanup["attempted"] = True
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                try:
                    state = original_execute(qmp, "query-status") or {}
                    if state.get("status") == "running":
                        original_execute(qmp, "input-send-event", events=[
                            dict(type="btn", data=dict(button="left", down=False))])
                        left_contact.discard(qmp)
                        contact_cleanup["released"] = True
                        break
                except Exception as error:
                    contact_cleanup["errors"].append(repr(error))
                time.sleep(0.1)

    module.QMP.execute = retry_execute
    try:
        with contextlib.redirect_stdout(output):
            module.main()
    except SystemExit as exit_status:
        rc = int(exit_status.code or 0)
    except Exception as error:  # record all input failures, then stop safely
        best_effort_release()
        return 99, {"ok": False, "exception": repr(error),
                    "stdout": output.getvalue(),
                    "contact_cleanup": contact_cleanup}
    else:
        rc = 0
    finally:
        # `native_input` can exit through SystemExit after an ACK/dispatch
        # timeout while the QMP mouse button is still down. Always attempt a
        # release on every exit path; a held contact can poison later guest
        # input and make the next gesture evidence ambiguous.
        if left_contact:
            best_effort_release()
        module.QMP.execute = original_execute
        sys.argv = old_argv
    try:
        row = json.loads(output.getvalue())
    except json.JSONDecodeError:
        row = {"stdout": output.getvalue()}
    if contact_cleanup["attempted"]:
        row["contact_cleanup"] = contact_cleanup
    return rc, row


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    parser.add_argument("--timeout", type=int, default=5400)
    parser.add_argument("--post-input-observe", type=int, default=180)
    parser.add_argument(
        "--retry-home-after-hotplug", action="store_true",
        help=("during the post-input observation, send one additional Home "
              "after the delay, then one ordinary swipe only after this boot's "
              "hotplug-state byte 1 becomes nonzero"))
    parser.add_argument("--retry-home-delay", type=int, default=600)
    parser.add_argument("--hotplug-state-timeout", type=int, default=300)
    parser.add_argument("--retry-observe", type=int, default=180)
    parser.add_argument("--home-entity-timeout", type=int, default=180)
    parser.add_argument(
        "--require-shell-service-return", action="store_true",
        help=("do not send Home or swipe input until this run's explicit "
              "progress.SHELL_SERVICE_RETURN.json marker exists"))
    parser.add_argument(
        "--initial-swipe-delay", type=int, default=0,
        help=("wait this many seconds after the Home/entity gate before the "
              "first swipe; zero preserves the original immediate behavior"))
    parser.add_argument(
        "--swipe-coords", type=int, nargs=4,
        metavar=("X1", "Y1", "X2", "Y2"),
        default=(589, 2450, 589, 650),
        help=("one upward swipe in framebuffer pixels (1179x2556); these "
              "coordinates are passed to native_input.py with --px"))
    parser.add_argument("--swipe-hold-ms", type=int, default=80,
                        help="duration of the ordinary swipe")
    parser.add_argument("--swipe-steps", type=int, default=12,
                        help="number of swipe position samples")
    parser.add_argument(
        "--swipe-dispatch-each-step", action="store_true",
        help="wait for each initial swipe sample to reach the HID dispatcher")
    parser.add_argument("--swipe-dispatch-step-timeout", type=float,
                        default=60,
                        help="per-sample HID dispatch timeout for the initial swipe")
    parser.add_argument(
        "--followup-swipe-delay", type=int, default=0,
        help=("after the first swipe, send one additional ordinary swipe "
              "after this many seconds; zero disables the follow-up"))
    parser.add_argument("--followup-swipe-hold-ms", type=int, default=600)
    parser.add_argument("--followup-swipe-steps", type=int, default=32)
    parser.add_argument(
        "--followup-swipe-coords", type=int, nargs=4,
        metavar=("X1", "Y1", "X2", "Y2"),
        default=(589, 2540, 589, 500),
        help="optional second swipe; defaults to the P141 bottom-edge profile")
    parser.add_argument(
        "--home-after-accepted-transition", action="store_true",
        help=("send one ordinary Home key after this boot records an accepted "
              "user-gesture lock dismissal (x3=1); never types a passcode"))
    parser.add_argument(
        "--swipe-after-home-timeout", action="store_true",
        help=("after an ACKed Home key and the bounded entity wait, send one "
              "ordinary upward swipe even if SpringBoard has not reported a "
              "nonnull home entity; use to test the lock-screen event path"))
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", args.tag):
        parser.error("unsafe tag")
    if min(args.timeout, args.post_input_observe,
           args.home_entity_timeout, args.swipe_hold_ms,
           args.swipe_steps, args.retry_home_delay,
           args.hotplug_state_timeout, args.retry_observe,
           args.swipe_dispatch_step_timeout) < 1:
        parser.error("timeouts must be positive")
    if args.initial_swipe_delay < 0:
        parser.error("--initial-swipe-delay cannot be negative")
    if args.followup_swipe_delay < 0 or args.followup_swipe_hold_ms < 1 or \
            args.followup_swipe_steps < 2:
        parser.error("follow-up swipe delay/duration/steps are invalid")
    if args.followup_swipe_delay and \
            args.followup_swipe_delay >= args.post_input_observe:
        parser.error("--followup-swipe-delay must be shorter than "
                     "--post-input-observe so the follow-up is reachable")
    if args.swipe_steps < 2:
        parser.error("--swipe-steps must be at least 2")
    x1, y1, x2, y2 = args.swipe_coords
    if not (0 <= x1 < 1179 and 0 <= x2 < 1179 and
            0 <= y1 < 2556 and 0 <= y2 < 2556):
        parser.error("--swipe-coords must be framebuffer pixels within 1179x2556")
    if args.swipe_coords[0] != args.swipe_coords[2] or \
            args.swipe_coords[1] <= args.swipe_coords[3]:
        parser.error("--swipe-coords must be a vertical upward swipe")
    fx1, fy1, fx2, fy2 = args.followup_swipe_coords
    if not (0 <= fx1 < 1179 and 0 <= fx2 < 1179 and
            0 <= fy1 < 2556 and 0 <= fy2 < 2556) or \
            fx1 != fx2 or fy1 <= fy2:
        parser.error("--followup-swipe-coords must be an upward swipe in bounds")

    root = Path("/tmp/dvm")
    run = root / f"{args.tag}.input"
    status_path = root / f"{args.tag}.input-status.json"
    lldb_log = root / f"{args.tag}.lldb.log"
    event_dir = root / f"{args.tag}.events"
    event_dir.mkdir(parents=True, exist_ok=True)
    result_path = event_dir / "home-then-unlock.json"
    shell_return_path = event_dir / "progress.SHELL_SERVICE_RETURN.json"
    deadline = time.monotonic() + args.timeout
    gate = None
    while time.monotonic() < deadline:
        status = read_json(status_path)
        shell_return = shell_service_return(shell_return_path)
        if (status.get("guest_state") == "R" and observer_ready(lldb_log) and
                (not args.require_shell_service_return or shell_return)):
            gate = {"helper_pid": status.get("guest_pid"),
                    "helper_epoch": status.get("epoch"),
                    "observer_ready": True,
                    "shell_service_return_required":
                        args.require_shell_service_return,
                    "shell_service_return": shell_return,
                    "springboard_home_entity_nonnull":
                        home_entity_ready(lldb_log),
                    "time": time.time()}
            break
        time.sleep(1)
    if gate is None:
        row = {"result": "readiness-timeout", "input_sent": False,
               "helper_status": read_json(status_path),
               "observer_ready": observer_ready(lldb_log),
               "shell_service_return_required":
                   args.require_shell_service_return,
               "shell_service_return": shell_service_return(
                   shell_return_path),
               "springboard_home_entity_nonnull": home_entity_ready(lldb_log),
               "time": time.time()}
        result_path.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
        stop_probe(event_dir, "DVMI2 Home/unlock readiness timeout", result_path)
        print(json.dumps(row, sort_keys=True), flush=True)
        return 2

    spec = importlib.util.spec_from_file_location(
        "native_input_home_unlock", REPO / "tools/input/native_input.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    home_rc, home = run_input(module, run, ["home"],
                              event_dir / "home-button", 360)
    home_entity_deadline = time.monotonic() + args.home_entity_timeout
    while (home_rc == 0 and home.get("ok") and
           time.monotonic() < home_entity_deadline and
           not home_entity_ready(lldb_log)):
        time.sleep(1)
    home_ready_after_key = home_entity_ready(lldb_log)
    swipe_allowed = (home_rc == 0 and home.get("ok") and
                     (home_ready_after_key or args.swipe_after_home_timeout))
    if swipe_allowed:
        gate_reason = ("Home button ACKed; non-null SpringBoard home entity "
                       "observed; one ordinary upward swipe follows\n"
                       if home_ready_after_key else
                       "Home button ACKed; no nonnull SpringBoard home entity "
                       "within the configured wait; one ordinary upward "
                       "swipe tests the lock-screen path\n")
        (event_dir / "capture-after-input").write_text(gate_reason)
        time.sleep(2 + args.initial_swipe_delay)
    swipe_rc, swipe = (run_input(
        module, run, ["swipe"] + [str(value) for value in args.swipe_coords],
        event_dir / "unlock-swipe", 360,
        ["--hold-ms", str(args.swipe_hold_ms),
         "--steps", str(args.swipe_steps)] +
        (["--dispatch-each-step", "--dispatch-step-timeout",
          str(args.swipe_dispatch_step_timeout)]
         if args.swipe_dispatch_each_step else []))
        if swipe_allowed
        else (99, {"ok": False, "not_attempted": True,
                   "reason": "home-key-not-acked-or-home-entity-not-ready"}))

    observe_start = time.monotonic()
    observe_until = observe_start + args.post_input_observe
    hotplug_path = event_dir / "hotplug-state-writes.jsonl"
    followup_swipe = None
    followup_at = (observe_start + args.followup_swipe_delay
                   if args.followup_swipe_delay else None)
    home_after_accepted_transition = None
    accepted_home_sent = False
    retry_result = None
    retry_home_sent = False
    retry_home_time = None
    retry_swipe = None
    while time.monotonic() < observe_until:
        elapsed = time.monotonic() - observe_start
        accepted_marker = read_json(
            event_dir / "accepted-lockscreen-gesture-transition.json")
        gesture_evidence = accepted_marker.get("dismissal_gesture")
        sequence_rejected = (
            accepted_marker.get("gesture_sequence_confirmed") is False or
            (isinstance(gesture_evidence, dict) and
             gesture_evidence.get("confirmed") is False))
        if (args.home_after_accepted_transition and
                not accepted_home_sent and
                accepted_marker.get("for_user_gesture") == 1 and
                not sequence_rejected):
            accepted_home_sent = True
            home_after_rc, home_after_row = run_input(
                module, run, ["home"],
                event_dir / "home-after-accepted-transition", 360)
            home_after_accepted_transition = {
                "returncode": home_after_rc,
                "home": home_after_row,
                "accepted_transition": accepted_marker,
                "time": time.time(),
            }
            if home_after_rc == 0 and home_after_row.get("ok"):
                observe_until = max(observe_until,
                                    time.monotonic() + args.retry_observe)
        if (followup_at is not None and followup_swipe is None and
                time.monotonic() >= followup_at):
            followup_rc, followup_row = run_input(
                module, run,
                ["swipe"] + [str(value) for value in
                             args.followup_swipe_coords],
                event_dir / "followup-bottom-swipe", 360,
                ["--hold-ms", str(args.followup_swipe_hold_ms),
                 "--steps", str(args.followup_swipe_steps),
                 "--dispatch-each-step", "--dispatch-step-timeout", "60"])
            followup_swipe = {"returncode": followup_rc,
                              "swipe": followup_row,
                              "coordinates": args.followup_swipe_coords,
                              "hold_ms": args.followup_swipe_hold_ms,
                              "steps": args.followup_swipe_steps,
                              "time": time.time()}
            followup_path = event_dir / "followup-bottom-swipe.json"
            followup_path.write_text(json.dumps(
                followup_swipe, indent=2, sort_keys=True) + "\n")
            if followup_rc == 0 and followup_row.get("ok"):
                observe_until = max(observe_until,
                                    time.monotonic() + args.retry_observe)
        if (args.retry_home_after_hotplug and not retry_home_sent and
                elapsed >= args.retry_home_delay):
            retry_home_sent = True
            retry_home_time = time.time()
            retry_rc, retry_home = run_input(
                module, run, ["home"], event_dir / "retry-home-button", 360)
            retry_result = {"home_returncode": retry_rc,
                            "home": retry_home,
                            "home_sent_time": retry_home_time,
                            "hotplug_state_path": str(hotplug_path)}
            if retry_rc == 0 and retry_home.get("ok"):
                state_deadline = min(
                    time.monotonic() + args.hotplug_state_timeout,
                    observe_until)
                state_row = None
                while time.monotonic() < state_deadline:
                    state_row = hotplug_state_one(hotplug_path,
                                                  retry_home_time)
                    if state_row:
                        break
                    time.sleep(0.5)
                retry_result["hotplug_state_after_home"] = state_row
                retry_result["hotplug_state_observed"] = bool(state_row)
                if state_row:
                    swipe_rc2, swipe2 = run_input(
                        module, run, ["swipe"] + [str(value) for value in
                                                args.swipe_coords],
                        event_dir / "retry-swipe-after-hotplug", 360,
                        ["--hold-ms", str(args.swipe_hold_ms),
                         "--steps", str(args.swipe_steps)])
                    retry_swipe = {"returncode": swipe_rc2, "swipe": swipe2,
                                   "trigger_state": state_row}
                    retry_result["swipe_after_hotplug"] = retry_swipe
                    if swipe_rc2 == 0 and swipe2.get("ok"):
                        observe_until = max(
                            observe_until,
                            time.monotonic() + args.retry_observe)
                        retry_result["post_swipe_observe_seconds"] = \
                            args.retry_observe
        time.sleep(0.5)
    frame = None
    try:
        frame_path = event_dir / "home-after-swipe.png"
        home_process = subprocess.run(
            [sys.executable, str(REPO / "tools/input/native_input.py"),
             "--run", str(run), "frame", str(frame_path)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=30, check=False)
        frame = {"returncode": home_process.returncode,
                 "path": str(frame_path), "output": home_process.stdout[-1000:]}
    except (OSError, subprocess.TimeoutExpired) as error:
        frame = {"error": repr(error)}

    surfaces = sorted(str(path) for path in event_dir.glob("posthome-iosurface-*.json"))
    result = ("home-and-swipe-acknowledged" if
              home_rc == 0 and home.get("ok") and
              home_ready_after_key and
              swipe_rc == 0 and swipe.get("ok") else
              "lock-screen-swipe-acknowledged-home-entity-unseen" if
              home_rc == 0 and home.get("ok") and
              args.swipe_after_home_timeout and not home_ready_after_key and
              swipe_rc == 0 and swipe.get("ok") else
              "home-entity-ready-swipe-incomplete" if
              home_rc == 0 and home.get("ok") and home_ready_after_key else
              "home-acknowledged-home-entity-not-seen" if
              home_rc == 0 and home.get("ok") else "input-incomplete")
    row = {"result": result,
           "gate": gate, "home_returncode": home_rc, "home": home,
           "home_entity_after_key": home_ready_after_key,
           "swipe_allowed_without_home_entity": args.swipe_after_home_timeout,
           "swipe_returncode": swipe_rc, "swipe": swipe,
           "initial_swipe_delay_seconds": args.initial_swipe_delay,
           "swipe_hold_ms": args.swipe_hold_ms,
           "swipe_steps": args.swipe_steps,
           "swipe_dispatch_each_step": args.swipe_dispatch_each_step,
           "followup_swipe": followup_swipe,
           "followup_swipe_delay_seconds": args.followup_swipe_delay,
           "home_after_accepted_transition":
               home_after_accepted_transition,
           "post_input_observation_seconds": args.post_input_observe,
           "hotplug_retry": retry_result,
           "iosurface_events": surfaces, "frame_capture": frame,
           "helper_status_at_end": read_json(status_path), "time": time.time()}
    result_path.write_text(json.dumps(row, indent=2, sort_keys=True) + "\n")
    stop_probe(event_dir, "DVMI2 Home and bounded post-key UI observation",
               result_path)
    print(json.dumps(row, sort_keys=True), flush=True)
    return 0 if row["result"] in (
        "home-and-swipe-acknowledged",
        "lock-screen-swipe-acknowledged-home-entity-unseen") else 4


if __name__ == "__main__":
    raise SystemExit(main())

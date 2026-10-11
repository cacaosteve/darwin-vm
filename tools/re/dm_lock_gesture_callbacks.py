"""Read-only SpringBoard lock-screen gesture and dismissal callbacks.

These probes bracket the real cover-sheet/home-gesture methods after a native
swipe has reached SpringBoard. They log call arguments and selected return
values without changing gesture state or invoking private APIs.
"""

import json
import os
import time
from pathlib import Path

import lldb

import bks_checkin_callbacks as bks
import render_gate_callbacks as render


USER_SITES = (
    # UIKitCore base-class sites reveal whether the screen-edge recognizers
    # advance beyond touch delivery into recognition.
    (0x18491FF3C, "GESTURE_RECOGNIZER_SET_STATE"),
    (0x184E39AE4, "GESTURE_RECOGNIZER_SHOULD_BEGIN"),
    (0x184F8B538, "GESTURE_RECOGNIZER_DRIVER_STATE_UPDATE"),
    # SpringBoard's concrete FluidSwitcher recognizer overrides setState:;
    # capture its caller before it forwards to UIKit's base implementation.
    (0x2243F9BEC, "FLUID_SCREEN_EDGE_SET_STATE_ENTRY"),
    # The screen-edge recognizer has a separate home-grabber active-zone test.
    (0x224B21904, "SCREEN_EDGE_IN_GRABBER_ZONE"),
    (0x22467C3A0, "COVERSHEET_DISMISS_GESTURE_BEGAN"),
    (0x22467C968, "COVERSHEET_DISMISS_GESTURE_CHANGED"),
    (0x2247008B8, "HOME_GESTURE_INTERACTION_HANDLE"),
    (0x224700A10, "HOME_GESTURE_INTERACTION_SHOULD_BEGIN"),
    (0x22432BB6C, "HOME_GESTURE_PAN_TOUCHES_BEGAN"),
    (0x22432B7C8, "HOME_GESTURE_PAN_TOUCHES_MOVED"),
    (0x22436F004, "HOME_GESTURE_PAN_TOUCHES_ENDED"),
    (0x2247C0350, "HOME_GESTURE_PAN_SHOULD_BEGIN"),
    (0x22436589C, "COVERSHEET_SYSTEM_GESTURE_SHOULD_BEGIN"),
    (0x22432A1D0, "SYSTEM_GESTURE_RECEIVE_TOUCH"),
    (0x224AE14AC, "ANY_LOCK_SCREEN_VISIBLE"),
    (0x22467B0AC, "COVERSHEET_DISMISS_GESTURE_ACTIVE"),
    (0x224C49560, "LOCKSCREEN_GESTURE_TRANSITION"),
)

RETURN_LABELS = {
    "GESTURE_RECOGNIZER_SHOULD_BEGIN",
    "HOME_GESTURE_INTERACTION_SHOULD_BEGIN",
    "HOME_GESTURE_PAN_SHOULD_BEGIN",
    "COVERSHEET_SYSTEM_GESTURE_SHOULD_BEGIN",
    "ANY_LOCK_SCREEN_VISIBLE",
    "COVERSHEET_DISMISS_GESTURE_ACTIVE",
    "SCREEN_EDGE_IN_GRABBER_ZONE",
}
META = {}
RETURNS = {}
ARMED_RETURNS = set()
COUNTS = {}
RETURN_COUNTS = {}
FILTER_COUNTS = {}
RECENT_DISMISSAL_GESTURE = []
ACCEPTED_TRANSITION_HOOK = None
SLIDE = 0
MAX_HITS = 256
SCREEN_EDGE_RECOGNIZERS = {
    "SBFluidSwitcherScreenEdgePanGestureRecognizer",
    "SBCoverSheetScreenEdgePanGestureRecognizer",
    "SBHomeGesturePanGestureRecognizer",
}
TRANSITION_MARKER = None
EVENT_OUT = None


def _emit(text):
    print(text, flush=True)
    if EVENT_OUT is not None:
        try:
            with EVENT_OUT.open("a") as output:
                output.write(text + "\n")
        except Exception as error:
            print("LOCK_GESTURE_FILE_ERROR " + repr(error), flush=True)


def _register(debugger, address, label, is_return=False, expected_tpidr=0,
              expected_sp=0, metadata=None):
    target = debugger.GetSelectedTarget()
    before = target.GetNumBreakpoints()
    breakpoint = target.BreakpointCreateByAddress(address)
    if (not breakpoint.IsValid() or
            target.GetNumBreakpoints() != before + 1):
        raise RuntimeError("failed to arm %s at 0x%x" % (label, address))
    breakpoint.SetAutoContinue(True)
    if is_return:
        RETURNS[breakpoint.GetID()] = {
            "label": label, "tpidr": expected_tpidr, "sp": expected_sp,
            "metadata": dict(metadata or {}),
        }
    else:
        META[breakpoint.GetID()] = label
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command add -F dm_lock_gesture_callbacks.on_hit %d" %
        breakpoint.GetID(), result)
    callback = "dm_lock_gesture_callbacks.on_hit"
    proof = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command list %d" % breakpoint.GetID(), proof)
    if (not result.Succeeded() or not proof.Succeeded() or
            callback not in (proof.GetOutput() or "") or
            breakpoint.GetNumLocations() < 1):
        breakpoint.SetEnabled(False)
        raise RuntimeError("callback/location proof failed for %s: %s" %
                           (label, proof.GetError() or result.GetError()))
    _emit("LOCK_GESTURE_BP_READY label=%s address=0x%x id=%d" %
          (label, address, breakpoint.GetID()))
    return breakpoint


def _arm_return(frame, label, metadata=None):
    thread = frame.GetThread()
    tpidr = bks._reg(frame, "tpidr_el1") or 0
    sp = bks._reg(frame, "sp") or 0
    lr = bks._reg(frame, "lr") or 0
    if not lr:
        return
    key = (label, tpidr, sp, lr)
    if key in ARMED_RETURNS:
        return
    ARMED_RETURNS.add(key)
    process = thread.GetProcess()
    debugger = process.GetTarget().GetDebugger()
    _register(debugger, lr, label + "_RETURN", True, tpidr, sp, metadata)


def _remember_dismissal_event(row):
    """Keep only the small same-boot sequence needed to arm render probes."""
    label = row.get("label")
    if label not in ("COVERSHEET_DISMISS_GESTURE_BEGAN",
                     "HOME_GESTURE_PAN_SHOULD_BEGIN_RETURN",
                     "HOME_GESTURE_PAN_TOUCHES_ENDED"):
        return
    event = {
        "label": label,
        "time": row.get("time"),
        "pid": row.get("pid"),
        "tpidr_el1": row.get("tpidr_el1"),
    }
    if label == "COVERSHEET_DISMISS_GESTURE_BEGAN":
        # x2 is the SBCoverSheetScreenEdgePanGestureRecognizer.
        event["recognizer"] = row.get("x2")
    elif label == "HOME_GESTURE_PAN_SHOULD_BEGIN_RETURN":
        event["recognizer"] = row.get("recognizer")
        value = row.get("x0")
        try:
            event["accepted"] = int(value, 0) == 1 if isinstance(value, str) else value == 1
        except (TypeError, ValueError):
            event["accepted"] = False
    else:
        # x0 is the recognizer at touchesEnded.
        event["recognizer"] = row.get("x0")
    RECENT_DISMISSAL_GESTURE.append(event)
    del RECENT_DISMISSAL_GESTURE[:-32]


def _confirmed_dismissal_sequence(marker):
    """Require the transition to follow this boot's accepted Cover Sheet pan."""
    try:
        transition_time = float(marker.get("time"))
        transition_tpidr = marker.get("tpidr_el1")
        transition_pid = int(marker.get("pid"))
    except (TypeError, ValueError):
        return {"confirmed": False, "reason": "transition-identity-missing"}
    candidates = [event for event in RECENT_DISMISSAL_GESTURE
                  if event.get("pid") == transition_pid and
                  event.get("tpidr_el1") == transition_tpidr]
    began = [event for event in candidates
             if event["label"] == "COVERSHEET_DISMISS_GESTURE_BEGAN" and
             0 <= transition_time - float(event.get("time", 0)) <= 60]
    accepted = [event for event in candidates
                if event["label"] == "HOME_GESTURE_PAN_SHOULD_BEGIN_RETURN" and
                event.get("accepted") and
                0 <= transition_time - float(event.get("time", 0)) <= 60]
    ended = [event for event in candidates
             if event["label"] == "HOME_GESTURE_PAN_TOUCHES_ENDED" and
             0 <= transition_time - float(event.get("time", 0)) <= 15]
    for pan_end in reversed(ended):
        for pan_began in reversed(began):
            if pan_began.get("recognizer") != pan_end.get("recognizer"):
                continue
            matching_accept = [event for event in accepted
                               if event.get("recognizer") in
                               (None, pan_end.get("recognizer")) and
                               float(pan_began["time"]) - 1.0 <= float(event["time"]) <=
                               float(pan_end["time"])]
            if matching_accept:
                return {
                    "confirmed": True,
                    "reason": "accepted-pan-cover-sheet-began-ended",
                    "pan_should_begin_return": matching_accept[-1],
                    "dismiss_began": pan_began,
                    "pan_ended": pan_end,
                }
    return {
        "confirmed": False,
        "reason": "no-recent-accepted-cover-sheet-pan-sequence",
        "recent_events": candidates[-8:],
    }


def _class_name(process, value):
    if not isinstance(value, int) or value <= 0x10000:
        return None
    return render._objc_class_name(process, value)


def on_hit(frame, bp_loc, _context):
    breakpoint = bp_loc.GetBreakpoint()
    bp_id = breakpoint.GetID()
    process = frame.GetThread().GetProcess()
    if bks._progname(process) != "SpringBoard":
        return False
    thread = frame.GetThread()
    tpidr = bks._reg(frame, "tpidr_el1") or 0
    sp = bks._reg(frame, "sp") or 0
    pid = process.GetProcessID()

    if bp_id in RETURNS:
        expected = RETURNS[bp_id]
        if (tpidr != expected["tpidr"] or sp != expected["sp"]):
            return False
        label = expected["label"]
        count = RETURN_COUNTS.get(label, 0) + 1
        RETURN_COUNTS[label] = count
        row = {
            "label": label, "count": count, "time": time.time(),
            "process": "SpringBoard", "pid": pid,
            "tpidr_el1": hex(tpidr), "sp": hex(sp),
            "pc": hex(bks._reg(frame, "pc") or 0),
            "x0": bks._reg(frame, "x0"),
        }
        row.update(expected.get("metadata", {}))
        _emit("LOCK_GESTURE_JSON " + json.dumps(row, sort_keys=True))
        _remember_dismissal_event(row)
        breakpoint.SetEnabled(False)
        RETURNS.pop(bp_id, None)
        return False

    label = META.get(bp_id)
    if not label:
        return False
    instance_class = _class_name(process, bks._reg(frame, "x0"))
    if (label.startswith("GESTURE_RECOGNIZER_") and
            instance_class not in SCREEN_EDGE_RECOGNIZERS):
        count = FILTER_COUNTS.get(label, 0) + 1
        FILTER_COUNTS[label] = count
        if count <= 64:
            _emit("LOCK_GESTURE_FILTERED " + json.dumps({
                "label": label, "count": count, "time": time.time(),
                "process": "SpringBoard", "pid": pid,
                "thread_id": thread.GetThreadID(), "tpidr_el1": hex(tpidr),
                "pc": hex(bks._reg(frame, "pc") or 0),
                "x0_class": instance_class,
                "x2": bks._reg(frame, "x2"),
            }, sort_keys=True))
        return False
    if (label == "FLUID_SCREEN_EDGE_SET_STATE_ENTRY" and
            instance_class != "SBFluidSwitcherScreenEdgePanGestureRecognizer"):
        return False
    if (label == "SCREEN_EDGE_IN_GRABBER_ZONE" and
            instance_class not in SCREEN_EDGE_RECOGNIZERS):
        count = FILTER_COUNTS.get(label, 0) + 1
        FILTER_COUNTS[label] = count
        if count <= 64:
            _emit("LOCK_GESTURE_FILTERED " + json.dumps({
                "label": label, "count": count, "time": time.time(),
                "process": "SpringBoard", "pid": pid,
                "thread_id": thread.GetThreadID(), "tpidr_el1": hex(tpidr),
                "pc": hex(bks._reg(frame, "pc") or 0),
                "x0_class": instance_class,
            }, sort_keys=True))
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count > MAX_HITS:
        breakpoint.SetEnabled(False)
        return False
    values = {"x%d" % index: bks._reg(frame, "x%d" % index)
              for index in range(8)}
    row = {
        "label": label, "count": count, "time": time.time(),
        "process": "SpringBoard", "pid": pid,
        "thread_id": thread.GetThreadID(), "tpidr_el1": hex(tpidr),
        "pc": hex(bks._reg(frame, "pc") or 0),
        "lr": hex(bks._reg(frame, "lr") or 0), "sp": hex(sp),
        **values,
        "d0": frame.FindRegister("d0").GetValue()
        if frame.FindRegister("d0").IsValid() else None,
        "d1": frame.FindRegister("d1").GetValue()
        if frame.FindRegister("d1").IsValid() else None,
    }
    if label.startswith("GESTURE_RECOGNIZER_") or label in (
            "FLUID_SCREEN_EDGE_SET_STATE_ENTRY",
            "SCREEN_EDGE_IN_GRABBER_ZONE"):
        row["x0_class"] = instance_class
    elif label in ("COVERSHEET_DISMISS_GESTURE_BEGAN",
                 "COVERSHEET_DISMISS_GESTURE_CHANGED",
                 "HOME_GESTURE_INTERACTION_HANDLE",
                 "HOME_GESTURE_INTERACTION_SHOULD_BEGIN",
                 "HOME_GESTURE_PAN_SHOULD_BEGIN",
                 "COVERSHEET_SYSTEM_GESTURE_SHOULD_BEGIN"):
        row["x0_class"] = _class_name(process, values["x0"])
        row["x2_class"] = _class_name(process, values["x2"])
    elif label.startswith("HOME_GESTURE_PAN_TOUCHES_"):
        row["x0_class"] = _class_name(process, values["x0"])
        row["x2_class"] = _class_name(process, values["x2"])
        row["x3_class"] = _class_name(process, values["x3"])
    else:
        row["x0_class"] = _class_name(process, values["x0"])
    _emit("LOCK_GESTURE_JSON " + json.dumps(row, sort_keys=True))
    _remember_dismissal_event(row)
    # Downstream CA/update probes can gate their bounded sample budget on the
    # accepted user transition, instead of collecting only pre-swipe traffic.
    if label == "LOCKSCREEN_GESTURE_TRANSITION" and values.get("x3") == 1:
        marker = {
            "label": "ACCEPTED_LOCKSCREEN_GESTURE_TRANSITION",
            "time": row["time"], "pid": pid,
            "tpidr_el1": hex(tpidr), "pc": row["pc"],
            "lock_manager": row.get("x0"),
            "for_user_gesture": values.get("x3"),
            "velocity": row.get("d1"),
        }
        marker["dismissal_gesture"] = _confirmed_dismissal_sequence(marker)
        marker["gesture_sequence_confirmed"] = marker["dismissal_gesture"]["confirmed"]
        try:
            temporary = TRANSITION_MARKER.with_suffix(".tmp")
            temporary.write_text(json.dumps(marker, sort_keys=True) + "\n")
            temporary.replace(TRANSITION_MARKER)
            _emit("LOCK_GESTURE_TRANSITION_MARKER " +
                  json.dumps(marker, sort_keys=True))
        except Exception as error:
            _emit("LOCK_GESTURE_TRANSITION_MARKER_ERROR " + repr(error))
        if ACCEPTED_TRANSITION_HOOK is not None:
            try:
                ACCEPTED_TRANSITION_HOOK(frame, marker)
            except Exception as error:
                _emit("LOCK_GESTURE_TRANSITION_HOOK_ERROR " +
                      json.dumps({"error": repr(error),
                                  "marker": marker}, sort_keys=True))
    if label in RETURN_LABELS:
        metadata = ({"recognizer": values.get("x0")}
                    if label == "HOME_GESTURE_PAN_SHOULD_BEGIN" else None)
        _arm_return(frame, label, metadata)
    return False


def install(debugger, slide):
    global SLIDE, TRANSITION_MARKER, EVENT_OUT, ACCEPTED_TRANSITION_HOOK
    SLIDE = int(slide)
    RECENT_DISMISSAL_GESTURE.clear()
    bks.SLIDE[0] = SLIDE
    event_dir = Path(os.environ.get(
        "DVM_PROBE_EVENT_DIR", "/tmp/dvm/post-shell-home-gesture-events"))
    event_dir.mkdir(parents=True, exist_ok=True)
    EVENT_OUT = event_dir / "lock-gesture.jsonl"
    # Consumers may opt into work that is safe only after an accepted user
    # transition. They register after this install returns, before input runs.
    ACCEPTED_TRANSITION_HOOK = None
    TRANSITION_MARKER = Path(os.environ.get(
        "DVM_LOCK_GESTURE_TRANSITION_MARKER",
        str(event_dir / "accepted-lockscreen-gesture-transition.json")))
    os.environ["DVM_LOCK_GESTURE_TRANSITION_MARKER"] = str(TRANSITION_MARKER)
    try:
        TRANSITION_MARKER.unlink()
    except FileNotFoundError:
        pass
    META.clear()
    RETURNS.clear()
    ARMED_RETURNS.clear()
    COUNTS.clear()
    RETURN_COUNTS.clear()
    FILTER_COUNTS.clear()
    armed = []
    for offset, label in USER_SITES:
        breakpoint = _register(debugger, SLIDE + offset, label)
        armed.append({"label": label, "id": breakpoint.GetID(),
                      "address": "0x%x" % (SLIDE + offset),
                      "locations": breakpoint.GetNumLocations()})
    _emit("LOCK_GESTURE_READY " + json.dumps(
        {"slide": "0x%x" % SLIDE, "sites": armed}, sort_keys=True),
        )

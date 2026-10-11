"""Low-load, read-only trace of native touch delivery and lock dismissal.

Installed only after the matched shell-service return. It composes Home
lifecycle and SpringBoard recognizer observers with a small register-only
BackBoard/UIKit route trace and selected Cover Sheet policy checkpoints. It
does not inspect large event graphs, stop the guest, or alter UI state.
"""

import json
import os
import sys
import time
from pathlib import Path

import lldb


HERE = Path(__file__).resolve().parent
INPUT_SITES = (
    (0x22A352848, "BACKBOARD_SYSTEM_EVENT", "backboardd"),
    (0x22A351FAC, "BACKBOARD_PRIMARY_EVENT", "backboardd"),
    (0x22A352074, "BACKBOARD_PROCESSOR_DISPATCH_CALL", "backboardd"),
    (0x22A415E40, "BACKBOARD_DIRECT_TOUCH_PROCESS_EVENT", "backboardd"),
    (0x22A414320, "BACKBOARD_HANDLE_DIRECT_TOUCH_EVENT", "backboardd"),
    (0x22A414E48, "BACKBOARD_HIT_COORDINATES", "backboardd"),
    (0x22439E9FC, "SPRINGBOARD_HID_EVENT", "SpringBoard"),
    (0x18493425C, "UI_EVENT", "SpringBoard"),
    (0x184E40DC0, "UI_WINDOW_HIT_TEST", "SpringBoard"),
)
POLICY_SITES = (
    (0x22436589C, "DELEGATE_GESTURE_SHOULD_BEGIN", "entry"),
    (0x22425E214, "SB_HOME_GESTURE_ENABLED", "entry"),
    (0x224A5040C, "CAN_DISMISS_GESTURE_BEGIN", "entry"),
    (0x224A50480, "CAN_PRESENT_GESTURE_BEGIN", "entry"),
    (0x224365E7C, "SHOULD_BEGIN_FINAL_AND_1", "checkpoint"),
    (0x224365E90, "SHOULD_BEGIN_FINAL_BRANCH", "checkpoint"),
    (0x224365E9C, "SHOULD_BEGIN_ACCEPT_PATH", "checkpoint"),
    (0x224365EA4, "SHOULD_BEGIN_REJECT_PATH", "checkpoint"),
    (0x224365F54, "SHOULD_BEGIN_REJECTION_REASON", "checkpoint"),
    (0x224365F90, "SHOULD_BEGIN_RETURN_FALSE", "checkpoint"),
    (0x224365F9C, "SHOULD_BEGIN_EPILOGUE_RESULT", "checkpoint"),
    (0x224365FC8, "SHOULD_BEGIN_RETURN_VALUE", "checkpoint"),
)
INPUT_META = {}
INPUT_COUNTS = {}
INPUT_LIMIT = 1024
INPUT_OUT = None
UPDATE_GATE_MODULE = None
UPDATE_GATE_DEBUGGER = None
UPDATE_GATE_SLIDE = 0
UPDATE_GATE_ARMED_AFTER_TRANSITION = False


def _import(debugger, name):
    path = HERE / (name + ".py")
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % path, result)
    if not result.Succeeded():
        raise RuntimeError("LLDB import failed for %s: %s" %
                           (name, result.GetError() or result.GetOutput()))
    module = sys.modules.get(name)
    if module is None:
        raise RuntimeError("LLDB did not register %s" % name)
    return module


def _reg(frame, name):
    try:
        register = frame.FindRegister(name)
        return register.GetValueAsUnsigned() if register.IsValid() else None
    except Exception:
        return None


def _emit(row):
    row["time"] = time.time()
    line = "UNLOCK_ROUTE_LITE " + json.dumps(row, sort_keys=True)
    try:
        if INPUT_OUT is not None:
            with INPUT_OUT.open("a") as stream:
                stream.write(line + "\n")
                stream.flush()
    except Exception as error:
        print("UNLOCK_ROUTE_LITE_FILE_ERROR " + repr(error), flush=True)
    print(line, flush=True)


def on_input(frame, bp_loc, _context):
    bp_id = bp_loc.GetBreakpoint().GetID()
    site = INPUT_META.get(bp_id)
    if not site:
        return False
    process = frame.GetThread().GetProcess()
    pid = process.GetProcessID()
    expected = site[2]
    try:
        import bks_checkin_callbacks as bks
        actual = bks._progname(process)
    except Exception:
        actual = None
    if actual != expected:
        return False
    count = INPUT_COUNTS.get(site[1], 0) + 1
    INPUT_COUNTS[site[1]] = count
    if count > INPUT_LIMIT:
        return False
    thread = frame.GetThread()
    row = {
        "label": site[1], "count": count, "process": actual, "pid": pid,
        "thread_id": thread.GetThreadID(),
        "tpidr_el1": hex(_reg(frame, "tpidr_el1") or 0),
        "pc": hex(_reg(frame, "pc") or 0),
        "lr": hex(_reg(frame, "lr") or 0),
        "sp": hex(_reg(frame, "sp") or 0),
    }
    for name in ("x0", "x1", "x2", "x3", "x4", "x5", "x8"):
        row[name] = _reg(frame, name)
    for name in ("d0", "d1"):
        register = frame.FindRegister(name)
        row[name] = register.GetValue() if register.IsValid() else None
    _emit(row)
    return False


def _arm_input(debugger, slide):
    target = debugger.GetSelectedTarget()
    armed = []
    for offset, label, process in INPUT_SITES:
        bp = target.BreakpointCreateByAddress(slide + offset)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("no input-route BP %s @ 0x%x" %
                               (label, slide + offset))
        bp.SetAutoContinue(True)
        INPUT_META[bp.GetID()] = (offset, label, process)
        result = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command add -F %s.on_input %d" %
            (__name__, bp.GetID()), result)
        proof = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command list %d" % bp.GetID(), proof)
        if (not result.Succeeded() or not proof.Succeeded() or
                (__name__ + ".on_input") not in (proof.GetOutput() or "")):
            bp.SetEnabled(False)
            raise RuntimeError("input callback proof failed for %s: %s" %
                               (label, proof.GetError() or result.GetError()))
        armed.append({"label": label, "address": hex(slide + offset),
                      "id": bp.GetID(), "process": process})
    return armed


def _arm_policy_subset(debugger, slide, policy):
    policy.SLIDE = slide
    policy.EVENT_DIR = Path(os.environ.get(
        "DVM_PROBE_EVENT_DIR", "/tmp/dvm/unlock-route-lite-events"))
    policy.ACTIVE.clear()
    policy.META.clear()
    policy.COUNTS.clear()
    target = debugger.GetSelectedTarget()
    armed = []
    for offset, label, kind in POLICY_SITES:
        bp = target.BreakpointCreateByAddress(slide + offset)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("no policy BP %s @ 0x%x" %
                               (label, slide + offset))
        bp.SetAutoContinue(True)
        policy.META[bp.GetID()] = {"kind": kind, "label": label}
        result = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command add -F dm_lock_delegate_policy_callbacks.on_site %d" %
            bp.GetID(), result)
        proof = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command list %d" % bp.GetID(), proof)
        if (not result.Succeeded() or not proof.Succeeded() or
                "dm_lock_delegate_policy_callbacks.on_site" not in
                (proof.GetOutput() or "")):
            bp.SetEnabled(False)
            raise RuntimeError("policy callback proof failed for %s: %s" %
                               (label, proof.GetError() or result.GetError()))
        armed.append({"label": label, "address": hex(slide + offset),
                      "id": bp.GetID(), "kind": kind})
    return armed


def on_accepted_transition(_frame, marker):
    """Arm the Update observer only after a real accepted user dismissal."""
    global UPDATE_GATE_ARMED_AFTER_TRANSITION
    if UPDATE_GATE_MODULE is None or UPDATE_GATE_ARMED_AFTER_TRANSITION:
        return
    UPDATE_GATE_MODULE.install(UPDATE_GATE_DEBUGGER, UPDATE_GATE_SLIDE)
    UPDATE_GATE_ARMED_AFTER_TRANSITION = True
    print("POST_SHELL_HOME_UPDATE_GATE_MIN_ARMED_AFTER_ACCEPTED_TRANSITION " +
          json.dumps({"slide": hex(UPDATE_GATE_SLIDE),
                      "transition": marker,
                      "sites": len(UPDATE_GATE_MODULE.SITES)},
                     sort_keys=True), flush=True)


def install(debugger, slide):
    global INPUT_OUT, UPDATE_GATE_MODULE, UPDATE_GATE_DEBUGGER
    global UPDATE_GATE_SLIDE, UPDATE_GATE_ARMED_AFTER_TRANSITION
    slide = int(slide)
    event_dir = Path(os.environ.get("DVM_PROBE_EVENT_DIR", "/tmp/dvm/events"))
    event_dir.mkdir(parents=True, exist_ok=True)
    INPUT_OUT = event_dir / "unlock-route-lite.jsonl"
    INPUT_META.clear()
    INPUT_COUNTS.clear()

    home = _import(debugger, "dm_post_shell_home_min_callbacks")
    gesture = _import(debugger, "dm_lock_gesture_callbacks")
    policy = _import(debugger, "dm_lock_delegate_policy_callbacks")

    # The policy module observes this same shouldBegin entry. Avoid installing
    # its duplicate copy from the generic gesture list.
    gesture.USER_SITES = tuple(
        (offset, label) for offset, label in gesture.USER_SITES
        if label != "COVERSHEET_SYSTEM_GESTURE_SHOULD_BEGIN")
    home.install(debugger, slide)
    gesture.install(debugger, slide)
    input_armed = _arm_input(debugger, slide)
    policy_armed = _arm_policy_subset(debugger, slide, policy)
    update_gate_min = None
    update_gate_min_enabled = os.environ.get(
        "DVM_POSTHOME_UPDATE_GATE_MIN_TRACE", "0") == "1"
    if update_gate_min_enabled:
        update_gate_min = _import(
            debugger, "dm_posthome_update_gate_min_callbacks")
        UPDATE_GATE_MODULE = update_gate_min
        UPDATE_GATE_DEBUGGER = debugger
        UPDATE_GATE_SLIDE = slide
        UPDATE_GATE_ARMED_AFTER_TRANSITION = False
        # Do not place Update breakpoints while the lock UI is still active.
        # The accepted-transition callback below installs them after writing
        # this boot's marker, avoiding pre-input stops and pre-transition data.
        gesture.ACCEPTED_TRANSITION_HOOK = on_accepted_transition
    else:
        UPDATE_GATE_MODULE = None
        UPDATE_GATE_DEBUGGER = None
        UPDATE_GATE_SLIDE = 0
        UPDATE_GATE_ARMED_AFTER_TRANSITION = False
    print("POST_SHELL_HOME_UNLOCK_DIAG_READY " + json.dumps({
        "slide": hex(slide), "home_hooks": len(home.SITES),
        "gesture_hooks": len(gesture.USER_SITES),
        "input_route_hooks": input_armed,
        "delegate_policy_hooks": policy_armed,
        "update_gate_min_trace": update_gate_min_enabled,
        "update_gate_min_sites": (len(update_gate_min.SITES)
                                  if update_gate_min else 0),
        "update_gate_min_arm": ("accepted-transition callback"
                                 if update_gate_min else "disabled"),
        "heavy_input_event_walk": False,
        "render_scheduler_trace": False,
        "read_only": True,
    }, sort_keys=True), flush=True)
    # The input trigger uses this stable marker family as its observer-ready
    # gate. Keep that contract when selecting this narrower composite module.
    print("POST_SHELL_HOME_GESTURE_READY " + json.dumps({
        "slide": hex(slide), "sites": len(gesture.USER_SITES),
        "diagnostic_module": __name__,
    }, sort_keys=True), flush=True)

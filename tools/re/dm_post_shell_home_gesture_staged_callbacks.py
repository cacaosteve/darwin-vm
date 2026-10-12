"""Low-pressure Home/gesture hooks with CA/render probes added after acceptance.

The Home lifecycle and gesture breakpoints are installed at the shell-return
gate.  Renderer/surface callbacks are not created until SpringBoard records a
positive user lock-screen transition (for_user_gesture == 1).  This keeps the
high-frequency display hooks out of the gesture-delivery window.
"""

import json
import os
import sys
import time
from pathlib import Path

import lldb


HERE = Path(__file__).resolve().parent
SLIDE = 0
EVENT_DIR = None
GESTURE = None
PRESENT_ARMED = False
PRESENT_INSTALLING = False
UPDATE_GATE_ARMED = False


def _emit(label, data):
    row = {"label": label, "time": time.time()}
    row.update(data)
    line = label + " " + json.dumps(row, sort_keys=True)
    print(line, flush=True)
    if EVENT_DIR is not None:
        try:
            with (EVENT_DIR / "staged-ui.jsonl").open("a") as output:
                output.write(line + "\n")
                output.flush()
        except Exception as error:
            print("STAGED_UI_FILE_ERROR " + repr(error), flush=True)


def _command_import(debugger, name, path):
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


def _arm_after_accepted_transition(frame, marker):
    global PRESENT_ARMED, PRESENT_INSTALLING, UPDATE_GATE_ARMED
    if PRESENT_ARMED or PRESENT_INSTALLING:
        return
    if marker.get("for_user_gesture") != 1:
        _emit("POSTHOME_PRESENT_DEFERRED_SKIP", {
            "reason": "transition-not-user-accepted", "marker": marker,
        })
        return

    PRESENT_INSTALLING = True
    try:
        event_dir = EVENT_DIR
        transition_path = Path(os.environ[
            "DVM_LOCK_GESTURE_TRANSITION_MARKER"])
        if not transition_path.exists():
            transition_tmp = transition_path.with_suffix(".tmp")
            transition_tmp.write_text(json.dumps(marker, sort_keys=True) + "\n")
            transition_tmp.replace(transition_path)
        capture_start = event_dir / "capture-after-input"
        temporary = capture_start.with_suffix(".tmp")
        temporary.write_text(json.dumps({
            "label": "CAPTURE_AFTER_ACCEPTED_LOCK_TRANSITION",
            "transition_marker": str(transition_path),
            "transition": marker,
        }, sort_keys=True) + "\n")
        temporary.replace(capture_start)

        debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
        present = _command_import(
            debugger, "dm_posthome_present_callbacks",
            HERE / "dm_posthome_present_callbacks.py")
        present.install(debugger, SLIDE, install_home=False)
        PRESENT_ARMED = True
        _emit("POSTHOME_PRESENT_DEFERRED_ARMED", {
            "slide": "0x%x" % SLIDE,
            "marker": str(transition_path),
            "capture_start": str(capture_start),
            "present_module": str(Path(present.__file__).resolve()),
            "accepted_transition": marker,
            "update_gate_min_armed_here": False,
        })
        if (not UPDATE_GATE_ARMED and
                os.environ.get("DVM_POSTHOME_UPDATE_GATE_MIN_TRACE", "0") == "1"):
            try:
                update_gate = _command_import(
                    debugger, "dm_posthome_update_gate_min_callbacks",
                    HERE / "dm_posthome_update_gate_min_callbacks.py")
                update_gate.install(debugger, SLIDE)
                UPDATE_GATE_ARMED = True
                _emit("POSTHOME_UPDATE_GATE_DEFERRED_ARMED", {
                    "slide": "0x%x" % SLIDE,
                    "module": str(Path(update_gate.__file__).resolve()),
                    "after_accepted_transition": True,
                    "accepted_transition": marker,
                })
            except Exception as error:
                _emit("POSTHOME_UPDATE_GATE_DEFERRED_ARM_FAILED", {
                    "error": repr(error), "slide": "0x%x" % SLIDE,
                    "marker": marker,
                })
    except Exception as error:
        _emit("POSTHOME_PRESENT_DEFERRED_ARM_FAILED", {
            "error": repr(error), "slide": "0x%x" % SLIDE,
            "marker": marker,
        })
    finally:
        PRESENT_INSTALLING = False


def install(debugger, slide):
    global SLIDE, EVENT_DIR, GESTURE, PRESENT_ARMED, PRESENT_INSTALLING
    global UPDATE_GATE_ARMED
    SLIDE = int(slide)
    EVENT_DIR = Path(os.environ.get(
        "DVM_PROBE_EVENT_DIR", "/tmp/dvm/post-shell-home-gesture-staged"))
    EVENT_DIR.mkdir(parents=True, exist_ok=True)
    PRESENT_ARMED = False
    PRESENT_INSTALLING = False
    UPDATE_GATE_ARMED = False

    transition_path = EVENT_DIR / "accepted-lockscreen-gesture-transition.json"
    capture_start = EVENT_DIR / "capture-after-input"
    os.environ["DVM_LOCK_GESTURE_TRANSITION_MARKER"] = str(transition_path)
    os.environ["DVM_POSTHOME_PRESENT_START_FILE"] = str(capture_start)
    os.environ["DVM_POSTHOME_PRESENT_SURFACE_START_FILE"] = str(transition_path)
    # The gesture installer clears the accepted-transition marker. Clear the
    # companion gate too so a stale file can never open present capture early.
    for path in (capture_start, capture_start.with_suffix(".tmp")):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    home = _command_import(
        debugger, "dm_post_shell_home_min_callbacks",
        HERE / "dm_post_shell_home_min_callbacks.py")
    GESTURE = _command_import(
        debugger, "dm_lock_gesture_callbacks",
        HERE / "dm_lock_gesture_callbacks.py")
    home.install(debugger, SLIDE)
    GESTURE.install(debugger, SLIDE)
    GESTURE.ACCEPTED_TRANSITION_HOOK = _arm_after_accepted_transition
    _emit("POST_SHELL_HOME_GESTURE_STAGED_READY", {
        "slide": "0x%x" % SLIDE,
        "home_sites": len(home.SITES),
        "gesture_sites": len(GESTURE.USER_SITES),
        "renderer_sites_installed": False,
        "renderer_arm_condition": "LOCKSCREEN_GESTURE_TRANSITION x3==1",
        "capture_start": str(capture_start),
        "surface_start": str(transition_path),
    })

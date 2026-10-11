"""Read-only SpringBoard gesture and post-home compositor capture.

This profile omits the broader BackBoard input-event hooks. It keeps only the
Home lifecycle, lock-gesture recognizers, and CA/render/swap/IOSurface ladder,
so an accepted swipe can be paired with the first visible rendering handoff.
"""

import json
import os
import sys
from pathlib import Path

import lldb


HERE = Path(__file__).resolve().parent


def _import(debugger, name, path):
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


def install(debugger, slide):
    event_dir = Path(os.environ.get(
        "DVM_PROBE_EVENT_DIR", "/tmp/dvm/post-shell-home-gesture-present"))
    event_dir.mkdir(parents=True, exist_ok=True)
    start_file = event_dir / "capture-after-input"
    os.environ["DVM_POSTHOME_PRESENT_START_FILE"] = str(start_file)
    present = _import(debugger, "dm_posthome_present_callbacks",
                      HERE / "dm_posthome_present_callbacks.py")
    gesture = _import(debugger, "dm_lock_gesture_callbacks",
                      HERE / "dm_lock_gesture_callbacks.py")
    scheduler_enabled = os.environ.get(
        "DVM_POSTHOME_RENDER_SCHEDULER_TRACE", "0") == "1"
    update_gate_min_enabled = os.environ.get(
        "DVM_POSTHOME_UPDATE_GATE_MIN_TRACE", "0") == "1"
    ca_handoff_enabled = os.environ.get(
        "DVM_POSTHOME_CA_HANDOFF_TRACE", "0") == "1"
    present.install(debugger, int(slide))
    gesture.install(debugger, int(slide))
    input_trace_enabled = os.environ.get(
        "DVM_POSTHOME_BACKBOARD_INPUT_TRACE", "0") == "1"
    if input_trace_enabled:
        input_trace = _import(debugger, "dm_unlock_input_callbacks",
                              HERE / "dm_unlock_input_callbacks.py")
        input_trace.install(debugger, int(slide))
    if scheduler_enabled:
        scheduler = _import(debugger, "dm_posthome_render_scheduler_callbacks",
                            HERE / "dm_posthome_render_scheduler_callbacks.py")
        scheduler.install(debugger, int(slide))
    if update_gate_min_enabled:
        update_gate_min = _import(
            debugger, "dm_posthome_update_gate_min_callbacks",
            HERE / "dm_posthome_update_gate_min_callbacks.py")
        update_gate_min.install(debugger, int(slide))
    if ca_handoff_enabled:
        ca_send = _import(debugger, "ca_msg36_send_callbacks",
                          HERE / "ca_msg36_send_callbacks.py")
        ca_receive = _import(debugger, "ca_msg36_callbacks",
                             HERE / "ca_msg36_callbacks.py")
        ca_send.install(debugger, int(slide))
        ca_receive.install(debugger, int(slide))
    print("POST_SHELL_HOME_GESTURE_PRESENT_READY " + json.dumps({
        "slide": "0x%x" % int(slide),
        "gesture_sites": len(gesture.USER_SITES),
        "render_sites": len(present.USER_SITES) + len(present.KERNEL_SITES),
        "render_scheduler_trace": scheduler_enabled,
        "update_gate_min_trace": update_gate_min_enabled,
        "update_gate_after_lock_transition": os.environ.get(
            "DVM_POSTHOME_UPDATE_GATE_AFTER_LOCK_TRANSITION", "0") == "1",
        "ca_handoff_trace": ca_handoff_enabled,
        "ca_messages_after_lock_transition": os.environ.get(
            "DVM_CA_MSG_AFTER_LOCK_TRANSITION", "0") == "1",
        "backboard_input_trace": input_trace_enabled,
        "surface_start_file": str(start_file),
        "lock_transition_marker": os.environ.get(
            "DVM_LOCK_GESTURE_TRANSITION_MARKER"),
        "backboard_input_hooks": input_trace_enabled,
    }, sort_keys=True), flush=True)

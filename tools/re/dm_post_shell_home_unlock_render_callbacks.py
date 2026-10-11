"""Combine post-home render/pixel and ordinary input observers.

The SpringBoard unlock/Home input path remains native and read-only. The
post-home compositor callbacks record CA, render, swap, and IOSurface state;
pixel copies are gated until the host confirms an ACKed ordinary gesture.
"""

import json
import os
from pathlib import Path
import sys

import lldb


HERE = Path(__file__).resolve().parent


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


def install(debugger, slide):
    home_probe = _command_import(
        debugger, "dm_post_shell_home_min_callbacks",
        HERE / "dm_post_shell_home_min_callbacks.py")
    event_dir = Path(os.environ.get(
        "DVM_PROBE_EVENT_DIR", "/tmp/dvm/post-shell-home-unlock-events"))
    event_dir.mkdir(parents=True, exist_ok=True)
    start_file = event_dir / "capture-after-input"
    os.environ["DVM_POSTHOME_PRESENT_START_FILE"] = str(start_file)

    present = _command_import(
        debugger, "dm_posthome_present_callbacks",
        HERE / "dm_posthome_present_callbacks.py")
    can_update_enabled = os.environ.get("DVM_POSTHOME_CAN_UPDATE", "0") == "1"
    if can_update_enabled:
        can_update = _command_import(
            debugger, "dm_posthome_can_update_callbacks",
            HERE / "dm_posthome_can_update_callbacks.py")
    input_probe = _command_import(
        debugger, "dm_unlock_input_callbacks",
        HERE / "dm_unlock_input_callbacks.py")
    gesture_probe = _command_import(
        debugger, "dm_lock_gesture_callbacks",
        HERE / "dm_lock_gesture_callbacks.py")
    ca_trace_enabled = os.environ.get("DVM_POSTHOME_CA_HANDOFF_TRACE", "0") == "1"
    if ca_trace_enabled:
        ca_send_probe = _command_import(
            debugger, "ca_msg36_send_callbacks",
            HERE / "ca_msg36_send_callbacks.py")
        ca_receive_probe = _command_import(
            debugger, "ca_msg36_callbacks", HERE / "ca_msg36_callbacks.py")
    # The host-side swipe trigger requires a non-null home-entity return; a
    # shell-service return alone is not enough to send UI input. Install the
    # lightweight SpringBoard lifecycle observer with the render/input probes
    # so that readiness is witnessed on this boot.
    home_probe.install(debugger, int(slide))
    present.install(debugger, int(slide))
    input_probe.install(debugger, int(slide))
    gesture_probe.install(debugger, int(slide))
    if ca_trace_enabled:
        # Trace the QuartzCore 0x9c4x client-send/server-receive family at
        # function boundaries. Global mach_msg traps remain disabled; these
        # callbacks are observational and keep the post-shell cut narrow.
        ca_send_probe.install(debugger, int(slide))
        ca_receive_probe.install(debugger, int(slide))
    if can_update_enabled:
        can_update.install(debugger, int(slide))
    scheduler_trace_enabled = os.environ.get(
        "DVM_POSTHOME_RENDER_SCHEDULER_TRACE", "0") == "1"
    hotplug_watch_enabled = os.environ.get(
        "DVM_POSTHOME_HOTPLUG_STATE_WATCH", "1") == "1"
    mutator_flag_trace_enabled = os.environ.get(
        "DVM_POSTHOME_MUTATOR_FLAG_TRACE", "0") == "1"
    if scheduler_trace_enabled:
        scheduler_probe = _command_import(
            debugger, "dm_posthome_render_scheduler_callbacks",
            HERE / "dm_posthome_render_scheduler_callbacks.py")
        scheduler_probe.install(debugger, int(slide))
        if hotplug_watch_enabled:
            hotplug_probe = _command_import(
                debugger, "dm_posthome_hotplug_state_watch",
                HERE / "dm_posthome_hotplug_state_watch.py")
            # Derive the live display/state objects from the first observed
            # post-shell render_for_time frame. This keeps the hardware-state
            # watch in the same capture, instead of requiring a late manual LLDB
            # command after a Home event.
            hotplug_probe.install_user_frame(debugger, int(slide))
    if mutator_flag_trace_enabled:
        mutator_probe = _command_import(
            debugger, "dm_posthome_mutator_flags_callbacks",
            HERE / "dm_posthome_mutator_flags_callbacks.py")
        mutator_probe.install(debugger, int(slide))
    row = {
        "label": "POST_SHELL_HOME_UNLOCK_RENDER_READY",
        "slide": "0x%x" % int(slide),
        "event_dir": str(event_dir),
        "surface_capture_start_file": str(start_file),
        "surface_samples_gated_until_acknowledged_input": True,
        "can_update_return_trace": can_update_enabled,
        "render_scheduler_trace": scheduler_trace_enabled,
        "hotplug_state_watch": scheduler_trace_enabled and hotplug_watch_enabled,
        "mutator_flag_trace": mutator_flag_trace_enabled,
        "ca_handoff_trace": ca_trace_enabled,
        "ca_global_mach_traps": False,
    }
    print("POST_SHELL_HOME_UNLOCK_RENDER_READY " +
          json.dumps(row, sort_keys=True), flush=True)

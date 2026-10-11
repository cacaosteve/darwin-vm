"""Arm the read-only compositor/surface ladder after accepted dismissal.

This composes the validated low-load Home/gesture diagnostic with the compact
Update/V170 observer.  When enabled, the existing BackBoard presentation and
IOSurface probes are installed only after SpringBoard records an accepted
user-gesture transition, preserving the lock-state baseline and avoiding an
OGL duplicate at the address already covered by the Update observer.
"""

import json
import os
import sys
from pathlib import Path

import lldb

HERE = Path(__file__).resolve().parent
DIAGNOSTIC = "dm_post_shell_home_unlock_diag_callbacks"
PRESENT = "dm_posthome_present_callbacks"
MUTATOR_FLAGS = "dm_posthome_mutator_flags_callbacks"
SCHEDULER = "dm_posthome_render_scheduler_callbacks"
HOTPLUG = "dm_posthome_hotplug_state_watch"
CA_SEND = "ca_msg36_send_callbacks"
CA_RECEIVE = "ca_msg36_callbacks"
ARMED_AFTER_TRANSITION = False
SCHEDULER_ARMED_AFTER_TRANSITION = False
CA_HANDOFF_ARMED_AFTER_TRANSITION = False
HOTPLUG_WATCH_ARMED_AFTER_TRANSITION = False


def _import(debugger, name, path):
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % path.resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("LLDB import failed for %s: %s" %
                           (name, result.GetError() or result.GetOutput()))
    module = sys.modules.get(name)
    if module is None:
        raise RuntimeError("LLDB did not register %s" % name)
    return module


def _arm_present_after_transition(debugger, slide, diagnostic, frame, marker):
    global ARMED_AFTER_TRANSITION, SCHEDULER_ARMED_AFTER_TRANSITION
    global CA_HANDOFF_ARMED_AFTER_TRANSITION
    global HOTPLUG_WATCH_ARMED_AFTER_TRANSITION
    if ARMED_AFTER_TRANSITION:
        return
    gesture_evidence = marker.get("dismissal_gesture", {})
    # The SpringBoard transition return itself is the authoritative positive
    # signal for this read-only follow-up.  Keep the recognizer-sequence
    # cross-check in the event, but do not suppress observation when that
    # auxiliary reconstruction misses a valid x3=1 / for-user-gesture call.
    # x3 == 1 at LOCKSCREEN_GESTURE_TRANSITION is the authoritative
    # SpringBoard signal that this transition came from a user gesture.
    # The recognizer-sequence reconstruction is diagnostic only: on some
    # accepted paths, SpringBoard reaches this method without the auxiliary
    # shouldBegin/began/ended rows needed to rebuild that sequence. Do not let
    # a reconstruction miss suppress the render observation we armed for.
    if marker.get("for_user_gesture") != 1:
        print("POSTHOME_PRESENT_TRANSITION_IGNORED " + json.dumps({
            "reason": "transition-not-confirmed-user-gesture",
            "transition": marker,
            "read_only": True,
        }, sort_keys=True), flush=True)
        return
    prior_hook = diagnostic.on_accepted_transition
    prior_hook(frame, marker)
    if os.environ.get("DVM_POSTHOME_PRESENT_AFTER_ACCEPTED_TRANSITION", "0") != "1":
        return

    home = sys.modules.get("dm_post_shell_home_min_callbacks")
    home_ready_threads = set(home.HOME_RETURNED) if home else set()
    present = _import(debugger, PRESENT, HERE / (PRESENT + ".py"))

    # The min observer already records OGL entry with the exact Update*.
    # Keep the rest of this ladder focused on Server/display/surface/swap.
    present.USER_SITES = tuple(
        site for site in present.USER_SITES
        if site[1] != "SERVER_OGL_RENDER")
    # This experiment needs userspace-to-swap attribution; do not arm an
    # A408/kernel breakpoint or alter any guest state.
    present.KERNEL_SITES = ()
    present.install(debugger, int(slide), install_home=False)

    scheduler_armed = False
    if os.environ.get("DVM_POSTHOME_RENDER_SCHEDULER_TRACE", "0") == "1":
        scheduler = _import(debugger, SCHEDULER,
                            HERE / (SCHEDULER + ".py"))
        scheduler.install(debugger, int(slide))
        SCHEDULER_ARMED_AFTER_TRANSITION = True
        scheduler_armed = True

        if os.environ.get("DVM_POSTHOME_HOTPLUG_STATE_WATCH", "1") == "1":
            try:
                hotplug = _import(debugger, HOTPLUG, HERE / (HOTPLUG + ".py"))
                hotplug.install_user_frame(debugger, int(slide))
                HOTPLUG_WATCH_ARMED_AFTER_TRANSITION = True
            except Exception as error:
                print("POSTHOME_HOTPLUG_STATE_ARM_FAILED " + json.dumps({
                    "error": repr(error), "read_only": True,
                }, sort_keys=True), flush=True)

    ca_handoff_armed = False
    if (os.environ.get("DVM_POSTHOME_CA_HANDOFF_TRACE", "0") == "1" or
            os.environ.get("DVM_CA_MSG_AFTER_LOCK_TRANSITION", "0") == "1"):
        ca_send = _import(debugger, CA_SEND, HERE / (CA_SEND + ".py"))
        ca_receive = _import(debugger, CA_RECEIVE,
                             HERE / (CA_RECEIVE + ".py"))
        ca_send.install(debugger, int(slide))
        ca_receive.install(debugger, int(slide))
        CA_HANDOFF_ARMED_AFTER_TRANSITION = True
        ca_handoff_armed = True

    mutator_armed = False
    if os.environ.get(
            "DVM_POSTHOME_MUTATOR_FLAGS_AFTER_ACCEPTED_TRANSITION", "0") == "1":
        try:
            mutator = _import(debugger, MUTATOR_FLAGS,
                              HERE / (MUTATOR_FLAGS + ".py"))
            mutator.install(debugger, int(slide))
            mutator_armed = True
        except Exception as error:
            print("POSTHOME_MUTATOR_FLAGS_ARM_FAILED " + json.dumps({
                "error": repr(error), "read_only": True,
            }, sort_keys=True), flush=True)

    # install_home=False is deliberately state-preserving; restore the set as
    # a guard in case a locally edited helper clears it during installation.
    if home:
        home.HOME_RETURNED.update(home_ready_threads)
    ARMED_AFTER_TRANSITION = True
    print("POSTHOME_PRESENT_AFTER_ACCEPTED_TRANSITION_ARMED " + json.dumps({
        "slide": hex(int(slide)),
        "transition": marker,
        "sites": len(present.USER_SITES),
        "sample_start_file": present.START_FILE,
        "kernel_sites": 0,
        "scheduler_trace_armed": scheduler_armed,
        "hotplug_state_watch_armed": HOTPLUG_WATCH_ARMED_AFTER_TRANSITION,
        "ca_handoff_trace_armed": ca_handoff_armed,
        "mutator_flags_armed": mutator_armed,
        "ogl_duplicate_removed": True,
        "read_only": True,
    }, sort_keys=True), flush=True)


def install(debugger, slide):
    global ARMED_AFTER_TRANSITION, SCHEDULER_ARMED_AFTER_TRANSITION
    global CA_HANDOFF_ARMED_AFTER_TRANSITION
    global HOTPLUG_WATCH_ARMED_AFTER_TRANSITION
    ARMED_AFTER_TRANSITION = False
    SCHEDULER_ARMED_AFTER_TRANSITION = False
    CA_HANDOFF_ARMED_AFTER_TRANSITION = False
    HOTPLUG_WATCH_ARMED_AFTER_TRANSITION = False
    diagnostic = _import(debugger, DIAGNOSTIC,
                         HERE / (DIAGNOSTIC + ".py"))
    diagnostic.install(debugger, int(slide))
    gesture = sys.modules.get("dm_lock_gesture_callbacks")
    if gesture is None:
        raise RuntimeError("diagnostic did not import gesture callbacks")

    def on_transition(frame, marker):
        _arm_present_after_transition(
            debugger, int(slide), diagnostic, frame, marker)

    gesture.ACCEPTED_TRANSITION_HOOK = on_transition
    print("POST_SHELL_HOME_TRANSITION_PRESENT_READY " + json.dumps({
        "slide": hex(int(slide)),
        "install_present_after_accepted_transition":
            os.environ.get("DVM_POSTHOME_PRESENT_AFTER_ACCEPTED_TRANSITION", "0") == "1",
        "render_scheduler_after_accepted_transition":
            os.environ.get("DVM_POSTHOME_RENDER_SCHEDULER_TRACE", "0") == "1",
        "hotplug_state_watch_after_accepted_transition":
            (os.environ.get("DVM_POSTHOME_RENDER_SCHEDULER_TRACE", "0") == "1" and
             os.environ.get("DVM_POSTHOME_HOTPLUG_STATE_WATCH", "1") == "1"),
        "ca_handoff_after_accepted_transition":
            (os.environ.get("DVM_POSTHOME_CA_HANDOFF_TRACE", "0") == "1" or
             os.environ.get("DVM_CA_MSG_AFTER_LOCK_TRANSITION", "0") == "1"),
        "update_gate_min_trace":
            os.environ.get("DVM_POSTHOME_UPDATE_GATE_MIN_TRACE", "0") == "1",
        "mutator_flags_after_accepted_transition":
            os.environ.get(
                "DVM_POSTHOME_MUTATOR_FLAGS_AFTER_ACCEPTED_TRANSITION", "0") == "1",
        "read_only": True,
    }, sort_keys=True), flush=True)

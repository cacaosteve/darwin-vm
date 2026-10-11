"""Observe SpringBoard shell/workspace and CAContext before its first send.

The FrontBoard scene-method probes are not exhaustive for the home screen.
These QuartzCore implementations form a broader boundary: context creation,
layer installation, and context transaction commit.  No state is modified.
"""

import json
import os
import time

import lldb

import ca_msg36_send_callbacks as client


SITES = (
    (0x1A840766C, "FBS_MODE_ENTRY"),
    # FBSDisplayMode's live caller is _FBSystemShellInitialize. The two
    # following sites are its normal epilogue and the SBSystemAppMain return
    # address after the call, respectively.
    (0x1C4A3C32C, "FB_SHELL_INIT_PRE_RETURN"),
    (0x224458BF4, "SB_AFTER_FB_SHELL_INIT"),
    (0x2242EB53C, "SB_WORKSPACE_CREATE_REQUEST"),
    (0x22430BF70, "SB_WORKSPACE_EXECUTE_TRANSITION"),
    (0x224B813D8, "SB_HOME_CONTROLLER_INIT_SHORT"),
    (0x224B814F8, "SB_HOME_CONTROLLER_INIT_FULL"),
    (0x224A46CA4, "SB_HOME_VIEW_LOAD"),
    (0x1C49E6FBC, "FB_SCENE_WORKSPACE_CREATE"),
    (0x1A844F7F4, "FBS_CA_CONTEXT_LAYER_CREATE"),
    (0x1847BEBFC, "CA_LOCAL_CONTEXT_WITH_OPTIONS"),
    (0x184517F88, "CA_CONTEXT_INIT_LOCAL"),
    (0x18450F428, "CA_CONTEXT_INIT_REMOTE"),
    (0x184511EA8, "CA_CONTEXT_WITH_ID"),
    (0x18451A138, "CA_CONTEXT_SET_LAYER"),
    (0x1843FF180, "CA_CONTEXT_COMMIT_TRANSACTION"),
)
LABELS = {}
COUNTS = {}
SLIDE = 0


def on_site(frame, bp_loc, _dict):
    breakpoint = bp_loc.GetBreakpoint()
    label = LABELS[breakpoint.GetID()]
    name, group = client._process_name(frame)
    if name != "SpringBoard" and not (
            label == "FB_SCENE_WORKSPACE_CREATE" and name == "backboardd"):
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    limit = 8 if label == "FBS_MODE_ENTRY" else 16
    if count <= limit:
        thread = frame.GetThread()
        payload = {
            "label": label,
            "count": count,
            "time": time.time(),
            "process": name,
            "group": group,
            "thread": thread.GetThreadID(),
            "pc": client._reg(frame, "pc"),
            "slide": SLIDE,
            "x0": client._reg(frame, "x0"),
            "x1": client._reg(frame, "x1"),
            "x2": client._reg(frame, "x2"),
            "x3": client._reg(frame, "x3"),
            "x4": client._reg(frame, "x4"),
        }
        if count <= 2:
            payload["backtrace"] = [
                thread.GetFrameAtIndex(i).GetPC()
                for i in range(min(thread.GetNumFrames(), 12))
            ]
        print("SB_CA_CONTEXT_JSON " + json.dumps(payload, sort_keys=True),
              flush=True)
    if count >= limit:
        breakpoint.SetEnabled(False)
        print("SB_CA_CONTEXT_DISABLED label=%s count=%d" % (label, count),
              flush=True)
    return False


def install(debugger, slide):
    global SLIDE
    SLIDE = int(slide)
    LABELS.clear()
    COUNTS.clear()
    interpreter = debugger.GetCommandInterpreter()
    result = lldb.SBCommandReturnObject()
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "ca_msg36_send_callbacks.py")
    interpreter.HandleCommand("command script import %s" % path, result)
    if not result.Succeeded():
        raise RuntimeError("failed client callback import: %s" %
                           result.GetError())
    client.install(debugger, slide)
    target = debugger.GetSelectedTarget()
    for static, label in SITES:
        address = static + SLIDE
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % address,
                                  result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed %s: %s" % (label, result.GetError()))
        breakpoint = target.GetBreakpointAtIndex(before)
        LABELS[breakpoint.GetID()] = label
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F sb_ca_context_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

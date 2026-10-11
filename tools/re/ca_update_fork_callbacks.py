"""Bounded CA client-send versus server-update probe for the 60 Hz UI boot.

This deliberately watches update creation before looking at prepared-layer
flags.  The existing client callback tracks SpringBoard/backboardd 0x9c42-45
sends; these three server stops classify render-update entry, AddContext, and
the first display layer decision.  No state is changed.
"""

import json
import os
import time

import lldb

import ca_msg36_send_callbacks as client


SITES = (
    (0x184776174, "SERVER_RENDER_UPDATE_ENTRY"),
    (0x184440B0C, "UPDATE_ADD_CONTEXT_PREPARE"),
    (0x18446C6C8, "FINISH_LAYER_DECISION"),
    # Coverage control: this constructor is known to run in SpringBoard on
    # the accepted 60 Hz fixture. Seeing it validates process identification
    # on the same boot as the absent/present CA client sends.
    (0x1A840766C, "FBS_MODE_ENTRY"),
    # Existing FrontBoard scene-pipeline implementations. If SpringBoard is
    # alive but sends no CA commit, these distinguish a missing scene request
    # from a scene that was requested but never activated or updated.
    (0x1A84301F8, "SCENE_CLIENT_REQUEST"),
    (0x1A8423960, "SCENE_CLIENT_CREATE"),
    (0x1A845CBE8, "SCENE_CLIENT_ACTIVATE_FUTURE"),
    (0x1A84280D0, "SCENE_WORKSPACE_REQUEST"),
    (0x1A8426390, "SCENE_DID_CREATE_CALLOUT"),
    (0x1A84722F8, "SCENE_CLIENT_AGENT_DID_INITIALIZE"),
    (0x1A841947C, "SCENE_HOST_AGENT_DID_INITIALIZE"),
    (0x1A8422B54, "SCENE_SEND_UPDATE"),
)
LABELS = {}
COUNTS = {}


def on_server(frame, bp_loc, _dict):
    breakpoint = bp_loc.GetBreakpoint()
    label = LABELS[breakpoint.GetID()]
    name, group = client._process_name(frame)
    if (label.startswith("SCENE_") and name not in {"SpringBoard", "Setup"}):
        return False
    if label == "FBS_MODE_ENTRY" and name != "SpringBoard":
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    limit = 8 if label == "FBS_MODE_ENTRY" else 32
    if count <= limit:
        payload = {
            "label": label,
            "count": count,
            "time": time.time(),
            "process": name,
            "group": group,
            "pc": client._reg(frame, "pc"),
            "x0": client._reg(frame, "x0"),
            "x1": client._reg(frame, "x1"),
            "x2": client._reg(frame, "x2"),
            "x3": client._reg(frame, "x3"),
            "x19": client._reg(frame, "x19"),
            "x20": client._reg(frame, "x20"),
            "x23": client._reg(frame, "x23"),
        }
        print("CA_UPDATE_FORK_JSON " + json.dumps(payload, sort_keys=True),
              flush=True)
    if count >= limit:
        breakpoint.SetEnabled(False)
        print("CA_UPDATE_FORK_DISABLED label=%s count=%d" % (label, count),
              flush=True)
    return False


def install(debugger, slide):
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
        address = static + int(slide)
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
            "breakpoint command add -F ca_update_fork_callbacks.on_server %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

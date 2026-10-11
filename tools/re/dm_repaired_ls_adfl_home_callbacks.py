"""Read-only repaired-parent ladder from ADFL through Main-home creation.

Reuse the established swap/migration/shell controls, then pair ADFL and
SBHomeScreenEntity init with their caller return addresses on the same
SpringBoard thread. The workspace and CA sites are existing static markers;
the CA marker is logged only after a non-null home-entity init return.
"""

import json
import time
from pathlib import Path

import lldb

import bks_checkin_callbacks as bks
import dm_repaired_ls_shell_callbacks as baseline


SITES = (
    (0x2244D0BA4, "SB_ADFL_ENTRY"),
    (0x2244D274C, "SB_ADFL_UPDATE_RET"),
    (0x2242EB53C, "SB_WORKSPACE_CREATE_REQUEST"),
    (0x22430BF70, "SB_WORKSPACE_EXECUTE_TRANSITION"),
    (0x224229FE0, "SB_HOME_ENTITY_INIT_ENTRY"),
    (0x18451A138, "CA_CONTEXT_SET_LAYER"),
)
META = {}
ARMED = set()
COUNTS = {}
ADFL_RETURNED = set()
HOME_RETURNED = set()


def _register(debugger, address, label, thread=0):
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint set --address 0x%x" % address, result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed %s: %s" % (label, result.GetError()))
    bp = target.GetBreakpointAtIndex(before)
    META[bp.GetID()] = label, thread
    bp.SetAutoContinue(True)
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand(
        "breakpoint command add -F dm_repaired_ls_adfl_home_callbacks.on_hit %d" %
        bp.GetID(), result)
    if not result.Succeeded():
        raise RuntimeError("callback failure %s: %s" % (label, result.GetError()))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x thread=0x%x" %
          (bp.GetID(), label, address, thread), flush=True)
    return bp


def _arm_return(frame, label, thread):
    address = bks._reg(frame, "lr")
    if not address:
        print("DM_ADFL_HOME_RETURN_UNARMED label=%s reason=null-lr" % label,
              flush=True)
        return
    key = label, thread, address
    if key in ARMED:
        return
    ARMED.add(key)
    debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
    _register(debugger, address, label, thread)


def on_hit(frame, bp_loc, _context):
    bp = bp_loc.GetBreakpoint()
    label, expected_thread = META[bp.GetID()]
    process = frame.GetThread().GetProcess()
    if bks._progname(process) != "SpringBoard":
        return False
    thread = bks._reg(frame, "tpidr_el1")
    if expected_thread and thread != expected_thread:
        return False
    if label == "CA_CONTEXT_SET_LAYER" and not HOME_RETURNED:
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count > 16:
        bp.SetEnabled(False)
        return False
    row = {
        "label": label, "count": count, "time": time.time(),
        "thread": thread, "pc": bks._reg(frame, "pc"),
        "lr": bks._reg(frame, "lr"), "x0": bks._reg(frame, "x0"),
        "x1": bks._reg(frame, "x1"), "x2": bks._reg(frame, "x2"),
    }
    print("DM_ADFL_HOME_JSON " + json.dumps(row, sort_keys=True), flush=True)
    if label == "SB_ADFL_ENTRY":
        _arm_return(frame, "SB_ADFL_CALLER_RETURN", thread)
    elif label == "SB_HOME_ENTITY_INIT_ENTRY":
        _arm_return(frame, "SB_HOME_ENTITY_INIT_RETURN", thread)
    elif label == "SB_ADFL_CALLER_RETURN":
        ADFL_RETURNED.add(thread)
        bp.SetEnabled(False)
    elif label == "SB_HOME_ENTITY_INIT_RETURN":
        if row["x0"]:
            HOME_RETURNED.add(thread)
        bp.SetEnabled(False)
    if ADFL_RETURNED and HOME_RETURNED:
        print("DM_ADFL_HOME_COMPLETE adfl_threads=%s home_threads=%s" %
              ([hex(x) for x in sorted(ADFL_RETURNED)],
               [hex(x) for x in sorted(HOME_RETURNED)]), flush=True)
    return False


def install(debugger, slide):
    interpreter = debugger.GetCommandInterpreter()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("command script import %s" %
                              Path(baseline.__file__).resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("failed baseline import: %s" % result.GetError())
    baseline.install(debugger, slide)
    META.clear()
    ARMED.clear()
    COUNTS.clear()
    ADFL_RETURNED.clear()
    HOME_RETURNED.clear()
    for static, label in SITES:
        _register(debugger, int(slide) + static, label)
    print("DM_ADFL_HOME_READY sites=%d" %
          debugger.GetSelectedTarget().GetNumBreakpoints(), flush=True)

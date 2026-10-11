"""Read-only backboardd DataMigration call/return bracket.

Addresses are from the iOS 27 beta 8 DataMigration image in the shared cache.
Only score a site on a backboardd thread that entered __DMPerformMigration.
No guest functions or memory are called/written.
"""

import json
import time

import lldb

import bks_checkin_callbacks as bks


SITES = {}
ACTIVE = {}
HITS = {}


def _install(debugger, address, label, callback="dm_perform_roundtrip_callbacks.on_stage"):
    target = debugger.GetSelectedTarget()
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint set --address 0x%x" % address, result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed to arm %s: %s" % (label, result.GetError()))
    bp = target.GetBreakpointAtIndex(before)
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command add -F %s %d" % (callback, bp.GetID()), result)
    if not result.Succeeded():
        raise RuntimeError("failed to register %s: %s" %
                           (label, result.GetError()))
    SITES[bp.GetID()] = label
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
          (bp.GetID(), label, address), flush=True)


def on_stage(frame, bp_loc, _context):
    process = frame.GetThread().GetProcess()
    if bks._progname(process) != "backboardd":
        return False
    thread = frame.GetThread()
    tid = thread.GetThreadID()
    label = SITES[bp_loc.GetBreakpoint().GetID()]
    if label == "DM_PERFORM_ENTRY":
        ACTIVE[tid] = ACTIVE.get(tid, 0) + 1
    elif tid not in ACTIVE:
        return False
    key = (tid, label)
    HITS[key] = HITS.get(key, 0) + 1
    if HITS[key] <= 12:
        event = {
            "label": label,
            "time": time.time(),
            "thread": tid,
            "depth": ACTIVE.get(tid, 0),
            "hit": HITS[key],
            "pc": bks._reg(frame, "pc"),
            "lr": bks._reg(frame, "lr"),
            "x0": bks._reg(frame, "x0"),
            "x1": bks._reg(frame, "x1"),
            "tpidr_el1": bks._reg(frame, "tpidr_el1"),
        }
        print("DM_PERFORM_ROUNDTRIP_JSON " +
              json.dumps(event, sort_keys=True), flush=True)
        bks._write_event("progress.%s.json" % label, event)
    if label == "DM_PERFORM_RETURN":
        ACTIVE[tid] -= 1
        if ACTIVE[tid] == 0:
            del ACTIVE[tid]
    return False


def install(debugger, slide):
    bks.SLIDE[0] = int(slide)
    SITES.clear()
    ACTIVE.clear()
    HITS.clear()
    for static, label in (
        (0x25E8F02D0, "DM_PERFORM_ENTRY"),
        (0x25E8F0310, "DM_ENVIRONMENT_SHARED_RETURN"),
        (0x25E8F031C, "DM_ENVIRONMENT_FIRST_CHECK_RETURN"),
        (0x25E8F0334, "DM_ENVIRONMENT_SECOND_CHECK_RETURN"),
        (0x25E8F0358, "DM_ENVIRONMENT_THIRD_CHECK_RETURN"),
        (0x25E8F0378, "DM_ENVIRONMENT_FOURTH_CHECK_RETURN"),
        (0x25E8F0394, "DM_ENVIRONMENT_PREFLIGHT_RETURN"),
        (0x25E8F03C4, "DM_AFTER_PREFLIGHT"),
        (0x25E8F03D0, "DM_CONNECTION_CLASS_RETURN"),
        (0x25E8F03F8, "DM_PERFORM_CONNECTION_CALL"),
        (0x25E8F03FC, "DM_PERFORM_CONNECTION_RETURN"),
        (0x25E8F044C, "DM_PERFORM_RETURN"),
        (0x25E8F4AF8, "DM_CONNECTION_ENTRY"),
        (0x25E8F4BA8, "DM_CONNECTION_SEND_CALL"),
        (0x25E8F4BAC, "DM_CONNECTION_SEND_RETURN"),
        (0x25E8F4C48, "DM_CONNECTION_RETURN"),
        (0x25E8F5764, "DM_XPC_SEND_ENTRY"),
        (0x25E8F5798, "DM_XPC_SYNC_CALL"),
        (0x25E8F579C, "DM_XPC_SYNC_RETURN"),
        (0x25E8F57C8, "DM_XPC_SEND_RETURN"),
    ):
        _install(debugger, bks.SLIDE[0] + static, label)
    print("DM_PERFORM_ROUNDTRIP_READY count=%d slide=0x%x" %
          (len(SITES), bks.SLIDE[0]), flush=True)

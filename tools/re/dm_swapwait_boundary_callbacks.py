"""Read-only bracket for the swap wait behind the migrator CA snapshot.

The iOS 27 _kern_SwapWait wrapper sends selector 6 with three scalar inputs:
the caller's w1 (fence), w2, and zero. Its 0x22a391300 site is immediately
after IOConnectCallScalarMethod returns. Keep that return paired by the
backboardd tpidr_el1, and compare earlier SwapEnd calls and the DCP A408 log
before attributing a missing fence completion.
"""

import json
import time

import lldb

import bks_checkin_callbacks as bks
from dm_plugin_lifecycle_callbacks import _read


SITES = (
    (0x22A391334, "KERN_SWAP_END_CALL"),
    (0x22A391368, "KERN_SWAP_END_RETURN"),
    (0x22A3912B4, "KERN_SWAP_WAIT_CALL"),
    (0x22A391300, "KERN_SWAP_WAIT_RETURN"),
    (0x2AC02CB78, "SB_SNAPSHOT_GENERATE"),
    (0x1845A509C, "CA_MACH_CALL"),
    (0x1845A50A0, "CA_MACH_RETURN"),
)
META = {}
END_CALLS = {}
WAIT_CALLS = {}
SNAPSHOT_THREADS = set()
CA_CALLS = {}
COUNTS = {}


def _u32(process, address):
    raw = _read(process, address, 4)
    return int.from_bytes(raw, "little") if len(raw) == 4 else None


def _event(frame, label, fields):
    row = {"label": label, "time": time.time(),
           "thread": frame.GetThread().GetThreadID(),
           "tpidr_el1": "0x%x" % bks._reg(frame, "tpidr_el1")}
    row.update(fields)
    print("DM_SWAPWAIT " + json.dumps(row, sort_keys=True), flush=True)


def on_hit(frame, bp_loc, _context):
    process = frame.GetThread().GetProcess()
    label = META[bp_loc.GetBreakpoint().GetID()]
    name = bks._progname(process)
    owner = bks._reg(frame, "tpidr_el1")
    if label.startswith("KERN_SWAP_"):
        if name != "backboardd":
            return False
        if label.endswith("_CALL"):
            obj = bks._reg(frame, "x0")
            count = COUNTS[label] = COUNTS.get(label, 0) + 1
            fields = {"count": count, "object": "0x%x" % obj,
                      "connection": _u32(process, obj + 0x14) if obj else None,
                      "caller_lr": "0x%x" % (bks._reg(frame, "lr") & 0x0000ffffffffffff)}
            if "WAIT" in label:
                fields.update(fence=bks._reg(frame, "w1"),
                              scalar_2=bks._reg(frame, "w2"))
                WAIT_CALLS[owner] = dict(fields, start=time.time())
            else:
                END_CALLS[owner] = dict(fields, start=time.time())
            if count <= 100 or "WAIT" in label:
                _event(frame, label, fields)
        else:
            calls = WAIT_CALLS if "WAIT" in label else END_CALLS
            call = calls.pop(owner, None)
            if call:
                _event(frame, label, {"result": "0x%x" % bks._reg(frame, "x0"),
                                      "elapsed": time.time() - call["start"],
                                      "count": call["count"],
                                      "object": call["object"],
                                      "connection": call["connection"],
                                      **({"fence": call["fence"]} if "fence" in call else {})})
        return False

    if name != "com.apple.migrationpluginwrapper":
        return False
    if label == "SB_SNAPSHOT_GENERATE":
        SNAPSHOT_THREADS.add(owner)
        _event(frame, label, {})
        return False
    if owner not in SNAPSHOT_THREADS:
        return False
    if label == "CA_MACH_CALL":
        msg = bks._reg(frame, "x0")
        msg_id = _u32(process, msg + 20)
        if msg_id == 40224:
            CA_CALLS[owner] = time.time()
            _event(frame, label, {"message_id": msg_id})
    elif owner in CA_CALLS:
        _event(frame, label, {"message_id": 40224,
                              "result": "0x%x" % bks._reg(frame, "x0"),
                              "elapsed": time.time() - CA_CALLS.pop(owner)})
    return False


def install(debugger, slide):
    bks.SLIDE[0] = int(slide)
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    for static, label in SITES:
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % (slide + static), result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed to arm %s: %s" % (label, result.GetError()))
        bp = target.GetBreakpointAtIndex(before)
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command add -F dm_swapwait_boundary_callbacks.on_hit %d" % bp.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" % (label, result.GetError()))
        META[bp.GetID()] = label
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(), result)
        if "dm_swapwait_boundary_callbacks.on_hit" not in (result.GetOutput() or result.GetError()):
            raise RuntimeError("missing command proof for %s" % label)
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" % (bp.GetID(), label, slide + static), flush=True)
    print("DM_SWAPWAIT_READY slide=0x%x sites=%d" % (slide, len(SITES)), flush=True)

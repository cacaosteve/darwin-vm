"""Low-overhead long-window progress for the first LS system-content seed."""

import json
import time

import lldb

import ccs_ls_seeding_correlation_callbacks as base


SITES = (
    (0x186EC1E84, "LSD_FIRST_IO_BLOCK", "lsd"),
    (0x186E75F0C, "LSD_SYSTEM_SCAN_CALL", "lsd"),
    (0x186F81090, "LSD_SCAN_DRIVER_CALL", "lsd"),
    (0x186F81094, "LSD_SCAN_DRIVER_RETURN", "lsd"),
    (0x186F80DA0, "LSD_REGISTER_ENTRY", "lsd"),
    (0x186F80DEC, "LSD_REGISTER_RETURN", "lsd"),
    (0x186F80E10, "LSD_REGISTER_NULL", "lsd"),
    (0x186E75F10, "LSD_SYSTEM_SCAN_RETURN", "lsd"),
    (0x186E75A98, "LSD_ENUM_RETURN", "lsd"),
    (0x186EC1FF0, "LSD_SEED_SYNCWRITE_CALL", "lsd"),
)


FIRST_THREAD = None
GATHERER = None
ACTIVE = {}
COUNTS = {}


def on_site(frame, bp_loc, context):
    global FIRST_THREAD, GATHERER
    breakpoint = bp_loc.GetBreakpoint()
    label, expected = base.LABELS[breakpoint.GetID()]
    process = frame.GetThread().GetProcess()
    if base._progname(process) != expected:
        return False
    thread = base._reg(frame, "tpidr_el1")
    x0 = base._reg(frame, "x0")
    if label == "LSD_FIRST_IO_BLOCK":
        raw = base._read(process, x0 + 0x28, 2)
        flags = int.from_bytes(raw, "little") if len(raw) == 2 else None
        if FIRST_THREAD is None and flags is not None and flags & 0x8:
            FIRST_THREAD = thread
        if thread != FIRST_THREAD:
            return False
    elif FIRST_THREAD is None:
        return False
    elif label == "LSD_SYSTEM_SCAN_CALL":
        if thread != FIRST_THREAD:
            return False
        GATHERER = x0
    elif label in ("LSD_REGISTER_ENTRY", "LSD_REGISTER_NULL"):
        gatherer_raw = base._read(process, x0 + 0x8, 8)
        gatherer = (int.from_bytes(gatherer_raw, "little")
                    if len(gatherer_raw) == 8 else None)
        if GATHERER is None or gatherer != GATHERER:
            return False
    elif label == "LSD_REGISTER_RETURN":
        owner = base._reg(frame, "x19")
        gatherer_raw = base._read(process, owner + 0x8, 8)
        gatherer = (int.from_bytes(gatherer_raw, "little")
                    if len(gatherer_raw) == 8 else None)
        if GATHERER is None or gatherer != GATHERER:
            return False
    elif thread != FIRST_THREAD:
        return False

    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    payload = {"label": label, "time": time.time(), "count": count,
               "thread_pointer": thread, "x0": x0}
    if label == "LSD_FIRST_IO_BLOCK":
        payload["reason_flags"] = flags
    elif label == "LSD_SYSTEM_SCAN_CALL":
        payload["gatherer"] = GATHERER
    elif label == "LSD_REGISTER_ENTRY":
        item = base._reg(frame, "x2")
        payload["item"] = item
        ACTIVE[thread] = (item, payload["time"])
    elif label in ("LSD_REGISTER_RETURN", "LSD_REGISTER_NULL"):
        active = ACTIVE.pop(thread, None)
        if active:
            payload["item"] = active[0]
            payload["elapsed"] = payload["time"] - active[1]
    payload["active_count"] = len(ACTIVE)
    # Each job has only two stops. Log every pair so the first missing
    # return and its item remain identifiable if progress ceases.
    print("CCS_LS_SEED_PROGRESS_JSON " + json.dumps(payload, sort_keys=True),
          flush=True)
    return False


def install(debugger, slide):
    global FIRST_THREAD, GATHERER
    FIRST_THREAD = None
    GATHERER = None
    ACTIVE.clear()
    COUNTS.clear()
    base.SLIDE = int(slide)
    base.LABELS.clear()
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    for static, label, expected in SITES:
        address = static + base.SLIDE
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % address,
                                  result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed %s: %s" % (label, result.GetError()))
        breakpoint = target.GetBreakpointAtIndex(before)
        base.LABELS[breakpoint.GetID()] = label, expected
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F ccs_ls_seed_progress_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

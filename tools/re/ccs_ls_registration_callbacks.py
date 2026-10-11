"""Read-only correlation of system-scan registration jobs and returns."""

import json
import time

import lldb

import ccs_ls_seeding_correlation_callbacks as base


SITES = (
    (0x186EC1E84, "LSD_FIRST_IO_BLOCK", "lsd"),
    (0x186E75F0C, "LSD_SYSTEM_SCAN_CALL", "lsd"),
    (0x186F81000, "LSD_SYSTEM_SCAN_BLOCK", "lsd"),
    (0x186F81090, "LSD_SCAN_DRIVER_CALL", "lsd"),
    (0x186F81094, "LSD_SCAN_DRIVER_RETURN", "lsd"),
    (0x186F812DC, "LSD_REGISTER_BLOCK_ENTRY", "lsd"),
    (0x186F80DA0, "LSD_REGISTER_METHOD_ENTRY", "lsd"),
    (0x186E750AC, "LSD_REGISTER_HELPER_ENTRY", "lsd"),
    (0x186E751A4, "LSD_REGISTER_SYNCWRITE_CALL", "lsd"),
    (0x186E751A8, "LSD_REGISTER_SYNCWRITE_RETURN", "lsd"),
    (0x186F80DEC, "LSD_REGISTER_METHOD_RETURN", "lsd"),
    (0x186F80E10, "LSD_REGISTER_METHOD_NULL", "lsd"),
    (0x186E75F10, "LSD_SYSTEM_SCAN_RETURN", "lsd"),
)


FIRST_THREAD = None
COUNTS = {}
ACTIVE = {}


def on_site(frame, bp_loc, context):
    global FIRST_THREAD
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
    elif label in ("LSD_SYSTEM_SCAN_CALL", "LSD_SYSTEM_SCAN_BLOCK",
                   "LSD_SCAN_DRIVER_CALL",
                   "LSD_SCAN_DRIVER_RETURN", "LSD_SYSTEM_SCAN_RETURN"):
        if thread != FIRST_THREAD:
            return False
    elif label != "LSD_REGISTER_BLOCK_ENTRY" and thread not in ACTIVE:
        return False

    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    payload = {"label": label, "time": time.time(), "count": count,
               "thread_pointer": thread, "original_thread": thread == FIRST_THREAD,
               "x0": x0, "x1": base._reg(frame, "x1"),
               "x2": base._reg(frame, "x2"),
               "x3": base._reg(frame, "x3")}
    if label == "LSD_FIRST_IO_BLOCK":
        payload["reason_flags"] = flags
    elif label == "LSD_SYSTEM_SCAN_BLOCK":
        payload["block_capture"] = base._read(process, x0 + 0x20,
                                               0x10).hex()
    elif label == "LSD_REGISTER_BLOCK_ENTRY":
        raw = base._read(process, x0 + 0x20, 0x18)
        if len(raw) == 0x18:
            rebuild_context = int.from_bytes(raw[:8], "little")
            item = int.from_bytes(raw[0x10:0x18], "little")
            payload["rebuild_context"] = rebuild_context
            payload["item"] = item
            payload["item_head"] = base._read(process, item, 0x40).hex()
            ACTIVE[thread] = item
    elif thread in ACTIVE:
        payload["item"] = ACTIVE[thread]
    if label in ("LSD_REGISTER_METHOD_RETURN", "LSD_REGISTER_METHOD_NULL"):
        ACTIVE.pop(thread, None)
    payload["active_threads"] = len(ACTIVE)
    if count <= 20 or count % 100 == 0 or label in (
            "LSD_FIRST_IO_BLOCK", "LSD_SCAN_DRIVER_RETURN",
            "LSD_SYSTEM_SCAN_RETURN"):
        print("CCS_LS_REGISTRATION_JSON " + json.dumps(payload, sort_keys=True),
              flush=True)
    return False


def install(debugger, slide):
    global FIRST_THREAD
    FIRST_THREAD = None
    COUNTS.clear()
    ACTIVE.clear()
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
            "breakpoint command add -F ccs_ls_registration_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

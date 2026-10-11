"""Read-only bracket of the first LaunchServices system-content scan."""

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
    (0x186F811A0, "LSD_SCAN_ITEM_BLOCK", "lsd"),
    (0x186F811F0, "LSD_SCAN_ITEM_INIT_CALL", "lsd"),
    (0x186F811F4, "LSD_SCAN_ITEM_INIT_RETURN", "lsd"),
    (0x186F81204, "LSD_SCAN_ITEM_PREPARE_CALL", "lsd"),
    (0x186F81208, "LSD_SCAN_ITEM_PREPARE_RETURN", "lsd"),
    (0x186F81288, "LSD_SCAN_ITEM_WALK_CALL", "lsd"),
    (0x186F8128C, "LSD_SCAN_ITEM_WALK_RETURN", "lsd"),
    (0x186F812DC, "LSD_REGISTER_ITEM_BLOCK", "lsd"),
    (0x186F81114, "LSD_SCAN_SYNCWRITE_CALL", "lsd"),
    (0x186E75F10, "LSD_SYSTEM_SCAN_RETURN", "lsd"),
)


FIRST_THREAD = None
COUNTS = {}


def on_site(frame, bp_loc, context):
    global FIRST_THREAD
    breakpoint = bp_loc.GetBreakpoint()
    label, expected = base.LABELS[breakpoint.GetID()]
    process = frame.GetThread().GetProcess()
    if base._progname(process) != expected:
        return False
    thread = base._reg(frame, "tpidr_el1")
    if label == "LSD_FIRST_IO_BLOCK":
        x0 = base._reg(frame, "x0")
        raw = base._read(process, x0 + 0x28, 2)
        flags = int.from_bytes(raw, "little") if len(raw) == 2 else None
        if FIRST_THREAD is None and flags is not None and flags & 0x8:
            FIRST_THREAD = thread
        if thread != FIRST_THREAD:
            return False
    elif FIRST_THREAD is None:
        return False
    elif thread != FIRST_THREAD and label not in (
            "LSD_SYSTEM_SCAN_BLOCK", "LSD_SCAN_ITEM_BLOCK",
            "LSD_REGISTER_ITEM_BLOCK"):
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count > 3 and count % 100:
        return False
    payload = {"label": label, "time": time.time(), "count": count,
               "thread_pointer": thread,
               "original_thread": thread == FIRST_THREAD,
               "x0": base._reg(frame, "x0"),
               "x1": base._reg(frame, "x1"),
               "x2": base._reg(frame, "x2")}
    if label == "LSD_FIRST_IO_BLOCK":
        payload["reason_flags"] = flags
    print("CCS_LS_SYSTEM_SCAN_JSON " + json.dumps(payload, sort_keys=True),
          flush=True)
    return False


def install(debugger, slide):
    global FIRST_THREAD
    FIRST_THREAD = None
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
            "breakpoint command add -F ccs_ls_system_scan_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

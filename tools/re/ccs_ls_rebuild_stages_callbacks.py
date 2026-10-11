"""Read-only checkpoints within the first LS database-missing rebuild."""

import json
import time

import lldb

import ccs_ls_seeding_correlation_callbacks as base


SITES = (
    (0x186EC1E84, "LSD_FIRST_IO_BLOCK", "lsd"),
    (0x186E75A54, "LSD_PERFORM_REBUILD_CALL", "lsd"),
    (0x186F8A144, "LSD_PERFORM_REBUILD_ENTRY", "lsd"),
    (0x186E75AB8, "LSD_REBUILD_OUTER_BLOCK", "lsd"),
    (0x186E75B38, "LSD_REBUILD_CONTEXT_CALL", "lsd"),
    (0x186E75B3C, "LSD_REBUILD_CONTEXT_RETURN", "lsd"),
    (0x186E75B54, "LSD_REBUILD_INNER_BLOCK", "lsd"),
    (0x186E75F0C, "LSD_SYSTEM_SCAN_CALL", "lsd"),
    (0x186E75F10, "LSD_SYSTEM_SCAN_RETURN", "lsd"),
    (0x186E75FB0, "LSD_SYSTEM_SAVE_CALL", "lsd"),
    (0x186E75FB4, "LSD_SYSTEM_SAVE_RETURN", "lsd"),
    (0x186E76008, "LSD_CRYPTEX_SCAN_CALL", "lsd"),
    (0x186E7600C, "LSD_CRYPTEX_SCAN_RETURN", "lsd"),
    (0x186E76100, "LSD_CONTAINER_SCAN_CALL", "lsd"),
    (0x186E76104, "LSD_CONTAINER_SCAN_RETURN", "lsd"),
    (0x186E76108, "LSD_FINISH_CALL", "lsd"),
)


FIRST_THREAD = None


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
    elif thread != FIRST_THREAD:
        return False
    payload = {"label": label, "time": time.time(),
               "thread_pointer": thread,
               "x0": base._reg(frame, "x0"),
               "x1": base._reg(frame, "x1")}
    if label == "LSD_FIRST_IO_BLOCK":
        payload["reason_flags"] = flags
    print("CCS_LS_REBUILD_STAGES_JSON " + json.dumps(payload, sort_keys=True),
          flush=True)
    return False


def install(debugger, slide):
    global FIRST_THREAD
    FIRST_THREAD = None
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
            "breakpoint command add -F ccs_ls_rebuild_stages_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

"""Focused SpringBoard/lsd request correlation and LS seeding-worker trace."""

import ccs_ls_seeding_correlation_callbacks as base
import lldb


SITES = (
    (0x186EF62B8, "SB_BLOCKING_SEND", "SpringBoard"),
    (0x186EF62BC, "SB_BLOCKING_RETURN", "SpringBoard"),
    (0x186F145C4, "LSD_HANDLER_ENTRY", "lsd"),
    (0x186F1461C, "LSD_MANUAL_RESULT", "lsd"),
    (0x186F14630, "LSD_SEED_WAIT_CALL", "lsd"),
    (0x186F14634, "LSD_SEED_WAIT_RETURN", "lsd"),
    (0x186E090C0, "LSD_WAIT_ENTRY", "lsd"),
    (0x186E09110, "LSD_WAIT_EXIT", "lsd"),
    (0x186F146AC, "LSD_SYNC_READ_CALL", "lsd"),
    (0x186F146B0, "LSD_SYNC_READ_RETURN", "lsd"),
    (0x186EB7FF8, "LSD_SEED_MARK", "lsd"),
    (0x186EB800C, "LSD_SEED_COMPLETE", "lsd"),
    (0x186EC1A04, "LSD_BUILDER_ENTRY", "lsd"),
    (0x186EC1DF8, "LSD_BUILDER_WORKER_ENTRY", "lsd"),
    (0x186EC1E68, "LSD_BUILDER_SYNC_CALL", "lsd"),
    (0x186EC1E6C, "LSD_BUILDER_SYNC_RETURN", "lsd"),
)


def on_site(frame, bp_loc, context):
    return base.on_site(frame, bp_loc, context)


def install(debugger, slide):
    base.SLIDE = int(slide)
    base.LABELS.clear()
    base.COUNTS.clear()
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
            "breakpoint command add -F ccs_ls_seed_worker_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

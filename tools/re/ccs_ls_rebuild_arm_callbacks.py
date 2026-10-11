"""Read-only probe of the first LS seed's rebuild arm and enumerate return."""

import json
import time

import lldb

import ccs_ls_seeding_correlation_callbacks as base


SITES = (
    (0x186EC1DF8, "LSD_BUILDER_WORKER_ENTRY", "lsd"),
    (0x186EC1E84, "LSD_IO_BLOCK_ENTRY", "lsd"),
    (0x186E75940, "LSD_ENUM_ENTRY", "lsd"),
    (0x186E75A54, "LSD_ENUM_PERFORM_REBUILD_CALL", "lsd"),
    (0x186E75A58, "LSD_ENUM_PERFORM_REBUILD_RETURN", "lsd"),
    (0x186E75A98, "LSD_ENUM_RETURN", "lsd"),
    (0x186EC1FF0, "LSD_IO_BLOCK_SYNCWRITE_CALL", "lsd"),
)


def _reason(flags):
    if flags is None:
        return None
    for bit, label in ((5, "creation-from-store-failed"),
                       (4, "corrupt"), (3, "database-missing"),
                       (2, "recovery-file"), (7, "allocation-failed"),
                       (8, "store-creation-failed"),
                       (6, "creation-from-fresh-failed"),
                       (0, "created-fresh")):
        if flags & (1 << bit):
            return label
    return "unexpected"


def on_site(frame, bp_loc, context):
    breakpoint = bp_loc.GetBreakpoint()
    label, expected = base.LABELS[breakpoint.GetID()]
    process = frame.GetThread().GetProcess()
    if base._progname(process) != expected:
        return False
    payload = {"label": label, "time": time.time(),
               "thread_pointer": base._reg(frame, "tpidr_el1"),
               "x0": base._reg(frame, "x0"),
               "x1": base._reg(frame, "x1")}
    if label == "LSD_IO_BLOCK_ENTRY":
        raw = base._read(process, payload["x0"] + 0x28, 2)
        flags = int.from_bytes(raw, "little") if len(raw) == 2 else None
        payload["reason_flags"] = flags
        payload["reason"] = _reason(flags)
    print("CCS_LS_REBUILD_ARM_JSON " + json.dumps(payload, sort_keys=True),
          flush=True)
    return False


def install(debugger, slide):
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
            "breakpoint command add -F ccs_ls_rebuild_arm_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

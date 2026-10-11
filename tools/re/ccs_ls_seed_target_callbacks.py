"""Read-only LS seed worker target and XPC peer-PID correlation probe."""

import json
import time

import lldb

import ccs_ls_seeding_correlation_callbacks as base


CURRENT_PID = 0x26FD74008
SITES = (
    (0x186EF62B8, "SB_BLOCKING_SEND", "SpringBoard"),
    (0x186EF62BC, "SB_BLOCKING_RETURN", "SpringBoard"),
    (0x186F145C4, "LSD_HANDLER_ENTRY", "lsd"),
    (0x186F14630, "LSD_SEED_WAIT_CALL", "lsd"),
    (0x186F14634, "LSD_SEED_WAIT_RETURN", "lsd"),
    (0x186F146AC, "LSD_SYNC_READ_CALL", "lsd"),
    (0x186EB7FF8, "LSD_SEED_MARK", "lsd"),
    (0x186EB800C, "LSD_SEED_COMPLETE", "lsd"),
    (0x186EC1DF8, "LSD_BUILDER_WORKER_ENTRY", "lsd"),
    (0x186EC1E68, "LSD_BUILDER_SYNC_CALL", "lsd"),
    (0x186EC1E6C, "LSD_BUILDER_SYNC_RETURN", "lsd"),
    (0x186EC1E84, "LSD_IO_BLOCK_ENTRY", "lsd"),
    (0x186EC1FF0, "LSD_IO_BLOCK_SYNCWRITE_CALL", "lsd"),
    (0x186EC1FF4, "LSD_IO_BLOCK_SYNCWRITE_RETURN", "lsd"),
)


def _u32(process, address):
    raw = base._read(process, address, 4)
    return int.from_bytes(raw, "little") if len(raw) == 4 else None


def _u64(process, address):
    raw = base._read(process, address, 8)
    return int.from_bytes(raw, "little") if len(raw) == 8 else None


def on_site(frame, bp_loc, context):
    breakpoint = bp_loc.GetBreakpoint()
    label, expected = base.LABELS[breakpoint.GetID()]
    process = frame.GetThread().GetProcess()
    if base._progname(process) != expected:
        return False
    if label in ("SB_BLOCKING_SEND", "LSD_HANDLER_ENTRY"):
        payload = {"label": label, "time": time.time(),
                   "thread_pointer": base._reg(frame, "tpidr_el1")}
        if label == "SB_BLOCKING_SEND":
            payload["cached_pid"] = _u32(process, base.SLIDE + CURRENT_PID)
        else:
            receiver = base._reg(frame, "x0")
            nsxpc = _u64(process, receiver + 8)
            state = _u32(process, nsxpc + 0x24) if nsxpc else None
            xpc = _u64(process, nsxpc + 8) if nsxpc else None
            payload.update(receiver=receiver, nsxpc=nsxpc, state=state,
                           xpc=xpc,
                           peer_pid=(_u32(process, xpc + 0x7c)
                                     if xpc and state is not None and
                                     not state & (1 << 6) else None))
        print("CCS_LS_TARGET_JSON " + json.dumps(payload, sort_keys=True),
              flush=True)
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
            "breakpoint command add -F ccs_ls_seed_target_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

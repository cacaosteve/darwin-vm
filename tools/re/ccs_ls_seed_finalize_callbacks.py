"""Read-only LS seed-finalization and SpringBoard shell unwind checkpoints."""

import json
import time

import lldb

import ccs_ls_seeding_correlation_callbacks as base


SITES = (
    (0x186EC1E84, "LSD_FIRST_IO_BLOCK", "lsd"),
    (0x186EC1FF0, "LSD_IO_BLOCK_SYNCWRITE_CALL", "lsd"),
    (0x186E5FB6C, "LSD_SYNCWRITE_GETDB_CALL", "lsd"),
    (0x186E5FB70, "LSD_SYNCWRITE_GETDB_RETURN", "lsd"),
    (0x186E5FBC0, "LSD_SYNCWRITE_BLOCK_CALL", "lsd"),
    (0x186E5FBC4, "LSD_SYNCWRITE_BLOCK_RETURN", "lsd"),
    (0x186EC1FF4, "LSD_IO_BLOCK_SYNCWRITE_RETURN", "lsd"),
    (0x186EC1E6C, "LSD_BUILDER_SYNC_RETURN", "lsd"),
    (0x186EB7FF8, "LSD_SEED_MARK", "lsd"),
    (0x186EB800C, "LSD_SEED_COMPLETE_ENTRY", "lsd"),
    (0x186EB801C, "LSD_SEED_COMPLETE_AFTER_DEC", "lsd"),
    (0x186F14634, "LSD_SEED_WAIT_RETURN", "lsd"),
    (0x186EF62B8, "SB_BLOCKING_SEND", "SpringBoard"),
    (0x186EF62BC, "SB_BLOCKING_RETURN", "SpringBoard"),
    (0x2244CF0BC, "SB_BEFORE_CCS_START", "SpringBoard"),
    (0x259097F8C, "CCS_START_RETURN", "SpringBoard"),
    (0x2244CF0C0, "SB_AFTER_CCS_START", "SpringBoard"),
    (0x1C4A3BFCC, "SHELL_AFTER_SERVICES_BLOCK", "SpringBoard"),
    (0x1C4A3C32C, "SHELL_PRE_RETURN", "SpringBoard"),
    (0x224458BF4, "SB_SYSTEM_APP_AFTER_SHELL", "SpringBoard"),
)


SEED_THREAD = None
COUNTS = {}


def on_site(frame, bp_loc, context):
    global SEED_THREAD
    breakpoint = bp_loc.GetBreakpoint()
    label, expected = base.LABELS[breakpoint.GetID()]
    process = frame.GetThread().GetProcess()
    if base._progname(process) != expected:
        return False
    thread = base._reg(frame, "tpidr_el1")
    if label == "LSD_FIRST_IO_BLOCK":
        x0 = base._reg(frame, "x0")
        flags = base._read(process, x0 + 0x28, 2)
        reason_flags = (int.from_bytes(flags, "little")
                        if len(flags) == 2 else None)
        if SEED_THREAD is None and reason_flags is not None and reason_flags & 0x8:
            SEED_THREAD = thread
        if thread != SEED_THREAD:
            return False
    elif label.startswith("LSD_SYNCWRITE_") or label.startswith("LSD_IO_BLOCK_") or label == "LSD_BUILDER_SYNC_RETURN":
        if thread != SEED_THREAD:
            return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count > 12 and count % 100:
        return False
    raw = base._read(process, base.SLIDE + base.SEEDING_COUNT, 4)
    payload = {"label": label, "count": count, "time": time.time(),
               "thread_pointer": thread, "lr": base._reg(frame, "lr"),
               "x0": base._reg(frame, "x0"),
               "w0": base._reg(frame, "w0"),
               "seeding_count": (int.from_bytes(raw, "little")
                                 if len(raw) == 4 else None)}
    if label in ("SB_BEFORE_CCS_START", "CCS_START_RETURN", "SB_AFTER_CCS_START",
                 "SHELL_AFTER_SERVICES_BLOCK"):
        lldb_thread = frame.GetThread()
        payload["backtrace"] = [lldb_thread.GetFrameAtIndex(i).GetPC()
                                for i in range(min(lldb_thread.GetNumFrames(), 8))]
    if label == "LSD_FIRST_IO_BLOCK":
        payload["reason_flags"] = reason_flags
    print("CCS_LS_SEED_FINALIZE_JSON " + json.dumps(payload, sort_keys=True),
          flush=True)
    return False


def install(debugger, slide):
    global SEED_THREAD
    SEED_THREAD = None
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
            "breakpoint command add -F ccs_ls_seed_finalize_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

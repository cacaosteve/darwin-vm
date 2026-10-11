"""Read-only lsd handler trace for the blocking LS server-store request."""

import json
import time

import lldb


PROGNAME_PTR = 0x1E6EF1590
SLIDE = 0
LABELS = {}
COUNTS = {}
SITES = (
    (0x186EF62B8, "SB_BLOCKING_REQUEST_SEND", "SpringBoard"),
    (0x186EF62BC, "SB_BLOCKING_REQUEST_RETURN", "SpringBoard"),
    (0x186F145C4, "LSD_BLOCKING_HANDLER_ENTRY", "lsd"),
    (0x186F1461C, "LSD_MANUAL_REBUILD_RESULT", "lsd"),
    (0x186F146AC, "LSD_SYNC_READ_CALL", "lsd"),
    (0x186F146B0, "LSD_SYNC_READ_RETURN", "lsd"),
    (0x186DF0C74, "LSD_SYNC_READ_ENTRY", "lsd"),
    (0x186DF1E04, "LSD_GET_SERVER_STORE_ENTRY", "lsd"),
    (0x186DF1E70, "LSD_MAY_MAP_RESULT", "lsd"),
    (0x186DF1E7C, "LSD_COPY_LOCAL_DB_CALL", "lsd"),
    (0x186DF1E80, "LSD_COPY_LOCAL_DB_RETURN", "lsd"),
    (0x186DF1E9C, "LSD_IS_SEEDED_RESULT", "lsd"),
    (0x186DF1F38, "LSD_RECOVERY_REBUILD_ENQUEUE", "lsd"),
    (0x186DF1F3C, "LSD_RECOVERY_REBUILD_ENQUEUED", "lsd"),
    (0x186EEEA18, "LSD_RECOVERY_REBUILD_BLOCK", "lsd"),
    (0x186DF1F8C, "LSD_SEEDED_COMPLETION", "lsd"),
    (0x186DF2020, "LSD_ERROR_COMPLETION", "lsd"),
)


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else 0


def _read(process, address, size):
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return data if error.Success() else b""


def _u64(process, address):
    return int.from_bytes(_read(process, address, 8), "little")


def _progname(process):
    pointer = _u64(process, SLIDE + PROGNAME_PTR)
    string = _u64(process, pointer) if pointer else 0
    raw = _read(process, string, 96) if string else b""
    return raw.split(b"\0", 1)[0].decode("ascii", "replace") if raw else ""


def on_site(frame, bp_loc, _dict):
    breakpoint = bp_loc.GetBreakpoint()
    label, expected = LABELS[breakpoint.GetID()]
    process = frame.GetThread().GetProcess()
    if _progname(process) != expected:
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count <= 8:
        thread = frame.GetThread()
        payload = {
            "label": label,
            "count": count,
            "time": time.time(),
            "thread_pointer": _reg(frame, "tpidr_el1"),
            "pc": _reg(frame, "pc"),
            "x0": _reg(frame, "x0"),
            "x1": _reg(frame, "x1"),
            "x2": _reg(frame, "x2"),
            "x21": _reg(frame, "x21"),
            "x22": _reg(frame, "x22"),
            "x25": _reg(frame, "x25"),
            "backtrace": [thread.GetFrameAtIndex(i).GetPC()
                          for i in range(min(thread.GetNumFrames(), 10))],
        }
        print("CCS_LS_SERVER_HANDLER_JSON " + json.dumps(payload, sort_keys=True),
              flush=True)
    if count >= 8:
        breakpoint.SetEnabled(False)
    return False


def install(debugger, slide):
    global SLIDE
    SLIDE = int(slide)
    LABELS.clear()
    COUNTS.clear()
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    for static, label, expected in SITES:
        address = static + SLIDE
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % address,
                                  result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed %s: %s" % (label, result.GetError()))
        breakpoint = target.GetBreakpointAtIndex(before)
        LABELS[breakpoint.GetID()] = label, expected
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F ccs_ls_server_handler_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

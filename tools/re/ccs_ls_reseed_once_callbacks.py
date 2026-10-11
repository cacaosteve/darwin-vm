"""Read-only trace of the LS server-store reseeding dispatch_once path."""

import json
import time

import lldb


PROGNAME_PTR = 0x1E6EF1590
SLIDE = 0
LABELS = {}
COUNTS = {}
SITES = (
    (0x186DC8C9C, "SESSION_TRYLOCK_RESULT"),
    (0x186DC8CD0, "SESSION_WAITLOCK_BEFORE"),
    (0x186DC8E54, "SERVER_STORE_RETURN"),
    (0x186DF25E8, "ONCE_TOKEN_TEST"),
    (0x186DF29B0, "ONCE_COLD_CALL"),
    (0x186F88828, "ONCE_COLD_ENTRY"),
    (0x186F88844, "DISPATCH_ONCE_CALL"),
    (0x186F88848, "DISPATCH_ONCE_RETURN"),
    (0x186DF25F0, "AFTER_ONCE"),
    (0x186DF2604, "POST_MAIN_THREAD_TEST"),
    (0x186DF2680, "RESEED_BLOCKING_MARKER"),
    (0x186DF2688, "BLOCKING_SELECTOR_CALL"),
    (0x186DF268C, "BLOCKING_SELECTOR_RETURN"),
    (0x186EF62A8, "BLOCKING_HELPER_ENTRY"),
    (0x186EF62B8, "BLOCKING_HELPER_SEND"),
    (0x186EF62BC, "BLOCKING_HELPER_RETURN"),
    (0x186DF2694, "POST_INVALIDATE"),
    (0x186DF28F4, "RETRY_SLEEP_CALL"),
    (0x186DF2908, "RETRY_COUNT_TEST"),
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
    process = frame.GetThread().GetProcess()
    if _progname(process) != "SpringBoard":
        return False
    breakpoint = bp_loc.GetBreakpoint()
    label = LABELS[breakpoint.GetID()]
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count <= 4:
        thread = frame.GetThread()
        x1 = _reg(frame, "x1")
        payload = {
            "label": label,
            "count": count,
            "time": time.time(),
            "pc": _reg(frame, "pc"),
            "thread_pointer": _reg(frame, "tpidr_el1"),
            "x0": _reg(frame, "x0"),
            "x1": x1,
            "x8": _reg(frame, "x8"),
            "x9": _reg(frame, "x9"),
            "x23": _reg(frame, "x23"),
            "x24": _reg(frame, "x24"),
            "once_token": _u64(process, SLIDE + 0x1E40E40C0),
            "block": _read(process, x1, 0x30).hex()
                     if label == "DISPATCH_ONCE_CALL" else "",
            "backtrace": [thread.GetFrameAtIndex(i).GetPC()
                          for i in range(min(thread.GetNumFrames(), 10))],
        }
        print("CCS_LS_RESEED_ONCE_JSON " + json.dumps(payload, sort_keys=True),
              flush=True)
    if count >= 4:
        breakpoint.SetEnabled(False)
    return False


def install(debugger, slide):
    global SLIDE
    SLIDE = int(slide)
    LABELS.clear()
    COUNTS.clear()
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    for static, label in SITES:
        address = static + SLIDE
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % address,
                                  result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed %s: %s" % (label, result.GetError()))
        breakpoint = target.GetBreakpointAtIndex(before)
        LABELS[breakpoint.GetID()] = label
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F ccs_ls_reseed_once_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

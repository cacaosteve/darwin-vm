"""Read-only bracket of SpringBoard's LaunchServices server-store fetch."""

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
    (0x186DC8E50, "SERVER_STORE_CALL"),
    (0x186DC8E54, "SERVER_STORE_RETURN"),
    (0x186DF21C8, "STORE_ENTRY"),
    (0x186DF244C, "XPC_CONNECTION_CALL"),
    (0x186DF2450, "XPC_CONNECTION_RETURN"),
    (0x186DF24A8, "PROXY_CALL"),
    (0x186DF24AC, "PROXY_RETURN"),
    (0x186DF24C0, "REMOTE_CALL"),
    (0x186DF24C4, "REMOTE_RETURN"),
    (0x186DF24D4, "SEEDING_REPLY_TEST"),
    (0x186DF2514, "SEEDING_RETRY_LOG"),
    (0x186DF2554, "BLOCKING_FETCH_INTERFACE_CALL"),
    (0x186DF2558, "BLOCKING_FETCH_INTERFACE_RETURN"),
    (0x186DF25E8, "ONCE_TOKEN_TEST"),
    (0x186DF261C, "REPLY_BRANCH"),
    (0x186DF2680, "RESEED_BLOCKING_MARKER"),
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


def _progname(process):
    pointer = int.from_bytes(_read(process, SLIDE + PROGNAME_PTR, 8), "little")
    string = int.from_bytes(_read(process, pointer, 8), "little") if pointer else 0
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
    if count <= 6:
        thread = frame.GetThread()
        payload = {
            "label": label,
            "count": count,
            "time": time.time(),
            "pc": _reg(frame, "pc"),
            "lr": _reg(frame, "lr"),
            "thread_pointer": _reg(frame, "tpidr_el1"),
            "x0": _reg(frame, "x0"),
            "x8": _reg(frame, "x8"),
            "x9": _reg(frame, "x9"),
            "x19": _reg(frame, "x19"),
            "x23": _reg(frame, "x23"),
            "x24": _reg(frame, "x24"),
            "sp_c4": _read(process, _reg(frame, "sp") + 0xC4, 4).hex(),
            "backtrace": [thread.GetFrameAtIndex(i).GetPC()
                          for i in range(min(thread.GetNumFrames(), 10))],
        }
        print("CCS_LS_SERVER_STORE_JSON " + json.dumps(payload, sort_keys=True),
              flush=True)
    if count >= 6:
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
            "breakpoint command add -F ccs_ls_server_store_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

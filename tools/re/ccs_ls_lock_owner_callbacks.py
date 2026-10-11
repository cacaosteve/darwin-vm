"""Read-only bracket of the earlier SpringBoard LS session-lock holder."""

import json
import time

import lldb


PROGNAME_PTR = 0x1E6EF1590
SLIDE = 0
LABELS = {}
COUNTS = {}
SITES = (
    (0x186DC8C98, "SESSION_TRYLOCK_BEFORE"),
    (0x186DC8C9C, "SESSION_TRYLOCK_AFTER"),
    (0x186DC8CD0, "SESSION_WAITLOCK_BEFORE"),
    (0x186DC8CD4, "SESSION_WAITLOCK_AFTER"),
    (0x186DC8CF0, "SESSION_DB_BEFORE"),
    (0x186DC8CF4, "SESSION_DB_AFTER"),
    (0x186DC8DF4, "NOTIFICATION_CHECK_PREPARE"),
    (0x186DC8DF8, "NOTIFICATION_CHECK_BEFORE"),
    (0x186DC8DFC, "NOTIFICATION_CHECK_AFTER"),
    (0x186DCA708, "NOTIFICATION_CHECK_ENTRY"),
    (0x186DCA7D0, "NOTIFICATION_CHECK_BEFORE_UNLOCK"),
    (0x186DC8E24, "SERVER_STORE_DEFAULTS"),
    (0x186DC8E50, "SERVER_STORE_BEFORE"),
    (0x186DC8E54, "SERVER_STORE_AFTER"),
    (0x186DC8E6C, "DB_CREATE_BEFORE"),
    (0x186DC8E70, "DB_CREATE_AFTER"),
    (0x186DC8EB0, "SECOND_SESSION_LOCK_BEFORE"),
    (0x186DC8EB4, "SECOND_SESSION_LOCK_AFTER"),
    (0x186DC8F78, "SESSION_UNLOCK_AFTER_NOTIFY"),
    (0x186DC9060, "SESSION_UNLOCK_OTHER"),
    (0x186DC90BC, "SESSION_AFTER_LOCAL_DB"),
    (0x186DC9244, "SESSION_CONTEXT_EPILOGUE"),
    (0x186DF21C8, "SERVER_STORE_ENTRY"),
)


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else 0


def _read(process, address, size):
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return data if error.Success() else b""


def _u64(process, address):
    data = _read(process, address, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else 0


def _progname(process):
    pointer = _u64(process, SLIDE + PROGNAME_PTR)
    string = _u64(process, pointer) if pointer else 0
    raw = _read(process, string, 96) if string else b""
    return raw.split(b"\0", 1)[0].decode("ascii", "replace") if raw else ""


def _string_candidates(process, address):
    """Read NSString storage heuristically without sending guest messages."""
    if not address:
        return {"pointer": 0}
    raw = _read(process, address, 0x30)
    result = {"pointer": address, "object_hex": raw.hex()}
    if len(raw) >= 0x20:
        for offset in (0x10, 0x18, 0x20):
            pointer = int.from_bytes(raw[offset:offset + 8], "little")
            chars = _read(process, pointer, 160)
            if chars:
                prefix = chars.split(b"\0", 1)[0]
                if prefix and all(32 <= byte < 127 for byte in prefix):
                    result["candidate_%x" % offset] = prefix.decode("ascii", "replace")
    return result


def on_site(frame, bp_loc, _dict):
    process = frame.GetThread().GetProcess()
    if _progname(process) != "SpringBoard":
        return False
    breakpoint = bp_loc.GetBreakpoint()
    label = LABELS[breakpoint.GetID()]
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count <= 8:
        thread = frame.GetThread()
        payload = {
            "label": label,
            "count": count,
            "time": time.time(),
            "pc": _reg(frame, "pc"),
            "lr": _reg(frame, "lr"),
            "thread": frame.GetThread().GetThreadID(),
            "thread_pointer": _reg(frame, "tpidr_el1"),
            "x0": _reg(frame, "x0"),
            "x2": _reg(frame, "x2"),
            "x3": _reg(frame, "x3"),
            "x19": _reg(frame, "x19"),
            "x20": _reg(frame, "x20"),
            "x21": _reg(frame, "x21"),
            "backtrace": [thread.GetFrameAtIndex(i).GetPC()
                          for i in range(min(thread.GetNumFrames(), 12))],
        }
        print("CCS_LS_LOCK_OWNER_JSON " + json.dumps(payload, sort_keys=True),
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
            "breakpoint command add -F ccs_ls_lock_owner_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

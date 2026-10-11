"""Read-only bracket of LSApplicationRecord database context acquisition."""

import json
import time

import lldb


PROGNAME_PTR = 0x1E6EF1590
SLIDE = 0
LABELS = {}
COUNTS = {}
SITES = (
    (0x186DC8768, "LS_RECORD_INNER_ENTRY"),
    (0x186DC8840, "LS_RECORD_CONTEXT_CHOICE"),
    (0x186DC884C, "LS_RECORD_AFTER_CURRENT_CONTEXT"),
    (0x186DC8860, "LS_RECORD_BEFORE_SERVICE_DOMAIN"),
    (0x186DC8864, "LS_RECORD_AFTER_SERVICE_DOMAIN"),
    (0x186DC8878, "LS_RECORD_BEFORE_DB_CONTEXT_GET"),
    (0x186DC887C, "LS_RECORD_AFTER_DB_CONTEXT_GET"),
    (0x186DC8940, "LS_RECORD_BEFORE_IO_FOR_CALLER"),
    (0x186DC8944, "LS_RECORD_AFTER_IO_FOR_CALLER"),
    (0x186DC89A4, "LS_RECORD_BEFORE_BUNDLE_LOOKUP"),
    (0x186DC89A8, "LS_RECORD_AFTER_BUNDLE_LOOKUP"),
    (0x186DC96B0, "DB_CONTEXT_GET_ENTRY"),
    (0x186DC971C, "DB_CONTEXT_BEFORE_INIT_OPTIONS"),
    (0x186DC9720, "DB_CONTEXT_AFTER_INIT_OPTIONS"),
    (0x186DC8BD0, "LS_CONTEXT_INIT_COMMON_ENTRY"),
    (0x186DC8C54, "LS_CONTEXT_AFTER_DEFAULTS"),
    (0x186DC8C68, "LS_CONTEXT_BEFORE_COPY_LOCAL_DB"),
    (0x186DC8C6C, "LS_CONTEXT_AFTER_COPY_LOCAL_DB"),
    (0x186DC8C98, "LS_CONTEXT_BEFORE_SESSION_LOCK"),
    (0x186DC8C9C, "LS_CONTEXT_AFTER_SESSION_LOCK"),
    (0x186DC8CF0, "LS_CONTEXT_BEFORE_SESSION_DB"),
    (0x186DC8CF4, "LS_CONTEXT_AFTER_SESSION_DB"),
    (0x186DC8D40, "LS_CONTEXT_BEFORE_PERSISTENT_STORE"),
    (0x186DC8D44, "LS_CONTEXT_AFTER_PERSISTENT_STORE"),
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
        if label == "LS_RECORD_INNER_ENTRY":
            payload["bundle_identifier"] = _string_candidates(
                process, _reg(frame, "x3"))
        print("CCS_LS_CONTEXT_JSON " + json.dumps(payload, sort_keys=True),
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
            "breakpoint command add -F ccs_ls_context_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

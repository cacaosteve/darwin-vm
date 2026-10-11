"""Read-only bracket of CCS bundle filter and LSApplicationRecord calls."""

import json
import time

import lldb


PROGNAME_PTR = 0x1E6EF1590
SLIDE = 0
LABELS = {}
COUNTS = {}
SITES = (
    (0x259096C58, "BUNDLE_FILTER_ENTRY"),
    (0x259096C88, "BUNDLE_FILTER_BEFORE_COLLECTION"),
    (0x259096C8C, "BUNDLE_FILTER_AFTER_COLLECTION"),
    (0x259096CB8, "BUNDLE_PREDICATE_ENTRY"),
    (0x259096CE8, "PREDICATE_AFTER_BUNDLE_IDENTIFIER"),
    (0x259096CEC, "PREDICATE_IDENTIFIER_GATE"),
    (0x259096D0C, "LS_RECORD_BEFORE_INIT"),
    (0x259096D10, "LS_RECORD_AFTER_INIT"),
    (0x186DD29E4, "LS_RECORD_ALLOW_PLACEHOLDER_ENTRY"),
    (0x186DD1348, "LS_RECORD_FETCH_PLACEHOLDER_ENTRY"),
    (0x259096D20, "LS_RECORD_NONNULL_GATE"),
    (0x259096D2C, "LS_RECORD_AFTER_PROPERTY1"),
    (0x259096D38, "LS_RECORD_AFTER_PROPERTY2"),
    (0x259096D44, "LS_RECORD_AFTER_PROPERTY3"),
    (0x259096D50, "LS_RECORD_AFTER_PROPERTY4"),
    (0x259096D68, "PREDICATE_BEFORE_MIN_VERSION"),
    (0x259096D74, "PREDICATE_AFTER_MIN_VERSION"),
    (0x259096E2C, "PREDICATE_EPILOGUE"),
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
            "x19": _reg(frame, "x19"),
            "x20": _reg(frame, "x20"),
            "x21": _reg(frame, "x21"),
            "backtrace": [thread.GetFrameAtIndex(i).GetPC()
                          for i in range(min(thread.GetNumFrames(), 12))],
        }
        if label in ("PREDICATE_AFTER_BUNDLE_IDENTIFIER",
                     "PREDICATE_IDENTIFIER_GATE", "LS_RECORD_BEFORE_INIT",
                     "LS_RECORD_AFTER_INIT"):
            payload["bundle_identifier"] = _string_candidates(
                process, _reg(frame, "x20"))
        elif label in ("LS_RECORD_ALLOW_PLACEHOLDER_ENTRY",
                       "LS_RECORD_FETCH_PLACEHOLDER_ENTRY"):
            payload["bundle_identifier"] = _string_candidates(
                process, _reg(frame, "x2"))
        print("CCS_LS_RECORD_JSON " + json.dumps(payload, sort_keys=True),
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
            "breakpoint command add -F ccs_ls_record_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

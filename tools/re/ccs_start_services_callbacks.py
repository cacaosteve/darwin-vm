"""Read-only bracket inside CCSControlCenterServicesManager.startServices."""

import json
import time

import lldb


PROGNAME_PTR = 0x1E6EF1590
SLIDE = 0
LABELS = {}
COUNTS = {}
SITES = (
    (0x2244CF0BC, "SB_BEFORE_CCS_START"),
    (0x2244CF0C0, "SB_AFTER_CCS_START"),
    (0x259097D08, "CCS_SWIFT_START_THUNK"),
    (0x259097DE0, "CCS_SWIFT_START_BODY"),
    (0x259097E80, "CCS_AFTER_ONCE"),
    (0x259097F54, "CCS_BEFORE_PROVIDER_SHARED"),
    (0x259097F64, "CCS_AFTER_PROVIDER_SHARED"),
    (0x259097F70, "CCS_BEFORE_PROVIDER_RESUME"),
    (0x259097F74, "CCS_AFTER_PROVIDER_RESUME"),
    (0x259097F8C, "CCS_START_RETURN"),
    (0x25908A868, "PROVIDER_SHARED_ENTRY"),
    (0x25908A8AC, "PROVIDER_SHARED_BLOCK"),
    (0x25908A90C, "PROVIDER_INIT_ENTRY"),
    (0x25908AB00, "PROVIDER_RESUME_ENTRY"),
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


def on_site(frame, bp_loc, _dict):
    process = frame.GetThread().GetProcess()
    if _progname(process) != "SpringBoard":
        return False
    breakpoint = bp_loc.GetBreakpoint()
    label = LABELS[breakpoint.GetID()]
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count <= 16:
        payload = {
            "label": label,
            "count": count,
            "time": time.time(),
            "pc": _reg(frame, "pc"),
            "lr": _reg(frame, "lr"),
            "thread": frame.GetThread().GetThreadID(),
            "x0": _reg(frame, "x0"),
            "x1": _reg(frame, "x1"),
            "x2": _reg(frame, "x2"),
            "x3": _reg(frame, "x3"),
            "x8": _reg(frame, "x8"),
            "x9": _reg(frame, "x9"),
            "x21": _reg(frame, "x21"),
            "x26": _reg(frame, "x26"),
        }
        print("CCS_START_SERVICES_JSON " + json.dumps(payload,
                                                   sort_keys=True),
              flush=True)
    if count >= 16:
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
            "breakpoint command add -F ccs_start_services_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

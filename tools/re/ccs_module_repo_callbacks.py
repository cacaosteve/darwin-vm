"""Read-only bracket in CCSModuleRepository synchronous initialization."""

import json
import time

import lldb


PROGNAME_PTR = 0x1E6EF1590
SLIDE = 0
LABELS = {}
COUNTS = {}
SITES = (
    (0x259095544, "MODULE_REPOSITORY_INIT_ENTRY"),
    (0x259095598, "REPOSITORY_AFTER_SUPER"),
    (0x2590955C0, "REPOSITORY_AFTER_INPUT_COPIES"),
    (0x2590955E8, "REPOSITORY_AFTER_HASH_TABLE"),
    (0x259095618, "REPOSITORY_AFTER_MAIN_QUEUE"),
    (0x259095650, "REPOSITORY_AFTER_CALLOUT_QUEUE"),
    (0x2590956A8, "REPOSITORY_BEFORE_DISPATCH_SYNC"),
    (0x2590956AC, "REPOSITORY_AFTER_DISPATCH_SYNC"),
    (0x2590956BC, "REPOSITORY_INIT_EPILOGUE"),
    (0x2590956D4, "REPOSITORY_SYNC_BLOCK_ENTRY"),
    (0x2590956F4, "REPOSITORY_AFTER_METADATA_UPDATE"),
    (0x25909571C, "REPOSITORY_AFTER_DEFAULTS"),
    (0x25909572C, "REPOSITORY_AFTER_LS_WORKSPACE"),
    (0x25909573C, "REPOSITORY_AFTER_LS_OBSERVER"),
    (0x259095748, "REPOSITORY_AFTER_VISIBILITY_REGISTER"),
    (0x259095768, "REPOSITORY_BLOCK_RETURN"),
    (0x25909602C, "METADATA_UPDATE_ENTRY"),
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
        print("CCS_MODULE_REPO_JSON " + json.dumps(payload,
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
            "breakpoint command add -F ccs_module_repo_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

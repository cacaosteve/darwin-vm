"""Read-only bracket for the FBInterfaceOrientationService singleton.

The shell enters its sharedInstance send at 0x1c4a3bf9c but did not reach
0x1c4a3bfcc in the prior bounded run. Resolve the inner method boundary.
"""

import json
import time

import lldb


PROGNAME_PTR = 0x1E6EF1590
SLIDE = 0
LABELS = {}
COUNTS = {}
SITES = (
    (0x1C4A3BF94, "SHELL_BEFORE_ORIENTATION"),
    (0x1C4A3BFA0, "SHELL_AFTER_ORIENTATION"),
    (0x1C4A3BFCC, "SHELL_AFTER_ORIENTATION_INIT"),
    (0x1C4A2B9AC, "ORIENTATION_SHARED_ENTRY"),
    (0x1C4A2B9D4, "ORIENTATION_SHARED_COLD"),
    (0x1C4A47FD4, "ORIENTATION_DISPATCH_ONCE"),
    (0x1C4A2B9F0, "ORIENTATION_BLOCK_ENTRY"),
    (0x1C4A2BA08, "ORIENTATION_BLOCK_AFTER_ALLOC_INIT"),
    (0x1C4A2BA2C, "ORIENTATION_INIT_ENTRY"),
    (0x1C4A2BA70, "ORIENTATION_INIT_AFTER_SERVER_ALLOC_INIT"),
    (0x1C4A2BA7C, "ORIENTATION_INIT_EPILOGUE"),
    (0x1C4A13A60, "ORIENTATION_SERVER_INIT_ENTRY"),
    (0x1C4A13AD4, "ORIENTATION_SERVER_CONFIG_CHECK"),
    (0x1C4A13B10, "ORIENTATION_SERVER_MISSING_SERVICE"),
    (0x1C4A13B64, "ORIENTATION_SERVER_RELEASES"),
    (0x1C4A13B8C, "ORIENTATION_SERVER_RETURN"),
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
        }
        print("FB_ORIENTATION_BRACKET_JSON " + json.dumps(payload, sort_keys=True),
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
            "breakpoint command add -F fb_orientation_bracket_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

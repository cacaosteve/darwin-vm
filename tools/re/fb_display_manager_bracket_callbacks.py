"""Read-only bracket around FBDisplayManager construction in SpringBoard.

The first accepted synthetic mode is constructed while
_FBSystemShellInitialize calls +[FBDisplayManager sharedInstance].  These
stops distinguish a blocked singleton constructor from a later shell step.
"""

import json
import time

import lldb


PROGNAME_PTR = 0x1E6EF1590
SLIDE = 0
LABELS = {}
COUNTS = {}
SITES = (
    (0x1C4A3BF00, "SHELL_BEFORE_DISPLAY_MANAGER"),
    (0x1C4A3BF04, "SHELL_AFTER_DISPLAY_MANAGER"),
    (0x1C4A08FBC, "DISPLAY_MANAGER_BEFORE_MONITOR"),
    (0x1C4A08FC4, "DISPLAY_MANAGER_AFTER_MONITOR"),
    (0x1C4A08FDC, "DISPLAY_MANAGER_BLOCK_EPILOGUE"),
    (0x1A842B9F4, "FBS_DISPLAY_MONITOR_INIT"),
    (0x1A842C020, "FBS_DISPLAY_SOURCE_INIT"),
    (0x1A842C44C, "FBS_DISPLAY_SOURCE_UPDATE"),
    (0x1A840766C, "FBS_MODE_ENTRY"),
    (0x1C4A3C32C, "FB_SHELL_INIT_PRE_RETURN"),
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
        print("FB_DISPLAY_BRACKET_JSON " + json.dumps(payload,
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
            "breakpoint command add -F fb_display_manager_bracket_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

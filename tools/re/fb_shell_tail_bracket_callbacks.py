"""Read-only milestones through the tail of _FBSystemShellInitialize.

The FBDisplayManager singleton has returned by 0x1c4a3bf04.  These stops
bracket later call groups without changing the synthetic display state.
"""

import json
import time

import lldb


PROGNAME_PTR = 0x1E6EF1590
SLIDE = 0
LABELS = {}
COUNTS = {}
SITES = (
    (0x1C4A3BF04, "AFTER_DISPLAY_MANAGER"),
    (0x1C4A3BF30, "TAIL_BF30"),
    (0x1C4A3BF94, "TAIL_BF94"),
    (0x1C4A3BFCC, "TAIL_BFCC"),
    (0x1C4A3C054, "TAIL_C054"),
    (0x1C4A3C058, "SHELL_SERVICE_CALL"),
    (0x1C4A3C05C, "SHELL_SERVICE_RETURN"),
    (0x1C4A3C08C, "TAIL_C08C"),
    (0x1C4A3C094, "TAIL_C094"),
    (0x1C4A3C0B0, "TAIL_C0B0"),
    (0x1C4A3C140, "TAIL_C140"),
    (0x1C4A3C1B0, "TAIL_C1B0"),
    (0x1C4A3C240, "TAIL_C240"),
    (0x1C4A3C2D8, "TAIL_C2D8"),
    (0x1C4A3C32C, "SHELL_PRE_RETURN"),
    (0x224458BF4, "SB_SYSTEM_APP_AFTER_SHELL"),
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
            "thread_pointer": _reg(frame, "tpidr_el1"),
            "x0": _reg(frame, "x0"),
            "x1": _reg(frame, "x1"),
            "x2": _reg(frame, "x2"),
            "x3": _reg(frame, "x3"),
        }
        print("FB_SHELL_TAIL_JSON " + json.dumps(payload, sort_keys=True),
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
            "breakpoint command add -F fb_shell_tail_bracket_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

"""Read-only checkpoints inside CCSModuleRepository metadata application."""

import json
import time

import lldb


PROGNAME_PTR = 0x1E6EF1590
SLIDE = 0
LABELS = {}
COUNTS = {}
SITES = (
    (0x259096064, "UPDATE_BEFORE_APPLY_ALL"),
    (0x259096068, "UPDATE_AFTER_APPLY_ALL"),
    (0x259096294, "APPLY_ENTRY"),
    (0x2590962DC, "APPLY_AFTER_QUEUE_ASSERT"),
    (0x259096304, "APPLY_AFTER_DICT_ALLOC"),
    (0x259096328, "APPLY_AFTER_FIRST_ENUM"),
    (0x259096358, "APPLY_LOOP_ITEM"),
    (0x259096368, "APPLY_AFTER_ITEM_METADATA"),
    (0x25909637C, "APPLY_AFTER_ITEM_INSERT"),
    (0x2590963A8, "APPLY_AFTER_ENUM"),
    (0x2590963B8, "APPLY_AFTER_EQUAL_COMPARE"),
    (0x2590963D0, "APPLY_BEFORE_INTERESTING_BUNDLES"),
    (0x2590963DC, "APPLY_AFTER_INTERESTING_BUNDLES"),
    (0x2590963E8, "APPLY_AFTER_AVAILABLE_METADATA"),
    (0x259096408, "APPLY_EPILOGUE"),
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
    if count <= 8:
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
            "x19": _reg(frame, "x19"),
            "x20": _reg(frame, "x20"),
            "x21": _reg(frame, "x21"),
            "x23": _reg(frame, "x23"),
        }
        print("CCS_METADATA_APPLY_JSON " + json.dumps(payload, sort_keys=True),
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
            "breakpoint command add -F ccs_metadata_apply_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

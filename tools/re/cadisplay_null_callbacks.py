"""Narrow probe for a null +[CADisplay mainDisplay] return.

UI_FS_SKS5 returned x0=0 from mainDisplay for backboardd and SpringBoard,
then SpringBoard critically crashed.  UI_FS_SKS3 on the same disk lineage
returned a non-null display before A385.  This module only instruments the
discovery edges from docs/re/cadisplay-discovery-gates.md so the debugger
tax stays low enough to tell query failure from a later surface gate.
"""
import json
import os
import time

import lldb


SLIDE = [0]
EVENT_DIR = os.environ.get("DVM_PROBE_EVENT_DIR", "")
SUCCESS_LABELS = set(filter(None, os.environ.get(
    "DVM_PROBE_SUCCESS_LABELS", "").split(",")))
PROGNAME_PTR = 0x1e6ef1590
CONFIG = {}
HITS = {}


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else None


def _read(process, address, size):
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return data if error.Success() else b""


def _u64(process, address):
    data = _read(process, address, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else None


def _cstring(process, address, limit=96):
    if not address:
        return "<null>"
    data = _read(process, address, limit)
    return (data.split(b"\0", 1)[0].decode("ascii", "replace")
            if data else "<read-error>")


def _progname(process):
    pointer = _u64(process, SLIDE[0] + PROGNAME_PTR)
    string = _u64(process, pointer) if pointer else None
    return _cstring(process, string)


def _write_event(name, payload):
    if not EVENT_DIR:
        return
    os.makedirs(EVENT_DIR, exist_ok=True)
    path = os.path.join(EVENT_DIR, name)
    temporary = "%s.%d.tmp" % (path, os.getpid())
    with open(temporary, "w") as stream:
        json.dump(payload, stream, sort_keys=True)
    os.replace(temporary, path)


def on_break(frame, bp_loc, _dict):
    bp_id = bp_loc.GetBreakpoint().GetID()
    config = CONFIG[bp_id]
    process = frame.GetThread().GetProcess()
    hit = HITS.get(bp_id, 0) + 1
    HITS[bp_id] = hit
    regs = {name: _reg(frame, name) for name in config["regs"]}
    payload = {
        "label": config["label"],
        "hit": hit,
        "progname": _progname(process),
        "thread": frame.GetThread().GetIndexID(),
        "time": time.time(),
        "registers": regs,
    }
    print("=== %s hit=%d/%d t=%.3f ===" % (
        config["label"], hit, config["limit"], payload["time"]))
    print("progname=%s" % payload["progname"])
    print("registers " + " ".join(
        "%s=%s" % (n, ("0x%x" % regs[n]) if regs[n] is not None else "?")
        for n in config["regs"]))
    print("TRACE_JSON " + json.dumps(payload, sort_keys=True))
    _write_event("last-hit.json", payload)
    if config["label"] in SUCCESS_LABELS:
        _write_event("success.%s.json" % config["label"], payload)
    if hit >= config["limit"]:
        bp_loc.GetBreakpoint().SetEnabled(False)
        print("bounded-disabled breakpoint=%d after=%d" % (bp_id, hit))
    return False


def _install(target, interpreter, address, label, regs, limit):
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint set --address 0x%x" % address, result)
    print(result.GetOutput() or result.GetError())
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed breakpoint %s at 0x%x" % (label, address))
    breakpoint = target.GetBreakpointAtIndex(before)
    CONFIG[breakpoint.GetID()] = {
        "label": label,
        "regs": regs.split(),
        "limit": limit,
    }
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand(
        "breakpoint command add -F cadisplay_null_callbacks.on_break %d" %
        breakpoint.GetID(), result)
    if not result.Succeeded():
        raise RuntimeError("callback attach failed for %d: %s" %
                           (breakpoint.GetID(), result.GetError()))
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint command list %d" %
                              breakpoint.GetID(), result)
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x\n%s" %
          (breakpoint.GetID(), label, address,
           result.GetOutput() or result.GetError()))


# Static QuartzCore VAs from docs/re/cadisplay-discovery-gates.md and
# tools/re/display_query_callbacks.py.
ENTRIES = [
    (0x1843e2e58, "CADISPLAY_MAIN_ENTRY", "x0", 24),
    (0x1843e2e84, "CADISPLAY_MAIN_RETURN", "x0", 24),
    (0x1843e3318, "ENSURE_DISPLAYS_ENTRY", "x0 x1", 24),
    (0x1843e33b4, "ENSURE_QUERY_CALL", "x0 x1", 24),
    (0x1844e47c8, "QD_ENTRY", "x0", 40),
    (0x1844e484c, "QD_CASGETDISPLAYS_RET", "w0", 40),
    (0x1844e4e54, "QD_ERROR_EDGE", "w19", 16),
    (0x1847574ec, "CAWS_ENABLE_OOP_OBSERVATION", "x0", 8),
    (0x2244d0ba4, "SB_ADFL_ENTRY", "x0 x2", 8),
]


def install(debugger, slide):
    SLIDE[0] = slide
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    base = "pc lr sp tpidr_el1 "
    for static, label, regs, limit in ENTRIES:
        _install(target, interpreter, static + slide, label, base + regs, limit)
    _write_event("ready", {"time": time.time(), "slide": slide,
                           "breakpoints": len(CONFIG)})
    print("CADISPLAY_NULL_TRACE_READY slide=0x%x breakpoints=%d" %
          (slide, len(CONFIG)))

"""Surface/render edges only — safe to arm before OOP/display discovery.

LLDB on CADisplay/query_displays stalls discovery (0xfb294002) and can kill
SpringBoard.  After no-LLDB controls reach A385 with a live display path, the
remaining question is whether render_update/create_surface/A408 ever run.
Install these breakpoints at Early boot; do not break on mainDisplay or query.
"""
import json
import os
import time

import lldb


SLIDE = [0]
KERNEL_SLIDE = int(os.environ.get("DVM_KERNEL_SLIDE", "0x20000000"), 0)
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
    # Decisive FB_SWAP with null surface is noise during layer setup.
    if config.get("decisive") and config.get("surface_reg"):
        surface = regs.get(config["surface_reg"])
        if not surface:
            return False
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
    print("TRACE_JSON " + json.dumps(payload, sort_keys=True))
    _write_event("last-hit.json", payload)
    if config["label"] in SUCCESS_LABELS:
        _write_event("success.%s.json" % config["label"], payload)
    if hit >= config["limit"]:
        bp_loc.GetBreakpoint().SetEnabled(False)
    return False


def _install(target, interpreter, address, label, regs, limit,
             surface_reg=None, decisive=False):
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint set --address 0x%x" % address, result)
    print(result.GetOutput() or result.GetError())
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed breakpoint %s" % label)
    bp = target.GetBreakpointAtIndex(before)
    CONFIG[bp.GetID()] = {
        "label": label, "regs": regs.split(), "limit": limit,
        "surface_reg": surface_reg, "decisive": decisive,
    }
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand(
        "breakpoint command add -F surface_only_callbacks.on_break %d" %
        bp.GetID(), result)
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(), result)
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x\n%s" %
          (bp.GetID(), label, address, result.GetOutput() or result.GetError()))


USER = [
    (0x184776174, "SERVER_RENDER_UPDATE_ENTRY", "x0 x1 x2 x3", 32, None, False),
    (0x1846820e4, "DISPLAY_ALLOCATE_SURFACE_ENTRY", "x0 x1 x2 x3", 16, None, False),
    (0x18445b330, "IOMFB_UPDATE_SURFACE_ENTRY", "x0 x1 x2 x3", 16, None, False),
    (0x1847ae2cc, "CREATE_SURFACE_ENTRY", "x0 x1 x2 x3", 16, None, False),
    (0x18471bf38, "ALLOCATE_IOSURFACE_ENTRY", "x0 x1 x2 x3", 16, None, False),
    (0x1847ae314, "ALLOCATE_IOSURFACE_RETURN", "x0", 16, "x0", False),
    (0x18447238c, "FB_SWAP_SET_LAYER_ENTRY", "x0 x1 x2 x3", 32, "x3", True),
    (0x2244d0ba4, "SB_ADFL_ENTRY", "x0 x2", 8, None, False),
]
KERNEL = [
    (0xfffffff00918e3c4, "H17P_SWAP_SUBMIT", "x0 x1 x2 x3", 16),
    (0xfffffff00a0c4664, "IOMFB_A408_BUILD", "x0 x1 x2 x3", 16),
]


def install(debugger, slide):
    SLIDE[0] = slide
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    base = "pc lr sp tpidr_el1 "
    for static, label, regs, limit, sreg, dec in USER:
        _install(target, interpreter, static + slide, label, base + regs,
                 limit, sreg, dec)
    for static, label, regs, limit in KERNEL:
        _install(target, interpreter, static + KERNEL_SLIDE, label,
                 base + regs, limit)
    _write_event("ready", {"time": time.time(), "slide": slide,
                           "breakpoints": len(CONFIG)})
    print("SURFACE_ONLY_READY slide=0x%x breakpoints=%d" % (slide, len(CONFIG)))

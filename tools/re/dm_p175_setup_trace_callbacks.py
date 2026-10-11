"""Persist live SpringBoard setup-path hits for the P175 reattach.

Wraps sb_setup_path_callbacks' existing read-only breakpoints so return values,
registers, and selected object fields survive LLDB console truncation. The
setupApplication entry additionally gets a same-stack dynamic return probe.
"""

import json
import os
import time

import lldb
import sb_setup_path_callbacks as base

OUT = "/tmp/dvm/p175-setup-path.jsonl"
CONFIG = {}
PENDING = {}


def _reg(frame, name):
    v = frame.FindRegister(name)
    return v.GetValueAsUnsigned() if v.IsValid() else None


def _emit(row):
    row["time"] = time.time()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    print("P175_SETUP_TRACE " + json.dumps(row, sort_keys=True), flush=True)


def _on_dynamic_return(frame, bp_loc, _dict):
    bp = bp_loc.GetBreakpoint()
    bp_id = bp.GetID()
    expect = PENDING.get(bp_id)
    if not expect:
        return False
    sp = _reg(frame, "sp")
    tpidr = _reg(frame, "tpidr_el1")
    if sp != expect["sp"] or tpidr != expect["tpidr"]:
        return False
    thread = frame.GetThread()
    _emit({"label": "SB_SETUPAPP_DYNAMIC_RETURN", "bp_id": bp_id,
           "thread_id": thread.GetThreadID(), "pid": thread.GetProcess().GetProcessID(),
           "expected_sp": hex(expect["sp"]), "sp": hex(sp or 0),
           "expected_tpidr": hex(expect["tpidr"]), "tpidr_el1": hex(tpidr or 0),
           "x0": hex(_reg(frame, "x0") or 0), "pc": hex(_reg(frame, "pc") or 0),
           "lr": hex(_reg(frame, "lr") or 0), "caller_lr": hex(expect["lr"]),
           "caller_lr_static": hex(expect["lr_static"]) if expect["lr_static"] else None})
    bp.SetEnabled(False)
    PENDING.pop(bp_id, None)
    return False


def _on_existing(frame, bp_loc, _dict):
    bp = bp_loc.GetBreakpoint()
    bp_id = bp.GetID()
    cfg = base.CONFIG.get(bp_id, {})
    label = cfg.get("label", "UNKNOWN")
    thread = frame.GetThread()
    process = thread.GetProcess()
    row = {"label": label, "bp_id": bp_id, "pid": process.GetProcessID(),
           "progname": base.progname(process), "thread_id": thread.GetThreadID(),
           "pc": hex(_reg(frame, "pc") or 0), "lr": hex(_reg(frame, "lr") or 0),
           "sp": hex(_reg(frame, "sp") or 0),
           "tpidr_el1": hex(_reg(frame, "tpidr_el1") or 0)}
    for name in cfg.get("regs", []):
        val = _reg(frame, name)
        row[name] = None if val is None else hex(val)
    for field, reg, offset, size in cfg.get("reads", []):
        baseval = _reg(frame, reg) if isinstance(reg, str) else reg
        try:
            err = lldb.SBError()
            raw = process.ReadMemory(baseval + offset, size, err)
            row[field] = raw.hex() if err.Success() else "<read-error>"
        except Exception as exc:
            row[field] = "<%s>" % type(exc).__name__
    _emit(row)
    if label == "SB_SETUPAPP_ENTRY":
        lr = _reg(frame, "lr") or 0
        sp = _reg(frame, "sp") or 0
        tpidr = _reg(frame, "tpidr_el1") or 0
        if lr:
            target = process.GetTarget()
            retbp = target.BreakpointCreateByAddress(lr)
            if retbp.IsValid() and retbp.GetNumLocations() > 0:
                PENDING[retbp.GetID()] = {
                    "sp": sp, "tpidr": tpidr, "lr": lr,
                    "lr_static": base._static(lr)}
                retbp.SetScriptCallbackFunction(__name__ + "._on_dynamic_return")
                retbp.SetAutoContinue(True)
                _emit({"label": "SB_SETUPAPP_DYNAMIC_RETURN_ARMED",
                       "bp_id": retbp.GetID(), "address": hex(lr),
                       "expected_sp": hex(sp), "expected_tpidr": hex(tpidr),
                       "caller_lr_static": hex(base._static(lr) or 0)})
    return base.on_break(frame, bp_loc, _dict)


def install(output_path=None):
    global OUT
    if output_path:
        OUT = output_path
    target = lldb.debugger.GetSelectedTarget()
    armed = []
    for i in range(target.GetNumBreakpoints()):
        bp = target.GetBreakpointAtIndex(i)
        bp_id = bp.GetID()
        if bp_id in base.CONFIG:
            bp.SetScriptCallbackFunction(__name__ + "._on_existing")
            armed.append(bp_id)
    if not armed:
        raise RuntimeError("no sb_setup_path_callbacks breakpoints found")
    _emit({"label": "P175_SETUP_TRACE_READY", "breakpoints": armed,
           "output": OUT})
    return armed

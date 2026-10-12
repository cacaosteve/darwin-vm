"""Identify the process at the first QuartzCore surface/swap boundaries."""

import json
import os
import time

import lldb


SLIDE = 0
PROGNAME_PTR = 0x1e6ef1590
OUT = "/tmp/dvm/surface-identity.jsonl"
LABELS = {
    18: "IOMFBDISPLAY_CREATE_SURFACE",
    19: "IOMFBDISPLAY_CREATE_SURFACE_RETURN",
    20: "IOMFBDISPLAY_FB_SWAP_SET_LAYER",
    21: "IOMFBDISPLAY_FB_SWAP_SENDER_CALL",
}
COUNTS = {}


def _read(process, address, size):
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return data if error.Success() else b""


def _u64(process, address):
    raw = _read(process, address, 8)
    return int.from_bytes(raw, "little") if len(raw) == 8 else 0


def _progname(process):
    ptr = _u64(process, SLIDE + PROGNAME_PTR)
    string = _u64(process, ptr) if ptr else 0
    raw = _read(process, string, 96) if string else b""
    return raw.split(b"\0", 1)[0].decode("ascii", "replace") if raw else "<unreadable>"


def on_hit(frame, bp_loc, _dict):
    process = frame.GetThread().GetProcess()
    bp_id = bp_loc.GetBreakpoint().GetID()
    count = COUNTS.get(bp_id, 0) + 1
    COUNTS[bp_id] = count
    row = {
        "label": LABELS.get(bp_id, "UNKNOWN"),
        "bp_id": bp_id,
        "count": count,
        "pid": process.GetProcessID(),
        "progname": _progname(process),
        "thread_id": frame.GetThread().GetThreadID(),
        "pc": hex(frame.GetPC()),
        "lr": hex(frame.FindRegister("lr").GetValueAsUnsigned()),
        "sp": hex(frame.FindRegister("sp").GetValueAsUnsigned()),
        "tpidr_el1": hex(frame.FindRegister("tpidr_el1").GetValueAsUnsigned()),
    }
    for reg in ("x0", "x1", "x2", "x3", "x4", "x5", "x6", "x7"):
        value = frame.FindRegister(reg)
        if value.IsValid():
            row[reg] = hex(value.GetValueAsUnsigned())
    row["time"] = time.time()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    print("SURFACE_IDENTITY " + json.dumps(row, sort_keys=True), flush=True)
    return False


def install(output_path=None):
    global OUT
    if output_path:
        OUT = output_path
    target = lldb.debugger.GetSelectedTarget()
    armed = []
    for bp_id in LABELS:
        bp = target.FindBreakpointByID(bp_id)
        if not bp or not bp.IsValid():
            continue
        bp.SetScriptCallbackFunction(__name__ + ".on_hit")
        bp.SetAutoContinue(True)
        armed.append(bp_id)
    if not armed:
        raise RuntimeError("none of the expected surface/swap breakpoints exist")
    print("SURFACE_IDENTITY_READY breakpoints=%s output=%s" % (armed, OUT), flush=True)
    return armed

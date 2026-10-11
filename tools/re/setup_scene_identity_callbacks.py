"""Log process identity for the late-armed Setup scene checkpoints.

The checkpoints live in Setup's executable, but a raw LLDB address hit is not
enough to establish that Setup executed them.  This callback records the
current task's program name on every hit and adds the scene arguments when the
task really is Setup.  It is read-only and auto-continues.
"""

import json
import os
import time

import lldb


SLIDE = 0
PROGNAME_PTR = 0x1e6ef1590
OUT = "/tmp/dvm/setup-scene-identity.jsonl"
LABELS = {
    122: "WILL_CONNECT_ENTRY",
    123: "BLOCK_DISPATCH_PRE",
    124: "BLOCK_DISPATCH_RETURN",
    125: "URL_ENUMERATION_DONE",
    126: "SCENE_ATTACH_PRE",
    127: "SCENE_ATTACH_RETURN",
    128: "WILL_CONNECT_RETURN",
}
COUNTS = {}


def _read(process, address, size):
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return data if error.Success() else b""


def _u64(process, address):
    data = _read(process, address, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else 0


def _progname(process):
    ptr = _u64(process, SLIDE + PROGNAME_PTR)
    string = _u64(process, ptr) if ptr else 0
    data = _read(process, string, 96) if string else b""
    return data.split(b"\0", 1)[0].decode("ascii", "replace") if data else "<unreadable>"


def on_hit(frame, bp_loc, _dict):
    process = frame.GetThread().GetProcess()
    bp_id = bp_loc.GetBreakpoint().GetID()
    count = COUNTS.get(bp_id, 0) + 1
    COUNTS[bp_id] = count
    name = _progname(process)
    row = {
        "label": LABELS.get(bp_id, "UNKNOWN"),
        "bp_id": bp_id,
        "count": count,
        "pid": process.GetProcessID(),
        "progname": name,
        "thread_id": frame.GetThread().GetThreadID(),
        "pc": hex(frame.GetPC()),
        "lr": hex(frame.FindRegister("lr").GetValueAsUnsigned()),
        "sp": hex(frame.FindRegister("sp").GetValueAsUnsigned()),
        "x0": hex(frame.FindRegister("x0").GetValueAsUnsigned()),
        "x1": hex(frame.FindRegister("x1").GetValueAsUnsigned()),
        "x2": hex(frame.FindRegister("x2").GetValueAsUnsigned()),
        "x3": hex(frame.FindRegister("x3").GetValueAsUnsigned()),
        "tpidr_el1": hex(frame.FindRegister("tpidr_el1").GetValueAsUnsigned()),
    }
    if name == "Setup":
        # Best-effort identity fields: x0 is the delegate at willConnect entry;
        # later checkpoints use x19/x20 as saved scene/session candidates.
        for reg in ("x19", "x20", "x21", "x22"):
            value = frame.FindRegister(reg)
            if value.IsValid():
                row[reg] = hex(value.GetValueAsUnsigned())
    row["time"] = time.time()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "a") as f:
        f.write(json.dumps(row, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())
    print("SETUP_SCENE_IDENTITY " + json.dumps(row, sort_keys=True), flush=True)
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
        raise RuntimeError("none of the Setup scene checkpoint breakpoints exist")
    print("SETUP_SCENE_IDENTITY_READY breakpoints=%s output=%s" % (armed, OUT), flush=True)
    return armed

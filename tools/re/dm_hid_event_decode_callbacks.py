"""Small read-only decoder for live BackBoard direct-touch event wrappers.

The existing unlock observer records registers but its best-effort ObjC class
reader cannot follow TBI/tagged HIDEvent pointers. This probe records the raw
wrapper, tries conservative pointer canonicalizations, and captures a bounded
byte window for the event and selected service objects. It never changes guest
state or stops the target.
"""

import json
import os
import struct
import time
from pathlib import Path

import lldb


SLIDE = 0
OUT = None
COUNTS = {}
SITES = {
    0x22A414320: "BACKBOARD_HANDLE_DIRECT_TOUCH_EVENT",
    0x18493425C: "SPRINGBOARD_UI_EVENT",
}
LIMIT = 16
DECODE_LIMIT = 4


def _reg(frame, name):
    try:
        reg = frame.FindRegister(name)
        return reg.GetValueAsUnsigned() if reg.IsValid() else None
    except Exception:
        return None


def _read(process, address, size):
    if not isinstance(address, int) or address < 0x10000:
        return None, "invalid-address"
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    if not error.Success() or not data:
        return None, error.GetCString() or "empty-read"
    return bytes(data), None


def _pointer_forms(value):
    if not isinstance(value, int):
        return []
    candidates = []
    for candidate in (value, value & 0x00FFFFFFFFFFFFFF,
                      value & 0x0000FFFFFFFFFFFF,
                      value & 0x00FFFFFFFFFFFFF0,
                      value & 0x0000FFFFFFFFFFF0):
        if candidate >= 0x10000 and candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _memory_probe(process, value, size=128):
    result = {"raw": hex(value) if isinstance(value, int) else None,
              "forms": []}
    for address in _pointer_forms(value):
        data, error = _read(process, address, size)
        row = {"address": hex(address), "read_error": error}
        if data:
            row["bytes"] = data.hex()
            row["u64"] = [hex(int.from_bytes(data[i:i + 8], "little"))
                          for i in range(0, min(len(data), 64), 8)]
            row["f32"] = [struct.unpack_from("<f", data, i)[0]
                          for i in range(0, min(len(data), 64), 4)]
        result["forms"].append(row)
    return result


def on_hit(frame, location, _context):
    process = frame.GetThread().GetProcess()
    address = location.GetAddress().GetLoadAddress(
        process.GetTarget()) - SLIDE
    label = SITES.get(address, hex(address))
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    # Never disable this breakpoint at the sample limit: reconnects can leave
    # an old remote software trap at the same PC, and an unowned trap halts the
    # guest. Continue silently once bounded logging is complete.
    if count > LIMIT:
        return False
    thread = frame.GetThread()
    row = {
        "label": label, "count": count, "time": time.time(),
        "pid": process.GetProcessID(), "thread_id": thread.GetThreadID(),
        "tpidr_el1": hex(_reg(frame, "tpidr_el1") or 0),
        "pc": hex(_reg(frame, "pc") or 0),
        "lr": hex(_reg(frame, "lr") or 0),
        "sp": hex(_reg(frame, "sp") or 0),
        "regs": {"x%d" % index: _reg(frame, "x%d" % index)
                 for index in range(8)},
    }
    if label == "BACKBOARD_HANDLE_DIRECT_TOUCH_EVENT" and count <= DECODE_LIMIT:
        regs = row["regs"]
        wrapper = regs.get("x2") or 0
        row["wrapper"] = _memory_probe(process, wrapper, 64)
        event_word = None
        wrapper_data, _ = _read(process, wrapper, 8)
        if wrapper_data:
            event_word = int.from_bytes(wrapper_data, "little")
        row["event"] = _memory_probe(process, event_word or 0, 160)
        row["sender"] = _memory_probe(process, regs.get("x3") or 0, 128)
        row["dispatcher"] = _memory_probe(process, regs.get("x4") or 0, 96)
    line = "HID_EVENT_DECODE " + json.dumps(row, sort_keys=True)
    print(line, flush=True)
    if OUT is not None:
        with OUT.open("a") as stream:
            stream.write(line + "\n")
            stream.flush()
    return False


def install(debugger, slide, event_path=None):
    global SLIDE, OUT
    SLIDE = int(slide)
    output = event_path or os.environ.get(
        "DVM_HID_DECODE_OUT", "/tmp/dvm/hid-event-decode.jsonl")
    OUT = Path(output)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    COUNTS.clear()
    target = debugger.GetSelectedTarget()
    armed = []
    for offset, label in SITES.items():
        bp = target.BreakpointCreateByAddress(SLIDE + offset)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("failed to bind %s at %#x" %
                               (label, SLIDE + offset))
        bp.SetAutoContinue(True)
        result = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command add -F dm_hid_event_decode_callbacks.on_hit %d" %
            bp.GetID(), result)
        proof = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command list %d" % bp.GetID(), proof)
        if (not result.Succeeded() or not proof.Succeeded() or
                "dm_hid_event_decode_callbacks.on_hit" not in
                (proof.GetOutput() or "")):
            bp.SetEnabled(False)
            raise RuntimeError("callback proof failed for %s" % label)
        armed.append({"label": label, "address": hex(SLIDE + offset),
                      "id": bp.GetID()})
    print("HID_EVENT_DECODE_READY " + json.dumps({
        "slide": hex(SLIDE), "output": str(OUT), "sites": armed},
        sort_keys=True), flush=True)

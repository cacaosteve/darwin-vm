"""Read-only watch of the live QuartzCore hotplug-state pointer and bytes.

The render_for_time vtable+0x178 gate reads display+0x330 and returns 0xff
when bytes 0/1 are both clear. This observer watches the pointer field and the
current eight-byte state object so a later transition can be attributed to an
actual writer. It never writes guest memory.
"""

import json
import os
import time
from pathlib import Path

import lldb


DISPLAY = None
SLIDE = 0
OUT = None
WATCHES = {}
HITS = {}
ARMED_FROM_V178 = False


def _reg(frame, name):
    try:
        value = frame.FindRegister(name)
        return value.GetValueAsUnsigned() if value.IsValid() else None
    except Exception:
        return None


def _read(process, address, size):
    if not address or size <= 0:
        return b""
    try:
        error = lldb.SBError()
        data = process.ReadMemory(address, size, error)
        return bytes(data) if error.Success() else b""
    except Exception:
        return b""


def _u64(process, address):
    data = _read(process, address, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else None


def _u32(process, address):
    data = _read(process, address, 4)
    return int.from_bytes(data, "little") if len(data) == 4 else None


def _emit(row):
    row["time"] = time.time()
    line = json.dumps(row, sort_keys=True)
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a") as stream:
            stream.write(line + "\n")
            stream.flush()
    except Exception as error:
        row["log_error"] = repr(error)
    print("POSTHOME_HOTPLUG_STATE " + line, flush=True)


def _arm_state(target, process, state):
    if not state:
        _emit({"label": "HOTPLUG_STATE_WATCH_UNARMED", "reason": "null-state"})
        return
    error = lldb.SBError()
    point = target.WatchAddress(state, 8, False, True, error)
    if not error.Success() or not point or not point.IsValid():
        _emit({"label": "HOTPLUG_STATE_WATCH_FAILED", "address": hex(state),
               "error": error.GetCString()})
        return
    result = lldb.SBCommandReturnObject()
    lldb.debugger.GetCommandInterpreter().HandleCommand(
        "watchpoint command add -F %s.on_watch %d" %
        (__name__, point.GetID()), result)
    if not result.Succeeded():
        target.DeleteWatchpoint(point.GetID())
        _emit({"label": "HOTPLUG_STATE_WATCH_FAILED", "address": hex(state),
               "error": result.GetError()})
        return
    WATCHES[point.GetID()] = {"label": "HOTPLUG_STATE_BYTES", "address": state,
                             "last": _read(process, state, 8)}
    _emit({"label": "HOTPLUG_STATE_WATCH_ARMED", "id": point.GetID(),
           "address": hex(state), "initial": WATCHES[point.GetID()]["last"].hex()})


def _arm_pointer(target, process):
    address = DISPLAY + 0x330
    error = lldb.SBError()
    point = target.WatchAddress(address, 8, False, True, error)
    if not error.Success() or not point or not point.IsValid():
        _emit({"label": "HOTPLUG_STATE_POINTER_WATCH_FAILED",
               "address": hex(address), "error": error.GetCString()})
        return
    result = lldb.SBCommandReturnObject()
    lldb.debugger.GetCommandInterpreter().HandleCommand(
        "watchpoint command add -F %s.on_watch %d" %
        (__name__, point.GetID()), result)
    if not result.Succeeded():
        target.DeleteWatchpoint(point.GetID())
        _emit({"label": "HOTPLUG_STATE_POINTER_WATCH_FAILED",
               "address": hex(address), "error": result.GetError()})
        return
    WATCHES[point.GetID()] = {"label": "HOTPLUG_STATE_POINTER", "address": address,
                             "last": _read(process, address, 8)}
    _emit({"label": "HOTPLUG_STATE_POINTER_WATCH_ARMED", "id": point.GetID(),
           "display": hex(DISPLAY), "address": hex(address),
           "initial": WATCHES[point.GetID()]["last"].hex()})


def on_watch(frame, watchpoint, _context=None):
    watch = WATCHES.get(watchpoint.GetID())
    if not watch:
        return False
    process = frame.GetThread().GetProcess()
    current = _read(process, watch["address"], len(watch["last"]))
    HITS[watch["label"]] = HITS.get(watch["label"], 0) + 1
    pc = _reg(frame, "pc") or 0
    lr = _reg(frame, "lr") or 0
    row = {
        "label": watch["label"] + "_WRITE",
        "hit": HITS[watch["label"]], "pid": process.GetProcessID(),
        "thread_id": frame.GetThread().GetThreadID(),
        "tpidr_el1": _reg(frame, "tpidr_el1"),
        "pc": hex(pc), "pc_shared_cache_candidate": hex(pc - SLIDE),
        "lr": hex(lr), "sp": hex(_reg(frame, "sp") or 0),
        "address": hex(watch["address"]),
        "old": watch["last"].hex(), "new": current.hex(),
        "registers": {"x%d" % index: _reg(frame, "x%d" % index)
                      for index in range(9)},
    }
    if watch["label"] == "HOTPLUG_STATE_POINTER" and len(current) == 8:
        state = int.from_bytes(current, "little")
        row["new_state_pointer"] = hex(state)
        row["new_state_bytes"] = _read(process, state, 8).hex() if state else None
        if state and current != watch["last"]:
            _arm_state(process.GetTarget(), process, state)
    elif watch["label"] == "HOTPLUG_STATE_BYTES":
        row["state_bytes"] = list(current)
    _emit(row)
    watch["last"] = current
    if HITS[watch["label"]] >= 64:
        watchpoint.SetEnabled(False)
        _emit({"label": watch["label"] + "_WATCH_DISABLED",
               "hit": HITS[watch["label"]]})
    return False


def on_v178(frame, bp_loc, _internal_dict=None):
    """Arm object watches from a backboardd user frame with valid address space."""
    global ARMED_FROM_V178, DISPLAY
    if ARMED_FROM_V178:
        return False
    process = frame.GetThread().GetProcess()
    display = _reg(frame, "x20")
    state = _u64(process, display + 0x330) if display else None
    vtable = _u64(process, display) if display else None
    DISPLAY = display
    _emit({"label": "HOTPLUG_STATE_V178_ARM_POINT",
           "pid": process.GetProcessID(), "thread_id": frame.GetThread().GetThreadID(),
           "pc": hex(_reg(frame, "pc") or 0),
           "display": hex(display or 0), "state_pointer": hex(state or 0),
           "state_bytes": _read(process, state, 8).hex() if state else None,
           "display_id_18": _u32(process, display + 0x18) if display else None,
           "vtable": hex(vtable or 0),
           "vtable_slot_178": hex(_u64(process, vtable + 0x178) or 0)
               if vtable else None,
           "mode_2b8": hex(_u64(process, display + 0x2b8) or 0)
               if display else None,
           "mode_byte_2bb": _read(process, display + 0x2bb, 1).hex()
               if display else None})
    if not display or not state:
        return False
    ARMED_FROM_V178 = True
    target = process.GetTarget()
    _arm_pointer(target, process)
    _arm_state(target, process, state)
    bp_loc.GetBreakpoint().SetEnabled(False)
    _emit({"label": "HOTPLUG_STATE_V178_ARMED", "display": hex(display),
           "state_pointer": hex(state)})
    return False


def install(debugger, slide, display, state_override=None):
    global DISPLAY, SLIDE, OUT, ARMED_FROM_V178
    DISPLAY, SLIDE = int(display), int(slide)
    event_dir = Path(os.environ.get("DVM_PROBE_EVENT_DIR", "/tmp/dvm"))
    OUT = event_dir / "hotplug-state-writes.jsonl"
    WATCHES.clear()
    HITS.clear()
    ARMED_FROM_V178 = False
    process = debugger.GetSelectedTarget().GetProcess()
    state = int(state_override) if state_override else _u64(process, DISPLAY + 0x330)
    _emit({"label": "HOTPLUG_STATE_WATCH_READY", "slide": hex(SLIDE),
           "display": hex(DISPLAY), "state_pointer": hex(state or 0),
           "state_bytes": _read(process, state, 8).hex() if state else None})
    _arm_pointer(debugger.GetSelectedTarget(), process)
    if state:
        _arm_state(debugger.GetSelectedTarget(), process, state)


def install_user_frame(debugger, slide):
    """Install a one-shot v178 callback and derive the current live objects there."""
    global SLIDE, OUT, ARMED_FROM_V178
    SLIDE = int(slide)
    event_dir = Path(os.environ.get("DVM_PROBE_EVENT_DIR", "/tmp/dvm"))
    OUT = event_dir / "hotplug-state-writes.jsonl"
    WATCHES.clear()
    HITS.clear()
    ARMED_FROM_V178 = False
    target = debugger.GetSelectedTarget()
    # The live scheduler trace identifies RFT_AFTER_V178 at static
    # 0x184410280 (runtime 0x19385c280 for slide 0x0f44c000).
    address = SLIDE + 0x184410280
    bp = target.BreakpointCreateByAddress(address)
    if not bp or not bp.IsValid() or bp.GetNumLocations() == 0:
        _emit({"label": "HOTPLUG_STATE_V178_BP_FAILED", "address": hex(address),
               "locations": bp.GetNumLocations() if bp and bp.IsValid() else 0})
        return
    bp.SetScriptCallbackFunction(__name__ + ".on_v178")
    bp.SetAutoContinue(True)
    _emit({"label": "HOTPLUG_STATE_V178_BP_ARMED", "id": bp.GetID(),
           "address": hex(address), "locations": bp.GetNumLocations()})

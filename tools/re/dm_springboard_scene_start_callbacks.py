"""Read-only SpringBoard main-scene startup checkpoints.

Installed after the migration/shell ladder so it can distinguish a SpringBoard
scene-configuration request from completion of the main scene connection. All
breakpoints auto-continue; a dynamic return probe is keyed by PID, TPIDR, and
SP rather than LLDB thread identity.
"""

import json
import os
import time
from pathlib import Path

import lldb


SITES = (
    (0x2244CFE84, "SB_SCENE_CONFIGURATION_ENTRY"),
    (0x2244D6CA4, "SB_COMPLETE_STARTUP_AFTER_MAIN_SCENE_CONNECT"),
    (0x2244D8CF4, "SB_FINALIZE_STARTUP_AFTER_SCENES_CONNECT"),
)

META = {}
RETURNS = {}
COUNTS = {}
SLIDE = 0
OUT = None
SITE_LIMIT = 64
RETURN_LIMIT = 32


def _reg(frame, name):
    try:
        value = frame.FindRegister(name)
        return value.GetValueAsUnsigned() if value.IsValid() else None
    except Exception:
        return None


def _emit(row):
    row["time"] = time.time()
    line = json.dumps(row, sort_keys=True)
    try:
        with OUT.open("a") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception as error:
        row["log_error"] = repr(error)
    print("SB_SCENE_START " + line, flush=True)


def _arm_return(frame, entry):
    lr = _reg(frame, "lr") or 0
    if not lr:
        _emit({"label": entry + "_RETURN_UNARMED", "reason": "null-lr"})
        return
    process = frame.GetThread().GetProcess()
    thread = frame.GetThread()
    bp = process.GetTarget().BreakpointCreateByAddress(lr)
    if not bp.IsValid() or bp.GetNumLocations() < 1:
        _emit({"label": entry + "_RETURN_UNARMED", "reason": "invalid-bp",
               "lr": hex(lr)})
        return
    RETURNS[bp.GetID()] = {
        "entry": entry,
        "pid": process.GetProcessID(),
        "tpidr": _reg(frame, "tpidr_el1") or 0,
        "sp": _reg(frame, "sp") or 0,
        "lr": lr,
    }
    bp.SetScriptCallbackFunction(__name__ + ".on_hit")
    bp.SetAutoContinue(True)
    _emit({"label": entry + "_RETURN_ARMED", "lr": hex(lr),
           "pid": process.GetProcessID(),
           "tpidr_el1": hex(_reg(frame, "tpidr_el1") or 0),
           "sp": hex(_reg(frame, "sp") or 0),
           "locations": bp.GetNumLocations()})


def on_hit(frame, bp_loc, _context):
    try:
        bp = bp_loc.GetBreakpoint()
        bp_id = bp.GetID()
        process = frame.GetThread().GetProcess()
        pid = process.GetProcessID()
        thread = frame.GetThread()
        tpidr = _reg(frame, "tpidr_el1") or 0
        sp = _reg(frame, "sp") or 0
        if bp_id in RETURNS:
            expected = RETURNS[bp_id]
            if (pid != expected["pid"] or tpidr != expected["tpidr"] or
                    sp != expected["sp"]):
                return False
            _emit({"label": expected["entry"] + "_RETURN", "pid": pid,
                   "thread_id": thread.GetThreadID(),
                   "tpidr_el1": hex(tpidr), "sp": hex(sp),
                   "entry_lr": hex(expected["lr"]),
                   "pc": hex(_reg(frame, "pc") or 0),
                   "x0": hex(_reg(frame, "x0") or 0)})
            bp.SetEnabled(False)
            RETURNS.pop(bp_id, None)
            return False

        label = META.get(bp_id)
        if not label:
            return False
        if pid != 1:
            return False
        count = COUNTS.get(label, 0) + 1
        COUNTS[label] = count
        if count > SITE_LIMIT:
            bp.SetEnabled(False)
            return False
        row = {"label": label, "count": count, "pid": pid,
               "thread_id": thread.GetThreadID(),
               "tpidr_el1": hex(tpidr), "sp": hex(sp),
               "pc": hex(_reg(frame, "pc") or 0),
               "lr": hex(_reg(frame, "lr") or 0),
               **{"x%d" % i: (hex(_reg(frame, "x%d" % i) or 0))
                  for i in range(6)}}
        _emit(row)
        if label == "SB_SCENE_CONFIGURATION_ENTRY" and count <= RETURN_LIMIT:
            _arm_return(frame, label)
        if count >= SITE_LIMIT:
            bp.SetEnabled(False)
    except Exception as error:
        _emit({"label": "SB_SCENE_START_CALLBACK_ERROR",
               "error": repr(error), "pc": hex(_reg(frame, "pc") or 0)})
    return False


def install(debugger, slide, output_path=None):
    global SLIDE, OUT
    SLIDE = int(slide)
    event_dir = Path(os.environ.get("DVM_PROBE_EVENT_DIR", "/tmp/dvm"))
    OUT = Path(output_path or event_dir / "springboard-scene-start.jsonl")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    target = debugger.GetSelectedTarget()
    META.clear()
    RETURNS.clear()
    COUNTS.clear()
    armed = []
    for offset, label in SITES:
        bp = target.BreakpointCreateByAddress(SLIDE + offset)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("could not arm %s at %#x" %
                               (label, SLIDE + offset))
        META[bp.GetID()] = label
        bp.SetScriptCallbackFunction(__name__ + ".on_hit")
        bp.SetAutoContinue(True)
        armed.append({"label": label, "id": bp.GetID(),
                      "address": hex(SLIDE + offset),
                      "locations": bp.GetNumLocations()})
    _emit({"label": "SB_SCENE_START_READY", "slide": hex(SLIDE),
           "armed": armed, "output": str(OUT)})

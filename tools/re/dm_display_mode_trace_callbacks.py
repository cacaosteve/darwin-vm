"""Low-volume, read-only trace of QuartzCore display-mode selection.

The trace follows the natural backboardd path from framebuffer-mode refresh
through current-mode storage and the power/hotplug mode lookup. It records
registers and small object snapshots only; it does not alter display state.
"""

import json
import os
import time
from pathlib import Path

import lldb

import bks_checkin_callbacks as bks


SITES = (
    (0x184680DC0, "DISPLAY_SET_2B8_SENTINEL"),
    (0x184681B04, "DISPLAY_MODE_SET_ENTRY"),
    (0x184681C38, "DISPLAY_MODE_SET_STORE"),
    (0x184681C40, "DISPLAY_MODE_POST_STORE"),
    (0x1847AE4C4, "DISPLAY_UPDATE_FRAMEBUFFER_ENTRY"),
    (0x1847AF1B4, "DISPLAY_MODE_UPDATE_FLAGS"),
    (0x1847AF218, "DISPLAY_MODE_ARRAY_RETURN"),
    (0x1847AF238, "DISPLAY_MODE_COLOR_ARRAY_COUNT_CALL"),
    (0x1847AF2D0, "DISPLAY_MODE_TIMING_ARRAY_COUNT_CALL"),
    (0x1847AF298, "DISPLAY_UPDATE_DIGITAL_MODES_RETURN_A"),
    (0x1847AF338, "DISPLAY_UPDATE_DIGITAL_MODES_RETURN_B"),
    (0x1847AF340, "DISPLAY_FETCH_CURRENT_MODE_RETURN"),
    (0x1847AF5E0, "DISPLAY_MODE_FALLBACK_MINUS_ONE"),
    (0x1847AF5F0, "DISPLAY_MODE_ADD_MODE"),
    (0x1847AF650, "DISPLAY_SET_ALL_MODES_CALL"),
    (0x1847AF678, "DISPLAY_POST_SET_V308_RETURN"),
    (0x1845BDD30, "DISPLAY_POWER_MODE_LOOKUP_RETURN"),
    (0x1845BE218, "DISPLAY_HOTPLUG_MODE_KEYS"),
    (0x1845BE298, "DISPLAY_HOTPLUG_MODE_MISS"),
    (0x1845BE2C8, "DISPLAY_HOTPLUG_MODE_LIST_MATCH"),
)

META = {}
COUNTS = {}
OUT = None
SLIDE = 0
MAX_ROWS_PER_SITE = 256


def _reg(frame, name):
    try:
        value = frame.FindRegister(name)
        return value.GetValueAsUnsigned() if value.IsValid() else None
    except Exception:
        return None


def _u64(process, address):
    if not address:
        return None
    try:
        data = bks._read(process, address, 8)
        return int.from_bytes(data, "little") if len(data) == 8 else None
    except Exception:
        return None


def _u32(process, address):
    if not address:
        return None
    try:
        data = bks._read(process, address, 4)
        return int.from_bytes(data, "little") if len(data) == 4 else None
    except Exception:
        return None


def _emit(row):
    row["time"] = time.time()
    encoded = json.dumps(row, sort_keys=True)
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a") as stream:
            stream.write(encoded + "\n")
            stream.flush()
    except Exception as error:
        row["log_error"] = repr(error)
    print("DISPLAY_MODE_TRACE " + encoded, flush=True)


def _display_pointer(label, regs):
    if label in ("DISPLAY_MODE_SET_ENTRY", "DISPLAY_UPDATE_FRAMEBUFFER_ENTRY",
                 "DISPLAY_SET_ALL_MODES_CALL"):
        return regs.get("x0")
    if label in ("DISPLAY_MODE_SET_STORE", "DISPLAY_MODE_POST_STORE",
                 "DISPLAY_MODE_UPDATE_FLAGS", "DISPLAY_MODE_ARRAY_RETURN",
                 "DISPLAY_UPDATE_DIGITAL_MODES_RETURN_A",
                 "DISPLAY_UPDATE_DIGITAL_MODES_RETURN_B",
                 "DISPLAY_FETCH_CURRENT_MODE_RETURN",
                 "DISPLAY_MODE_FALLBACK_MINUS_ONE",
                 "DISPLAY_POST_SET_V308_RETURN"):
        return regs.get("x19")
    return regs.get("x19") or regs.get("x0")


def on_hit(frame, bp_loc, _context):
    try:
        label = META.get(bp_loc.GetBreakpoint().GetID())
        if not label:
            return False
        process = frame.GetThread().GetProcess()
        if bks._progname(process) != "backboardd":
            return False
        count = COUNTS.get(label, 0) + 1
        COUNTS[label] = count
        if count > MAX_ROWS_PER_SITE and count & (count - 1):
            return False
        names = ("x0", "x1", "x2", "x3", "x19", "x20", "x21", "x22",
                 "x23", "x24", "x25", "x26", "x27")
        regs = {name: _reg(frame, name) for name in names}
        display = _display_pointer(label, regs)
        state = _u64(process, display + 0x330) if display else None
        row = {
            "label": label,
            "count": count,
            "slide": "0x%x" % SLIDE,
            "pid": process.GetProcessID(),
            "process": "backboardd",
            "thread": frame.GetThread().GetThreadID(),
            "tpidr_el1": _reg(frame, "tpidr_el1"),
            "pc": _reg(frame, "pc"),
            "lr": _reg(frame, "lr"),
            "sp": _reg(frame, "sp"),
            "registers": regs,
            "display_candidate": display,
        }
        if display:
            row["display_fields"] = {
                "id_18": _u32(process, display + 0x18),
                "mode_2b8": _u64(process, display + 0x2B8),
                "target_mode_2c0": _u64(process, display + 0x2C0),
                "pending_mode_6b00": _u64(process, display + 0x6B00),
                "state_330": state,
                "state_bytes_4_5": (
                    list(bks._read(process, state + 4, 2)) if state else None),
            }
        if label == "DISPLAY_MODE_ADD_MODE" and regs.get("x1"):
            row["mode_candidate"] = [
                _u64(process, regs["x1"] + offset)
                for offset in (0, 8, 16, 24, 32, 40, 48, 56)]
        if label == "DISPLAY_SET_ALL_MODES_CALL" and regs.get("x1"):
            row["mode_set_header"] = [
                _u64(process, regs["x1"] + offset)
                for offset in (0, 8, 16, 24, 32, 40, 48, 56)]
        _emit(row)
    except Exception as error:
        _emit({"label": "DISPLAY_MODE_TRACE_CALLBACK_ERROR",
               "error": repr(error), "pc": _reg(frame, "pc")})
    return False


def install(debugger, slide):
    global OUT, SLIDE
    SLIDE = int(slide)
    bks.SLIDE[0] = SLIDE
    event_dir = Path(os.environ.get("DVM_PROBE_EVENT_DIR", "/tmp/dvm"))
    OUT = event_dir / "display-mode-trace.jsonl"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    META.clear()
    COUNTS.clear()
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    armed = []
    for offset, label in SITES:
        address = SLIDE + offset
        bp = target.BreakpointCreateByAddress(address)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("could not resolve %s at 0x%x" % (label, address))
        META[bp.GetID()] = label
        callback = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F %s.on_hit %d" % (__name__, bp.GetID()),
            callback)
        bp.SetAutoContinue(True)
        proof = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(),
                                  proof)
        proof_text = proof.GetOutput() or proof.GetError() or ""
        if (not callback.Succeeded() or not proof.Succeeded() or
                (__name__ + ".on_hit") not in proof_text):
            bp.SetEnabled(False)
            raise RuntimeError("callback proof failed for %s: %s" %
                               (label, proof_text))
        armed.append({"label": label, "id": bp.GetID(),
                      "address": "0x%x" % address,
                      "locations": bp.GetNumLocations()})
    _emit({"label": "DISPLAY_MODE_TRACE_READY", "slide": "0x%x" % SLIDE,
           "armed": armed, "read_only": True, "log": str(OUT)})

"""Read-only post-home samples of QuartzCore's update-flag color classifier.

The hook set is intentionally limited to the w25 producer and bit-22 branch
sites in CA::Render::Updater::prepare_layer. It records the RGBA inputs,
intermediate w25 values, and the prepared flag words that feed
Render::Update::add_context. It never changes registers or guest memory.
"""

import json
import os
import struct
import time
from pathlib import Path

import lldb

import bks_checkin_callbacks as bks
import dm_post_shell_home_min_callbacks as home


SITES = (
    (0x184440B0C, "PREPARE_LAYER_CALL"),
    (0x184440B10, "PREPARE_LAYER_RETURN"),
    (0x184440E30, "UPDATE_ADD_CONTEXT_ORR_2"),
    (0x184440E38, "UPDATE_ADD_CONTEXT_STORE_2"),
    (0x184440E60, "UPDATE_ADD_CONTEXT_BIT22_TEST"),
    (0x184440E64, "UPDATE_ADD_CONTEXT_MASK_NULL_TEST"),
    (0x184440E6C, "UPDATE_ADD_CONTEXT_BIT3_TEST"),
    (0x184440E74, "UPDATE_ADD_CONTEXT_ORR_8"),
    (0x184440E78, "UPDATE_ADD_CONTEXT_STORE_8"),
    (0x18443353C, "MUT_W25_SET1"),
    (0x184433598, "MUT_W25_ALPHA_CLASSIFY_RESULT"),
    (0x18443370C, "MUT_W25_ALPHA_ACCUM_RESULT"),
    (0x184433740, "MUT_VIRT_A8_RET"),
    (0x18443374C, "MUT_W25_VCALL_ACCUM_RESULT"),
    (0x184433A6C, "MUT_ORR_BIT23"),
    (0x184433A74, "MUT_W25_BIT22_GATE"),
    (0x184433A80, "MUT_ORR_BIT22"),
    (0x184433A8C, "MUT_ORR_BIT22_STORE"),
    (0x18443B0A8, "MUT_MERGE_SP0F0"),
)
META = {}
COUNTS = {}
PREPARES_BY_TPIDR = {}
SLIDE = 0
OUT = None
MAX_ROWS_PER_SITE = 256


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
        return bytes(data) if error.Success() and data and len(data) == size else b""
    except Exception:
        return b""


def _u64(process, address):
    data = _read(process, address, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else None


def _u32(process, address):
    data = _read(process, address, 4)
    return int.from_bytes(data, "little") if len(data) == 4 else None


def _f32(process, address):
    data = _read(process, address, 4)
    return struct.unpack("<f", data)[0] if len(data) == 4 else None


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
    print("POSTHOME_MUTATOR_FLAGS " + line, flush=True)


def on_hit(frame, bp_loc, _context):
    try:
        bp = bp_loc.GetBreakpoint()
        label = META.get(bp.GetID())
        if not label:
            return False
        process = frame.GetThread().GetProcess()
        if bks._progname(process) != "backboardd":
            return False
        marker = Path(os.environ.get("DVM_POSTHOME_PRESENT_START_FILE", ""))
        if not home.HOME_RETURNED and not (str(marker) and marker.is_file()):
            return False
        count = COUNTS.get(label, 0) + 1
        COUNTS[label] = count
        if count > MAX_ROWS_PER_SITE and count & (count - 1):
            return False
        registers = {
            "x%d" % index: _reg(frame, "x%d" % index)
            for index in tuple(range(4)) + tuple(range(19, 29))
        }
        sp = registers.get("x29")
        sp = _reg(frame, "sp")
        tpidr = _reg(frame, "tpidr_el1")
        row = {
            "label": label,
            "count": count,
            "pid": process.GetProcessID(),
            "thread_id": frame.GetThread().GetThreadID(),
            "tpidr_el1": tpidr,
            "pc": _reg(frame, "pc"),
            "lr": _reg(frame, "lr"),
            "sp": sp,
            "registers": registers,
            "capture_gate": "home-entity" if home.HOME_RETURNED else "post-input-marker",
        }
        if label == "PREPARE_LAYER_CALL":
            update = registers.get("x21")
            prepare = {
                "update": update,
                "node": registers.get("x2"),
                "out_ref": registers.get("x4"),
                "sp": sp,
                "started": time.time(),
            }
            PREPARES_BY_TPIDR.setdefault(tpidr, []).append(prepare)
            row.update(prepare)
        elif label == "PREPARE_LAYER_RETURN":
            active = PREPARES_BY_TPIDR.get(tpidr, [])
            prepare = active[-1] if active else None
            if prepare:
                out_ref = prepare.get("out_ref")
                row.update({
                    "update": prepare.get("update"),
                    "node": prepare.get("node"),
                    "elapsed_s": time.time() - prepare.get("started", time.time()),
                    "prepared_out_ref": out_ref,
                    "prepared_bytes": _read(process, out_ref, 16).hex() if out_ref else None,
                })
                active.pop()
                if not active:
                    PREPARES_BY_TPIDR.pop(tpidr, None)
        elif label.startswith("UPDATE_ADD_CONTEXT_"):
            update = registers.get("x21")
            raw = _u64(process, sp + 0xF0) if sp else None
            row.update({
                "update": update,
                "update_flags_140": _u64(process, update + 0x140) if update else None,
                "update_bit3_5da": _read(process, update + 0x5DA, 1).hex() if update else None,
                "prepared_sp_f0": raw,
                "prepared_bit22": bool(raw & (1 << 22)) if raw is not None else None,
                "mask_x9": _reg(frame, "x9"),
                "w8": _reg(frame, "w8"), "w9": _reg(frame, "w9"),
            })
        elif label in ("MUT_W25_ALPHA_CLASSIFY_RESULT",
                     "MUT_W25_ALPHA_ACCUM_RESULT",
                     "MUT_W25_VCALL_ACCUM_RESULT"):
            rgba_obj = registers.get("x1")
            row["rgba_object"] = rgba_obj
            row["rgba"] = ([_f32(process, rgba_obj + offset)
                            for offset in (0x10, 0x14, 0x18, 0x1C)]
                           if rgba_obj else None)
            row["rgba_aux"] = _u64(process, rgba_obj + 0x88) if rgba_obj else None
            row["w25"] = _reg(frame, "w25")
            row["w9"] = _reg(frame, "w9")
            row["w11"] = _reg(frame, "w11")
            row["w20"] = _reg(frame, "w20")
            row["w23"] = _reg(frame, "w23")
            row["w24"] = _reg(frame, "w24")
            row["update_context_x19"] = registers.get("x19")
            active = PREPARES_BY_TPIDR.get(tpidr, [])
            if active:
                row["parent_update"] = active[-1].get("update")
                row["parent_node"] = active[-1].get("node")
        elif label in ("MUT_W25_SET1", "MUT_VIRT_A8_RET",
                       "MUT_W25_BIT22_GATE", "MUT_ORR_BIT23",
                       "MUT_ORR_BIT22", "MUT_ORR_BIT22_STORE",
                       "MUT_MERGE_SP0F0"):
            x19 = registers.get("x19")
            row["w25"] = _reg(frame, "w25")
            row["w9"] = _reg(frame, "w9")
            row["w0"] = _reg(frame, "w0")
            row["prepared_accumulator_x19_5d0"] = (
                _u64(process, x19 + 0x5D0) if x19 else None)
            row["local_state_sp_f0"] = _u64(process, sp + 0xF0) if sp else None
            row["mask_sp_170"] = _u64(process, sp + 0x170) if sp else None
            row["layer_x23_flags_18"] = (
                _u32(process, registers.get("x23") + 0x18)
                if registers.get("x23") else None)
            active = PREPARES_BY_TPIDR.get(tpidr, [])
            if active:
                row["parent_update"] = active[-1].get("update")
                row["parent_node"] = active[-1].get("node")
        _emit(row)
    except Exception as error:
        _emit({"label": "POSTHOME_MUTATOR_CALLBACK_ERROR",
               "error": repr(error), "pc": _reg(frame, "pc")})
    return False


def install(debugger, slide):
    global SLIDE, OUT
    SLIDE = int(slide)
    event_dir = Path(os.environ.get("DVM_PROBE_EVENT_DIR", "/tmp/dvm"))
    OUT = event_dir / "posthome-mutator-flags.jsonl"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    META.clear()
    COUNTS.clear()
    PREPARES_BY_TPIDR.clear()
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    armed = []
    for offset, label in SITES:
        address = SLIDE + offset
        bp = target.BreakpointCreateByAddress(address)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("could not resolve %s at 0x%x" % (label, address))
        META[bp.GetID()] = label
        bp.SetScriptCallbackFunction(__name__ + ".on_hit")
        bp.SetAutoContinue(True)
        proof = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(), proof)
        proof_text = proof.GetOutput() or proof.GetError() or ""
        if not proof.Succeeded() or (__name__ + ".on_hit") not in proof_text:
            bp.SetEnabled(False)
            raise RuntimeError("callback proof failed for %s: %s" %
                               (label, proof_text))
        armed.append({"label": label, "id": bp.GetID(),
                      "address": "0x%x" % address,
                      "locations": bp.GetNumLocations()})
    _emit({"label": "POSTHOME_MUTATOR_FLAGS_READY", "slide": hex(SLIDE),
           "armed": armed, "log": str(OUT)})

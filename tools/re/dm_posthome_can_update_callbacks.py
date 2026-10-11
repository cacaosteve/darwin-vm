"""Sample IOMFBDisplay eligibility results after the shell/home observer arms.

This read-only cut records the result of ``IOMFBDisplay::can_update_`` and
the outer ``can_update`` caller, together with the active display-state bytes
when readable.  It deliberately does not change the return value or display
state.  The probe is dynamically installed after shell return so early boot
traffic does not fill the log.
"""

import json
import os
import time
from pathlib import Path

import lldb


SITES = (
    (0x1844300B0, "CAN_UPDATE_INNER_RETURN"),
    (0x18442F2E8, "CAN_UPDATE_OUTER_AFTER_INNER"),
)
META = {}
COUNTS = {}
OUT = None


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
        return data if error.Success() else b""
    except Exception:
        return b""


def _u64(process, address):
    data = _read(process, address, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else None


def _write(row):
    row["time"] = time.time()
    line = json.dumps(row, sort_keys=True)
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a") as stream:
            stream.write(line + "\n")
            stream.flush()
    except Exception as error:
        row["log_error"] = repr(error)
    print("CAN_UPDATE_DIAG " + line, flush=True)


def on_hit(frame, bp_loc, _context):
    try:
        bp = bp_loc.GetBreakpoint()
        label = META.get(bp.GetID())
        if not label:
            return False
        process = frame.GetThread().GetProcess()
        count = COUNTS.get(label, 0) + 1
        COUNTS[label] = count
        # x19 preserves the receiver across both the inner predicate return
        # and its outer caller.  x27 at the inner return is a volatile saved
        # register in this build, not the display object (its apparent
        # vtable is low/noncanonical and varies between hits).
        obj_reg = "x19"
        obj = _reg(frame, obj_reg)
        state_pointer = _u64(process, obj + 0x330) if obj else None
        state = _read(process, state_pointer, 8) if state_pointer else b""
        state_active = (len(state) == 8 and state[0] == 1 and
                        state[1] == 1 and state[5] == 1)
        # Keep the first state samples, power-of-two checkpoints, and a small
        # periodic control sample.  The active display can call this predicate
        # every scheduler tick; logging every hit made a long observational
        # window unnecessarily expensive and could starve the compositor.
        if state_active:
            if count > 16 and count & (count - 1):
                return False
        elif count > 8 and count % 256:
            return False
        vtable = _u64(process, obj) if obj else None
        vtable_9a8 = _u64(process, vtable + 0x9A8) if vtable else None
        row = {
            "label": label,
            "count": count,
            "pid": process.GetProcessID(),
            "thread_id": frame.GetThread().GetThreadID(),
            "pc": _reg(frame, "pc"),
            "lr": _reg(frame, "lr"),
            "sp": _reg(frame, "sp"),
            "object_reg": obj_reg,
            "display": obj,
            "display_state_pointer": state_pointer,
            "display_state_330": state.hex() if state else None,
            "display_state_active_shape": state_active,
            "display_2b8": _read(process, obj + 0x2B8, 8).hex()
            if obj else None,
            "display_3b9": _read(process, obj + 0x3B9, 1).hex()
            if obj else None,
            "vtable": vtable,
            "vtable_method_9a8_candidate": vtable_9a8,
            "registers": {
                name: _reg(frame, name)
                for name in ("x0", "x1", "x19", "x20", "x21", "x22",
                             "x23", "x24", "x25", "x26", "x27", "x28")
            },
        }
        _write(row)
    except Exception as error:
        _write({"label": "CAN_UPDATE_DIAG_CALLBACK_ERROR",
                "error": repr(error),
                "pc": _reg(frame, "pc")})
    return False


def install(debugger, slide):
    global OUT
    event_dir = Path(os.environ.get(
        "DVM_PROBE_EVENT_DIR", "/tmp/dvm/posthome-can-update-events"))
    OUT = event_dir / "can-update-inner.jsonl"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    META.clear()
    COUNTS.clear()
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    armed = []
    for offset, label in SITES:
        bp = target.BreakpointCreateByAddress(int(slide) + offset)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("could not resolve %s at 0x%x" %
                               (label, int(slide) + offset))
        META[bp.GetID()] = label
        bp.SetScriptCallbackFunction(__name__ + ".on_hit")
        bp.SetAutoContinue(True)
        proof = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(),
                                  proof)
        proof_text = proof.GetOutput() or proof.GetError() or ""
        if (not proof.Succeeded() or
                (__name__ + ".on_hit") not in proof_text):
            bp.SetEnabled(False)
            raise RuntimeError("callback proof failed for %s: %s" %
                               (label, proof_text))
        armed.append({"id": bp.GetID(), "label": label,
                      "address": "0x%x" % (int(slide) + offset),
                      "locations": bp.GetNumLocations()})
    print("POSTHOME_CAN_UPDATE_READY " + json.dumps({
        "slide": "0x%x" % int(slide), "armed": armed,
        "log": str(OUT)}, sort_keys=True), flush=True)

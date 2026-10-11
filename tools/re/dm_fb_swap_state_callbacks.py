"""Read-only register/header sample at IOMFBDisplay::fb_swap_set_layer.

This companion probe records every argument at the verified entry, including
the framebuffer/layer reference header when readable. It does not alter guest
state or framebuffer contents.
"""

import json
import os
import time
from pathlib import Path

import lldb

import first_surface_callbacks as surface


BREAKPOINT = 0x18447238C
SLIDE = 0
OUT = None
HITS = 0
LIMIT = 64


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
        error = lldb.SBError()
        data = process.ReadMemory(address, 8, error)
        return int.from_bytes(data, "little") if error.Success() and len(data) == 8 else None
    except Exception:
        return None


def on_hit(frame, _bp_loc, _context):
    global HITS
    try:
        process = frame.GetThread().GetProcess()
        if surface._progname(process) != "backboardd":
            return False
        HITS += 1
        if HITS > LIMIT:
            _bp_loc.GetBreakpoint().SetEnabled(False)
            return False
        row = {
            "label": "FB_SWAP_SET_LAYER_FULL_ARGS",
            "hit": HITS,
            "time": time.time(),
            "pid": process.GetProcessID(),
            "tpidr_el1": _reg(frame, "tpidr_el1"),
            "pc": _reg(frame, "pc"),
            "lr": _reg(frame, "lr"),
            "sp": _reg(frame, "sp"),
            **{"x%d" % index: _reg(frame, "x%d" % index)
               for index in range(9)},
        }
        x1 = row.get("x1")
        if x1:
            row["x1_words_0x00_0x38"] = [
                _u64(process, x1 + offset) for offset in range(0, 0x40, 8)]
        line = json.dumps(row, sort_keys=True)
        print("FB_SWAP_STATE_JSON " + line, flush=True)
        if OUT:
            OUT.parent.mkdir(parents=True, exist_ok=True)
            with OUT.open("a") as stream:
                stream.write(line + "\n")
                stream.flush()
    except Exception as error:
        print("FB_SWAP_STATE_ERROR " + repr(error), flush=True)
    return False


def install(debugger, slide, output_path=None):
    global SLIDE, OUT, HITS
    SLIDE = int(slide)
    OUT = Path(output_path or os.environ.get(
        "DVM_PROBE_EVENT_DIR", "/tmp/dvm/fb-swap-state")) / "fb-swap-state.jsonl"
    HITS = 0
    surface.SLIDE[0] = SLIDE
    target = debugger.GetSelectedTarget()
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint set --address 0x%x" % (SLIDE + BREAKPOINT), result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed to create fb_swap_set_layer diagnostic: %s" %
                           (result.GetError() or result.GetOutput()))
    bp = target.GetBreakpointAtIndex(before)
    bp.SetScriptCallbackFunction(__name__ + ".on_hit")
    bp.SetAutoContinue(True)
    proof = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command list %d" % bp.GetID(), proof)
    output = proof.GetOutput() or proof.GetError() or ""
    if (bp.GetNumLocations() < 1 or not proof.Succeeded() or
            __name__ + ".on_hit" not in output):
        bp.SetEnabled(False)
        raise RuntimeError("fb_swap diagnostic callback not verified: " + output)
    print("FB_SWAP_STATE_READY " + json.dumps({
        "slide": "0x%x" % SLIDE,
        "address": "0x%x" % (SLIDE + BREAKPOINT),
        "breakpoint_id": bp.GetID(),
        "locations": bp.GetNumLocations(),
        "output": str(OUT),
        "limit": LIMIT,
    }, sort_keys=True), flush=True)

"""Capture post-input backboardd IOSurface pixels at fb_swap_set_layer.

This is a deliberately narrow, read-only LLDB callback for an already-running
post-shell guest. It samples the IOSurface passed in x3 and does not touch
SpringBoard, A408, or scanout state.
"""

import json
import os
import time
from pathlib import Path

import lldb

import first_surface_callbacks as surface


BREAKPOINT = 0x18447238C
SLIDE = 0
EVENT_DIR = ""
START_FILE = ""
HITS = 0
GATED_HITS = 0
LIMIT = 16


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else None


def _register(debugger, address):
    target = debugger.GetSelectedTarget()
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint set --address 0x%x" % address, result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed to set fb_swap_set_layer breakpoint: %s" %
                           result.GetError())
    breakpoint = target.GetBreakpointAtIndex(before)
    breakpoint.SetAutoContinue(True)
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command add -F dm_unlock_surface_callbacks.on_hit %d" %
        breakpoint.GetID(), result)
    if not result.Succeeded():
        raise RuntimeError("failed to attach surface callback: %s" %
                           result.GetError())
    if breakpoint.GetNumLocations() < 1:
        breakpoint.SetEnabled(False)
        raise RuntimeError("fb_swap_set_layer breakpoint has no locations")
    print("UNLOCK_SURFACE_ARMED address=0x%x id=%d" %
          (address, breakpoint.GetID()), flush=True)


def on_hit(frame, _bp_loc, _context):
    global HITS, GATED_HITS
    process = frame.GetThread().GetProcess()
    if surface._progname(process) != "backboardd":
        return False
    # A long-lived lock/home observer may see many ordinary swaps before the
    # host sends the one requested gesture.  Keep the capture budget for the
    # post-input state: the host creates this marker only after the helper has
    # ACKed the gesture.  An empty marker preserves the historical behavior.
    if START_FILE and not Path(START_FILE).exists():
        GATED_HITS += 1
        return False
    HITS += 1
    if HITS > LIMIT:
        _bp_loc.GetBreakpoint().SetEnabled(False)
        return False
    row = {
        "label": "UNLOCK_FB_SWAP_SET_LAYER",
        "hit": HITS,
        "time": time.time(),
        "pc": _reg(frame, "pc"),
        "lr": _reg(frame, "lr"),
        "tpidr_el1": _reg(frame, "tpidr_el1"),
        "x3_iosurface": _reg(frame, "x3"),
    }
    address = row["x3_iosurface"]
    if address and address > 0x10000:
        metadata = surface._surface_metadata(process, address)
        row["surface_metadata"] = metadata
        row["surface_capture"] = surface._capture_surface(
            process, address, metadata, HITS)
    print("UNLOCK_SURFACE_JSON " + json.dumps(row, sort_keys=True),
          flush=True)
    if EVENT_DIR:
        surface.EVENT_DIR = EVENT_DIR
        surface._write_event("unlock-surface-%03d.json" % HITS, row)
    return False


def install(debugger, slide, event_dir):
    global SLIDE, EVENT_DIR, START_FILE, GATED_HITS
    SLIDE = int(slide)
    EVENT_DIR = str(Path(event_dir))
    START_FILE = os.environ.get("DVM_UNLOCK_SURFACE_START_FILE", "")
    GATED_HITS = 0
    surface.SLIDE[0] = SLIDE
    surface.EVENT_DIR = EVENT_DIR
    _register(debugger, SLIDE + BREAKPOINT)
    print(json.dumps({"label": "UNLOCK_SURFACE_READY", "slide": hex(SLIDE),
                      "event_dir": EVENT_DIR, "limit": LIMIT,
                      "start_file": START_FILE,
                      "capture_gated_until_marker": bool(START_FILE)},
          sort_keys=True),
          flush=True)

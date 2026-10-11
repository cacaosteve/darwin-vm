"""Register-only SpringBoard setup/finalize milestones for the live P150 run.

These are non-stopping observations at the two known updateInSetupMode call
sites and the scene-finalization/setup-reason path.  They help tell whether
SpringBoard has progressed from applicationDidFinishLaunching into UIKit scene
finalization without sampling stacks or interrupting the guest.
"""

import json
import os
import time

import lldb


SITES = (
    (0x2244D2748, "SB_ADFL_UPDATE_SETUP_CALL"),
    (0x2244D274C, "SB_ADFL_UPDATE_SETUP_RETURN"),
    (0x2244D8CF4, "SB_FINALIZE_SCENES_ENTRY"),
    (0x2244D8DFC, "SB_FINALIZE_UPDATE_SETUP_CALL"),
    (0x2244D8E00, "SB_FINALIZE_UPDATE_SETUP_RETURN"),
    (0x2246D37F0, "SB_SETUP_UPDATE_ENTRY"),
    (0x2246D395C, "SB_SETUP_REASON_RESULT"),
    (0x2246D3ADC, "SB_SETUP_SET_REASON_ENTRY"),
)

META = {}
COUNTS = {}
OUT = "/tmp/dvm/p150-setup-progress.jsonl"
SLIDE = 0
LIMIT = 32


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else None


def _emit(row):
    row["time"] = time.time()
    line = json.dumps(row, sort_keys=True)
    with open(OUT, "a") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    print("P150_SETUP_PROGRESS " + line, flush=True)


def on_hit(frame, bp_loc, _context):
    bp = bp_loc.GetBreakpoint()
    bp_id = bp.GetID()
    label = META.get(bp_id)
    if not label:
        return False
    thread = frame.GetThread()
    process = thread.GetProcess()
    if process.GetProcessID() != 1:
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    row = {
        "label": label,
        "count": count,
        "pid": process.GetProcessID(),
        "thread_id": thread.GetThreadID(),
        "tpidr_el1": hex(_reg(frame, "tpidr_el1") or 0),
        "pc": hex(_reg(frame, "pc") or 0),
        "lr": hex(_reg(frame, "lr") or 0),
        "sp": hex(_reg(frame, "sp") or 0),
        **{"x%d" % i: _reg(frame, "x%d" % i) for i in range(6)},
    }
    _emit(row)
    if count >= LIMIT:
        bp.SetEnabled(False)
    return False


def install(debugger, slide, output_path=None):
    global SLIDE, OUT
    SLIDE = int(slide)
    if output_path:
        OUT = output_path
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    target = debugger.GetSelectedTarget()
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
    _emit({"label": "P150_SETUP_PROGRESS_READY", "slide": hex(SLIDE),
           "armed": armed, "output": OUT})

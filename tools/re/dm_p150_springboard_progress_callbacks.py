"""Small register-only SpringBoard startup observer for the live P150 child.

This observer avoids LLDB backtraces and does not alter guest state.  Its
purpose is to tell whether applicationDidFinishLaunching returns and whether
SpringBoard enters the workspace/Home-entity path after the shell callback.
"""

import json
import os
import time

import lldb


SITES = (
    (0x2244D0BA4, "SB_ADFL_ENTRY"),
    # This return PC belongs to the same shared-cache image but is not near
    # the ADFL entry offset; PLUGIN149/150 LR is 0x184973f8c before slide.
    (0x184973F8C, "SB_ADFL_RETURN_SITE"),
    (0x2242EB53C, "SB_WORKSPACE_CREATE_REQUEST"),
    (0x22430BF70, "SB_WORKSPACE_EXECUTE_TRANSITION"),
    (0x224229FE0, "SB_HOME_ENTITY_INIT_ENTRY"),
)

SLIDE = 0
OUT = "/tmp/dvm/p150-springboard-progress.jsonl"
COUNTS = {}
LIMIT = 8


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else None


def _chain(process, fp):
    rows = []
    for _ in range(6):
        if not fp:
            break
        error = lldb.SBError()
        data = process.ReadMemory(fp, 16, error)
        if not error.Success() or len(data) != 16:
            break
        next_fp = int.from_bytes(data[:8], "little")
        saved_lr = int.from_bytes(data[8:], "little")
        rows.append({"fp": hex(fp), "next_fp": hex(next_fp),
                     "saved_lr": hex(saved_lr)})
        if next_fp <= fp or next_fp - fp > 0x100000:
            break
        fp = next_fp
    return rows


def on_hit(frame, location, _context):
    process = frame.GetThread().GetProcess()
    if process.GetProcessID() != 1:
        return False
    address = location.GetAddress().GetLoadAddress(
        process.GetTarget()) - SLIDE
    label = next((name for offset, name in SITES if offset == address),
                 hex(address))
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count > LIMIT:
        location.GetBreakpoint().SetEnabled(False)
        return False
    row = {
        "label": label,
        "count": count,
        "time": time.time(),
        "pid": process.GetProcessID(),
        "thread_id": frame.GetThread().GetThreadID(),
        "tpidr_el1": hex(_reg(frame, "tpidr_el1") or 0),
        "pc": hex(_reg(frame, "pc") or 0),
        "lr": hex(_reg(frame, "lr") or 0),
        "sp": hex(_reg(frame, "sp") or 0),
        "fp": hex(_reg(frame, "x29") or 0),
        **{"x%d" % index: _reg(frame, "x%d" % index)
           for index in range(8)},
    }
    row["frame_pointer_chain"] = _chain(process, _reg(frame, "x29") or 0)
    with open(OUT, "a") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    print("P150_SB_PROGRESS " + json.dumps(row, sort_keys=True), flush=True)

    if label == "SB_HOME_ENTITY_INIT_ENTRY":
        lr = _reg(frame, "lr") or 0
        if lr:
            bp = process.GetTarget().BreakpointCreateByAddress(lr)
            if bp.IsValid():
                bp.SetAutoContinue(True)
                result = lldb.SBCommandReturnObject()
                process.GetTarget().GetDebugger().GetCommandInterpreter().HandleCommand(
                    "breakpoint command add -F " + __name__ + ".on_home_return %d" %
                    bp.GetID(), result)
                print("P150_SB_HOME_RETURN_ARMED id=%d lr=0x%x" %
                      (bp.GetID(), lr), flush=True)
    return False


def on_home_return(frame, location, _context):
    process = frame.GetThread().GetProcess()
    if process.GetProcessID() != 1:
        return False
    row = {"label": "SB_HOME_ENTITY_INIT_RETURN", "time": time.time(),
           "pid": 1, "thread_id": frame.GetThread().GetThreadID(),
           "tpidr_el1": hex(_reg(frame, "tpidr_el1") or 0),
           "pc": hex(_reg(frame, "pc") or 0),
           "lr": hex(_reg(frame, "lr") or 0),
           "sp": hex(_reg(frame, "sp") or 0),
           "x0": hex(_reg(frame, "x0") or 0)}
    with open(OUT, "a") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    print("P150_SB_PROGRESS " + json.dumps(row, sort_keys=True), flush=True)
    location.GetBreakpoint().SetEnabled(False)
    return False


def install(debugger, slide, output_path=None):
    global SLIDE, OUT
    SLIDE = int(slide)
    if output_path:
        OUT = output_path
    target = debugger.GetSelectedTarget()
    armed = []
    for offset, label in SITES:
        bp = target.BreakpointCreateByAddress(SLIDE + offset)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("could not arm %s at %#x" %
                               (label, SLIDE + offset))
        bp.SetScriptCallbackFunction(__name__ + ".on_hit")
        bp.SetAutoContinue(True)
        armed.append({"label": label, "id": bp.GetID(),
                      "address": hex(SLIDE + offset),
                      "locations": bp.GetNumLocations()})
    row = {"label": "P150_SB_PROGRESS_READY", "slide": hex(SLIDE),
           "armed": armed, "time": time.time(), "output": OUT}
    with open(OUT, "a") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")
        stream.flush()
    print("P150_SB_PROGRESS " + json.dumps(row, sort_keys=True), flush=True)

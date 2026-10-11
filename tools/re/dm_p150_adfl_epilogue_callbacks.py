"""Function-exit checkpoints for SpringBoard ADFL in the current P150 boot."""

import json
import os
import time

import lldb


SITES = ((0x2244D57E8, "SB_ADFL_RETAB_EPILOGUE"),
         (0x2244D57FC, "SB_ADFL_CLEANUP_EPILOGUE"))
META = {}
OUT = "/tmp/dvm/p150-adfl-epilogue.jsonl"


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else None


def on_hit(frame, bp_loc, _context):
    bp = bp_loc.GetBreakpoint()
    label = META.get(bp.GetID())
    process = frame.GetThread().GetProcess()
    if label is None or process.GetProcessID() != 1:
        return False
    row = {"label": label, "time": time.time(),
           "pid": process.GetProcessID(),
           "thread_id": frame.GetThread().GetThreadID(),
           "tpidr_el1": hex(_reg(frame, "tpidr_el1") or 0),
           "pc": hex(_reg(frame, "pc") or 0),
           "lr": hex(_reg(frame, "lr") or 0),
           "sp": hex(_reg(frame, "sp") or 0),
           "x0": _reg(frame, "x0"), "w0": _reg(frame, "w0")}
    with open(OUT, "a") as stream:
        stream.write(json.dumps(row, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    print("P150_ADFL_EXIT " + json.dumps(row, sort_keys=True), flush=True)
    return False


def install(debugger, slide, output_path=None):
    global OUT
    if output_path:
        OUT = output_path
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    target = debugger.GetSelectedTarget()
    armed = []
    for offset, label in SITES:
        bp = target.BreakpointCreateByAddress(int(slide) + offset)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("could not arm %s" % label)
        META[bp.GetID()] = label
        bp.SetScriptCallbackFunction(__name__ + ".on_hit")
        bp.SetAutoContinue(True)
        armed.append({"label": label, "id": bp.GetID(),
                      "address": hex(int(slide) + offset)})
    print("P150_ADFL_EXIT_READY " + json.dumps({"armed": armed,
          "output": OUT, "time": time.time()}, sort_keys=True), flush=True)

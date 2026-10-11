"""Low-overhead post-shell UI markers for SpringBoard and backboardd.

This is a fallback when the composed poster/CA/render callbacks make LLDB's
remote register unwinder crash. It records registers only, writes a durable
JSONL stream, and auto-continues every breakpoint.
"""

import json
import os
from pathlib import Path
import time

import lldb


SITES = (
    (0x2244D0BA4, "SB_ADFL_ENTRY", "SpringBoard"),
    (0x2242EB53C, "SB_WORKSPACE_CREATE_REQUEST", "SpringBoard"),
    (0x22430BF70, "SB_WORKSPACE_EXECUTE_TRANSITION", "SpringBoard"),
    (0x224229FE0, "SB_HOME_ENTITY_INIT_ENTRY", "SpringBoard"),
    (0x201F37E68, "POSTER_CONFIG_BLOCK", "SpringBoard"),
    (0x201F37E98, "POSTER_CONFIG_CONTROLLER_GATE", "SpringBoard"),
    (0x201F37EDC, "POSTER_CONFIG_SCENE_RESOLVED", "SpringBoard"),
    (0x201F37FDC, "POSTER_CONFIG_SCENE_STORE", "SpringBoard"),
    (0x201ED190C, "POSTER_UPDATE_SCENES", "SpringBoard"),
    (0x201ED1964, "POSTER_SCENE_NULL_GATE", "SpringBoard"),
    (0x201ED1968, "POSTER_SCENE_PRESENT_ARM", "SpringBoard"),
    (0x201ED28E0, "POSTER_SCENE_WILL_UPDATE_SETTINGS", "SpringBoard"),
    (0x201EE0C48, "POSTER_HOST_SET_CONTEXT_SCENE", "SpringBoard"),
    (0x18451A138, "CA_CONTEXT_SET_LAYER", "SpringBoard"),
    (0x184776174, "SERVER_RENDER_UPDATE", "backboardd"),
    (0x1844510F0, "SERVER_DISPLAY_RENDER", "backboardd"),
    (0x18444B954, "SERVER_OGL_RENDER", "backboardd"),
)

META = {}
COUNTS = {}
SLIDE = 0
OUT = None


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else None


def on_hit(frame, bp_loc, _dict):
    bp = bp_loc.GetBreakpoint()
    label, expected = META[bp.GetID()]
    process = frame.GetThread().GetProcess()
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    row = {
        "label": label,
        "count": count,
        "time": time.time(),
        "pid": process.GetProcessID(),
        "progname_expected": expected,
        "thread": frame.GetThread().GetThreadID(),
        "tpidr_el1": _reg(frame, "tpidr_el1"),
        "pc": _reg(frame, "pc"),
        "lr": _reg(frame, "lr"),
        "sp": _reg(frame, "sp"),
        **{"x%d" % i: _reg(frame, "x%d" % i) for i in range(8)},
    }
    if label == "POSTER_SCENE_NULL_GATE":
        row["scene"] = _reg(frame, "x8")
        row["scene_nonnull"] = bool(row["scene"])
    elif label in ("POSTER_CONFIG_SCENE_RESOLVED", "POSTER_CONFIG_SCENE_STORE"):
        row["candidate_scene"] = _reg(frame, "x20")
    try:
        with OUT.open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception:
        pass
    print("POST_SHELL_MIN_UI " + json.dumps(row, sort_keys=True), flush=True)
    return False


def install(debugger, slide, output_path=None):
    global SLIDE, OUT
    SLIDE = int(slide)
    OUT = Path(output_path or os.environ.get(
        "DVM_POST_SHELL_MIN_UI_LOG",
        "/tmp/dvm/post-shell-min-ui.jsonl"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    target = debugger.GetSelectedTarget()
    META.clear()
    COUNTS.clear()
    for static, label, expected in SITES:
        bp = target.BreakpointCreateByAddress(SLIDE + static)
        if not bp.IsValid():
            raise RuntimeError("could not arm %s" % label)
        META[bp.GetID()] = label, expected
        bp.SetScriptCallbackFunction(__name__ + ".on_hit")
        bp.SetAutoContinue(True)
        print("POST_SHELL_MIN_UI_ARM label=%s address=0x%x" %
              (label, SLIDE + static), flush=True)
    ready = {"label": "POST_SHELL_MIN_UI_READY", "time": time.time(),
             "slide": hex(SLIDE), "sites": len(SITES), "output": str(OUT)}
    with OUT.open("a") as stream:
        stream.write(json.dumps(ready, sort_keys=True) + "\n")
    print("POST_SHELL_MIN_UI_READY " + json.dumps(ready, sort_keys=True),
          flush=True)

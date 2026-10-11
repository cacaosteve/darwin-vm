"""Small, non-stopping SpringBoard home/poster markers for post-shell runs.

Arm this module from the matched SHELL_SERVICE_RETURN callback using the live
dyld shared-cache slide.  It intentionally avoids stack unwinds, process-name
queries, render forests, and HMP stops.  A separate host-side sampler can read
the running framebuffer while LLDB remains attached.
"""

import json
import os
from pathlib import Path
import time

import lldb


# Shared-cache offsets for 24A5430a. Keep this set intentionally narrow.
SITES = (
    (0x2244D0BA4, "SB_ADFL_ENTRY"),
    (0x2242EB53C, "SB_WORKSPACE_CREATE_REQUEST"),
    (0x22430BF70, "SB_WORKSPACE_EXECUTE_TRANSITION"),
    (0x224229FE0, "SB_HOME_ENTITY_INIT_ENTRY"),
    # The post-shell trace showed a delayed lock reassertion through this
    # coordinator/manager pair. Keep these entry samples read-only and arm
    # their returns dynamically so a later natural swipe can be timed against
    # the actual lock transition rather than a guessed delay alone.
    (0x224AE1A80, "SB_LOCK_COORDINATOR_LOCK_ALL_UI"),
    (0x224C46D88, "SB_LOCK_MANAGER_LOCK_UI"),
    (0x201FEE880, "POSTER_SCENE_DID_UPDATE_CLIENT_SETTINGS"),
    (0x201F7325C, "POSTER_SCENE_UPDATE_LAYERS_BLOCK"),
    (0x201EE0C48, "POSTER_HOST_SET_CONTEXT_SCENE"),
)

META = {}
COUNTS = {}
RETURNS = {}
OUT = None
SLIDE = 0
HOME_RETURNED = set()


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else None


def _emit(row):
    row["time"] = time.time()
    line = json.dumps(row, sort_keys=True)
    try:
        with OUT.open("a") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        pass
    print("POST_SHELL_HOME_MIN " + line, flush=True)


def _arm_return(frame, label, call_sp, tpidr, pid):
    lr = _reg(frame, "lr") or 0
    if not lr:
        _emit({"label": label + "_RETURN_UNARMED", "reason": "null-lr"})
        return
    target = frame.GetThread().GetProcess().GetTarget()
    bp = target.BreakpointCreateByAddress(lr)
    if not bp.IsValid():
        _emit({"label": label + "_RETURN_UNARMED", "reason": "invalid-bp",
               "lr": hex(lr)})
        return
    RETURNS[bp.GetID()] = {
        "label": label + "_RETURN", "sp": call_sp, "tpidr": tpidr,
        "pid": pid, "entry_lr": lr,
    }
    bp.SetScriptCallbackFunction(__name__ + ".on_hit")
    # Return probes are observational too. Without auto-continue LLDB stops at
    # the first ADFL/home return and starves the very post-return UI path being
    # measured.
    bp.SetAutoContinue(True)
    _emit({"label": label + "_RETURN_ARMED", "lr": hex(lr),
           "sp": hex(call_sp or 0), "tpidr_el1": hex(tpidr or 0),
           "pid": pid})


def on_hit(frame, bp_loc, _dict):
    bp = bp_loc.GetBreakpoint()
    bp_id = bp.GetID()
    process = frame.GetThread().GetProcess()
    pid = process.GetProcessID()
    tpidr = _reg(frame, "tpidr_el1") or 0
    sp = _reg(frame, "sp") or 0
    if bp_id in RETURNS:
        expected = RETURNS[bp_id]
        if (pid != expected["pid"] or tpidr != expected["tpidr"] or
                sp != expected["sp"]):
            return False
        row = {"label": expected["label"], "pid": pid,
               "tpidr_el1": hex(tpidr), "sp": hex(sp),
               "entry_lr": hex(expected["entry_lr"]),
               "x0": _reg(frame, "x0"), "pc": _reg(frame, "pc")}
        _emit(row)
        if (expected["label"].startswith("SB_HOME_ENTITY_INIT") and
                expected["label"].endswith("_RETURN") and row["x0"]):
            HOME_RETURNED.add(tpidr)
            _emit({"label": "SB_HOME_ENTITY_READY",
                   "pid": pid, "tpidr_el1": hex(tpidr),
                   "entity": row["x0"]})
        bp.SetEnabled(False)
        RETURNS.pop(bp_id, None)
        return False

    label = META.get(bp_id)
    if not label:
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    row = {"label": label, "count": count, "pid": pid,
           "thread_id": frame.GetThread().GetThreadID(),
           "tpidr_el1": hex(tpidr), "sp": hex(sp),
           "pc": hex(_reg(frame, "pc") or 0),
           "lr": hex(_reg(frame, "lr") or 0),
           "fp": hex(_reg(frame, "x29") or 0),
           **{"x%d" % i: _reg(frame, "x%d" % i) for i in range(4)}}
    # Read a small frame-pointer chain directly from guest memory. This avoids
    # LLDB's remote register unwinder, which has failed on prior long captures.
    # Each row is only a saved FP/LR pair; it is a stack clue, not a symbolized
    # or complete backtrace.
    process = frame.GetThread().GetProcess()
    fp = _reg(frame, "x29") or 0
    chain = []
    for _ in range(8):
        try:
            error = lldb.SBError()
            pair = process.ReadMemory(fp, 16, error) if fp else b""
            if not error.Success():
                break
        except Exception:
            break
        if len(pair) != 16:
            break
        next_fp = int.from_bytes(pair[:8], "little")
        saved_lr = int.from_bytes(pair[8:], "little")
        chain.append({"fp": hex(fp), "next_fp": hex(next_fp),
                      "saved_lr": hex(saved_lr)})
        if next_fp <= fp or next_fp - fp > 0x100000:
            break
        fp = next_fp
    row["frame_pointer_chain"] = chain
    if label == "POSTER_SCENE_DID_UPDATE_CLIENT_SETTINGS":
        row["scene"] = row["x2"]
    elif label in ("POSTER_SCENE_UPDATE_LAYERS_BLOCK",
                   "POSTER_HOST_SET_CONTEXT_SCENE"):
        row["scene_or_context"] = row["x1"]
    _emit(row)
    if label in ("SB_ADFL_ENTRY", "SB_HOME_ENTITY_INIT_ENTRY",
                 "SB_WORKSPACE_CREATE_REQUEST",
                 "SB_WORKSPACE_EXECUTE_TRANSITION",
                 "SB_LOCK_COORDINATOR_LOCK_ALL_UI",
                 "SB_LOCK_MANAGER_LOCK_UI"):
        _arm_return(frame, label, sp, tpidr, pid)
    return False


def install(debugger, slide, output_path=None):
    global OUT, SLIDE
    SLIDE = int(slide)
    event_dir = os.environ.get("DVM_PROBE_EVENT_DIR", "")
    default_output = (str(Path(event_dir) / "post-shell-home-min.jsonl")
                      if event_dir else
                      "/tmp/dvm/post-shell-home-min.jsonl")
    OUT = Path(output_path or os.environ.get(
        "DVM_POST_SHELL_HOME_MIN_LOG",
        default_output))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    target = debugger.GetSelectedTarget()
    META.clear()
    COUNTS.clear()
    RETURNS.clear()
    HOME_RETURNED.clear()
    armed = []
    for offset, label in SITES:
        bp = target.BreakpointCreateByAddress(SLIDE + offset)
        if not bp.IsValid():
            raise RuntimeError("could not arm %s" % label)
        META[bp.GetID()] = label
        bp.SetScriptCallbackFunction(__name__ + ".on_hit")
        bp.SetAutoContinue(True)
        proof = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command list %d" % bp.GetID(), proof)
        proof_text = proof.GetOutput() or proof.GetError() or ""
        callback_name = __name__ + ".on_hit"
        if not proof.Succeeded() or callback_name not in proof_text:
            bp.SetEnabled(False)
            raise RuntimeError("callback proof failed for %s: %s" %
                               (label, proof_text))
        armed.append({
            "label": label, "id": bp.GetID(),
            "address": "0x%x" % (SLIDE + offset),
            "locations": bp.GetNumLocations(),
            "callback": callback_name,
        })
    row = {"label": "POST_SHELL_HOME_MIN_READY", "slide": hex(SLIDE),
           "sites": len(SITES), "armed": armed, "output": str(OUT)}
    _emit(row)

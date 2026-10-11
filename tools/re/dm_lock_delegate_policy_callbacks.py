"""Read-only samples of the Cover Sheet pan-delegate policy decision.

Addresses are static SpringBoard offsets and are relocated with the live
dyld-cache slide. Internal checkpoints are restricted to the active
gestureRecognizerShouldBegin frame (same thread, TPIDR and stack), so the
probe records the decision inputs without stopping unrelated SpringBoard
work. It never changes a return value or recognizer state.
"""

import json
import os
import time

import lldb


EVENT_DIR = None
SLIDE = 0
SITES = {
    0x22436589c: "DELEGATE_GESTURE_SHOULD_BEGIN",
    0x224a5040c: "CAN_DISMISS_GESTURE_BEGIN",
    0x224a50480: "CAN_PRESENT_GESTURE_BEGIN",
    0x224a504c4: "DELEGATE_CONTEXT_PROVIDER",
    0x22438f9cc: "DELEGATE_SYNTHETIC_APPEAR_STATE",
    0x22427a8b4: "DELEGATE_ANY_GESTURE_ACTIVE",
    0x22467ba10: "SLIDING_INTERSTITIAL_DISMISSABLE",
    0x22425e214: "SB_HOME_GESTURE_ENABLED",
}

# Checkpoints in the statically decoded delegate method. They expose the
# actual values consumed by its conditional branches, without patching them.
CHECKPOINTS = {
    0x224365a40: "SHOULD_BEGIN_GATE_A",
    0x224365a68: "SHOULD_BEGIN_GATE_B",
    0x224365ad4: "SHOULD_BEGIN_GATE_C",
    0x224365af4: "SHOULD_BEGIN_GATE_D",
    0x224365b1c: "SHOULD_BEGIN_GATE_E",
    0x224365b48: "SHOULD_BEGIN_GATE_F",
    0x224365ca8: "SHOULD_BEGIN_GATE_G",
    0x224365d50: "SHOULD_BEGIN_HOME_PREF_CALL",
    0x224365d54: "SHOULD_BEGIN_HOME_PREF_RESULT",
    0x224365d5c: "SHOULD_BEGIN_GESTURE_POLICY_CALL",
    0x224365d60: "SHOULD_BEGIN_GESTURE_POLICY_RESULT",
    0x224365d64: "SHOULD_BEGIN_HOME_POLICY_COMBINE",
    0x224365d90: "SHOULD_BEGIN_POLICY_COMBINE",
    0x224365da8: "SHOULD_BEGIN_FINAL_POLICY_BRANCH",
    0x224365e7c: "SHOULD_BEGIN_FINAL_AND_1",
    0x224365e8c: "SHOULD_BEGIN_FINAL_COMPARE",
    0x224365e90: "SHOULD_BEGIN_FINAL_BRANCH",
    0x224365e94: "SHOULD_BEGIN_FINAL_HOME_BRANCH",
    0x224365e9c: "SHOULD_BEGIN_ACCEPT_PATH",
    0x224365ea4: "SHOULD_BEGIN_REJECT_PATH",
    0x224365f54: "SHOULD_BEGIN_REJECTION_REASON",
    0x224365f90: "SHOULD_BEGIN_RETURN_FALSE",
    0x224365f9c: "SHOULD_BEGIN_EPILOGUE_RESULT",
    0x224365fc8: "SHOULD_BEGIN_RETURN_VALUE",
}

ACTIVE = {}
META = {}
COUNTS = {}


def _reg(frame, name):
    try:
        value = frame.FindRegister(name)
        return value.GetValueAsUnsigned() if value.IsValid() else None
    except Exception:
        return None


def _read(process, address, size):
    try:
        error = lldb.SBError()
        data = process.ReadMemory(address, size, error)
        return data if error.Success() else b""
    except Exception:
        return b""


def _uint(process, address, size):
    data = _read(process, address, size)
    return int.from_bytes(data, "little") if len(data) == size else None


def _emit(row):
    path = EVENT_DIR / "delegate-policy.jsonl"
    try:
        EVENT_DIR.mkdir(parents=True, exist_ok=True)
        with path.open("a") as stream:
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()
    except Exception as error:
        row["log_error"] = repr(error)
    print("DELEGATE_POLICY " + json.dumps(row, sort_keys=True), flush=True)


def _snapshot(frame, label):
    process = frame.GetThread().GetProcess()
    row = {
        "time": time.time(),
        "label": label,
        "pid": process.GetProcessID(),
        "thread_id": frame.GetThread().GetThreadID(),
    }
    for name in ("pc", "lr", "sp", "tpidr_el1", "x0", "x1", "x2",
                 "x3", "x4", "x8", "x19", "x20", "x21", "x22",
                 "x23", "x24", "x25", "x26", "x27", "x28"):
        row[name] = _reg(frame, name)
    x0 = row.get("x0")
    if x0:
        row["x0_isa"] = _uint(process, x0, 8)
        row["x0_plus8"] = _uint(process, x0 + 8, 8)
        row["x0_plus12"] = _uint(process, x0 + 12, 4)
        row["x0_plus16"] = _uint(process, x0 + 16, 8)
        row["x0_plus48"] = _uint(process, x0 + 0x48, 8)
    sp = row.get("sp")
    if sp:
        for offset in (0x10, 0x1c, 0x20, 0x28, 0x48, 0x50, 0x88):
            row["stack_%x" % offset] = _uint(process, sp + offset, 8 if offset != 0x1c else 4)
    return process, row


def on_return(frame, bp_loc, _):
    bid = bp_loc.GetBreakpoint().GetID()
    info = META.get(bid)
    if not info or info.get("kind") != "return":
        return False
    process, row = _snapshot(frame, info["label"] + "_RETURN")
    matched = (row.get("sp") == info["sp"] and
               row.get("tpidr_el1") == info["tpidr"])
    if matched:
        row.update({"matched_entry": True,
                    "entry_self": info.get("self"),
                    "entry_lr": info.get("lr"),
                    "entry_sp": info.get("sp"),
                    "entry_tpidr": info.get("tpidr")})
        _emit(row)
        if info["label"] == "DELEGATE_GESTURE_SHOULD_BEGIN":
            ACTIVE.pop((row["thread_id"], info["sp"] - 0x150,
                        info["tpidr"]), None)
        process.GetTarget().BreakpointDelete(bid)
        META.pop(bid, None)
    return False


def on_site(frame, bp_loc, _):
    bid = bp_loc.GetBreakpoint().GetID()
    info = META.get(bid, {})
    if info.get("kind") == "checkpoint":
        process, row = _snapshot(frame, info["label"])
        key = (row.get("thread_id"), row.get("sp", 0) + 0x150,
               row.get("tpidr_el1"))
        active = ACTIVE.get(key)
        # Also check the method's preserved entry stack and thread-local token.
        if not active:
            for candidate_key, candidate in ACTIVE.items():
                if (candidate_key[0] == row.get("thread_id") and
                        candidate_key[2] == row.get("tpidr_el1") and
                        row.get("sp") == candidate.get("body_sp")):
                    active = candidate
                    break
        if active:
            row.update({"same_delegate_frame": True,
                        "recognizer": active.get("recognizer"),
                        "delegate": active.get("delegate"),
                        "entry_sp": active.get("entry_sp")})
            _emit(row)
        return False

    process, row = _snapshot(frame, info.get("label", "UNKNOWN"))
    if info.get("kind") == "entry":
        label = info["label"]
        COUNTS[label] = COUNTS.get(label, 0) + 1
        row["count"] = COUNTS[label]
        if label == "DELEGATE_GESTURE_SHOULD_BEGIN":
            entry_sp = row.get("sp")
            tpidr = row.get("tpidr_el1")
            thread_id = row.get("thread_id")
            delegate = row.get("x0")
            recognizer = row.get("x2")
            active_key = (thread_id, entry_sp - 0x150 if entry_sp else 0,
                          tpidr)
            ACTIVE[active_key] = {
                "entry_sp": entry_sp,
                "body_sp": entry_sp - 0x150 if entry_sp else None,
                "tpidr": tpidr,
                "thread_id": thread_id,
                "delegate": delegate,
                "recognizer": recognizer,
            }
            row["tracked_recognizer"] = recognizer
        _emit(row)
        lr, sp, tpidr = row.get("lr"), row.get("sp"), row.get("tpidr_el1")
        if lr and sp and tpidr:
            target = process.GetTarget()
            ret_bp = target.BreakpointCreateByAddress(int(lr))
            if ret_bp.IsValid() and ret_bp.GetNumLocations():
                ret_bp.SetAutoContinue(True)
                META[ret_bp.GetID()] = {
                    "kind": "return", "label": label,
                    "sp": sp, "tpidr": tpidr, "self": row.get("x0"),
                    "lr": lr,
                }
                result = lldb.SBCommandReturnObject()
                target.GetDebugger().GetCommandInterpreter().HandleCommand(
                    "breakpoint command add -F dm_lock_delegate_policy_callbacks.on_return %d" % ret_bp.GetID(),
                    result)
                if not result.Succeeded():
                    _emit({"time": time.time(), "label": "RETURN_ARM_ERROR",
                           "site": label, "error": result.GetError() or result.GetOutput()})
    return False


def install(debugger, slide):
    global EVENT_DIR, SLIDE
    SLIDE = int(slide)
    EVENT_DIR = __import__("pathlib").Path(os.environ.get(
        "DVM_PROBE_EVENT_DIR", "/tmp/dvm/delegate-policy"))
    target = debugger.GetSelectedTarget()
    installed = []
    for offset, label in SITES.items():
        address = offset + SLIDE
        bp = target.BreakpointCreateByAddress(address)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("no delegate policy BP %s @ 0x%x" % (label, address))
        bp.SetAutoContinue(True)
        META[bp.GetID()] = {"kind": "entry", "label": label}
        result = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command add -F dm_lock_delegate_policy_callbacks.on_site %d" % bp.GetID(),
            result)
        if not result.Succeeded():
            raise RuntimeError(result.GetError() or result.GetOutput())
        installed.append((label, address, bp.GetID()))
    for offset, label in CHECKPOINTS.items():
        address = offset + SLIDE
        bp = target.BreakpointCreateByAddress(address)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("no delegate checkpoint BP %s @ 0x%x" % (label, address))
        bp.SetAutoContinue(True)
        META[bp.GetID()] = {"kind": "checkpoint", "label": label}
        result = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command add -F dm_lock_delegate_policy_callbacks.on_site %d" % bp.GetID(),
            result)
        if not result.Succeeded():
            raise RuntimeError(result.GetError() or result.GetOutput())
        installed.append((label, address, bp.GetID()))
    print("DELEGATE_POLICY_READY " + json.dumps({
        "slide": hex(SLIDE), "sites": len(installed),
        "entries": len(SITES), "checkpoints": len(CHECKPOINTS),
        "addresses": [{"label": name, "address": hex(addr), "id": bid}
                      for name, addr, bid in installed],
    }, sort_keys=True), flush=True)

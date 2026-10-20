"""Capped, read-only Settings launch/scene/bitmap trace for the P276 cache.

The hooks are installed at the normal live-cache slide, but emit only after a
host marker is created immediately before the Settings tap.  Process-name and
target-string filters keep unrelated launch/scene traffic out of the record.
All callbacks auto-continue; the bitmap BRK site is observed, never suppressed.
"""
import json
import os
import time

import lldb

import bks_checkin_callbacks as bks


PROGNAME_PTR = 0x1E6EF1590
POSIX_SPAWN = 0x237CDBEB0
POSIX_SPAWNP = 0x187F0EF50
EXECVE = 0x237CEB100
BITMAP_FACTORY = 0x1D41E3DB0
BITMAP_NIL_TRAP = 0x1D41E3EA0
FBS_SITES = (
    (0x1A84301F8, "FBS_SCENES_CLIENT_REQUEST"),
    (0x1A8423960, "FBS_SCENES_CLIENT_CREATE"),
    (0x1A845CBE8, "FBS_SCENES_CLIENT_ACTIVATE_FUTURE"),
    (0x1A84280D0, "FBS_WORKSPACE_REQUEST"),
    (0x1A8426390, "FBS_SCENE_DID_CREATE_CALLOUT"),
    (0x1A84722F8, "FBS_CLIENT_AGENT_DID_INITIALIZE"),
    (0x1A841947C, "FBS_HOST_AGENT_DID_INITIALIZE"),
    (0x1A8422B54, "FBS_SCENE_SEND_UPDATE"),
)

SLIDE = 0
DEBUGGER = None
EVENT_DIR = ""
TAP_MARKER = ""
CONFIG = {}
COUNTS = {}
RETURNS = {}
LAST_FACTORY = {}
MAX_SITE_HITS = 8
MAX_SPAWN_MATCHES = 12
SUCCESS_LABELS = set()
POST_LAUNCH_ARMED = False


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else 0


def _read(process, address, size):
    if not address or size <= 0:
        return b""
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return data if error.Success() else b""


def _cstring(process, address, limit=256):
    data = _read(process, address, limit)
    return data.split(b"\0", 1)[0].decode("utf-8", "replace") if data else "<unreadable>"


def _vector(process, address, limit=24):
    values = []
    if not address:
        return values
    for i in range(limit):
        raw = _read(process, address + i * 8, 8)
        if len(raw) != 8:
            break
        pointer = int.from_bytes(raw, "little")
        if not pointer:
            break
        values.append(_cstring(process, pointer, 256))
    return values


def _progname(process):
    pointer = bks._read(process, SLIDE + PROGNAME_PTR, 8)
    pointer = int.from_bytes(pointer, "little") if len(pointer) == 8 else 0
    string = bks._read(process, pointer, 8) if pointer else b""
    string = int.from_bytes(string, "little") if len(string) == 8 else 0
    return _cstring(process, string, 96) if string else "<unreadable>"


def _emit(label, frame, fields=None):
    process = frame.GetThread().GetProcess()
    row = {
        "label": label,
        "time": time.time(),
        "progname": _progname(process),
        "qemu_debug_target_pid": process.GetProcessID(),
        "thread_id": frame.GetThread().GetThreadID(),
        "tpidr_el1": "0x%x" % _reg(frame, "tpidr_el1"),
        "pc": "0x%x" % _reg(frame, "pc"),
        "lr": "0x%x" % _reg(frame, "lr"),
    }
    if fields:
        row.update(fields)
    line = "SETTINGS_APP_TRACE " + json.dumps(row, sort_keys=True)
    print(line, flush=True)
    if EVENT_DIR:
        os.makedirs(EVENT_DIR, exist_ok=True)
        path = os.path.join(EVENT_DIR, "settings-app-trace.jsonl")
        with open(path, "a") as output:
            output.write(line + "\n")
            output.flush()
        if label in SUCCESS_LABELS:
            marker = os.path.join(EVENT_DIR, "success.%s.json" % label)
            with open(marker, "w") as output:
                json.dump(row, output, sort_keys=True)
                output.write("\n")
                output.flush()
    return row


def _tap_gate_open():
    return not TAP_MARKER or os.path.exists(TAP_MARKER)


def _install(target, label, address, kind):
    bp = target.BreakpointCreateByAddress(int(address))
    if not bp.IsValid() or bp.GetNumLocations() < 1:
        raise RuntimeError("could not resolve %s at 0x%x" % (label, address))
    bp.SetScriptCallbackFunction(__name__ + ".on_hit")
    bp.SetAutoContinue(True)
    CONFIG[bp.GetID()] = {"label": label, "kind": kind}
    proof = lldb.SBCommandReturnObject()
    DEBUGGER.GetCommandInterpreter().HandleCommand(
        "breakpoint command list %d" % bp.GetID(), proof)
    if not proof.Succeeded() or __name__ + ".on_hit" not in (proof.GetOutput() or ""):
        bp.SetEnabled(False)
        raise RuntimeError("callback proof failed for %s" % label)
    print("SETTINGS_APP_TRACE_BP label=%s id=%d address=0x%x auto_continue=%d" %
          (label, bp.GetID(), address, bp.GetAutoContinue()), flush=True)
    return bp


def _arm_spawn_return(frame, label, path, argv, pid_pointer):
    lr = _reg(frame, "lr") & 0x0000FFFFFFFFFFFF
    if not lr:
        return
    process = frame.GetThread().GetProcess()
    bp = process.GetTarget().BreakpointCreateByAddress(lr)
    if not bp.IsValid() or bp.GetNumLocations() < 1:
        return
    bp.SetScriptCallbackFunction(__name__ + ".on_hit")
    bp.SetAutoContinue(True)
    RETURNS[bp.GetID()] = {
        "label": label + "_RETURN",
        "sp": _reg(frame, "sp"),
        "tpidr": _reg(frame, "tpidr_el1"),
        "pid_pointer": pid_pointer,
        "path": path,
        "argv": argv,
        "candidate_hits": 0,
    }
    CONFIG[bp.GetID()] = {"label": label + "_RETURN", "kind": "spawn_return"}


def _spawn_args(frame, kind):
    process = frame.GetThread().GetProcess()
    if kind == "execve":
        path_ptr, argv_ptr, env_ptr, pid_ptr = (
            _reg(frame, "x0"), _reg(frame, "x1"), _reg(frame, "x2"), 0)
    else:
        pid_ptr, path_ptr, argv_ptr, env_ptr = (
            _reg(frame, "x0"), _reg(frame, "x1"), _reg(frame, "x4"), _reg(frame, "x5"))
    return (pid_ptr, _cstring(process, path_ptr, 512),
            _vector(process, argv_ptr), _vector(process, env_ptr))


def _handle_spawn(frame, label, kind):
    global POST_LAUNCH_ARMED
    process = frame.GetThread().GetProcess()
    caller = _progname(process)
    if kind == "execve":
        if caller != "xpcproxy":
            return False
    elif caller not in {"launchd", "xpcproxy"}:
        return False
    pid_ptr, path, argv, env = _spawn_args(frame, kind)
    joined = " ".join([path] + argv + env).lower()
    if "preferences" not in joined:
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count > MAX_SPAWN_MATCHES:
        return False
    row = _emit(label, frame, {
        "spawn_api": kind,
        "path": path,
        "argv": argv,
        "preferences_environment": [x for x in env if "preferences" in x.lower()],
        "pid_output_pointer": "0x%x" % pid_ptr,
        "caller": caller,
        "caller_pid": process.GetProcessID(),
    })
    if kind != "execve" and caller == "launchd":
        _arm_spawn_return(frame, label, path, argv, pid_ptr)
    if not POST_LAUNCH_ARMED:
        _arm_post_launch_probes(process.GetTarget(), frame)
    return False


def _arm_post_launch_probes(target, frame):
    global POST_LAUNCH_ARMED
    if POST_LAUNCH_ARMED:
        return
    POST_LAUNCH_ARMED = True
    for offset, label in FBS_SITES:
        _install(target, label, SLIDE + offset, "scene")
    _install(target, "SETTINGS_BITMAP_FACTORY_SITE",
             SLIDE + BITMAP_FACTORY, "bitmap_factory")
    trap = _install(target, "SETTINGS_BITMAP_NIL_TRAP_SITE",
                     SLIDE + BITMAP_NIL_TRAP, "bitmap_trap")
    trap.SetAutoContinue(False)
    _emit("SETTINGS_POST_LAUNCH_PROBES_ARMED", frame, {
        "armed_from": "Preferences-matching launchd/xpcproxy spawn or exec",
        "scene_sites": len(FBS_SITES),
        "bitmap_factory": hex(SLIDE + BITMAP_FACTORY),
        "bitmap_trap": hex(SLIDE + BITMAP_NIL_TRAP),
    })


def on_hit(frame, location, _context):
    bp = location.GetBreakpoint()
    bp_id = bp.GetID()
    cfg = CONFIG.get(bp_id)
    if not cfg:
        return False
    kind = cfg["kind"]
    if kind == "spawn_return":
        expected = RETURNS.get(bp_id)
        if not expected:
            bp.SetEnabled(False)
            return False
        process = frame.GetThread().GetProcess()
        sp, tpidr = _reg(frame, "sp"), _reg(frame, "tpidr_el1")
        expected["candidate_hits"] += 1
        matched = sp == expected["sp"] and tpidr == expected["tpidr"]
        if expected["candidate_hits"] <= 8:
            fields = {
                "path": expected["path"], "argv": expected["argv"],
                "return_x0": "0x%x" % _reg(frame, "x0"),
                "return_match": matched,
                "expected_sp": "0x%x" % expected["sp"],
                "actual_sp": "0x%x" % sp,
                "expected_tpidr": "0x%x" % expected["tpidr"],
                "actual_tpidr": "0x%x" % tpidr,
            }
            if matched:
                status = _reg(frame, "x0")
                raw = _read(process, expected["pid_pointer"], 4) if status == 0 else b""
                fields["child_pid"] = int.from_bytes(raw, "little") if len(raw) == 4 else None
            _emit(expected["label"], frame, fields)
        if matched or expected["candidate_hits"] >= 8:
            bp.SetEnabled(False)
            RETURNS.pop(bp_id, None)
        return False
    if not _tap_gate_open():
        return False
    if kind in {"spawn", "execve"}:
        return _handle_spawn(frame, cfg["label"], kind)
    process = frame.GetThread().GetProcess()
    progname = _progname(process)
    label = cfg["label"]
    if kind == "scene":
        if progname != "Preferences":
            return False
        count = COUNTS.get(bp_id, 0) + 1
        COUNTS[bp_id] = count
        if count > MAX_SITE_HITS:
            bp.SetEnabled(False)
            return False
        _emit(label, frame, {"registers": {
            name: "0x%x" % _reg(frame, name)
            for name in ("x0", "x1", "x2", "x3", "x4", "x5", "x6", "lr", "sp")}})
    elif kind == "bitmap_factory":
        if progname != "Preferences":
            return False
        sample = {
            "x0_receiver": "0x%x" % _reg(frame, "x0"),
            "x1_selector": "0x%x" % _reg(frame, "x1"),
            "x2_cgimage": "0x%x" % _reg(frame, "x2"),
            "x21_target": "0x%x" % _reg(frame, "x21"),
            "target_isa": "0x%x" % (int.from_bytes(
                _read(process, _reg(frame, "x21"), 8), "little")
                if _reg(frame, "x21") else 0),
            "logical_width_d0": _double(frame, "d0"),
            "logical_height_d1": _double(frame, "d1"),
            "raw_scale_d2": _double(frame, "d2"),
        }
        tpidr = _reg(frame, "tpidr_el1")
        LAST_FACTORY[tpidr] = sample
        count = COUNTS.get(bp_id, 0) + 1
        COUNTS[bp_id] = count
        if count <= MAX_SITE_HITS:
            _emit("SETTINGS_BITMAP_FACTORY", frame, sample)
        if count >= MAX_SITE_HITS:
            bp.SetEnabled(False)
    elif kind == "bitmap_trap":
        if progname != "Preferences":
            return False
        tpidr = _reg(frame, "tpidr_el1")
        _emit("SETTINGS_BITMAP_NIL_CGIMAGE_TRAP", frame, {
            "prior_factory_same_tpidr": LAST_FACTORY.get(tpidr),
        })
        bp.SetEnabled(False)
        return True
    return False


def _double(frame, name):
    value = frame.FindRegister(name)
    if not value.IsValid():
        return None
    error = lldb.SBError()
    result = value.GetData().GetDouble(error, 0)
    return result if error.Success() else None


def install(debugger, slide):
    global SLIDE, DEBUGGER, EVENT_DIR, TAP_MARKER, SUCCESS_LABELS
    SLIDE = int(slide)
    DEBUGGER = debugger
    bks.SLIDE[0] = SLIDE
    EVENT_DIR = os.environ.get("DVM_PROBE_EVENT_DIR", "")
    TAP_MARKER = os.environ.get("DVM_SETTINGS_TAP_MARKER", "")
    SUCCESS_LABELS = set(filter(None, os.environ.get(
        "DVM_PROBE_SUCCESS_LABELS", "").split(",")))
    if not TAP_MARKER:
        raise RuntimeError("DVM_SETTINGS_TAP_MARKER must be set; probe fails closed")
    global POST_LAUNCH_ARMED
    CONFIG.clear(); COUNTS.clear(); RETURNS.clear(); LAST_FACTORY.clear()
    POST_LAUNCH_ARMED = False
    target = debugger.GetSelectedTarget()
    # Only launch APIs are installed from boot. They are path/argv filtered;
    # the FBS and IconServices sites are armed dynamically from the first
    # matched Preferences launch request, after the tap marker exists.
    entries = [
        (POSIX_SPAWN, "SETTINGS_LAUNCHD_POSIX_SPAWN", "spawn"),
        (POSIX_SPAWNP, "SETTINGS_LAUNCHD_POSIX_SPAWNP", "spawn"),
        (EXECVE, "SETTINGS_XPCPROXY_EXECVE", "execve"),
    ]
    for offset, label, kind in entries:
        bp = _install(target, label, SLIDE + offset, kind)
        if kind == "bitmap_trap":
            bp.SetAutoContinue(False)
    print("SETTINGS_APP_TRACE_READY slide=0x%x tap_marker=%s boot_breakpoints=%d post_launch_sites=%d gated_until_tap=1" %
          (SLIDE, TAP_MARKER or "<none>", len(entries), len(FBS_SITES) + 2), flush=True)

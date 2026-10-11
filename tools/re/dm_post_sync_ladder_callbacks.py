"""Track the matched DM migration return through SpringBoard's BKS shell path.

Install from the boot-time gate probe.  This is a read-only observer: it
records DataMigrator's DMPerformMigration entry/return and correlates
SpringBoard's migration check-in, outer BKS check-in, and shell-service return
on the same TPIDR.  The final shell-service return is an optional condition
stop; the intermediate SystemApp sync return is never a stop here.
"""

import json
import os
from pathlib import Path
import sys
import time

import lldb

import bks_checkin_callbacks as bks


SITES = (
    (0x25E8F02D0, "DM_PERFORM_ENTRY"),
    (0x25E8F044C, "DM_PERFORM_RETURN"),
    (0x1C4A3C058, "SHELL_SERVICE_CALL"),
    (0x18A1476A0, "BKS_MIGRATION_CHECKIN_CALL"),
    (0x18A1476A4, "BKS_MIGRATION_CHECKIN_RETURN"),
    (0x18A147774, "BKS_CHECKIN_RETURN"),
    (0x1C4A3C05C, "SHELL_SERVICE_RETURN"),
    (0x1C4A3C08C, "SHELL_TAIL_C08C"),
    (0x1C4A3C094, "SHELL_TAIL_C094"),
    (0x1C4A3C0B0, "SHELL_TAIL_C0B0"),
    (0x1C4A3C140, "SHELL_TAIL_C140"),
    (0x1C4A3C1B0, "SHELL_TAIL_C1B0"),
    (0x1C4A3C240, "SHELL_TAIL_C240"),
    (0x1C4A3C2D8, "SHELL_TAIL_C2D8"),
    (0x1C4A3C32C, "SHELL_PRE_RETURN"),
    (0x2244D0BA4, "SB_ADFL_ENTRY"),
)

META = {}
EVENT_DIR = os.environ.get("DVM_PROBE_EVENT_DIR", "")
SUCCESS_LABELS = set(filter(None, os.environ.get(
    "DVM_PROBE_SUCCESS_LABELS", "").split(",")))
DM_ACTIVE = {}
SHELL_CALLS = {}
MIGRATION_CALLS = {}
MIGRATION_RETURNS = {}
BKS_RETURNS = {}
HITS = {}
SLIDE = 0
POST_SHELL_UI_ARMED = False


def _emit(label, frame, extra=None):
    process = frame.GetThread().GetProcess()
    thread = frame.GetThread()
    owner = bks._reg(frame, "tpidr_el1") or 0
    row = {
        "label": label,
        "time": time.time(),
        "progname": bks._progname(process),
        "pid": process.GetProcessID(),
        "thread": thread.GetThreadID(),
        "tpidr_el1": "0x%x" % owner,
        "pc": "0x%x" % (bks._reg(frame, "pc") or 0),
        "lr": "0x%x" % (bks._reg(frame, "lr") or 0),
        "sp": "0x%x" % (bks._reg(frame, "sp") or 0),
        "x0": "0x%x" % (bks._reg(frame, "x0") or 0),
        "x1": "0x%x" % (bks._reg(frame, "x1") or 0),
    }
    row.update(extra or {})
    print("DM_POST_SYNC_LADDER " + json.dumps(row, sort_keys=True),
          flush=True)
    if EVENT_DIR:
        os.makedirs(EVENT_DIR, exist_ok=True)
        name = "progress.%s.json" % label
        path = os.path.join(EVENT_DIR, name)
        temporary = "%s.%d.tmp" % (path, os.getpid())
        with open(temporary, "w") as stream:
            json.dump(row, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if label in SUCCESS_LABELS:
            success = os.path.join(EVENT_DIR, "success.%s.json" % label)
            temporary = "%s.%d.tmp" % (success, os.getpid())
            with open(temporary, "w") as stream:
                json.dump(row, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, success)
    return row


def on_hit(frame, bp_loc, _context):
    label = META.get(bp_loc.GetBreakpoint().GetID())
    if not label:
        return False
    process = frame.GetThread().GetProcess()
    progname = bks._progname(process)
    thread = frame.GetThread()
    tid = thread.GetThreadID()
    owner = bks._reg(frame, "tpidr_el1") or 0
    key = (process.GetProcessID(), owner)
    if label.startswith("DM_PERFORM_") and progname != "backboardd":
        return False
    if (label.startswith(("BKS_", "SHELL_")) or label == "SB_ADFL_ENTRY") \
            and progname != "SpringBoard":
        return False

    if label == "DM_PERFORM_ENTRY":
        DM_ACTIVE.setdefault(key, []).append({
            "entry_time": time.time(), "thread": tid,
            "entry_pc": "0x%x" % (bks._reg(frame, "pc") or 0),
        })
        _emit(label, frame, {"depth": len(DM_ACTIVE[key])})
        return False
    if label == "DM_PERFORM_RETURN":
        active = DM_ACTIVE.get(key)
        if not active:
            return False
        call = active.pop()
        if not active:
            DM_ACTIVE.pop(key, None)
        _emit(label, frame, {
            "entry_time": call["entry_time"],
            "elapsed_s": time.time() - call["entry_time"],
            "entry_thread": call["thread"],
            "return_thread": tid,
            "same_lldb_thread": call["thread"] == tid,
            "result_x0": "0x%x" % (bks._reg(frame, "x0") or 0),
        })
        return False

    if label == "SHELL_SERVICE_CALL":
        SHELL_CALLS[key] = {"time": time.time(), "thread": tid,
                            "tpidr_el1": "0x%x" % owner}
        _emit(label, frame)
        return False
    if label == "BKS_MIGRATION_CHECKIN_CALL":
        shell = SHELL_CALLS.get(key)
        MIGRATION_CALLS[key] = {
            "time": time.time(), "thread": tid,
            "tpidr_el1": "0x%x" % owner,
            "shell_call_time": shell.get("time") if shell else None,
            "shell_call_thread": shell.get("thread") if shell else None,
            "shell_call_tpidr_el1": shell.get("tpidr_el1") if shell else None,
        }
        _emit(label, frame, {
            "same_shell_service_tpidr": bool(
                shell and shell.get("tpidr_el1") == "0x%x" % owner),
            "same_lldb_thread_id_as_shell_call": bool(
                shell and shell.get("thread") == tid),
            "shell_call_time": shell.get("time") if shell else None,
        })
        return False
    if label == "BKS_MIGRATION_CHECKIN_RETURN":
        call = MIGRATION_CALLS.get(key)
        if not call:
            return False
        result = _emit(label, frame, {
            "call_time": call["time"],
            "elapsed_s": time.time() - call["time"],
            "call_thread": call["thread"],
            "same_lldb_thread": call["thread"] == tid,
            "same_shell_service_tpidr": call.get(
                "shell_call_tpidr_el1") == "0x%x" % owner,
            "same_lldb_thread_id_as_shell_call": bool(
                call.get("shell_call_thread") == tid),
            "shell_call_time": call.get("shell_call_time"),
        })
        MIGRATION_RETURNS[key] = result
        return False
    if label == "BKS_CHECKIN_RETURN":
        migration_return = MIGRATION_RETURNS.get(key)
        if not migration_return:
            return False
        result = _emit(label, frame, {
            "migration_checkin_return_time": migration_return["time"],
            "elapsed_after_migration_return_s":
                time.time() - migration_return["time"],
            "migration_checkin_result_x0": migration_return.get("x0"),
            "same_migration_checkin_thread":
                migration_return.get("thread") == tid,
        })
        BKS_RETURNS[key] = result
        return False
    if label == "SHELL_SERVICE_RETURN":
        bks_return = BKS_RETURNS.get(key)
        if not bks_return:
            return False
        result = _emit(label, frame, {
            "bks_checkin_return_time": bks_return["time"],
            "elapsed_after_bks_checkin_s": time.time() - bks_return["time"],
            "same_bks_checkin_thread": bks_return.get("thread") == tid,
        })
        _arm_post_shell_ui(frame)
        bp = bp_loc.GetBreakpoint()
        # This is deliberately later than SystemApp sync return and BKS check-in.
        # The caller can request it as the stop while retaining all prior rows.
        if "POST_SYNC_SHELL_SERVICE_RETURN" in SUCCESS_LABELS:
            success_path = os.path.join(
                EVENT_DIR, "success.POST_SYNC_SHELL_SERVICE_RETURN.json")
            os.makedirs(EVENT_DIR, exist_ok=True)
            with open(success_path, "w") as stream:
                json.dump(result, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            return True
        return False
    if label.startswith("SHELL_TAIL_") or label in ("SHELL_PRE_RETURN",
                                                       "SB_ADFL_ENTRY"):
        if label == "SB_ADFL_ENTRY" or key in BKS_RETURNS:
            _emit(label, frame, {
                "bks_checkin_return_seen": key in BKS_RETURNS,
                "shell_call_seen": key in SHELL_CALLS,
            })
        return False
    return False


def _arm_post_shell_ui(frame):
    """Install the existing SpringBoard/CA lock and home probes after shell."""
    global POST_SHELL_UI_ARMED
    if POST_SHELL_UI_ARMED or os.environ.get("DVM_POST_SYNC_ARM_UI") != "1":
        return
    debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
    # A compact SpringBoard-only hook set is useful when a broad CoreAnimation
    # / backboardd breakpoint forest makes LLDB fragile on this GDB stub.  The
    # selected module still receives the same live slide and is armed directly
    # from the matched shell-return callback (no reattach window).
    module_name = os.environ.get("DVM_POST_SHELL_UI_MODULE",
                                 "dm_post_shell_ui_callbacks")
    if not module_name.replace("_", "").isalnum():
        raise RuntimeError("invalid post-shell UI module name")
    module_path = Path(__file__).with_name(module_name + ".py")
    # LLDB may command-import this module as a second Python module instance;
    # its module global SLIDE can then remain zero even though the boot-time
    # gate probe resolved the cache slide. The atomic gate-ready event is the
    # authoritative same-boot value.
    active_slide = 0
    ready_path = os.path.join(os.environ.get("DVM_PROBE_EVENT_DIR", ""),
                              "ready.gate-light.json")
    try:
        with open(ready_path) as stream:
            active_slide = int(json.load(stream).get("slide", "0"), 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        active_slide = 0
    if not active_slide:
        bks._write_event("progress.POST_SHELL_UI_ARM_FAILED.json", {
            "time": time.time(), "reason": "missing same-boot gate-light slide",
            "ready_path": ready_path,
        })
        print("POST_SHELL_UI_ARM_FAILED missing-gate-light-slide",
              flush=True)
        return
    # LLDB's SetScriptCallbackFunction requires a command-script-imported
    # module, not merely a Python importlib entry. Import the environment-
    # selected path, then look it up by that same selected stem (not the old
    # default name). This preserves LLDB's callback registry and supports
    # alternate compact post-shell modules.
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % module_path.resolve(), result)
    if not result.Succeeded():
        bks._write_event("progress.POST_SHELL_UI_ARM_FAILED.json", {
            "time": time.time(),
            "error": result.GetError() or result.GetOutput(),
            "module": str(module_path),
        })
        print("POST_SHELL_UI_ARM_FAILED " +
              (result.GetError() or result.GetOutput()), flush=True)
        return
    module = sys.modules.get(module_name)
    if module is None:
        bks._write_event("progress.POST_SHELL_UI_ARM_FAILED.json", {
            "time": time.time(), "reason": "module-not-registered",
            "module_name": module_name, "module": str(module_path),
        })
        print("POST_SHELL_UI_ARM_FAILED module-not-registered name=%s" %
              module_name, flush=True)
        return
    try:
        module.install(debugger, active_slide)
    except Exception as exc:
        bks._write_event("progress.POST_SHELL_UI_ARM_FAILED.json", {
            "time": time.time(), "error": repr(exc),
            "module": str(module_path),
        })
        print("POST_SHELL_UI_ARM_FAILED %r" % (exc,), flush=True)
        return
    POST_SHELL_UI_ARMED = True
    bks._write_event("ready.post-shell-ui.json", {
        "time": time.time(), "slide": "0x%x" % active_slide,
        "source": str(module_path),
        "arm_point": "SHELL_SERVICE_RETURN",
    })
    print("POST_SHELL_UI_ARMED after=SHELL_SERVICE_RETURN slide=0x%x" %
          active_slide, flush=True)


def _arm(debugger, slide, offset, label):
    target = debugger.GetSelectedTarget()
    bp = target.BreakpointCreateByAddress(int(slide + offset))
    if not bp.IsValid():
        raise RuntimeError("failed to arm %s at 0x%x" %
                           (label, slide + offset))
    bp.SetScriptCallbackFunction(__name__ + ".on_hit")
    proof = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command list %d" % bp.GetID(), proof)
    text = proof.GetOutput() or proof.GetError() or ""
    if __name__ + ".on_hit" not in text:
        bp.SetEnabled(False)
        raise RuntimeError("callback proof failed for %s: %s" %
                           (label, text))
    META[bp.GetID()] = label
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x callback=%s" %
          (bp.GetID(), label, slide + offset, __name__ + ".on_hit"),
          flush=True)


def install(debugger, slide):
    global EVENT_DIR, SUCCESS_LABELS, SLIDE, POST_SHELL_UI_ARMED
    SLIDE = int(slide)
    POST_SHELL_UI_ARMED = False
    EVENT_DIR = os.environ.get("DVM_PROBE_EVENT_DIR", "")
    SUCCESS_LABELS = set(filter(None, os.environ.get(
        "DVM_PROBE_SUCCESS_LABELS", "").split(",")))
    META.clear()
    DM_ACTIVE.clear()
    SHELL_CALLS.clear()
    MIGRATION_CALLS.clear()
    MIGRATION_RETURNS.clear()
    BKS_RETURNS.clear()
    HITS.clear()
    module_path = os.path.abspath(__file__)
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % module_path, result)
    if not result.Succeeded():
        raise RuntimeError("failed LLDB module registration: %s" %
                           (result.GetError() or result.GetOutput()))
    for offset, label in SITES:
        _arm(debugger, int(slide), offset, label)
    bks._write_event("ready.post-sync-ladder.json", {
        "slide": "0x%x" % int(slide),
        "sites": {label: "0x%x" % (int(slide) + offset)
                  for offset, label in SITES},
        "stop_label": "POST_SYNC_SHELL_SERVICE_RETURN",
        "correlation": "DM tpidr; SpringBoard migration/BKS/shell returns on same tpidr",
        "time": time.time(),
    })
    print("DM_POST_SYNC_LADDER_READY slide=0x%x sites=%d" %
          (int(slide), len(SITES)), flush=True)

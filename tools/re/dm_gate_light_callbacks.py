"""Coverage-light DM -> SystemApp gate trace with deferred reply matching.

Only two shared-cache breakpoints are installed at reset: backboardd's DM
client migration entry and DataMigrator's runPluginAtPath entry. The sync-call
and return sites are armed dynamically only after this boot enters the exact
SystemAppMigrator path. When DVM_GATE_LIGHT_ARM_REPLY_AFTER_SYSTEMAPP=1, the
full reply/object matcher is also imported and installed at that positive gate,
before the wrapper's synchronous send. All callbacks auto-continue.
"""
import importlib.util
import json
import os
import sys
import time

import bks_checkin_callbacks as bks
import dm_plugin_lifecycle_callbacks as lifecycle
import dm_post_sync_ladder_callbacks as post_sync

SLIDE = 0
META = {}
BPS = {}
SYSTEMAPP_ID = "com.apple.MobileInstallation.SystemAppMigrator"
SYSTEMAPP_PATH = "/System/Library/DataClassMigrators/SystemAppMigrator.migrator"
OWNERS = {}
REPLY_JOIN_ARMED = False
SPAWN_RETURN_META = {}
SPAWN_SAMPLES = 0
XPCPROXY_SPAWN_SAMPLES = 0
SEEN_DM_MIGRATE_ENTRY = False
SEEN_CLIENT_RUN_ENTRY = False
SUCCESS_LABELS = set(filter(None, os.environ.get(
    "DVM_PROBE_SUCCESS_LABELS", "").split(",")))

# Shared-cache offsets, kept in sync with dm_migration_server_migrate_callbacks
# and dm_plugin_lifecycle_callbacks. These are unslid offsets.
DM_CLIENT_MIGRATE_ENTRY = 0x25E8F4AF8
CLIENT_RUN_ENTRY = 0x25E8F1A10
CLIENT_SYNC_CALL = 0x25E8F1F88
CLIENT_SYNC_RETURN = 0x25E8F1F8C

# Exported dyld-cache entries (24A5430a) from the extracted cache. These are
# launchd-side process creation paths; all are filtered by the live process
# name and by an executable path containing "datamigrat" before emitting a
# target event. Keep the offsets unslid and use this boot's resolved cache
# slide, just like the DM gate sites above.
POSIX_SPAWN = 0x237CDBEB0
POSIX_SPAWNP = 0x187F0EF50
EXECVE = 0x237CEB100


def _install(debugger, label, address):
    target = debugger.GetSelectedTarget()
    bp = target.BreakpointCreateByAddress(int(address))
    if not bp.IsValid():
        raise RuntimeError("failed to create %s at 0x%x" % (label, address))
    bp.SetScriptCallbackFunction(__name__ + ".on_hit")
    result = __import__("lldb").SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command list %d" % bp.GetID(), result)
    proof = result.GetOutput() or result.GetError() or ""
    if __name__ + ".on_hit" not in proof:
        bp.SetEnabled(False)
        raise RuntimeError("callback proof failed for %s: %s" % (label, proof))
    META[bp.GetID()] = label
    BPS[label] = bp
    print("DM_GATE_LIGHT_BP label=%s id=%d address=0x%x callback=proven" %
          (label, bp.GetID(), address), flush=True)
    return bp


def _emit(label, frame, extra=None):
    process = frame.GetThread().GetProcess()
    row = {
        "label": label,
        "time": time.time(),
        "progname": bks._progname(process),
        "pid": process.GetProcessID(),
        "thread": frame.GetThread().GetThreadID(),
        "tpidr_el1": "0x%x" % (bks._reg(frame, "tpidr_el1") or 0),
        "pc": "0x%x" % (bks._reg(frame, "pc") or 0),
        "lr": "0x%x" % (bks._reg(frame, "lr") or 0),
    }
    if extra:
        row.update(extra)
    print("DM_GATE_LIGHT " + json.dumps(row, sort_keys=True), flush=True)
    bks._write_event("progress.%s.json" % label, row)
    if label in SUCCESS_LABELS:
        bks._write_event("success.%s.json" % label, row)
    return row


def _dyn_install(debugger, label, offset):
    address = SLIDE + offset
    bp = _install(debugger, label, address)
    return bp


def _install_spawn_return(debugger, frame, record):
    """Observe the matched spawn return and expose identity-filter misses."""
    process = frame.GetThread().GetProcess()
    target = process.GetTarget()
    address = (bks._reg(frame, "lr") or 0) & 0x0000FFFFFFFFFFFF
    if not address:
        return
    bp = target.BreakpointCreateByAddress(int(address))
    if not bp.IsValid():
        return
    bp.SetScriptCallbackFunction(__name__ + ".on_hit")
    result = __import__("lldb").SBCommandReturnObject()
    process.GetTarget().GetDebugger().GetCommandInterpreter().HandleCommand(
        "breakpoint command list %d" % bp.GetID(), result)
    proof = result.GetOutput() or result.GetError() or ""
    if __name__ + ".on_hit" not in proof:
        bp.SetEnabled(False)
        return
    record = dict(record)
    record["thread"] = frame.GetThread().GetThreadID()
    record["tpidr_el1"] = "0x%x" % (bks._reg(frame, "tpidr_el1") or 0)
    record["sp"] = "0x%x" % (bks._reg(frame, "sp") or 0)
    record["progname"] = bks._progname(process)
    record["process_id"] = process.GetProcessID()
    record["candidate_hits"] = 0
    record["return_address"] = "0x%x" % address
    SPAWN_RETURN_META[bp.GetID()] = record
    print("DM_GATE_LIGHT_BP label=DATAMIGRATOR_SPAWN_RETURN id=%d address=0x%x callback=proven expected_prog=%s expected_pid=%s expected_thread=%s expected_tpidr=%s expected_sp=%s" %
          (bp.GetID(), address, record["progname"], record["process_id"],
           record["thread"], record["tpidr_el1"], record["sp"]), flush=True)


def _activation_token(value):
    value = (value or "").lower()
    return ("datamigrat" in value or "data-migration" in value or
            "migrationservice" in value)


def _vector_strings(process, vector, limit=32, string_limit=384):
    """Read a bounded argv/envp vector of guest C strings."""
    if not vector:
        return []
    values = []
    for index in range(limit):
        pointer_data = bks._read(process, vector + index * 8, 8)
        if len(pointer_data) != 8:
            break
        pointer = int.from_bytes(pointer_data, "little")
        if not pointer:
            break
        values.append(bks._cstring(process, pointer, string_limit))
    return values


def _caller_pcs(thread, limit=8):
    pcs = []
    try:
        for index in range(1, min(thread.GetNumFrames(), limit + 1)):
            frame = thread.GetFrameAtIndex(index)
            pc = frame.GetPC() if frame.IsValid() else 0
            if pc:
                pcs.append("0x%x" % pc)
    except Exception as exc:
        # A stack-walk failure must not abort the launch observer callback.
        pcs.append("<unwind-error:%s>" % type(exc).__name__)
    return pcs


def _activation_args(frame, label):
    process = frame.GetThread().GetProcess()
    if label == "LAUNCHD_EXECVE":
        path_pointer = bks._reg(frame, "x0") or 0
        argv_pointer = bks._reg(frame, "x1") or 0
        envp_pointer = bks._reg(frame, "x2") or 0
        pid_pointer = 0
    else:
        # posix_spawn(pid*, path, file_actions, attrp, argv, envp)
        pid_pointer = bks._reg(frame, "x0") or 0
        path_pointer = bks._reg(frame, "x1") or 0
        argv_pointer = bks._reg(frame, "x4") or 0
        envp_pointer = bks._reg(frame, "x5") or 0
    path = bks._cstring(process, path_pointer, 512)
    argv = _vector_strings(process, argv_pointer)
    envp = _vector_strings(process, envp_pointer)
    selected_env = [value for value in envp if _activation_token(value)]
    xpcproxy = "xpcproxy" in (path or "").lower()
    argv_text = " ".join(argv)
    targeted = (_activation_token(path) or _activation_token(argv_text) or
                any(_activation_token(value) for value in selected_env))
    return {
        "path": path,
        "path_pointer": "0x%x" % path_pointer,
        "pid_pointer": "0x%x" % pid_pointer,
        "argv": argv,
        "env_matches": selected_env,
        "xpcproxy_path": xpcproxy,
        "datamigrator_candidate": targeted,
        "caller_stack": _caller_pcs(frame.GetThread()),
    }


def _handle_spawn(frame, bp_id, label):
    global SPAWN_SAMPLES, XPCPROXY_SPAWN_SAMPLES
    process = frame.GetThread().GetProcess()
    thread = frame.GetThread()
    progname = bks._progname(process)

    # Dynamic return breakpoint. Log bounded candidates before matching so a
    # wrong thread/TPIDR assumption cannot turn a real return into silence.
    pending = SPAWN_RETURN_META.get(bp_id)
    if pending is not None:
        owner = bks._reg(frame, "tpidr_el1") or 0
        sp = bks._reg(frame, "sp") or 0
        actual_progname = bks._progname(process)
        actual_pid = process.GetProcessID()
        actual_thread = thread.GetThreadID()
        expected_tpidr = pending["tpidr_el1"]
        expected_sp = pending["sp"]
        same_tpidr = "0x%x" % owner == expected_tpidr
        same_thread = actual_thread == pending["thread"]
        same_sp = "0x%x" % sp == expected_sp
        same_process_name = actual_progname == pending["progname"]
        # SP and TPIDR identify the saved caller context more reliably than
        # LLDB's transient thread id or a process-name read that can be null at
        # a shared return PC. Keep process/thread/name fields diagnostic only.
        matched = same_sp and same_tpidr
        pending["candidate_hits"] += 1
        if matched:
            status = bks._reg(frame, "x0") or 0
            pid_pointer = int(pending.get("pid_pointer", "0"), 16)
            child_pid = (bks._read(process, pid_pointer, 4)
                         if pid_pointer and status == 0 else b"")
            extra = {
                "path": pending["path"],
                "spawn_api": pending["spawn_api"],
                "argv": pending.get("argv", []),
                "return_x0": "0x%x" % status,
                "pid_pointer": "0x%x" % pid_pointer,
                "child_pid": int.from_bytes(child_pid, "little") if len(child_pid) == 4 else None,
                "entry_time": pending["time"],
                "return_match": "saved_sp_and_tpidr",
                "expected_thread": pending["thread"],
                "actual_thread": actual_thread,
                "expected_tpidr_el1": expected_tpidr,
                "actual_tpidr_el1": "0x%x" % owner,
                "expected_sp": expected_sp,
                "actual_sp": "0x%x" % sp,
                "expected_process_id": pending["process_id"],
                "actual_process_id": actual_pid,
            }
            _emit("DATAMIGRATOR_ACTIVATION_RETURN", frame, extra)
            breakpoint = process.GetTarget().FindBreakpointByID(bp_id)
            if breakpoint.IsValid():
                breakpoint.SetEnabled(False)
            SPAWN_RETURN_META.pop(bp_id, None)
            return False
        # Emit bounded candidates from every process at this return PC. A
        # process-name mismatch is evidence to inspect, not a reason to hide
        # the hit and make the return look silent.
        if pending["candidate_hits"] <= 16:
            _emit("DATAMIGRATOR_SPAWN_RETURN_CANDIDATE", frame, {
                "path": pending["path"],
                "spawn_api": pending["spawn_api"],
                "entry_time": pending["time"],
                "candidate_index": pending["candidate_hits"],
                "expected_thread": pending["thread"],
                "actual_thread": actual_thread,
                "same_thread": same_thread,
                "expected_tpidr_el1": expected_tpidr,
                "actual_tpidr_el1": "0x%x" % owner,
                "same_tpidr_el1": same_tpidr,
                "expected_sp": expected_sp,
                "actual_sp": "0x%x" % sp,
                "same_sp": same_sp,
                "expected_process_id": pending["process_id"],
                "actual_process_id": actual_pid,
                "same_process_name": same_process_name,
                "return_match": False,
            })
        return False

    # Avoid walking argv/envp on every system spawn/exec breakpoint. The only
    # expected activation paths are launchd's xpcproxy/direct spawn and
    # xpcproxy's exec of the service executable (plus direct DataMigrator
    # calls from another process).
    if label == "LAUNCHD_EXECVE":
        early_path = bks._cstring(process, bks._reg(frame, "x0") or 0, 512)
        if (progname not in ("launchd", "xpcproxy", "com.apple.datamigrator") and
                not _activation_token(early_path) and "xpcproxy" not in early_path.lower()):
            return False
    elif progname != "launchd":
        early_path = bks._cstring(process, bks._reg(frame, "x1") or 0, 512)
        if not _activation_token(early_path):
            return False

    args = _activation_args(frame, label)
    path = args["path"]
    targeted = args["datamigrator_candidate"]
    xpcproxy_candidate = args["xpcproxy_path"]
    relevant_exec = (label == "LAUNCHD_EXECVE" and
                     (progname == "xpcproxy" or xpcproxy_candidate or targeted))
    relevant_spawn = (label != "LAUNCHD_EXECVE" and progname == "launchd" and
                      (targeted or xpcproxy_candidate))
    direct_spawn = (label != "LAUNCHD_EXECVE" and targeted and
                    progname != "launchd")
    if not (relevant_exec or relevant_spawn or direct_spawn):
        return False

    SPAWN_SAMPLES += 1
    if xpcproxy_candidate and not targeted:
        XPCPROXY_SPAWN_SAMPLES += 1
        # Retain enough ordinary service-label traffic to prove the hook is
        # live on a long miss boot without logging every launch indefinitely.
        if XPCPROXY_SPAWN_SAMPLES > 256:
            return False
    if label == "LAUNCHD_EXECVE":
        event_label = ("DATAMIGRATOR_EXECVE_CANDIDATE" if targeted else
                       "XPCPROXY_SERVICE_EXECVE_CANDIDATE")
    elif xpcproxy_candidate and not targeted:
        event_label = "LAUNCHD_XPCPROXY_SPAWN"
    else:
        event_label = "DATAMIGRATOR_SPAWN_CALL"
    args.update({
        "spawn_api": label,
        "caller_progname": progname,
        "caller_pid": process.GetProcessID(),
        "argv_candidate_match": any(_activation_token(value) for value in args["argv"]),
    })
    row = _emit(event_label, frame, args)
    # Arm the return only for a DataMigrator-labeled launch/exec candidate.
    # Broad xpcproxy tracing remains observational and cannot accumulate LRs.
    if targeted and (label != "LAUNCHD_EXECVE" or progname == "launchd"):
        _install_spawn_return(process.GetTarget().GetDebugger(), frame, {
            "path": path,
            "spawn_api": label,
            "pid_pointer": args["pid_pointer"],
            "argv": args["argv"],
            "time": row["time"],
        })
    return False

def _arm_reply_join(debugger, frame, gate_row):
    """Load the heavier request/reply probe only after SystemApp is observed."""
    global REPLY_JOIN_ARMED
    if REPLY_JOIN_ARMED:
        return
    if os.environ.get("DVM_GATE_LIGHT_ARM_REPLY_AFTER_SYSTEMAPP") != "1":
        return
    process = frame.GetThread().GetProcess()
    module_path = os.path.join(os.path.dirname(__file__),
                               "dm_lock_checkin_wrapper_reply_callbacks.py")
    command = "command script import %s" % module_path
    result = __import__("lldb").SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(command, result)
    if not result.Succeeded():
        err = result.GetError() or result.GetOutput() or "unknown LLDB import error"
        _emit("GATE_LIGHT_REPLY_JOIN_IMPORT_FAILED", frame, {
            "error": err, "module_path": module_path,
            "identifier": gate_row["identifier"],
        })
        return
    spec = importlib.util.spec_from_file_location(
        "dm_lock_checkin_wrapper_reply_callbacks", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("unable to load reply probe from %s" % module_path)
    module = importlib.util.module_from_spec(spec)
    # Reuse LLDB's registered module if command-script import populated it.
    import sys
    module = sys.modules.get(spec.name, module)
    if spec.name not in sys.modules:
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
    try:
        # Keep the pinned reply destination watcher live across every
        # mach_msg2 sender for the full SystemApp window. The transport module
        # defaults this expensive global comparator off; deferred installation
        # must opt it in explicitly or its close-time send coverage is zero.
        module.install(debugger, SLIDE)
        # kernelpath.install() resets its module state, so set this only after
        # installation. Otherwise the deferred join appears armed while the
        # all-sender comparator remains disabled.
        module.kernelpath.full.TRACE_ANY_PINNED_REPLY_DEST = True
        _emit("GATE_LIGHT_PINNED_REPLY_SEND_WATCH_READY", frame, {
            "enabled": bool(
                module.kernelpath.full.TRACE_ANY_PINNED_REPLY_DEST),
            "site_static": "0xfffffff00aaa3f54",
            "site_runtime": "0x%x" % (
                0xfffffff00aaa3f54 + int(SLIDE)),
            "reason": "deferred reply join explicitly enables all-sender post-copyin destination comparison",
        })
        # This probe is installed from the lifecycle entry breakpoint itself.
        # The composed plugin-lifecycle module therefore cannot observe the
        # CLIENT_RUN_ENTRY that already brought us here. Seed that exact live
        # call into its owner tables so the upcoming CLIENT_SYNC_CALL can arm
        # post-copyin object resolution. Without this seed, the outer gate
        # observer sees the call but the inner transport callback discards it
        # as an unknown owner.
        owner = bks._reg(frame, "tpidr_el1") or 0
        receiver = bks._reg(frame, "x0") or 0
        selector = bks._reg(frame, "x1") or 0
        completion = int(gate_row.get("completion_block", "0"), 0)
        lifecycle_plugin = module.kernelpath.plugin
        transport = module.kernelpath.full
        owner_record = {
            "path": gate_row["path"],
            "identifier": gate_row["identifier"],
            "run_entry": gate_row,
        }
        lifecycle_plugin.OWNERS[owner] = owner_record
        transport.CALLS[owner] = {
            "owner": owner,
            "path": gate_row["path"],
            "identifier": gate_row["identifier"],
            "path_ptr": gate_row.get("path_object", "0x0"),
            "identifier_ptr": gate_row.get("identifier_object", "0x0"),
            "completion": "0x%x" % completion,
            "receiver": "0x%x" % receiver,
            "selector": "0x%x" % selector,
            "deferred_gate_seed": True,
        }
        transport.RECEIVERS["0x%x" % receiver] = {
            "path": gate_row["path"],
            "identifier": gate_row["identifier"],
            "owner": owner,
        }
        # This callback is installed at CLIENT_RUN_ENTRY, which currently
        # shares its PC with DataMigration.framework's wrapper-method entry.
        # LLDB has already stopped at that address before the deferred module
        # is loaded, so a newly-created breakpoint there may miss this very
        # invocation. Seed the method's x5 completion block directly from the
        # gate frame; later sync-return/result callbacks can then be matched
        # by the same thread + TPIDR instead of silently dropping the hit.
        run_calls = getattr(module, "RUN_PLUGIN_CALLS", None)
        if run_calls is not None and completion:
            invoke_raw = int.from_bytes(
                bks._read(process, completion + 0x10, 8), "little")
            run_calls[(frame.GetThread().GetThreadID(), owner)] = {
                "time": float(gate_row.get("time", time.time())),
                "thread": frame.GetThread().GetThreadID(),
                "owner_tpidr": "0x%x" % owner,
                "identifier": gate_row["identifier"],
                "path": gate_row["path"],
                "completion_block": "0x%x" % completion,
                "completion_invoke_raw": "0x%x" % invoke_raw,
                "completion_invoke": "0x%x" %
                    module._user_pointer(invoke_raw),
                "request": "0x%x" % transport.SYNC_CALLS.get(
                    owner, {}).get("request", 0),
                "seeded_from_client_run_entry": True,
                "entry_capture": gate_row,
            }
            _emit("GATE_LIGHT_WRAPPER_RESULT_SEED", frame, {
                "identifier": gate_row["identifier"],
                "completion_block": "0x%x" % completion,
                "completion_invoke": "0x%x" %
                    module._user_pointer(invoke_raw),
                "thread": frame.GetThread().GetThreadID(),
                "owner_tpidr": "0x%x" % owner,
                "seeded": True,
                "reason": "deferred install occurred at shared CLIENT_RUN_ENTRY / wrapper entry PC",
            })
        # The lifecycle hook at CLIENT_RUN_ENTRY was installed only after
        # this entry breakpoint fired. Seed its per-owner state explicitly;
        # the next CLIENT_SYNC_CALL callback then follows the ordinary
        # post-copyin resolver path without relying on a missed entry hook.
        print("GATE_LIGHT_REPLY_JOIN_OWNER_SEEDED owner=0x%x id=%s "
              "receiver=0x%x completion=0x%x" % (
                  owner, gate_row["identifier"], receiver, completion),
              flush=True)
    except Exception as exc:
        _emit("GATE_LIGHT_REPLY_JOIN_INSTALL_FAILED", frame, {
            "error": repr(exc), "module_path": module_path,
            "identifier": gate_row["identifier"],
            "owner": gate_row["tpidr_el1"],
        })
        raise
    REPLY_JOIN_ARMED = True
    _emit("GATE_LIGHT_REPLY_JOIN_ARMED", frame, {
        "module_path": module_path,
        "identifier": gate_row["identifier"],
        "owner": gate_row["tpidr_el1"],
        "path": gate_row["path"],
        "arm_time": time.time(),
        "install": "dm_lock_checkin_wrapper_reply_callbacks.install",
    })


def on_hit(frame, bp_loc, _context):
    global SEEN_DM_MIGRATE_ENTRY, SEEN_CLIENT_RUN_ENTRY
    bp_id = bp_loc.GetBreakpoint().GetID()
    if bp_id in SPAWN_RETURN_META or META.get(bp_id, "").startswith("LAUNCHD_"):
        return _handle_spawn(frame, bp_id, META.get(bp_id, "DATAMIGRATOR_SPAWN_RETURN"))
    label = META.get(bp_id)
    if not label:
        return False
    process = frame.GetThread().GetProcess()
    progname = bks._progname(process)
    owner = bks._reg(frame, "tpidr_el1") or 0
    debugger = process.GetTarget().GetDebugger()

    if label == "DM_CLIENT_MIGRATE_ENTRY":
        if progname != "backboardd":
            return False
        row = _emit(label, frame, {
            "xpcproxy_spawn_samples": XPCPROXY_SPAWN_SAMPLES,
            "activation_spawn_samples": SPAWN_SAMPLES,
        })
        SEEN_DM_MIGRATE_ENTRY = True
        if SEEN_CLIENT_RUN_ENTRY:
            _emit("GATE_ACTIVATION_CHAIN_COMPLETE", frame, {
                "dm_entry_time": row["time"],
                "client_run_seen": True,
                "xpcproxy_spawn_samples": XPCPROXY_SPAWN_SAMPLES,
            })
        BPS[label].SetEnabled(False)
        return False

    if label == "CLIENT_RUN_ENTRY":
        if progname != "com.apple.datamigrator":
            return False
        path = lifecycle._nsstring(process, bks._reg(frame, "x2") or 0)
        identifier = lifecycle._nsstring(process, bks._reg(frame, "x3") or 0)
        if identifier != SYSTEMAPP_ID:
            return False
        row = _emit(label, frame, {
            "path": path,
            "identifier": identifier,
            "path_object": "0x%x" % (bks._reg(frame, "x2") or 0),
            "identifier_object": "0x%x" % (bks._reg(frame, "x3") or 0),
            "completion_block": "0x%x" % (bks._reg(frame, "x5") or 0),
        })
        SEEN_CLIENT_RUN_ENTRY = True
        if SEEN_DM_MIGRATE_ENTRY:
            _emit("GATE_ACTIVATION_CHAIN_COMPLETE", frame, {
                "client_run_time": row["time"],
                "dm_entry_seen": True,
                "xpcproxy_spawn_samples": XPCPROXY_SPAWN_SAMPLES,
            })
        OWNERS[owner] = {"path": path, "identifier": identifier,
                         "run_entry": row}
        _arm_reply_join(debugger, frame, row)
        if "CLIENT_SYNC_CALL" not in BPS:
            _dyn_install(debugger, "CLIENT_SYNC_CALL", CLIENT_SYNC_CALL)
        return False

    if label == "CLIENT_SYNC_CALL":
        entry = OWNERS.get(owner)
        if progname != "com.apple.datamigrator" or not entry:
            return False
        request = bks._reg(frame, "x21") or 0
        _emit(label, frame, {
            "identifier": entry["identifier"],
            "path": entry["path"],
            "run_entry_time": entry["run_entry"]["time"],
            "request": "0x%x" % request,
            "connection": "0x%x" % (bks._reg(frame, "x0") or 0),
            "message": "0x%x" % (bks._reg(frame, "x1") or 0),
            "message_bytes": bks._read(process, bks._reg(frame, "x1") or 0,
                                        0x100).hex(),
        })
        if "CLIENT_SYNC_RETURN" not in BPS:
            _dyn_install(debugger, "CLIENT_SYNC_RETURN", CLIENT_SYNC_RETURN)
        return False

    if label == "CLIENT_SYNC_RETURN":
        entry = OWNERS.get(owner)
        if progname != "com.apple.datamigrator" or not entry:
            return False
        _emit(label, frame, {
            "identifier": entry["identifier"], "path": entry["path"],
            "result_x0": "0x%x" % (bks._reg(frame, "x0") or 0),
        })
        BPS[label].SetEnabled(False)
    return False


def install(debugger, slide):
    global SLIDE, SPAWN_SAMPLES, XPCPROXY_SPAWN_SAMPLES
    global SEEN_DM_MIGRATE_ENTRY, SEEN_CLIENT_RUN_ENTRY
    SLIDE = int(slide)
    bks.SLIDE[0] = SLIDE
    lifecycle.bks.SLIDE[0] = SLIDE
    META.clear()
    BPS.clear()
    SUCCESS_LABELS.clear()
    SUCCESS_LABELS.update(filter(None, os.environ.get(
        "DVM_PROBE_SUCCESS_LABELS", "").split(",")))
    OWNERS.clear()
    REPLY_JOIN_ARMED = False
    SPAWN_RETURN_META.clear()
    SPAWN_SAMPLES = 0
    XPCPROXY_SPAWN_SAMPLES = 0
    SEEN_DM_MIGRATE_ENTRY = False
    SEEN_CLIENT_RUN_ENTRY = False
    # Arm the enclosing migration/check-in ladder from boot.  BKS's client
    # call begins before DataMigrator is launched, so installing these sites
    # only after CLIENT_SYNC_RETURN would miss the return edge.
    post_sync.install(debugger, SLIDE)
    _install(debugger, "DM_CLIENT_MIGRATE_ENTRY",
             SLIDE + DM_CLIENT_MIGRATE_ENTRY)
    _install(debugger, "CLIENT_RUN_ENTRY", SLIDE + CLIENT_RUN_ENTRY)
    _install(debugger, "LAUNCHD_POSIX_SPAWN", SLIDE + POSIX_SPAWN)
    _install(debugger, "LAUNCHD_POSIX_SPAWNP", SLIDE + POSIX_SPAWNP)
    _install(debugger, "LAUNCHD_EXECVE", SLIDE + EXECVE)
    mode_trace_armed = False
    if os.environ.get("DVM_DISPLAY_MODE_TRACE", "0") == "1":
        from pathlib import Path
        trace_path = Path(__file__).with_name(
            "dm_display_mode_trace_callbacks.py").resolve()
        result = __import__("lldb").SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "command script import %s" % trace_path, result)
        if not result.Succeeded():
            raise RuntimeError("display-mode trace import failed: %s" %
                               (result.GetError() or result.GetOutput()))
        trace = sys.modules.get("dm_display_mode_trace_callbacks")
        if trace is None:
            raise RuntimeError("display-mode trace module not registered")
        trace.install(debugger, SLIDE)
        mode_trace_armed = True
    mode_provider_trace_armed = False
    if os.environ.get("DVM_DISPLAY_MODE_PROVIDER_TRACE", "0") == "1":
        from pathlib import Path
        trace_path = Path(__file__).with_name(
            "render_gate_callbacks.py").resolve()
        result = __import__("lldb").SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "command script import %s" % trace_path, result)
        if not result.Succeeded():
            raise RuntimeError("mode-provider trace import failed: %s" %
                               (result.GetError() or result.GetOutput()))
        provider = sys.modules.get("render_gate_callbacks")
        if provider is None:
            raise RuntimeError("render-gate callback module not registered")
        provider.install_iomfb_mode_provider_only(debugger, SLIDE)
        mode_provider_trace_armed = True
    bks._write_event("ready.gate-light.json", {
        "probe": __name__, "slide": "0x%x" % SLIDE,
        "initial_breakpoints": sorted(BPS),
        "launch_observer": {
            "spawn_filter": "launchd plus executable/argv/env DataMigrator token or xpcproxy path",
            "execve_filter": "all processes when xpcproxy or DataMigrator token/path",
            "argv_envp": "bounded vector capture; environment values emitted only on token match",
            "entries": {name: "0x%x" % (SLIDE + offset) for name, offset in {
                "posix_spawn": POSIX_SPAWN,
                "posix_spawnp": POSIX_SPAWNP,
                "execve": EXECVE,
            }.items()},
        },
        "purpose": "capture DataMigrator XPC activation through DM and SystemApp migration gates",
        "display_mode_provider_trace": mode_provider_trace_armed,
        "generic_xpcproxy_spawn_log_cap": 256,
        "chain_success_label": "GATE_ACTIVATION_CHAIN_COMPLETE (both DM_CLIENT_MIGRATE_ENTRY and CLIENT_RUN_ENTRY)",
        "deferred_reply_join": os.environ.get("DVM_GATE_LIGHT_ARM_REPLY_AFTER_SYSTEMAPP") == "1",
        "display_mode_trace": mode_trace_armed,
        "time": time.time(),
    })
    if os.environ.get("DVM_NATIVE_INPUT_BOOTSTRAP") == "1":
        # The QEMU native-input transport has no guest consumer until the
        # trusted /usr/local/libexec/dvm-input helper is launched. Arm its
        # established launchd-only bootstrap at reset, before the first
        # launchd sendto return; never spawn it from SpringBoard or a sandboxed
        # daemon. This is optional so ordinary gate-light probes stay inert.
        from pathlib import Path
        helper_path = Path(__file__).resolve().parents[1] / "input" / "bootstrap_helper.py"
        command_result = __import__("lldb").SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "command script import %s" % helper_path, command_result)
        if not command_result.Succeeded():
            raise RuntimeError("native input bootstrap import failed: %s" %
                               (command_result.GetError() or command_result.GetOutput()))
        helper = sys.modules.get("bootstrap_helper")
        if helper is None:
            raise RuntimeError("native input bootstrap module was not registered")
        helper.install(debugger, SLIDE, command="helper", process="launchd")
        bks._write_event("ready.native-input-bootstrap.json", {
            "module": str(helper_path), "slide": "0x%x" % SLIDE,
            "process": "launchd", "command": "helper",
            "time": time.time(),
        })
        print("DM_NATIVE_INPUT_BOOTSTRAP_READY process=launchd slide=0x%x" %
              SLIDE, flush=True)
    print("DM_GATE_LIGHT_READY slide=0x%x initial_bps=%d "
          "activation=spawn-argv+xpcproxy-exec; sync/reply dynamically armed" %
          (SLIDE, len(BPS)), flush=True)

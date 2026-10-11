"""Resume a frozen post-sync observation using captured same-boot call state.

The primary gate probe can leave QEMU alive after LLDB exits on an unexpected
trace stop.  This thin continuation re-arms only the migration/check-in return
ladder and the matched SystemApp sync return, seeding entry state from that
same boot's event directory.  It does not infer that any missing return ran.
"""

import json
import os
import time

import lldb

import bks_checkin_callbacks as bks
import dm_post_sync_ladder_callbacks as ladder

SYSTEMAPP_SYNC_RETURN = 0x25E8F1F8C
META = {}
EVENT_DIR = ""
SYSTEMAPP_OWNER = None


def _read_event(directory, name):
    try:
        with open(os.path.join(directory, name), "r") as stream:
            return json.load(stream)
    except (OSError, ValueError):
        return None


def _seed_ladder(directory):
    """Restore only call-entry correlation captured in this same boot."""
    entry = _read_event(directory, "progress.DM_PERFORM_ENTRY.json")
    if entry:
        key = (int(entry.get("pid", 0)),
               int(entry.get("tpidr_el1", "0"), 0))
        ladder.DM_ACTIVE[key] = [{
            "entry_time": float(entry.get("time", time.time())),
            "thread": entry.get("thread"),
            "entry_pc": entry.get("pc"),
        }]

    shell = _read_event(directory, "progress.SHELL_SERVICE_CALL.json")
    if shell:
        key = (int(shell.get("pid", 0)),
               int(shell.get("tpidr_el1", "0"), 0))
        ladder.SHELL_CALLS[key] = {
            "time": float(shell.get("time", time.time())),
            "thread": shell.get("thread"),
            "tpidr_el1": shell.get("tpidr_el1"),
        }

    migration = _read_event(
        directory, "progress.BKS_MIGRATION_CHECKIN_CALL.json")
    if migration:
        key = (int(migration.get("pid", 0)),
               int(migration.get("tpidr_el1", "0"), 0))
        ladder.MIGRATION_CALLS[key] = {
            "time": float(migration.get("time", time.time())),
            "thread": migration.get("thread"),
            "tpidr_el1": migration.get("tpidr_el1"),
            "shell_call_time": (shell or {}).get("time"),
            "shell_call_thread": (shell or {}).get("thread"),
            "shell_call_tpidr_el1": (shell or {}).get("tpidr_el1"),
        }
    print("DM_POST_SYNC_RESUME_SEEDED " + json.dumps({
        "dm_perform_entry": bool(entry),
        "shell_service_call": bool(shell),
        "migration_checkin_call": bool(migration),
        "same_shell_checkin_tpidr": bool(
            shell and migration and
            shell.get("tpidr_el1") == migration.get("tpidr_el1")),
    }, sort_keys=True), flush=True)


def on_sync_return(frame, bp_loc, _context):
    process = frame.GetThread().GetProcess()
    if bks._progname(process) != "com.apple.datamigrator":
        return False
    owner = bks._reg(frame, "tpidr_el1") or 0
    if SYSTEMAPP_OWNER is None or owner != SYSTEMAPP_OWNER:
        return False
    request_row = _read_event(EVENT_DIR, "progress.CLIENT_SYNC_CALL.json") or {}
    row = {
        "label": "CLIENT_SYNC_RETURN",
        "time": time.time(),
        "progname": bks._progname(process),
        "pid": process.GetProcessID(),
        "thread": frame.GetThread().GetThreadID(),
        "tpidr_el1": "0x%x" % owner,
        "pc": "0x%x" % (bks._reg(frame, "pc") or 0),
        "result_x0": "0x%x" % (bks._reg(frame, "x0") or 0),
        "same_boot_request": request_row.get("request"),
    }
    bks._write_event("progress.CLIENT_SYNC_RETURN.json", row)
    bks._write_event("success.PLUGIN_SYNC_RETURN.json", row)
    print("DM_POST_SYNC_RESUME " + json.dumps(row, sort_keys=True), flush=True)
    return False


def _arm_sync_return(debugger, slide):
    target = debugger.GetSelectedTarget()
    address = int(slide) + SYSTEMAPP_SYNC_RETURN
    bp = target.BreakpointCreateByAddress(address)
    if not bp.IsValid():
        raise RuntimeError("failed to arm matched CLIENT_SYNC_RETURN")
    bp.SetScriptCallbackFunction(__name__ + ".on_sync_return")
    META[bp.GetID()] = "CLIENT_SYNC_RETURN"
    print("DM_POST_SYNC_RESUME_BP label=CLIENT_SYNC_RETURN address=0x%x" %
          address, flush=True)


def install(debugger, slide):
    global EVENT_DIR, SYSTEMAPP_OWNER
    EVENT_DIR = os.environ.get("DVM_PROBE_EVENT_DIR", "")
    armed = _read_event(EVENT_DIR, "progress.GATE_LIGHT_REPLY_JOIN_ARMED.json")
    if not armed or not armed.get("owner"):
        raise RuntimeError("same-boot SystemApp owner event is missing")
    SYSTEMAPP_OWNER = int(armed["owner"], 0)
    bks.SLIDE[0] = int(slide)
    ladder.install(debugger, int(slide))
    _seed_ladder(EVENT_DIR)
    _arm_sync_return(debugger, int(slide))
    bks._write_event("ready.post-sync-resume.json", {
        "slide": "0x%x" % int(slide),
        "systemapp_owner": "0x%x" % SYSTEMAPP_OWNER,
        "source_events": [
            "progress.DM_PERFORM_ENTRY.json",
            "progress.SHELL_SERVICE_CALL.json",
            "progress.BKS_MIGRATION_CHECKIN_CALL.json",
        ],
        "scope": "same-boot returns only; no retroactive event claims",
        "time": time.time(),
    })
    print("DM_POST_SYNC_RESUME_READY slide=0x%x owner=0x%x" %
          (int(slide), SYSTEMAPP_OWNER), flush=True)

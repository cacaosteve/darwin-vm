"""Sparse, read-only progress markers for Passbook and SpringBoard migration.

The request hooks identify the two DataMigrator RPCs. Repeated SplashBoard
snapshot-generation entries are a work-rate marker, while the PassKit entry
marks the live Passbook implementation. Neither marker alone proves completion;
the paired client returns and later saved stacks settle that question.
"""

import json
import os
import time

import lldb

import bks_checkin_callbacks as bks
from dm_plugin_lifecycle_callbacks import _nsstring, _xpc_fields, _cstring, _read


SITES = (
    (0x25E8F1A10, "CLIENT_RUN_ENTRY"),
    (0x25E8F1F88, "CLIENT_SYNC_CALL"),
    (0x25E8F1F8C, "CLIENT_SYNC_RETURN"),
    (0x2AC02CB78, "SB_SNAPSHOT_GENERATE"),
    (0x1845A4AF0, "SB_CA_SNAPSHOTS"),
    (0x1A46EEA40, "PASSBOOK_MIGRATE_ENTRY"),
    (0x25E8F044C, "DM_PERFORM_RETURN"),
    (0x18A147774, "BKS_CHECKIN_RETURN"),
)
KERNEL_EXIT = 0xFFFFFFF00AFF6BD8 + int(os.environ.get("DVM_KERNEL_SLIDE", "0x20000000"), 0)
META = {}
OWNERS = {}
COUNTS = {}
TARGET_IDS = {"com.apple.PassbookDataMigrator", "com.apple.sbmigrator"}


def _emit(label, frame, extra=None):
    row = {"label": label, "time": time.time(),
           "thread": frame.GetThread().GetThreadID(),
           "tpidr_el1": "0x%x" % bks._reg(frame, "tpidr_el1")}
    row.update(extra or {})
    print("DM_PLUGIN_PROGRESS " + json.dumps(row, sort_keys=True), flush=True)


def on_hit(frame, bp_loc, _context):
    process = frame.GetThread().GetProcess()
    label = META[bp_loc.GetBreakpoint().GetID()]
    owner = bks._reg(frame, "tpidr_el1")
    if label == "WRAPPER_KERNEL_EXIT":
        proc = bks._reg(frame, "x0")
        if _cstring(process, proc + 0x55c, 32) != "com.apple.migrat":
            return False
        _emit(label, frame, {"proc": "0x%x" % proc,
                             "pid": int.from_bytes(_read(process, proc + 0x60, 4), "little"),
                             "exit_args": "0x%x" % bks._reg(frame, "x1")})
        return False
    name = bks._progname(process)
    if label.startswith("CLIENT_") and name != "com.apple.datamigrator":
        return False
    if label.startswith(("SB_", "PASSBOOK_")) and name != "com.apple.migrationpluginwrapper":
        return False
    if label == "DM_PERFORM_RETURN" and name != "backboardd":
        return False
    if label == "BKS_CHECKIN_RETURN" and name != "SpringBoard":
        return False
    if label == "CLIENT_RUN_ENTRY":
        path = _nsstring(process, bks._reg(frame, "x2"))
        identifier = _nsstring(process, bks._reg(frame, "x3"))
        if identifier not in TARGET_IDS:
            return False
        OWNERS[owner] = {"path": path, "identifier": identifier}
        _emit(label, frame, OWNERS[owner])
        return False
    if label in ("CLIENT_SYNC_CALL", "CLIENT_SYNC_RETURN"):
        item = OWNERS.get(owner)
        if not item:
            return False
        extra = dict(item)
        if label == "CLIENT_SYNC_CALL":
            request = bks._reg(frame, "x21")
            extra.update(request="0x%x" % request,
                         fields=_xpc_fields(process, request))
        else:
            extra["result"] = "0x%x" % bks._reg(frame, "x0")
            OWNERS.pop(owner, None)
        _emit(label, frame, extra)
        return False
    if label in ("SB_SNAPSHOT_GENERATE", "SB_CA_SNAPSHOTS", "PASSBOOK_MIGRATE_ENTRY"):
        count = COUNTS.get(label, 0) + 1
        COUNTS[label] = count
        if count <= 5 or count % 10 == 0:
            _emit(label, frame, {"count": count, "receiver": "0x%x" % bks._reg(frame, "x0")})
        return False
    _emit(label, frame, {"result": "0x%x" % bks._reg(frame, "x0")})
    return False


def _install_one(debugger, address, label):
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint set --address 0x%x" % address, result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed to arm %s: %s" % (label, result.GetError()))
    bp = target.GetBreakpointAtIndex(before)
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint command add -F dm_plugin_progress_callbacks.on_hit %d" % bp.GetID(), result)
    if not result.Succeeded():
        raise RuntimeError("failed callback %s: %s" % (label, result.GetError()))
    META[bp.GetID()] = label
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(), result)
    proof = result.GetOutput() or result.GetError()
    if "dm_plugin_progress_callbacks.on_hit" not in proof:
        raise RuntimeError("missing command proof %s: %s" % (label, proof))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" % (bp.GetID(), label, address), flush=True)


def install(debugger, slide):
    bks.SLIDE[0] = int(slide)
    for static, label in SITES:
        _install_one(debugger, slide + static, label)
    _install_one(debugger, KERNEL_EXIT, "WRAPPER_KERNEL_EXIT")
    print("DM_PLUGIN_PROGRESS_READY slide=0x%x sites=%d" % (slide, len(SITES) + 1), flush=True)

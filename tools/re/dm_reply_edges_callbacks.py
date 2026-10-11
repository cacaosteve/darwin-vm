"""Read-only CA snapshot and passd/CoreLocation reply brackets.

The CA Mach call is reached from the matched SpringBoard migrator's snapshot
generator. The CoreLocation call is reached from passd during the matched
Passbook request. Track their client-side returns before attributing silence
to a server handler or a guest graphics dependency.
"""

import json
import os
import time

import lldb

import bks_checkin_callbacks as bks
from dm_plugin_lifecycle_callbacks import _nsstring, _xpc_fields, _cstring, _read


TARGET_IDS = {"com.apple.PassbookDataMigrator", "com.apple.sbmigrator"}
SITES = (
    (0x25E8F1A10, "CLIENT_RUN_ENTRY"),
    (0x25E8F1F88, "CLIENT_SYNC_CALL"),
    (0x25E8F1F8C, "CLIENT_SYNC_RETURN"),
    (0x2AC02CB78, "SB_SNAPSHOT_GENERATE"),
    (0x1845A4AF0, "CA_CREATE_ENTRY"),
    (0x1845A509C, "CA_MACH_CALL"),
    (0x1845A50A0, "CA_MACH_RETURN"),
    (0x1845A484C, "CA_RENDER_RETURN"),
    (0x1845A4D18, "CA_CREATE_RETURN"),
    (0x1A783200C, "PASSD_CL_CALL"),
    (0x1A7832010, "PASSD_CL_RETURN"),
    (0x25E8F044C, "DM_PERFORM_RETURN"),
    (0x18A147774, "BKS_CHECKIN_RETURN"),
)
KERNEL_EXIT = 0xFFFFFFF00AFF6BD8 + int(os.environ.get("DVM_KERNEL_SLIDE", "0x20000000"), 0)
META = {}
CLIENT_OWNERS = {}
SB_THREADS = {}
CA_CALLS = {}
CL_CALLS = {}
SB_REQUEST = False
PASSBOOK_REQUEST = False


def _u32(process, address):
    raw = _read(process, address, 4)
    return int.from_bytes(raw, "little") if len(raw) == 4 else None


def _event(label, frame, extra=None):
    row = {"label": label, "time": time.time(),
           "thread": frame.GetThread().GetThreadID(),
           "tpidr_el1": "0x%x" % bks._reg(frame, "tpidr_el1")}
    row.update(extra or {})
    print("DM_REPLY_EDGE " + json.dumps(row, sort_keys=True), flush=True)


def on_hit(frame, bp_loc, _context):
    global SB_REQUEST, PASSBOOK_REQUEST
    process = frame.GetThread().GetProcess()
    label = META[bp_loc.GetBreakpoint().GetID()]
    owner = bks._reg(frame, "tpidr_el1")
    if label == "WRAPPER_KERNEL_EXIT":
        proc = bks._reg(frame, "x0")
        if _cstring(process, proc + 0x55c, 32) == "com.apple.migrat":
            _event(label, frame, {"pid": _u32(process, proc + 0x60), "proc": "0x%x" % proc})
        return False
    name = bks._progname(process)
    if label.startswith("CLIENT_") and name != "com.apple.datamigrator":
        return False
    if (label.startswith("SB_") or label.startswith("CA_")) and name != "com.apple.migrationpluginwrapper":
        return False
    if label.startswith("PASSD_") and name != "passd":
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
        CLIENT_OWNERS[owner] = {"path": path, "identifier": identifier}
        _event(label, frame, CLIENT_OWNERS[owner])
        return False
    if label in ("CLIENT_SYNC_CALL", "CLIENT_SYNC_RETURN"):
        item = CLIENT_OWNERS.get(owner)
        if not item:
            return False
        if label == "CLIENT_SYNC_CALL":
            SB_REQUEST |= item["identifier"] == "com.apple.sbmigrator"
            PASSBOOK_REQUEST |= item["identifier"] == "com.apple.PassbookDataMigrator"
            request = bks._reg(frame, "x21")
            _event(label, frame, {**item, "request": "0x%x" % request,
                                  "fields": _xpc_fields(process, request)})
        else:
            _event(label, frame, {**item, "result": "0x%x" % bks._reg(frame, "x0")})
            CLIENT_OWNERS.pop(owner, None)
        return False
    if label == "SB_SNAPSHOT_GENERATE":
        if SB_REQUEST:
            SB_THREADS[owner] = SB_THREADS.get(owner, 0) + 1
            _event(label, frame, {"count_on_thread": SB_THREADS[owner],
                                  "receiver": "0x%x" % bks._reg(frame, "x0")})
        return False
    if label.startswith("CA_"):
        if owner not in SB_THREADS:
            return False
        if label == "CA_MACH_CALL":
            msg = bks._reg(frame, "x0")
            call = {"time": time.time(), "message": "0x%x" % msg,
                    "remote_port": _u32(process, msg + 8),
                    "reply_port": _u32(process, msg + 12),
                    "message_id": _u32(process, msg + 20),
                    "options": bks._reg(frame, "x1"),
                    "send_size": bks._reg(frame, "x2"),
                    "reply_size": bks._reg(frame, "x3"),
                    "timeout": bks._reg(frame, "x5")}
            CA_CALLS[owner] = call
            _event(label, frame, call)
        elif label == "CA_MACH_RETURN":
            call = CA_CALLS.pop(owner, None)
            _event(label, frame, {"mach_result": "0x%x" % bks._reg(frame, "x0"),
                                  "elapsed": time.time() - call["time"] if call else None,
                                  "message_id": call["message_id"] if call else None})
        else:
            _event(label, frame, {"result": "0x%x" % bks._reg(frame, "x0")})
        return False
    if label == "PASSD_CL_CALL":
        if PASSBOOK_REQUEST:
            CL_CALLS[owner] = time.time()
            _event(label, frame, {"receiver": "0x%x" % bks._reg(frame, "x0"),
                                  "selector": "getAuthorizationStatusForBundleID:orBundlePath:replyBlock:",
                                  "selector_or_arg": "0x%x" % bks._reg(frame, "x1"),
                                  "bundle_or_arg": "0x%x" % bks._reg(frame, "x2")})
        return False
    if label == "PASSD_CL_RETURN":
        call = CL_CALLS.pop(owner, None)
        if call is not None:
            _event(label, frame, {"result": "0x%x" % bks._reg(frame, "x0"),
                                  "elapsed": time.time() - call})
        return False
    _event(label, frame, {"result": "0x%x" % bks._reg(frame, "x0")})
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
    interpreter.HandleCommand("breakpoint command add -F dm_reply_edges_callbacks.on_hit %d" % bp.GetID(), result)
    if not result.Succeeded():
        raise RuntimeError("failed callback %s: %s" % (label, result.GetError()))
    META[bp.GetID()] = label
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(), result)
    proof = result.GetOutput() or result.GetError()
    if "dm_reply_edges_callbacks.on_hit" not in proof:
        raise RuntimeError("missing command proof %s: %s" % (label, proof))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" % (bp.GetID(), label, address), flush=True)


def install(debugger, slide):
    bks.SLIDE[0] = int(slide)
    for static, label in SITES:
        _install_one(debugger, slide + static, label)
    _install_one(debugger, KERNEL_EXIT, "WRAPPER_KERNEL_EXIT")
    print("DM_REPLY_EDGE_READY slide=0x%x sites=%d" % (slide, len(SITES) + 1), flush=True)

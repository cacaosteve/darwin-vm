"""Read-only SpringBoard migrator CA request vs render-server receive probe.

Record the client's synchronous 0x9d20 send, CA::Render::Server receive
headers in backboardd, the matching MIG handler, and passd's CoreLocation
authorization call. Port names are task-local: correlate by message ID and
timing, not by comparing numeric port names across processes.
"""

import json
import time

import lldb

import bks_checkin_callbacks as bks
from dm_plugin_lifecycle_callbacks import _read


SITES = (
    (0x2AC02CB78, "SB_SNAPSHOT_GENERATE"),
    (0x1845A509C, "CA_MACH_CALL"),
    (0x1845A50A0, "CA_MACH_RETURN"),
    (0x1843F9664, "SERVER_THREAD_ENTRY"),
    (0x1843F9804, "SERVER_RECEIVE_RETURN"),
    (0x1845A51D0, "SERVER_HANDLER_ENTRY"),
    (0x1847574EC, "OOP_OBSERVATION_ENTRY"),
    (0x1A783200C, "PASSD_CL_CALL"),
    (0x1A7832010, "PASSD_CL_RETURN"),
)
META = {}
SNAPSHOT_THREADS = set()
CA_CALLS = {}
HANDLER_THREADS = {}
CL_CALLS = {}
RECEIVE_COUNT = 0
RECEIVE_IDS = {}
PASSD_COUNT = 0


def _u32(process, address):
    raw = _read(process, address, 4)
    return int.from_bytes(raw, "little") if len(raw) == 4 else None


def _event(label, frame, extra=None):
    row = {"label": label, "time": time.time(),
           "thread": frame.GetThread().GetThreadID(),
           "tpidr_el1": "0x%x" % bks._reg(frame, "tpidr_el1")}
    row.update(extra or {})
    print("DM_CA_SERVER " + json.dumps(row, sort_keys=True), flush=True)


def on_hit(frame, bp_loc, _context):
    global RECEIVE_COUNT, PASSD_COUNT
    process = frame.GetThread().GetProcess()
    name = bks._progname(process)
    label = META[bp_loc.GetBreakpoint().GetID()]
    owner = bks._reg(frame, "tpidr_el1")
    if label in ("SB_SNAPSHOT_GENERATE", "CA_MACH_CALL", "CA_MACH_RETURN"):
        if name != "com.apple.migrationpluginwrapper":
            return False
        if label == "SB_SNAPSHOT_GENERATE":
            SNAPSHOT_THREADS.add(owner)
            _event(label, frame, {"receiver": "0x%x" % bks._reg(frame, "x0")})
            return False
        if owner not in SNAPSHOT_THREADS:
            return False
        if label == "CA_MACH_CALL":
            msg = bks._reg(frame, "x0")
            call = {"message_id": _u32(process, msg + 20),
                    "remote_port": _u32(process, msg + 8),
                    "reply_port": _u32(process, msg + 12),
                    "time": time.time()}
            CA_CALLS[owner] = call
            _event(label, frame, call)
        else:
            call = CA_CALLS.pop(owner, None)
            _event(label, frame, {"result": "0x%x" % bks._reg(frame, "x0"),
                                  "message_id": call["message_id"] if call else None,
                                  "elapsed": time.time() - call["time"] if call else None})
        return False
    if label in ("SERVER_THREAD_ENTRY", "SERVER_RECEIVE_RETURN", "SERVER_HANDLER_ENTRY", "OOP_OBSERVATION_ENTRY", "SERVER_HANDLER_RETURN"):
        if name != "backboardd":
            return False
        if label == "SERVER_THREAD_ENTRY":
            _event(label, frame)
        elif label == "SERVER_RECEIVE_RETURN":
            RECEIVE_COUNT += 1
            sp = bks._reg(frame, "sp")
            msg_id = _u32(process, sp + 0x50 + 20)
            if msg_id is not None:
                RECEIVE_IDS[msg_id] = RECEIVE_IDS.get(msg_id, 0) + 1
            if msg_id == 40224 or RECEIVE_COUNT <= 12 or RECEIVE_COUNT % 100 == 0:
                _event(label, frame, {"mach_result": "0x%x" % bks._reg(frame, "x0"),
                                      "message_id": msg_id,
                                      "message_size": _u32(process, sp + 0x54),
                                      "remote_port": _u32(process, sp + 0x58),
                                      "local_port": _u32(process, sp + 0x5c),
                                      "total_receives": RECEIVE_COUNT,
                                      "target_receives": RECEIVE_IDS.get(40224, 0)})
        elif label == "SERVER_HANDLER_ENTRY":
            msg = bks._reg(frame, "x0")
            msg_id = _u32(process, msg + 20)
            if msg_id != 40224:
                return False
            HANDLER_THREADS[owner] = {"time": time.time(), "msg": msg}
            _event(label, frame, {"message": "0x%x" % msg,
                                  "message_id": msg_id,
                                  "reply_port": _u32(process, msg + 8)})
            lr = bks._reg(frame, "lr") & 0x0000ffffffffffff
            target = process.GetTarget()
            bp = target.BreakpointCreateByAddress(lr)
            bp.SetScriptCallbackFunction("dm_ca_server_receive_callbacks.on_hit")
            META[bp.GetID()] = "SERVER_HANDLER_RETURN"
            print("DYNAMIC_PROOF id=%d label=SERVER_HANDLER_RETURN address=0x%x" % (bp.GetID(), lr), flush=True)
        elif label == "SERVER_HANDLER_RETURN":
            call = HANDLER_THREADS.pop(owner, None)
            if call:
                _event(label, frame, {"result": "0x%x" % bks._reg(frame, "x0"),
                                      "elapsed": time.time() - call["time"]})
                process.GetTarget().BreakpointDelete(bp_loc.GetBreakpoint().GetID())
        else:
            _event(label, frame)
        return False
    if label in ("PASSD_CL_CALL", "PASSD_CL_RETURN"):
        if name != "passd":
            return False
        if label == "PASSD_CL_CALL":
            PASSD_COUNT += 1
            CL_CALLS[owner] = time.time()
            if PASSD_COUNT <= 20:
                _event(label, frame, {"selector": "getAuthorizationStatusForBundleID:orBundlePath:replyBlock:",
                                      "receiver": "0x%x" % bks._reg(frame, "x0"),
                                      "count": PASSD_COUNT})
        else:
            call = CL_CALLS.pop(owner, None)
            if call is not None and PASSD_COUNT <= 20:
                _event(label, frame, {"result": "0x%x" % bks._reg(frame, "x0"),
                                      "elapsed": time.time() - call})
        return False
    return False


def install(debugger, slide):
    bks.SLIDE[0] = int(slide)
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    for static, label in SITES:
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % (slide + static), result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed to arm %s: %s" % (label, result.GetError()))
        bp = target.GetBreakpointAtIndex(before)
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command add -F dm_ca_server_receive_callbacks.on_hit %d" % bp.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" % (label, result.GetError()))
        META[bp.GetID()] = label
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(), result)
        if "dm_ca_server_receive_callbacks.on_hit" not in (result.GetOutput() or result.GetError()):
            raise RuntimeError("missing command proof for %s" % label)
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" % (bp.GetID(), label, slide + static), flush=True)
    print("DM_CA_SERVER_READY slide=0x%x sites=%d" % (slide, len(SITES)), flush=True)

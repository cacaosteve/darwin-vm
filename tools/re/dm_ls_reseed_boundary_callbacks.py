"""Read-only 00LaunchServicesMigrator client and lsd reseed boundary.

All shared-cache addresses are unslid.  The client is keyed by its first
blocking-store thread, while lsd stages are counted separately.  Counts are
progress evidence, not a claim that a particular server item answered the
client request.
"""

import json
import os
import time

import lldb

import bks_checkin_callbacks as bks


CURRENT_PID = 0x26FD74008
SEEDING_COUNT = 0x1E40E4330
SITES = (
    (0x22A3912B4, "SWAP_WAIT_ENTRY", "any"),
    (0x22A391300, "SWAP_WAIT_RETURN", "any"),
    (0x25E8F02D0, "DM_PERFORM_ENTRY", "backboardd"),
    (0x25E8F044C, "DM_PERFORM_RETURN", "backboardd"),
    (0x25E8F5798, "DM_XPC_SYNC_CALL", "backboardd"),
    (0x25E8F579C, "DM_XPC_SYNC_RETURN", "backboardd"),
    (0x186DF21C8, "LS_STORE_ENTRY", "wrapper"),
    (0x186DF2688, "LS_BLOCKING_SELECTOR_CALL", "wrapper"),
    (0x186DF268C, "LS_BLOCKING_SELECTOR_RETURN", "wrapper"),
    (0x186EF62B8, "LS_BLOCKING_SEND", "wrapper"),
    (0x186EF62BC, "LS_BLOCKING_RETURN", "wrapper"),
    (0x186DC8E54, "LS_STORE_RETURN", "wrapper"),
    (0x186F145C4, "LSD_BLOCKING_HANDLER_ENTRY", "lsd"),
    (0x186F14630, "LSD_SEED_WAIT_CALL", "lsd"),
    (0x186F14634, "LSD_SEED_WAIT_RETURN", "lsd"),
    (0x186DF1E04, "LSD_GET_STORE_ENTRY", "lsd"),
    (0x186DF1F8C, "LSD_SEEDED_COMPLETION", "lsd"),
    (0x186DF2020, "LSD_ERROR_COMPLETION", "lsd"),
    (0x186EB4D80, "LSD_RECOVERY_PATH", "lsd"),
    (0x186EB4D98, "LSD_RECOVERY_EXISTS", "lsd"),
    (0x186EC1E84, "LSD_REBUILD_REASON", "lsd"),
    (0x186E75F0C, "LSD_SYSTEM_SCAN_CALL", "lsd"),
    (0x186E75F10, "LSD_SYSTEM_SCAN_RETURN", "lsd"),
    (0x186F81090, "LSD_SCAN_DRIVER_CALL", "lsd"),
    (0x186F81094, "LSD_SCAN_DRIVER_RETURN", "lsd"),
    (0x186F80DA0, "LSD_REGISTER_ENTRY", "lsd"),
    (0x186F80DEC, "LSD_REGISTER_RETURN", "lsd"),
    (0x186EC1FF0, "LSD_SEED_SYNCWRITE_CALL", "lsd"),
    (0x186EC1FF4, "LSD_SEED_SYNCWRITE_RETURN", "lsd"),
    (0x186EC1E6C, "LSD_BUILDER_SYNC_RETURN", "lsd"),
    (0x186EB800C, "LSD_SEED_COMPLETE_ENTRY", "lsd"),
    (0x186EB801C, "LSD_SEED_COMPLETE_AFTER_DEC", "lsd"),
)
_SITE_FILTER = set(filter(None, os.environ.get(
    "DVM_LS_RESEED_LABELS", "").split(",")))
META = {}
COUNTS = {}
CLIENT_THREAD = None
DM_THREAD = None
WAIT_START = {}
REGISTER_START = {}


def _u32(process, address):
    raw = bks._read(process, address, 4) if address else b""
    return int.from_bytes(raw, "little") if len(raw) == 4 else None


def _match(name, expected):
    if expected == "any":
        return True
    if expected == "wrapper":
        return "migrationpluginwrapper" in name
    return name == expected


def on_hit(frame, bp_loc, _context):
    global CLIENT_THREAD, DM_THREAD
    label, expected = META[bp_loc.GetBreakpoint().GetID()]
    process = frame.GetThread().GetProcess()
    name = bks._progname(process)
    if not _match(name, expected):
        return False
    thread = bks._reg(frame, "tpidr_el1")
    if label == "DM_PERFORM_ENTRY":
        DM_THREAD = thread
    elif label.startswith("DM_") and thread != DM_THREAD:
        return False
    if label == "LS_BLOCKING_SEND" and CLIENT_THREAD is None:
        CLIENT_THREAD = thread
    elif label.startswith("LS_") and CLIENT_THREAD is not None and thread != CLIENT_THREAD:
        return False
    count = COUNTS[label] = COUNTS.get(label, 0) + 1
    now = time.time()
    if label == "SWAP_WAIT_ENTRY":
        fence, options = bks._reg(frame, "w1"), bks._reg(frame, "w2")
        if fence == 1 and options == 1:
            WAIT_START[thread] = now
        else:
            return False
    elif label == "SWAP_WAIT_RETURN":
        if thread not in WAIT_START:
            return False
    elif label == "LSD_REGISTER_ENTRY":
        REGISTER_START[thread] = (now, bks._reg(frame, "x2"))
    elif label == "LSD_REGISTER_RETURN":
        if thread not in REGISTER_START:
            return False
    # Keep item-pair capture low volume but retain periodic progress through a
    # long first-boot scan. Other labels are infrequent and all are printed.
    if label.startswith("LSD_REGISTER_") and count > 8 and count % 50:
        return False
    row = {"label": label, "time": now, "process": name, "thread": thread,
           "count": count, "x0": bks._reg(frame, "x0"),
           "x1": bks._reg(frame, "x1"), "x2": bks._reg(frame, "x2")}
    if label == "LSD_REBUILD_REASON":
        raw = bks._read(process, row["x0"] + 0x28, 2)
        row["reason_flags"] = int.from_bytes(raw, "little") if len(raw) == 2 else None
    if label in ("LSD_RECOVERY_PATH", "LSD_RECOVERY_EXISTS"):
        path = bks._read(process, bks._reg(frame, "sp") + 8, 0x400)
        row["path"] = path.split(b"\0", 1)[0].decode("utf-8", "replace")
        if label == "LSD_RECOVERY_EXISTS":
            row["exists"] = bks._reg(frame, "w0")
    if label.startswith("LSD_SEED_") or label == "LSD_BUILDER_SYNC_RETURN":
        row["seeding_count"] = _u32(process, bks.SLIDE[0] + SEEDING_COUNT)
    if label == "LS_BLOCKING_SEND":
        row["client_pid"] = _u32(process, bks.SLIDE[0] + CURRENT_PID)
    if label == "LSD_BLOCKING_HANDLER_ENTRY":
        receiver = row["x0"]
        nsxpc = bks._u64(process, receiver + 8) if receiver else None
        state = _u32(process, nsxpc + 0x24) if nsxpc else None
        xpc = bks._u64(process, nsxpc + 8) if nsxpc else None
        row["peer_pid"] = (_u32(process, xpc + 0x7c)
                           if xpc and state is not None and not state & (1 << 6)
                           else None)
    if label == "SWAP_WAIT_RETURN":
        row["elapsed"] = now - WAIT_START.pop(thread)
        row["result"] = bks._reg(frame, "w0")
    if label == "LSD_REGISTER_RETURN":
        start, item = REGISTER_START.pop(thread)
        row.update(elapsed=now-start, item=item)
    if label.startswith("LS_") or label.startswith("LSD_BLOCKING_"):
        t = frame.GetThread()
        row["stack"] = [t.GetFrameAtIndex(i).GetPC()
                        for i in range(min(8, t.GetNumFrames()))]
    print("DM_LS_RESEED_JSON " + json.dumps(row, sort_keys=True), flush=True)
    return False


def install(debugger, slide):
    global CLIENT_THREAD, DM_THREAD
    CLIENT_THREAD = DM_THREAD = None
    bks.SLIDE[0] = int(slide)
    META.clear()
    COUNTS.clear()
    WAIT_START.clear()
    REGISTER_START.clear()
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    for static, label, expected in SITES:
        if _SITE_FILTER and label not in _SITE_FILTER:
            continue
        address = static + int(slide)
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % address, result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed %s: %s" % (label, result.GetError()))
        bp = target.GetBreakpointAtIndex(before)
        META[bp.GetID()] = label, expected
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F dm_ls_reseed_boundary_callbacks.on_hit %d" % bp.GetID(),
            result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" % (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (bp.GetID(), label, address), flush=True)
    print("DM_LS_RESEED_READY sites=%d" % len(SITES), flush=True)

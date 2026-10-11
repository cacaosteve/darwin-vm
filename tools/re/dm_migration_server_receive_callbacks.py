"""Read-only client-send versus DataMigrator DMXPC message-handler probe.

Arms server and client-return sites only after backboardd enters the matched
msgID-0 migration send. A server handler hit is a receive-path checkpoint;
without decoding the dictionary it is not yet proof of this exact request.
"""

import json
import time

import bks_checkin_callbacks as bks
import dm_perform_roundtrip_callbacks as common


OWNER = None
SERVER_OWNER = None
BPS = {}
ADDR = {
    "DM_CLIENT_MIGRATE_ENTRY": 0x25E8F4AF8,
    "DM_CLIENT_SYNC_CALL": 0x25E8F5798,
    "DM_CLIENT_SYNC_RETURN": 0x25E8F579C,
    "DM_SERVER_HANDLE_ENTRY": 0x25E8EF374,
    "DM_SERVER_HANDLE_RETURN": 0x25E8EF448,
}


def _arm(debugger, label):
    if label in BPS and BPS[label].IsEnabled():
        return
    target = debugger.GetSelectedTarget()
    common._install(debugger, bks.SLIDE[0] + ADDR[label], label,
                    "dm_migration_server_receive_callbacks.on_hit")
    BPS[label] = target.GetBreakpointAtIndex(target.GetNumBreakpoints() - 1)


def _disable(label):
    bp = BPS.get(label)
    if bp is not None and bp.IsEnabled():
        bp.SetEnabled(False)


def on_hit(frame, bp_loc, _context):
    global OWNER, SERVER_OWNER
    process = frame.GetThread().GetProcess()
    label = common.SITES[bp_loc.GetBreakpoint().GetID()]
    tpidr = bks._reg(frame, "tpidr_el1")
    debugger = process.GetTarget().GetDebugger()
    if label == "DM_CLIENT_MIGRATE_ENTRY":
        if OWNER is not None or bks._progname(process) != "backboardd":
            return False
        OWNER = tpidr
        _disable(label)
        _arm(debugger, "DM_CLIENT_SYNC_CALL")
    elif label == "DM_CLIENT_SYNC_CALL":
        if tpidr != OWNER:
            return False
        _disable(label)
        _arm(debugger, "DM_CLIENT_SYNC_RETURN")
        _arm(debugger, "DM_SERVER_HANDLE_ENTRY")
    elif label == "DM_CLIENT_SYNC_RETURN":
        if tpidr != OWNER:
            return False
        _disable(label)
    elif label == "DM_SERVER_HANDLE_ENTRY":
        if bks._progname(process) != "com.apple.datamigrator":
            return False
        if SERVER_OWNER is None:
            SERVER_OWNER = tpidr
            _arm(debugger, "DM_SERVER_HANDLE_RETURN")
    elif label == "DM_SERVER_HANDLE_RETURN":
        if tpidr != SERVER_OWNER:
            return False
        _disable(label)
    event = {
        "label": label,
        "time": time.time(),
        "thread": frame.GetThread().GetThreadID(),
        "tpidr_el1": tpidr,
        "pc": bks._reg(frame, "pc"),
        "lr": bks._reg(frame, "lr"),
        "x0": bks._reg(frame, "x0"),
        "x1": bks._reg(frame, "x1"),
        "x2": bks._reg(frame, "x2"),
    }
    print("DM_SERVER_RECEIVE_JSON " + json.dumps(event, sort_keys=True),
          flush=True)
    print("DM_SERVER_RECEIVE_ACTIVE " + ",".join(sorted(
        name for name, bp in BPS.items() if bp.IsEnabled())), flush=True)
    return False


def install(debugger, slide):
    global OWNER, SERVER_OWNER
    bks.SLIDE[0] = int(slide)
    common.SITES.clear()
    BPS.clear()
    OWNER = None
    SERVER_OWNER = None
    _arm(debugger, "DM_CLIENT_MIGRATE_ENTRY")
    print("DM_SERVER_RECEIVE_READY slide=0x%x" % bks.SLIDE[0],
          flush=True)

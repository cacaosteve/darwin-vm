"""Read-only correlation of first DataMigrator dictionary after client sync send.

The server's generic DMXPC handler is not yet request attribution. On the
first handler after backboardd's migration send, follow its dictionary branch
and any xpc_dictionary_get_int64(dict, "msgID") call on that exact dict.
"""

import json
import time

import bks_checkin_callbacks as bks
import dm_perform_roundtrip_callbacks as common


CLIENT_OWNER = None
SERVER_OWNER = None
SERVER_MSG = None
MSGID_OWNER = None
BPS = {}
ADDR = {
    "DM_CLIENT_MIGRATE_ENTRY": 0x25E8F4AF8,
    "DM_CLIENT_SYNC_CALL": 0x25E8F5798,
    "DM_CLIENT_SYNC_RETURN": 0x25E8F579C,
    "DM_SERVER_HANDLE_ENTRY": 0x25E8EF374,
    "DM_SERVER_DICT_BRANCH": 0x25E8EF3E8,
    "DM_SERVER_GET_INT64_ENTRY": 0x1805BE868,
}


def _arm(debugger, label, address=None):
    if label in BPS and BPS[label].IsEnabled():
        return
    target = debugger.GetSelectedTarget()
    if address is None:
        address = bks.SLIDE[0] + ADDR[label]
    common._install(debugger, address, label,
                    "dm_migration_server_msgid_callbacks.on_hit")
    BPS[label] = target.GetBreakpointAtIndex(target.GetNumBreakpoints() - 1)


def _disable(label):
    bp = BPS.get(label)
    if bp is not None and bp.IsEnabled():
        bp.SetEnabled(False)


def _emit(frame, label, extra=None):
    event = {
        "label": label,
        "time": time.time(),
        "vcpu": frame.GetThread().GetThreadID(),
        "tpidr_el1": bks._reg(frame, "tpidr_el1"),
        "pc": bks._reg(frame, "pc"),
        "lr": bks._reg(frame, "lr"),
        "x0": bks._reg(frame, "x0"),
        "x1": bks._reg(frame, "x1"),
        "x2": bks._reg(frame, "x2"),
    }
    if extra:
        event.update(extra)
    print("DM_SERVER_MSGID_JSON " + json.dumps(event, sort_keys=True),
          flush=True)


def on_hit(frame, bp_loc, _context):
    global CLIENT_OWNER, SERVER_OWNER, SERVER_MSG, MSGID_OWNER
    process = frame.GetThread().GetProcess()
    label = common.SITES[bp_loc.GetBreakpoint().GetID()]
    tpidr = bks._reg(frame, "tpidr_el1")
    debugger = process.GetTarget().GetDebugger()
    if label == "DM_CLIENT_MIGRATE_ENTRY":
        if CLIENT_OWNER is not None or bks._progname(process) != "backboardd":
            return False
        CLIENT_OWNER = tpidr
        _emit(frame, label)
        _disable(label)
        _arm(debugger, "DM_CLIENT_SYNC_CALL")
    elif label == "DM_CLIENT_SYNC_CALL":
        if tpidr != CLIENT_OWNER:
            return False
        _emit(frame, label)
        _disable(label)
        _arm(debugger, "DM_CLIENT_SYNC_RETURN")
        _arm(debugger, "DM_SERVER_HANDLE_ENTRY")
    elif label == "DM_CLIENT_SYNC_RETURN":
        if tpidr != CLIENT_OWNER:
            return False
        _emit(frame, label)
        _disable(label)
    elif label == "DM_SERVER_HANDLE_ENTRY":
        if SERVER_MSG is not None or bks._progname(process) != "com.apple.datamigrator":
            return False
        SERVER_OWNER = tpidr
        SERVER_MSG = bks._reg(frame, "x2")
        _emit(frame, label, {"server_msg": SERVER_MSG})
        _disable(label)
        _arm(debugger, "DM_SERVER_DICT_BRANCH")
    elif label == "DM_SERVER_DICT_BRANCH":
        if tpidr != SERVER_OWNER:
            return False
        _emit(frame, label, {"server_msg": SERVER_MSG})
        _disable(label)
        _arm(debugger, "DM_SERVER_GET_INT64_ENTRY")
    elif label == "DM_SERVER_GET_INT64_ENTRY":
        if bks._reg(frame, "x0") != SERVER_MSG:
            return False
        key = bks._cstring(process, bks._reg(frame, "x1"), 48)
        _emit(frame, label, {"key": key, "server_msg": SERVER_MSG})
        if key == "msgID":
            MSGID_OWNER = tpidr
            _disable(label)
            _arm(debugger, "DM_SERVER_MSGID_RETURN", bks._reg(frame, "lr"))
    elif label == "DM_SERVER_MSGID_RETURN":
        if tpidr != MSGID_OWNER:
            return False
        _emit(frame, label, {"msgID": bks._reg(frame, "x0"),
                             "server_msg": SERVER_MSG})
        _disable(label)
    print("DM_SERVER_MSGID_ACTIVE " + ",".join(sorted(
        name for name, bp in BPS.items() if bp.IsEnabled())), flush=True)
    return False


def install(debugger, slide):
    global CLIENT_OWNER, SERVER_OWNER, SERVER_MSG, MSGID_OWNER
    bks.SLIDE[0] = int(slide)
    common.SITES.clear()
    BPS.clear()
    CLIENT_OWNER = SERVER_OWNER = SERVER_MSG = MSGID_OWNER = None
    _arm(debugger, "DM_CLIENT_MIGRATE_ENTRY")
    print("DM_SERVER_MSGID_READY slide=0x%x" % bks.SLIDE[0], flush=True)

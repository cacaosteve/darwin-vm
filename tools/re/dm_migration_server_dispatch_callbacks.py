"""Read-only branch bracket of the first DataMigrator dictionary message.

After backboardd's migration sync send, retain the first daemon dictionary
and identify whether DMXPCConnection uses its stored dispatch block or the
fallback message method. Follow the selected call through its return.
"""

import json
import time

import bks_checkin_callbacks as bks
import dm_perform_roundtrip_callbacks as common


CLIENT_OWNER = None
SERVER_OWNER = None
SERVER_MSG = None
BPS = {}
ADDR = {
    "DM_CLIENT_MIGRATE_ENTRY": 0x25E8F4AF8,
    "DM_CLIENT_SYNC_CALL": 0x25E8F5798,
    "DM_CLIENT_SYNC_RETURN": 0x25E8F579C,
    "DM_SERVER_HANDLE_ENTRY": 0x25E8EF374,
    "DM_SERVER_DICT_BRANCH": 0x25E8EF3E8,
    "DM_SERVER_BLOCK_PATH": 0x25E8EF3F0,
    "DM_SERVER_BLOCK_CALL": 0x25E8EF3FC,
    "DM_SERVER_BLOCK_RETURN": 0x25E8EF400,
    "DM_SERVER_FALLBACK_PATH": 0x25E8EF404,
    "DM_SERVER_FALLBACK_CALL": 0x25E8EF40C,
    "DM_SERVER_FALLBACK_RETURN": 0x25E8EF410,
}


def _arm(debugger, label):
    if label in BPS and BPS[label].IsEnabled():
        return
    target = debugger.GetSelectedTarget()
    common._install(debugger, bks.SLIDE[0] + ADDR[label], label,
                    "dm_migration_server_dispatch_callbacks.on_hit")
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
    print("DM_SERVER_DISPATCH_JSON " + json.dumps(event, sort_keys=True),
          flush=True)


def on_hit(frame, bp_loc, _context):
    global CLIENT_OWNER, SERVER_OWNER, SERVER_MSG
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
        # These adjacent branch sites must be armed before the server starts
        # executing the handler. Dynamic arming from DICT_BRANCH is too late.
        _arm(debugger, "DM_SERVER_DICT_BRANCH")
        _arm(debugger, "DM_SERVER_BLOCK_PATH")
        _arm(debugger, "DM_SERVER_BLOCK_CALL")
        _arm(debugger, "DM_SERVER_BLOCK_RETURN")
        _arm(debugger, "DM_SERVER_FALLBACK_PATH")
        _arm(debugger, "DM_SERVER_FALLBACK_CALL")
        _arm(debugger, "DM_SERVER_FALLBACK_RETURN")
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
    elif label == "DM_SERVER_DICT_BRANCH":
        if tpidr != SERVER_OWNER:
            return False
        owner = bks._reg(frame, "x19")
        _emit(frame, label, {"server_msg": SERVER_MSG,
                             "handler_owner": owner,
                             "stored_block": bks._u64(process, owner + 0x30)})
        _disable(label)
    elif label in ("DM_SERVER_BLOCK_PATH", "DM_SERVER_FALLBACK_PATH",
                   "DM_SERVER_FALLBACK_CALL"):
        if tpidr != SERVER_OWNER:
            return False
        _emit(frame, label, {"server_msg": SERVER_MSG})
        _disable(label)
    elif label == "DM_SERVER_BLOCK_CALL":
        if tpidr != SERVER_OWNER:
            return False
        _emit(frame, label, {"server_msg": SERVER_MSG,
                             "block_header": bks._reg(frame, "x8") - 0x10,
                             "block_invoke_signed": bks._reg(frame, "x9")})
        _disable(label)
    elif label == "DM_SERVER_BLOCK_RETURN":
        if tpidr != SERVER_OWNER:
            return False
        _emit(frame, label, {"server_msg": SERVER_MSG})
        _disable(label)
    elif label == "DM_SERVER_FALLBACK_RETURN":
        if tpidr != SERVER_OWNER:
            return False
        _emit(frame, label, {"server_msg": SERVER_MSG})
        _disable(label)
    print("DM_SERVER_DISPATCH_ACTIVE " + ",".join(sorted(
        name for name, bp in BPS.items() if bp.IsEnabled())), flush=True)
    return False


def install(debugger, slide):
    global CLIENT_OWNER, SERVER_OWNER, SERVER_MSG
    bks.SLIDE[0] = int(slide)
    common.SITES.clear()
    BPS.clear()
    CLIENT_OWNER = SERVER_OWNER = SERVER_MSG = None
    _arm(debugger, "DM_CLIENT_MIGRATE_ENTRY")
    print("DM_SERVER_DISPATCH_READY slide=0x%x" % bks.SLIDE[0], flush=True)

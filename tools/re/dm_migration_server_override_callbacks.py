"""Locate the daemon override handling backboardd's migration dictionary.

The first DataMigrator dictionary after the matched backboardd sync send uses
DMXPCConnection's Objective-C handleMessage: fallback. Record the caller of
its msgID lookup, its Mach-O base, and a read-only guest stack sample.
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
    "DM_SERVER_FALLBACK_CALL": 0x25E8EF40C,
    "DM_SERVER_FALLBACK_RETURN": 0x25E8EF410,
    "DM_SERVER_GET_INT64_ENTRY": 0x1805BE868,
}


def _arm(debugger, label):
    if label in BPS and BPS[label].IsEnabled():
        return
    target = debugger.GetSelectedTarget()
    common._install(debugger, bks.SLIDE[0] + ADDR[label], label,
                    "dm_migration_server_override_callbacks.on_hit")
    BPS[label] = target.GetBreakpointAtIndex(target.GetNumBreakpoints() - 1)


def _disable(label):
    bp = BPS.get(label)
    if bp is not None and bp.IsEnabled():
        bp.SetEnabled(False)


def _macho_base(process, address):
    page = address & ~0xfff
    for _ in range(0x40000 // 0x1000):
        raw = bks._read(process, page, 4)
        if raw == b"\xcf\xfa\xed\xfe":
            return page
        page -= 0x1000
    return None


def _stack(frame):
    thread = frame.GetThread()
    frames = []
    for index in range(min(thread.GetNumFrames(), 12)):
        item = thread.GetFrameAtIndex(index)
        frames.append({"pc": item.GetPC(),
                       "function": item.GetFunctionName()})
    return frames


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
    print("DM_SERVER_OVERRIDE_JSON " + json.dumps(event, sort_keys=True),
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
        for name in ("DM_CLIENT_SYNC_RETURN", "DM_SERVER_HANDLE_ENTRY",
                     "DM_SERVER_FALLBACK_CALL", "DM_SERVER_FALLBACK_RETURN"):
            _arm(debugger, name)
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
        _arm(debugger, "DM_SERVER_GET_INT64_ENTRY")
    elif label == "DM_SERVER_FALLBACK_CALL":
        if tpidr != SERVER_OWNER:
            return False
        _emit(frame, label, {"server_msg": SERVER_MSG,
                             "handler_owner": bks._reg(frame, "x0")})
        _disable(label)
    elif label == "DM_SERVER_FALLBACK_RETURN":
        if tpidr != SERVER_OWNER:
            return False
        _emit(frame, label, {"server_msg": SERVER_MSG})
        _disable(label)
    elif label == "DM_SERVER_GET_INT64_ENTRY":
        if bks._reg(frame, "x0") != SERVER_MSG:
            return False
        key = bks._cstring(process, bks._reg(frame, "x1"), 48)
        if key != "msgID":
            return False
        caller = bks._reg(frame, "lr")
        base = _macho_base(process, caller)
        _emit(frame, label, {"server_msg": SERVER_MSG, "key": key,
                             "caller": caller, "macho_base": base,
                             "caller_static": (0x100000000 + caller - base
                                               if base is not None else None),
                             "stack": _stack(frame)})
        _disable(label)
    print("DM_SERVER_OVERRIDE_ACTIVE " + ",".join(sorted(
        name for name, bp in BPS.items() if bp.IsEnabled())), flush=True)
    return False


def install(debugger, slide):
    global CLIENT_OWNER, SERVER_OWNER, SERVER_MSG
    bks.SLIDE[0] = int(slide)
    common.SITES.clear()
    BPS.clear()
    CLIENT_OWNER = SERVER_OWNER = SERVER_MSG = None
    _arm(debugger, "DM_CLIENT_MIGRATE_ENTRY")
    print("DM_SERVER_OVERRIDE_READY slide=0x%x" % bks.SLIDE[0], flush=True)

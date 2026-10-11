"""Read-only DataMigrator msgID-0 migration-dispatch call/return bracket.

Find the daemon main Mach-O base from the first matched msgID lookup, then
arm its msgID-0 jump arm and migration delegate call/return on that thread.
The main executable is PIE, so these addresses are offsets from its header.
"""

import json
import time

import bks_checkin_callbacks as bks
import dm_perform_roundtrip_callbacks as common


CLIENT_OWNER = None
CLIENT_REQUEST = None
SERVER_OWNER = None
SERVER_MSG = None
MAIN_BASE = None
BPS = {}
CALLBACK_MODULE = "dm_migration_server_migrate_callbacks"
SHARED = {
    "DM_CLIENT_MIGRATE_ENTRY": 0x25E8F4AF8,
    "DM_CLIENT_SYNC_CALL": 0x25E8F5798,
    "DM_CLIENT_SYNC_RETURN": 0x25E8F579C,
    "DM_SERVER_HANDLE_ENTRY": 0x25E8EF374,
    "DM_SERVER_FALLBACK_RETURN": 0x25E8EF410,
    "DM_SERVER_GET_INT64_ENTRY": 0x1805BE868,
}
MAIN = {
    "DM_MSGID0_ARM": 0x1C78,
    "DM_MIGRATE_ARGS_READY": 0x29D0,
    "DM_MIGRATION_CALL": 0x2A8C,
    "DM_MIGRATION_RETURN": 0x2A90,
    "DM_REPLY_BLOCK_CALL": 0x2B40,
}


def _arm(debugger, label, address):
    if label in BPS and BPS[label].IsEnabled():
        return
    target = debugger.GetSelectedTarget()
    common._install(debugger, address, label,
                    CALLBACK_MODULE + ".on_hit")
    BPS[label] = target.GetBreakpointAtIndex(target.GetNumBreakpoints() - 1)


def _arm_shared(debugger, label):
    _arm(debugger, label, bks.SLIDE[0] + SHARED[label])


def _disable(label):
    bp = BPS.get(label)
    if bp is not None and bp.IsEnabled():
        bp.SetEnabled(False)


def _macho_base(process, address):
    page = address & ~0xfff
    for _ in range(0x40000 // 0x1000):
        if bks._read(process, page, 4) == b"\xcf\xfa\xed\xfe":
            return page
        page -= 0x1000
    return None


def _emit(frame, label, extra=None):
    process = frame.GetThread().GetProcess()
    event = {
        "label": label,
        "time": time.time(),
        "progname": bks._progname(process),
        "vcpu": frame.GetThread().GetThreadID(),
        "tpidr_el1": bks._reg(frame, "tpidr_el1"),
        "pc": bks._reg(frame, "pc"),
        "lr": bks._reg(frame, "lr"),
        "sp": bks._reg(frame, "sp"),
        "registers": {"x%d" % index: bks._reg(frame, "x%d" % index)
                      for index in range(9)},
        "backtrace": [frame.GetThread().GetFrameAtIndex(index).GetPC()
                      for index in range(min(frame.GetThread().GetNumFrames(), 12))],
    }
    if extra:
        event.update(extra)
    print("DM_SERVER_MIGRATE_JSON " + json.dumps(event, sort_keys=True),
          flush=True)
    bks._write_event("progress.%s.json" % label, event)


def on_hit(frame, bp_loc, _context):
    global CLIENT_OWNER, CLIENT_REQUEST, SERVER_OWNER, SERVER_MSG, MAIN_BASE
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
        _arm_shared(debugger, "DM_CLIENT_SYNC_CALL")
    elif label == "DM_CLIENT_SYNC_CALL":
        if tpidr != CLIENT_OWNER:
            return False
        CLIENT_REQUEST = {
            "owner": tpidr,
            "thread": frame.GetThread().GetThreadID(),
            "connection": bks._reg(frame, "x0"),
            "message": bks._reg(frame, "x1"),
            "registers": {"x%d" % index: bks._reg(frame, "x%d" % index)
                          for index in range(9)},
            "message_bytes": bks._read(
                process, bks._reg(frame, "x1"), 0x100).hex(),
            "time": time.time(),
        }
        _emit(frame, label, {"request": CLIENT_REQUEST})
        _disable(label)
        for name in ("DM_CLIENT_SYNC_RETURN", "DM_SERVER_HANDLE_ENTRY",
                     "DM_SERVER_FALLBACK_RETURN"):
            _arm_shared(debugger, name)
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
        _emit(frame, label, {
            "server_msg": SERVER_MSG,
            "request": CLIENT_REQUEST,
            "server_connection_or_receiver": bks._reg(frame, "x0"),
            "server_x1": bks._reg(frame, "x1"),
            "server_message_bytes": bks._read(process, SERVER_MSG, 0x100).hex(),
        })
        _disable(label)
        _arm_shared(debugger, "DM_SERVER_GET_INT64_ENTRY")
    elif label == "DM_SERVER_GET_INT64_ENTRY":
        if bks._reg(frame, "x0") != SERVER_MSG or \
                bks._cstring(process, bks._reg(frame, "x1"), 48) != "msgID":
            return False
        MAIN_BASE = _macho_base(process, bks._reg(frame, "lr"))
        _emit(frame, label, {"server_msg": SERVER_MSG,
                             "main_base": MAIN_BASE,
                             "key": "msgID",
                             "request": CLIENT_REQUEST})
        _disable(label)
        if MAIN_BASE is not None:
            for name, offset in MAIN.items():
                _arm(debugger, name, MAIN_BASE + offset)
    elif label in MAIN:
        if tpidr != SERVER_OWNER:
            return False
        _emit(frame, label, {"server_msg": SERVER_MSG,
                             "main_base": MAIN_BASE,
                             "request": CLIENT_REQUEST,
                             "x0": bks._reg(frame, "x0"),
                             "x2": bks._reg(frame, "x2"),
                             "x3": bks._reg(frame, "x3"),
                             "x4": bks._reg(frame, "x4"),
                             "x5": bks._reg(frame, "x5"),
                             "x6": bks._reg(frame, "x6")})
        _disable(label)
    elif label == "DM_SERVER_FALLBACK_RETURN":
        if tpidr != SERVER_OWNER:
            return False
        _emit(frame, label, {"server_msg": SERVER_MSG})
        _disable(label)
    print("DM_SERVER_MIGRATE_ACTIVE " + ",".join(sorted(
        name for name, bp in BPS.items() if bp.IsEnabled())), flush=True)
    return False


def install(debugger, slide):
    global CLIENT_OWNER, CLIENT_REQUEST, SERVER_OWNER, SERVER_MSG, MAIN_BASE
    bks.SLIDE[0] = int(slide)
    common.SITES.clear()
    BPS.clear()
    CLIENT_OWNER = CLIENT_REQUEST = SERVER_OWNER = SERVER_MSG = MAIN_BASE = None
    _arm_shared(debugger, "DM_CLIENT_MIGRATE_ENTRY")
    print("DM_SERVER_MIGRATE_READY slide=0x%x" % bks.SLIDE[0], flush=True)

"""Read-only rolling call/return ladder for backboardd DataMigration.

Keep at most the current call/return edge plus its enclosing method return
armed. Earlier broad breakpoint sets missed known straight-line sites on the
QEMU gdbstub, so absence from those sets cannot locate a stall.
"""

import json
import time

import bks_checkin_callbacks as bks
import dm_perform_roundtrip_callbacks as common


OWNER_TPIDR = None
BPS = {}

ADDR = {
    "DM_PERFORM_ENTRY": 0x25E8F02D0,
    "DM_CLASS_CALL": 0x25E8F03CC,
    "DM_CLASS_RETURN": 0x25E8F03D0,
    "DM_INIT_ENTRY": 0x25E8EF130,
    "DM_INIT_XPC_CALL": 0x25E8EF180,
    "DM_INIT_XPC_RETURN": 0x25E8EF184,
    "DM_INIT_FOREGROUND_CALL": 0x25E8EF198,
    "DM_INIT_FOREGROUND_RETURN": 0x25E8EF19C,
    "DM_FOREGROUND_ENTRY": 0x25E8EF248,
    "DM_FOREGROUND_ENABLED_CALL": 0x25E8EF268,
    "DM_FOREGROUND_ENABLED_RETURN": 0x25E8EF26C,
    "DM_FOREGROUND_UID_CALL": 0x25E8EF278,
    "DM_FOREGROUND_UID_RETURN": 0x25E8EF27C,
    "DM_XPC_SESSION_INFO_ENTRY": 0x1805B92F0,
    "DM_XPC_SESSION_INTERFACE_CALL": 0x1805B9368,
    "DM_XPC_SESSION_INTERFACE_RETURN": 0x1805B936C,
    "DM_FOREGROUND_SET_UID_CALL": 0x25E8EF2CC,
    "DM_FOREGROUND_SET_UID_RETURN": 0x25E8EF2D0,
    "DM_FOREGROUND_RETURN": 0x25E8EF2E0,
    "DM_INIT_RESUME_CALL": 0x25E8EF1A0,
    "DM_INIT_RESUME_RETURN": 0x25E8EF1A4,
    "DM_INIT_RETURN": 0x25E8EF1DC,
    "DM_CONTROLLER_CALL": 0x25E8F03F8,
    "DM_CONTROLLER_RETURN": 0x25E8F03FC,
    "DM_CONTROLLER_ENTRY": 0x25E8F2514,
    "DM_DATE_CALL": 0x25E8F2564,
    "DM_DATE_RETURN": 0x25E8F2568,
    "DM_INTERVAL_CALL": 0x25E8F2574,
    "DM_INTERVAL_RETURN": 0x25E8F2578,
    "DM_RETRY_DATE_CALL": 0x25E8F257C,
    "DM_RETRY_DATE_RETURN": 0x25E8F2580,
    "DM_CLAIM_RETURN": 0x25E8F2584,
    "DM_DATE_RELEASE_CALL": 0x25E8F2588,
    "DM_DATE_RELEASE_RETURN": 0x25E8F258C,
    "DM_MAX_ATTEMPTS_CALL": 0x25E8F2590,
    "DM_MAX_ATTEMPTS_RETURN": 0x25E8F2594,
    "DM_MIGRATE_CALL": 0x25E8F25B4,
    "DM_MIGRATE_RETURN": 0x25E8F25B8,
    "DM_CONNECTION_MIGRATE_ENTRY": 0x25E8F4AF8,
    "DM_CONNECTION_SEND_CALL": 0x25E8F4BA8,
    "DM_CONNECTION_SEND_RETURN": 0x25E8F4BAC,
    "DM_CONNECTION_MIGRATE_RETURN": 0x25E8F4C48,
    "DM_XPC_SEND_ENTRY": 0x25E8F5764,
    "DM_XPC_SYNC_CALL": 0x25E8F5798,
    "DM_XPC_SYNC_RETURN": 0x25E8F579C,
    "DM_XPC_SEND_RETURN": 0x25E8F57C8,
    "DM_PERFORM_RETURN": 0x25E8F044C,
}

NEXT = {
    "DM_PERFORM_ENTRY": ("DM_CLASS_CALL",),
    "DM_CLASS_CALL": ("DM_INIT_ENTRY", "DM_CLASS_RETURN"),
    "DM_INIT_ENTRY": ("DM_INIT_XPC_CALL",),
    "DM_INIT_XPC_CALL": ("DM_INIT_XPC_RETURN",),
    "DM_INIT_XPC_RETURN": ("DM_INIT_FOREGROUND_CALL",),
    "DM_INIT_FOREGROUND_CALL": ("DM_FOREGROUND_ENTRY", "DM_INIT_FOREGROUND_RETURN"),
    "DM_FOREGROUND_ENTRY": ("DM_FOREGROUND_ENABLED_CALL",),
    "DM_FOREGROUND_ENABLED_CALL": ("DM_FOREGROUND_ENABLED_RETURN",),
    "DM_FOREGROUND_ENABLED_RETURN": ("DM_FOREGROUND_UID_CALL", "DM_FOREGROUND_RETURN"),
    "DM_FOREGROUND_UID_CALL": ("DM_XPC_SESSION_INFO_ENTRY", "DM_FOREGROUND_UID_RETURN"),
    "DM_XPC_SESSION_INFO_ENTRY": ("DM_XPC_SESSION_INTERFACE_CALL",),
    "DM_XPC_SESSION_INTERFACE_CALL": ("DM_XPC_SESSION_INTERFACE_RETURN",),
    "DM_XPC_SESSION_INTERFACE_RETURN": (),
    "DM_FOREGROUND_UID_RETURN": ("DM_FOREGROUND_RETURN", "DM_FOREGROUND_SET_UID_CALL"),
    "DM_FOREGROUND_SET_UID_CALL": ("DM_FOREGROUND_SET_UID_RETURN",),
    "DM_FOREGROUND_SET_UID_RETURN": ("DM_FOREGROUND_RETURN",),
    "DM_FOREGROUND_RETURN": ("DM_INIT_FOREGROUND_RETURN",),
    "DM_INIT_FOREGROUND_RETURN": ("DM_CLASS_RETURN", "DM_INIT_RESUME_CALL"),
    "DM_INIT_RESUME_CALL": ("DM_INIT_RESUME_RETURN",),
    "DM_INIT_RESUME_RETURN": ("DM_INIT_RETURN",),
    "DM_INIT_RETURN": (),
    "DM_CLASS_RETURN": ("DM_CONTROLLER_CALL",),
    "DM_CONTROLLER_CALL": ("DM_CONTROLLER_ENTRY", "DM_CONTROLLER_RETURN"),
    "DM_CONTROLLER_ENTRY": ("DM_DATE_CALL",),
    "DM_DATE_CALL": ("DM_DATE_RETURN",),
    "DM_DATE_RETURN": ("DM_INTERVAL_CALL",),
    "DM_INTERVAL_CALL": ("DM_INTERVAL_RETURN",),
    "DM_INTERVAL_RETURN": ("DM_RETRY_DATE_CALL",),
    "DM_RETRY_DATE_CALL": ("DM_RETRY_DATE_RETURN",),
    "DM_RETRY_DATE_RETURN": ("DM_CLAIM_RETURN",),
    "DM_CLAIM_RETURN": ("DM_DATE_RELEASE_CALL",),
    "DM_DATE_RELEASE_CALL": ("DM_DATE_RELEASE_RETURN",),
    "DM_DATE_RELEASE_RETURN": ("DM_MAX_ATTEMPTS_CALL",),
    "DM_MAX_ATTEMPTS_CALL": ("DM_MAX_ATTEMPTS_RETURN",),
    "DM_MAX_ATTEMPTS_RETURN": ("DM_MIGRATE_CALL",),
    "DM_MIGRATE_CALL": ("DM_CONNECTION_MIGRATE_ENTRY", "DM_MIGRATE_RETURN"),
    "DM_CONNECTION_MIGRATE_ENTRY": ("DM_CONNECTION_SEND_CALL",),
    "DM_CONNECTION_SEND_CALL": ("DM_XPC_SEND_ENTRY", "DM_CONNECTION_SEND_RETURN"),
    "DM_XPC_SEND_ENTRY": ("DM_XPC_SYNC_CALL",),
    "DM_XPC_SYNC_CALL": ("DM_XPC_SYNC_RETURN",),
    "DM_XPC_SYNC_RETURN": ("DM_XPC_SEND_RETURN",),
    "DM_XPC_SEND_RETURN": ("DM_CONNECTION_SEND_RETURN",),
    "DM_CONNECTION_SEND_RETURN": ("DM_CONNECTION_MIGRATE_RETURN",),
    "DM_CONNECTION_MIGRATE_RETURN": ("DM_MIGRATE_RETURN",),
    "DM_MIGRATE_RETURN": ("DM_MIGRATE_CALL", "DM_CONTROLLER_RETURN"),
    "DM_CONTROLLER_RETURN": ("DM_PERFORM_RETURN",),
}


def _arm(debugger, label):
    if label in BPS and BPS[label].IsEnabled():
        return
    target = debugger.GetSelectedTarget()
    common._install(debugger, bks.SLIDE[0] + ADDR[label], label,
                    "dm_migration_rolling_callbacks.on_hit")
    BPS[label] = target.GetBreakpointAtIndex(target.GetNumBreakpoints() - 1)


def _disable(label):
    bp = BPS.get(label)
    if bp is not None and bp.IsEnabled():
        bp.SetEnabled(False)


def on_hit(frame, bp_loc, _context):
    global OWNER_TPIDR
    process = frame.GetThread().GetProcess()
    tid = frame.GetThread().GetThreadID()
    tpidr = bks._reg(frame, "tpidr_el1")
    label = common.SITES[bp_loc.GetBreakpoint().GetID()]
    if label == "DM_PERFORM_ENTRY":
        if bks._progname(process) != "backboardd":
            return False
        if OWNER_TPIDR is not None or not tpidr:
            return False
        OWNER_TPIDR = tpidr
    elif tpidr != OWNER_TPIDR:
        return False
    event = {
        "label": label,
        "time": time.time(),
        "thread": tid,
        "pc": bks._reg(frame, "pc"),
        "lr": bks._reg(frame, "lr"),
        "x0": bks._reg(frame, "x0"),
        "x1": bks._reg(frame, "x1"),
        "tpidr_el1": tpidr,
    }
    print("DM_ROLLING_JSON " + json.dumps(event, sort_keys=True), flush=True)
    debugger = process.GetTarget().GetDebugger()
    _disable(label)
    if label in ("DM_INIT_FOREGROUND_CALL", "DM_FOREGROUND_ENTRY"):
        _disable("DM_CLASS_RETURN")
    if label == "DM_FOREGROUND_ENTRY":
        _disable("DM_INIT_FOREGROUND_RETURN")
    if label == "DM_FOREGROUND_UID_CALL":
        _disable("DM_FOREGROUND_RETURN")
    if label == "DM_FOREGROUND_UID_RETURN":
        for old in ("DM_XPC_SESSION_INFO_ENTRY", "DM_XPC_SESSION_INTERFACE_CALL",
                    "DM_XPC_SESSION_INTERFACE_RETURN"):
            _disable(old)
    if label == "DM_FOREGROUND_RETURN":
        for old in ("DM_FOREGROUND_UID_CALL", "DM_FOREGROUND_SET_UID_CALL",
                    "DM_FOREGROUND_SET_UID_RETURN"):
            _disable(old)
    if label == "DM_MIGRATE_CALL":
        _disable("DM_CONTROLLER_RETURN")
    if label == "DM_CONNECTION_SEND_CALL":
        _disable("DM_MIGRATE_RETURN")
    if label == "DM_XPC_SYNC_CALL":
        _disable("DM_CONNECTION_SEND_RETURN")
    if label == "DM_CONNECTION_SEND_RETURN":
        _disable("DM_XPC_SEND_ENTRY")
    if label == "DM_CLASS_RETURN":
        for old in ("DM_INIT_ENTRY", "DM_INIT_RETURN", "DM_INIT_XPC_CALL",
                    "DM_INIT_XPC_RETURN", "DM_INIT_FOREGROUND_CALL",
                    "DM_INIT_FOREGROUND_RETURN", "DM_INIT_RESUME_CALL",
                    "DM_INIT_RESUME_RETURN"):
            _disable(old)
    elif label == "DM_CONTROLLER_RETURN":
        for old in ("DM_CONTROLLER_ENTRY", "DM_MIGRATE_CALL",
                    "DM_MIGRATE_RETURN"):
            _disable(old)
    for following in NEXT.get(label, ()):
        _arm(debugger, following)
    print("DM_ROLLING_ACTIVE " + ",".join(sorted(
        name for name, bp in BPS.items() if bp.IsEnabled())), flush=True)
    return False


def install(debugger, slide):
    global OWNER_TPIDR
    bks.SLIDE[0] = int(slide)
    common.SITES.clear()
    BPS.clear()
    OWNER_TPIDR = None
    _arm(debugger, "DM_PERFORM_ENTRY")
    print("DM_ROLLING_READY slide=0x%x" % bks.SLIDE[0], flush=True)

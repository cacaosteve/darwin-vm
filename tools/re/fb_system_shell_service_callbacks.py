"""Read-only bracket of FBSystemShell's BackBoard check-in path."""

import json
import time

import lldb

import ccs_ls_seeding_correlation_callbacks as base


SITES = (
    (0x1C4A3C058, "FB_SERVICE_CALL"),
    (0x18A1471A0, "BKS_START_ENTRY"),
    (0x18A1471D0, "BKS_SET_STATUS_CALL"),
    (0x18A1471D4, "BKS_SET_STATUS_RETURN"),
    (0x18A1474A8, "BKS_CHECKIN_ENTRY"),
    (0x18A147598, "BKS_MIGRATION_BRANCH"),
    (0x18A1475C0, "BKS_ENDPOINT_CALL"),
    (0x18A1475C4, "BKS_ENDPOINT_RETURN"),
    (0x18A1475DC, "BKS_CONNECTION_CALL"),
    (0x18A1475E0, "BKS_CONNECTION_RETURN"),
    (0x18A147630, "BKS_CONFIGURE_CALL"),
    (0x18A147634, "BKS_CONFIGURE_RETURN"),
    (0x18A147654, "BKS_ACTIVATE_CALL"),
    (0x18A147658, "BKS_ACTIVATE_RETURN"),
    (0x18A147678, "BKS_REMOTE_TARGET_CALL"),
    (0x18A14767C, "BKS_REMOTE_TARGET_RETURN"),
    (0x18A147690, "BKS_REMOTE_TARGET2_CALL"),
    (0x18A147694, "BKS_REMOTE_TARGET2_RETURN"),
    (0x18A1476A0, "BKS_MIGRATION_CHECKIN_CALL"),
    (0x18A1476A4, "BKS_MIGRATION_CHECKIN_RETURN"),
    (0x18A1476B8, "BKS_BYPASS_TARGET_CALL"),
    (0x18A1476BC, "BKS_BYPASS_TARGET_RETURN"),
    (0x18A1476C8, "BKS_BYPASS_CHECKIN_CALL"),
    (0x18A1476CC, "BKS_BYPASS_CHECKIN_RETURN"),
    (0x18A147774, "BKS_CHECKIN_RETURN"),
    (0x1C4A3C05C, "FB_SERVICE_RETURN"),
)


ACTIVE_THREAD = None
COUNTS = {}


def on_site(frame, bp_loc, context):
    global ACTIVE_THREAD
    breakpoint = bp_loc.GetBreakpoint()
    label = base.LABELS[breakpoint.GetID()][0]
    process = frame.GetThread().GetProcess()
    if base._progname(process) != "SpringBoard":
        return False
    thread = base._reg(frame, "tpidr_el1")
    if label == "FB_SERVICE_CALL" and ACTIVE_THREAD is None:
        ACTIVE_THREAD = thread
    if ACTIVE_THREAD is None or thread != ACTIVE_THREAD:
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count > 12:
        return False
    payload = {"label": label, "count": count, "time": time.time(),
               "thread_pointer": thread, "pc": base._reg(frame, "pc"),
               "lr": base._reg(frame, "lr"), "x0": base._reg(frame, "x0"),
               "x1": base._reg(frame, "x1"), "x2": base._reg(frame, "x2"),
               "x20": base._reg(frame, "x20"),
               "x22": base._reg(frame, "x22")}
    if label == "BKS_START_ENTRY":
        service = payload["x0"]
        status = base._read(process, service + 0x30, 8)
        wait = base._read(process, service + 0x20, 1)
        payload["checkin_status"] = (int.from_bytes(status, "little")
                                     if len(status) == 8 else None)
        payload["wait_for_migration"] = (wait[0] if wait else None)
    if label in ("BKS_START_ENTRY", "BKS_CHECKIN_ENTRY"):
        lldb_thread = frame.GetThread()
        payload["backtrace"] = [lldb_thread.GetFrameAtIndex(i).GetPC()
                                for i in range(min(lldb_thread.GetNumFrames(), 8))]
    print("FB_SYSTEM_SHELL_SERVICE_JSON " + json.dumps(payload, sort_keys=True),
          flush=True)
    return False


def install(debugger, slide):
    global ACTIVE_THREAD
    ACTIVE_THREAD = None
    COUNTS.clear()
    base.SLIDE = int(slide)
    base.LABELS.clear()
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    for static, label in SITES:
        address = static + base.SLIDE
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % address,
                                  result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed %s: %s" % (label, result.GetError()))
        breakpoint = target.GetBreakpointAtIndex(before)
        base.LABELS[breakpoint.GetID()] = label, "SpringBoard"
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F fb_system_shell_service_callbacks.on_site %d" %
            breakpoint.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

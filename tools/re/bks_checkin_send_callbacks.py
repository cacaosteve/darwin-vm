"""Read-only dispatch and wait trace for the BKS migration check-in send."""

import json
import time

import lldb

import ccs_ls_seeding_correlation_callbacks as base


CALL = 0x18A1476A0
RETURN = 0x18A1476A4
SELECTOR = 0x1F658EBBE
DISPATCH = (0x188000850, 0x1880008C4, 0x188000D24)
WAITS = (
    (0x237CCFCCC, "MACH_MSG2"),
    (0x237CCFC0C, "MACH_MSG"),
    (0x1AE8BAE94, "DISPATCH_SEMAPHORE"),
    (0x1AE8D4690, "DISPATCH_MACH_REPLY"),
)

ACTIVE_THREAD = None
PROXY = None
DISPATCH_IDS = []
WAIT_RETURNS = {}
WAIT_SEQUENCE = 0


def _canonical(value):
    value &= 0x0000FFFFFFFFFFFF
    if value & (1 << 47):
        value |= 0xFFFF000000000000
    return value


def _emit(label, frame, **extra):
    thread = frame.GetThread()
    payload = {"label": label, "time": time.time(),
               "thread_pointer": base._reg(frame, "tpidr_el1"),
               "pc": base._reg(frame, "pc"), "lr": base._reg(frame, "lr"),
               "x0": base._reg(frame, "x0"), "x1": base._reg(frame, "x1"),
               "x2": base._reg(frame, "x2"), "x17": base._reg(frame, "x17"),
               "backtrace": [thread.GetFrameAtIndex(i).GetPC()
                             for i in range(min(thread.GetNumFrames(), 20))]}
    payload.update(extra)
    print("BKS_CHECKIN_SEND_JSON " + json.dumps(payload, sort_keys=True),
          flush=True)


def _install(debugger, address, label, callback):
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
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
        "breakpoint command add -F bks_checkin_send_callbacks.%s %d" %
        (callback, breakpoint.GetID()), result)
    if not result.Succeeded():
        raise RuntimeError("failed callback %s: %s" %
                           (label, result.GetError()))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
          (breakpoint.GetID(), label, address), flush=True)
    return breakpoint


def _matching(frame):
    return (ACTIVE_THREAD is not None and
            base._reg(frame, "tpidr_el1") == ACTIVE_THREAD and
            base._progname(frame.GetThread().GetProcess()) == "SpringBoard")


def on_call(frame, bp_loc, context):
    global ACTIVE_THREAD, PROXY
    if base._progname(frame.GetThread().GetProcess()) != "SpringBoard":
        return False
    if ACTIVE_THREAD is not None:
        return False
    ACTIVE_THREAD = base._reg(frame, "tpidr_el1")
    PROXY = base._reg(frame, "x0")
    _emit("CHECKIN_CALL", frame, proxy=PROXY,
          selector_runtime=base.SLIDE + SELECTOR)
    debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
    for static in DISPATCH:
        breakpoint = _install(debugger, base.SLIDE + static,
                              "OBJC_DISPATCH_%x" % static, "on_dispatch")
        DISPATCH_IDS.append(breakpoint.GetID())
    for static, name in WAITS:
        _install(debugger, base.SLIDE + static,
                 "WAIT_" + name, "on_wait")
    return False


def on_dispatch(frame, bp_loc, context):
    if not _matching(frame):
        return False
    if (base._reg(frame, "x0") != PROXY or
            base._reg(frame, "x1") != base.SLIDE + SELECTOR):
        return False
    target = _canonical(base._reg(frame, "x17"))
    _emit("OBJC_DISPATCH", frame, proxy=PROXY,
          target_runtime=target, target_static=target - base.SLIDE)
    process_target = frame.GetThread().GetProcess().GetTarget()
    for breakpoint_id in DISPATCH_IDS:
        process_target.FindBreakpointByID(breakpoint_id).SetEnabled(False)
    if target:
        debugger = process_target.GetDebugger()
        _install(debugger, target, "TARGET_ENTRY", "on_target")
    return False


def on_target(frame, bp_loc, context):
    if _matching(frame):
        _emit("TARGET_ENTRY", frame)
        bp_loc.GetBreakpoint().SetEnabled(False)
    return False


def on_wait(frame, bp_loc, context):
    global WAIT_SEQUENCE
    if not _matching(frame):
        return False
    WAIT_SEQUENCE += 1
    if WAIT_SEQUENCE > 24:
        return False
    label = base.LABELS[bp_loc.GetBreakpoint().GetID()][0]
    return_address = _canonical(base._reg(frame, "lr"))
    _emit(label, frame, sequence=WAIT_SEQUENCE,
          return_address=return_address)
    debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
    breakpoint = _install(debugger, return_address,
                          "WAIT_RETURN_%d" % WAIT_SEQUENCE, "on_wait_return")
    WAIT_RETURNS[breakpoint.GetID()] = (WAIT_SEQUENCE, time.time())
    return False


def on_wait_return(frame, bp_loc, context):
    if not _matching(frame):
        return False
    breakpoint = bp_loc.GetBreakpoint()
    sequence, started = WAIT_RETURNS[breakpoint.GetID()]
    _emit("WAIT_RETURN", frame, sequence=sequence,
          elapsed=time.time() - started)
    breakpoint.SetEnabled(False)
    return False


def on_return(frame, bp_loc, context):
    if _matching(frame):
        _emit("CHECKIN_RETURN", frame)
    return False


def install(debugger, slide):
    global ACTIVE_THREAD, PROXY, WAIT_SEQUENCE
    ACTIVE_THREAD = None
    PROXY = None
    WAIT_SEQUENCE = 0
    DISPATCH_IDS.clear()
    WAIT_RETURNS.clear()
    base.SLIDE = int(slide)
    base.LABELS.clear()
    _install(debugger, base.SLIDE + CALL, "CHECKIN_CALL", "on_call")
    _install(debugger, base.SLIDE + RETURN, "CHECKIN_RETURN", "on_return")

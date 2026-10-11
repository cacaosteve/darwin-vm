"""Bounded lsd IPC activity after SpringBoard's blocking LS store request."""

import json
import time

import lldb


PROGNAME_PTR = 0x1E6EF1590
SPRINGBOARD_SEND = 0x186EF62B8
SPRINGBOARD_RETURN = 0x186EF62BC
MACH_MSG2_RETURN = 0x237CCFCD4
SLIDE = 0
LSD_BP = None
COUNTS = {}


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else 0


def _read(process, address, size):
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return data if error.Success() else b""


def _u64(process, address):
    return int.from_bytes(_read(process, address, 8), "little")


def _progname(process):
    pointer = _u64(process, SLIDE + PROGNAME_PTR)
    string = _u64(process, pointer) if pointer else 0
    raw = _read(process, string, 96) if string else b""
    return raw.split(b"\0", 1)[0].decode("ascii", "replace") if raw else ""


def _emit(label, frame):
    thread = frame.GetThread()
    payload = {
        "label": label,
        "time": time.time(),
        "thread_pointer": _reg(frame, "tpidr_el1"),
        "pc": _reg(frame, "pc"),
        "lr": _reg(frame, "lr"),
        "registers": {"x%d" % i: _reg(frame, "x%d" % i)
                      for i in range(8)},
        "backtrace": [thread.GetFrameAtIndex(i).GetPC()
                      for i in range(min(thread.GetNumFrames(), 12))],
    }
    print("CCS_LS_LSD_ACTIVITY_JSON " + json.dumps(payload, sort_keys=True),
          flush=True)


def on_springboard_send(frame, bp_loc, _dict):
    global LSD_BP
    if _progname(frame.GetThread().GetProcess()) != "SpringBoard":
        return False
    count = COUNTS.get("send", 0) + 1
    COUNTS["send"] = count
    if count == 1:
        _emit("SPRINGBOARD_BLOCKING_SEND", frame)
        LSD_BP.SetEnabled(True)
        print("LSD_MACH_RETURN_ARMED time=%f" % time.time(), flush=True)
    return False


def on_springboard_return(frame, bp_loc, _dict):
    if _progname(frame.GetThread().GetProcess()) == "SpringBoard":
        _emit("SPRINGBOARD_BLOCKING_RETURN", frame)
    return False


def on_lsd_return(frame, bp_loc, _dict):
    if _progname(frame.GetThread().GetProcess()) != "lsd":
        return False
    count = COUNTS.get("lsd", 0) + 1
    COUNTS["lsd"] = count
    if count <= 40:
        _emit("LSD_MACH_MSG2_RETURN", frame)
    if count >= 40:
        bp_loc.GetBreakpoint().SetEnabled(False)
    return False


def _install(target, address, label, callback, enabled=True):
    breakpoint = target.BreakpointCreateByAddress(address)
    if not breakpoint.IsValid() or breakpoint.GetNumLocations() != 1:
        raise RuntimeError("failed %s at 0x%x" % (label, address))
    breakpoint.SetScriptCallbackFunction(callback)
    breakpoint.SetAutoContinue(True)
    breakpoint.SetEnabled(enabled)
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x enabled=%s" %
          (breakpoint.GetID(), label, address, enabled), flush=True)
    return breakpoint


def install(debugger, slide):
    global SLIDE, LSD_BP
    SLIDE = int(slide)
    COUNTS.clear()
    target = debugger.GetSelectedTarget()
    LSD_BP = _install(target, SLIDE + MACH_MSG2_RETURN,
                      "LSD_MACH_MSG2_RETURN",
                      "ccs_ls_lsd_activity_callbacks.on_lsd_return", False)
    _install(target, SLIDE + SPRINGBOARD_SEND,
             "SPRINGBOARD_BLOCKING_SEND",
             "ccs_ls_lsd_activity_callbacks.on_springboard_send")
    _install(target, SLIDE + SPRINGBOARD_RETURN,
             "SPRINGBOARD_BLOCKING_RETURN",
             "ccs_ls_lsd_activity_callbacks.on_springboard_return")

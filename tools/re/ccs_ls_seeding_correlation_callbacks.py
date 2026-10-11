"""Read-only correlation of SpringBoard's blocking LS request and lsd seeding wait."""

import json
import time

import lldb


PROGNAME_PTR = 0x1E6EF1590
SEEDING_COUNT = 0x1E40E4330
SLIDE = 0
LABELS = {}
COUNTS = {}
SITES = (
    (0x186EF62B8, "SB_BLOCKING_SEND", "SpringBoard"),
    (0x186EF62BC, "SB_BLOCKING_RETURN", "SpringBoard"),
    (0x186F145C4, "LSD_HANDLER_ENTRY", "lsd"),
    (0x186F1461C, "LSD_MANUAL_RESULT", "lsd"),
    (0x186F14630, "LSD_SEED_WAIT_CALL", "lsd"),
    (0x186F14634, "LSD_SEED_WAIT_RETURN", "lsd"),
    (0x186E090C0, "LSD_WAIT_ENTRY", "lsd"),
    (0x186E090F4, "LSD_WAIT_COUNT_LOOP", "lsd"),
    (0x186E09104, "LSD_DISPATCH_WAIT_CALL", "lsd"),
    (0x186E09108, "LSD_DISPATCH_WAIT_RETURN", "lsd"),
    (0x186E09110, "LSD_WAIT_EXIT", "lsd"),
    (0x186F146AC, "LSD_SYNC_READ_CALL", "lsd"),
    (0x186F146B0, "LSD_SYNC_READ_RETURN", "lsd"),
)


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else 0


def _read(process, address, size):
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return data if error.Success() else b""


def _progname(process):
    pointer = int.from_bytes(_read(process, SLIDE + PROGNAME_PTR, 8), "little")
    string = int.from_bytes(_read(process, pointer, 8), "little") if pointer else 0
    raw = _read(process, string, 96) if string else b""
    return raw.split(b"\0", 1)[0].decode("ascii", "replace") if raw else ""


def on_site(frame, bp_loc, _dict):
    breakpoint = bp_loc.GetBreakpoint()
    label, expected = LABELS[breakpoint.GetID()]
    process = frame.GetThread().GetProcess()
    if _progname(process) != expected:
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count <= 12:
        thread = frame.GetThread()
        seed_raw = _read(process, SLIDE + SEEDING_COUNT, 4)
        payload = {
            "label": label,
            "count": count,
            "time": time.time(),
            "thread_pointer": _reg(frame, "tpidr_el1"),
            "pc": _reg(frame, "pc"),
            "x0": _reg(frame, "x0"),
            "x1": _reg(frame, "x1"),
            "x2": _reg(frame, "x2"),
            "x20": _reg(frame, "x20"),
            "x22": _reg(frame, "x22"),
            "seeding_count": int.from_bytes(seed_raw, "little") if seed_raw else None,
            "backtrace": [thread.GetFrameAtIndex(i).GetPC()
                          for i in range(min(thread.GetNumFrames(), 8))],
        }
        print("CCS_LS_SEEDING_JSON " + json.dumps(payload, sort_keys=True),
              flush=True)
    if count >= 12:
        breakpoint.SetEnabled(False)
    return False


def install(debugger, slide):
    global SLIDE
    SLIDE = int(slide)
    LABELS.clear()
    COUNTS.clear()
    target = debugger.GetSelectedTarget()
    for static, label, expected in SITES:
        address = static + SLIDE
        breakpoint = target.BreakpointCreateByAddress(address)
        if not breakpoint.IsValid() or breakpoint.GetNumLocations() != 1:
            raise RuntimeError("failed %s at 0x%x" % (label, address))
        breakpoint.SetScriptCallbackFunction(
            "ccs_ls_seeding_correlation_callbacks.on_site")
        breakpoint.SetAutoContinue(True)
        LABELS[breakpoint.GetID()] = label, expected
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (breakpoint.GetID(), label, address), flush=True)

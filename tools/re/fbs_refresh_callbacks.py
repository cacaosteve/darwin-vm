"""Observe SpringBoard's synthetic display-mode validation.

The FBSDisplayMode breakpoints are read-only.  Compare their output with the
separate display_latch_callbacks trace for the same synthetic mode candidate.
"""

import lldb


SLIDE = 0
PROGNAME_PTR = 0x1e6ef1590
BP_LABEL = {}
ENTRY_COUNT = 0
RAW_COUNT = 0


def _reg(frame, name):
    reg = frame.FindRegister(name)
    return reg.GetValueAsUnsigned() if reg.IsValid() else None


def _fp(frame, name):
    reg = frame.FindRegister(name)
    if not reg.IsValid():
        return None
    error = lldb.SBError()
    value = reg.GetData().GetDouble(error, 0)
    return value if error.Success() else reg.GetValue()


def _read(process, address, size):
    if not address:
        return b""
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return bytes(data) if error.Success() and data else b""


def _u64(process, address):
    data = _read(process, address, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else 0


def _progname(process):
    pointer = _u64(process, PROGNAME_PTR + SLIDE)
    string = _u64(process, pointer) if pointer else 0
    data = _read(process, string, 96)
    return data.split(b"\0", 1)[0].decode("ascii", "replace") if data else "?"


def on_break(frame, bp_loc, internal_dict=None):
    global ENTRY_COUNT, RAW_COUNT
    label = BP_LABEL.get(bp_loc.GetBreakpoint().GetID())
    thread = frame.GetThread()
    process = thread.GetProcess()
    name = _progname(process)
    if label == "FBS_MODE_ENTRY" and RAW_COUNT < 8:
        RAW_COUNT += 1
        print("FBS_MODE_ENTRY_SEEN hit=%d progname=%s width=%s height=%s "
              "preferred_scale=%s refresh_hz_d1=%s" % (
                  RAW_COUNT, name, _reg(frame, "x2"), _reg(frame, "x3"),
                  _reg(frame, "x4"), _fp(frame, "d1")), flush=True)
    if name != "SpringBoard":
        return False
    if label == "FBS_MODE_ENTRY":
        ENTRY_COUNT += 1
        if ENTRY_COUNT <= 24:
            sp = _reg(frame, "sp")
            print("FBS_MODE_ENTRY hit=%d width=%s height=%s "
                  "preferred_scale=%s scale_override_d0=%s "
                  "refresh_hz_d1=%s gamut=%s hdr=%s rotation=%s "
                  "virtual=%s validity=%s "
                  "thread=%s" % (
                      ENTRY_COUNT, _reg(frame, "x2"), _reg(frame, "x3"),
                      _reg(frame, "x4"), _fp(frame, "d0"),
                      _fp(frame, "d1"), _reg(frame, "x5"),
                      _reg(frame, "x6"), _reg(frame, "x7"),
                      (_u64(process, sp) & 0xff) if sp else None,
                      _u64(process, sp + 8) if sp else None,
                      thread.GetThreadID()), flush=True)
            if ENTRY_COUNT == 1:
                print("FBS_MODE_ENTRY_CALLER lr_static=%s" %
                      hex((_reg(frame, "lr") or 0) - SLIDE), flush=True)
                for index in range(min(thread.GetNumFrames(), 20)):
                    current = thread.GetFrameAtIndex(index)
                    print("FBS_MODE_ENTRY_FRAME index=%d pc_static=%s "
                          "fp=%s sp=%s" % (
                              index, hex(current.GetPC() - SLIDE),
                              hex(current.GetFP()), hex(current.GetSP())),
                          flush=True)
    elif label == "FBS_MODE_RETURN":
        if ENTRY_COUNT <= 24:
            print("FBS_MODE_RETURN object=%s thread=%s" % (
                hex(_reg(frame, "x0") or 0), thread.GetThreadID()), flush=True)
    elif label == "FBS_MODE_FATAL":
        print("FBS_MODE_FATAL pc_static=0x1a84078b0 thread=%s" %
              thread.GetThreadID(), flush=True)
        for index in range(min(thread.GetNumFrames(), 16)):
            current = thread.GetFrameAtIndex(index)
            print("FBS_MODE_FATAL_FRAME index=%d pc_static=%s" % (
                index, hex(current.GetPC() - SLIDE)), flush=True)
    return False


def install(debugger, slide):
    global SLIDE, ENTRY_COUNT, RAW_COUNT
    SLIDE = int(slide)
    ENTRY_COUNT = 0
    RAW_COUNT = 0
    BP_LABEL.clear()
    target = debugger.GetSelectedTarget()
    for address, label in ((0x1a840766c, "FBS_MODE_ENTRY"),
                           (0x1a8407828, "FBS_MODE_RETURN"),
                           (0x1a84078b0, "FBS_MODE_FATAL")):
        point = target.BreakpointCreateByAddress(address + SLIDE)
        if not point.IsValid():
            raise RuntimeError("breakpoint failed: %s" % label)
        BP_LABEL[point.GetID()] = label
        result = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command add -F fbs_refresh_callbacks.on_break %d" %
            point.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("breakpoint command failed %s: %s" %
                               (label, result.GetError()))
        print("FBS_MODE_BP label=%s static=%s runtime=%s" % (
            label, hex(address), hex(address + SLIDE)), flush=True)
    print("FBS_MODE_READY slide=%s" % hex(SLIDE), flush=True)

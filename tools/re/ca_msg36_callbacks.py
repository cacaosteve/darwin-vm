"""Capture CA render-server Mach receives for interesting msgh_ids.

Historically filtered for `0x36`, but static RE on the restored cache shows
`server_thread` dispatches the `0x9c40`–`0x9c45` family (and related high IDs),
not `0x36`. Keep `0x36` in the interesting set for continuity; default also
includes the `0x9c4x` IDs GATE72 already sampled.
"""
import json
import os
import time

import lldb


STATIC_RETURN = 0x1843f9804  # SRV_MACHMSG_RET from display_port_callbacks.py
PROGNAME_PTR = 0x1e6ef1590
SLIDE = 0
EVENT_DIR = os.environ.get("DVM_PROBE_EVENT_DIR", "")
HITS = 0
TOTAL_HITS = 0
BP_ID = None
INTERESTING_IDS = set()
for part in os.environ.get(
        "DVM_CA_MSG_IDS", "0x9c40,0x9c41,0x9c42,0x9c43,0x9c44,0x9c45,0x36"
).split(","):
    part = part.strip()
    if part:
        INTERESTING_IDS.add(int(part, 0))


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else None


def _read(process, address, size):
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return data if error.Success() else b""


def _u64(process, address):
    data = _read(process, address, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else None


def _progname(process):
    pointer = _u64(process, SLIDE + PROGNAME_PTR)
    string = _u64(process, pointer) if pointer else None
    if not string:
        return "<unreadable>"
    data = _read(process, string, 96)
    return data.split(b"\0", 1)[0].decode("ascii", "replace") if data else "<unreadable>"


def _write_event(payload):
    if not EVENT_DIR:
        return
    os.makedirs(EVENT_DIR, exist_ok=True)
    path = os.path.join(EVENT_DIR, "ca-message-recv.json")
    temporary = path + ".%d.tmp" % os.getpid()
    with open(temporary, "w") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def on_break(frame, bp_loc, _dict):
    global HITS, TOTAL_HITS
    process = frame.GetThread().GetProcess()
    sp = _reg(frame, "sp")
    if not sp:
        return False
    raw = _read(process, sp + 0x50, 24)
    if len(raw) != 24:
        return False
    words = [int.from_bytes(raw[i:i + 4], "little") for i in range(0, 24, 4)]
    message_id = words[5]
    TOTAL_HITS += 1
    if message_id not in INTERESTING_IDS:
        if TOTAL_HITS <= 8:
            print("CA_MACHMSG_SAMPLE n=%d progname=%s id=0x%x size=%d local=0x%x remote=0x%x" % (
                TOTAL_HITS, _progname(process), message_id, words[1],
                words[3], words[2]), flush=True)
        return False
    HITS += 1
    size = words[1]
    body_size = max(0, min(size - 24, 256))
    body = _read(process, sp + 0x68, body_size) if body_size else b""
    last_mut = None
    try:
        import render_gate_callbacks as rgc
        last_mut = getattr(rgc, "LAST_MUT", None)
    except Exception:
        last_mut = None
    payload = {
        "label": "CA_MSG_RECV",
        "hit": HITS,
        "time": time.time(),
        "progname": _progname(process),
        "thread": frame.GetThread().GetThreadID(),
        "tpidr_el1": _reg(frame, "tpidr_el1"),
        "pc": _reg(frame, "pc"),
        "lr": _reg(frame, "lr"),
        "sp": sp,
        "msgh_bits": words[0],
        "size": size,
        "remote_port": words[2],
        "local_port": words[3],
        "voucher_port": words[4],
        "message_id": message_id,
        "body_prefix_hex": body.hex(),
        "last_mutator": last_mut,
    }
    print("=== CA_MSG_RECV hit=%d id=0x%x ===" % (HITS, message_id), flush=True)
    print("CA_MSG_RECV_JSON " + json.dumps(payload, sort_keys=True), flush=True)
    _write_event(payload)
    if message_id == 0x36:
        # Preserve legacy artifact name for continuity.
        if EVENT_DIR:
            path = os.path.join(EVENT_DIR, "ca-message-0x36.json")
            temporary = path + ".%d.tmp" % os.getpid()
            with open(temporary, "w") as stream:
                json.dump(payload, stream, sort_keys=True)
                stream.write("\n")
            os.replace(temporary, path)
    if HITS >= 32:
        bp_loc.GetBreakpoint().SetEnabled(False)
        print("bounded-disabled CA_MSG_RECV after=%d" % HITS, flush=True)
    return False


def install(debugger, slide):
    global BP_ID, SLIDE, HITS, TOTAL_HITS
    SLIDE = int(slide)
    HITS = 0
    TOTAL_HITS = 0
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    address = STATIC_RETURN + SLIDE
    interpreter.HandleCommand("breakpoint set --address 0x%x" % address, result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed CA message return breakpoint: %s" % result.GetError())
    breakpoint = target.GetBreakpointAtIndex(before)
    BP_ID = breakpoint.GetID()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand(
        "breakpoint command add -F ca_msg36_callbacks.on_break %d" % BP_ID,
        result)
    if not result.Succeeded():
        raise RuntimeError("failed CA message callback: %s" % result.GetError())
    print("COMMAND_LIST_PROOF id=%d label=CA_MSG_RECV address=0x%x" %
          (BP_ID, address), flush=True)

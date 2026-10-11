"""Trace CA client Mach-message construction/send on the QuartzCore path.

Prefer these caller sites over global `mach_msg*_trap` breakpoints. GATE73
showed that trapping every process at the syscall entry slows the guest so
much that render/mutator probes never fire; a missing `0x36` there is
inconclusive.

Static RE on the restored dyld cache (2026-09-23) shows the client builder at
`0x184404ab0` selecting msgh_id among `0x9c42`/`0x9c43`/`0x9c44`/`0x9c45` and
storing it with `stp wzr, w9, [x24, #0x10]` at `0x184404c54` (msgh_id at
`+0x14`). The send wrapper at `0x184404f6c` then calls `mach_msg` (`bl
0x18813aff0`) with `MACH_SEND_MSG`. Literal `mov #0x36` sites in QuartzCore
are error/type codes, not msgh_id stores; `server_thread` does not dispatch
msgh_id `0x36`. Watch the `0x9c4x` family (GATE72 already saw `0x9c41` on the
receive side).

Opt-in via `DVM_CA_MSG36_SEND_TRACE=1` from `render_gate_surface_callbacks`.
Set `DVM_CA_MSG36_SEND_GLOBAL=1` to also install the old global traps.
"""
import json
import os
import time

import lldb


# Client builder: after msgid csel, stores msgh_id at [x24+0x14].
MSGID_STORE = 0x184404C54
# Send wrapper entry (x0 = message buffer).
SEND_WRAPPER = 0x184404F6C
# Primary in-process commit caller (before builder). Loads ctx+0x228 port,
# ctx+0x210→+0xa8 as w1, and ctx+0x1e8 (filled from the +0x360 layer-walk
# slot) as x3. Same object family as LAYER_WALK mask work.
COMMIT_CALL = 0x184401EB0
# After bl 0x18469aadc on ctx+0x360 — walk result in x0 before commit builder.
WALK360_RET = 0x184401E84
# Legacy global traps (opt-in only; too slow for a full boot).
MACH_MSG2_TRAP = 0x237CCFCCC
MACH_MSG_TRAP = 0x237CCFC0C

PROGNAME_PTR = 0x1E6EF1590
SLIDE = 0
EVENT_DIR = os.environ.get("DVM_PROBE_EVENT_DIR", "")
PROCESS_NAMES = set(filter(None, os.environ.get(
    "DVM_CA_MSG36_PROCESSES", "backboardd,SpringBoard").split(",")))
INTERESTING_IDS = set()
for part in os.environ.get(
        "DVM_CA_MSG_IDS", "0x9c40,0x9c41,0x9c42,0x9c43,0x9c44,0x9c45,0x36"
).split(","):
    part = part.strip()
    if part:
        INTERESTING_IDS.add(int(part, 0))

GROUP_NAMES = {}
THREAD_NAMES = {}
SAMPLES = {}
HITS = 0
BP_IDS = {}
LAST_SEND = {}
AFTER_LOCK_TRANSITION = os.environ.get(
    "DVM_CA_MSG_AFTER_LOCK_TRANSITION", "0") == "1"
TRANSITION_MARKER = os.environ.get("DVM_LOCK_GESTURE_TRANSITION_MARKER", "")


def _transition_gate_open():
    if not AFTER_LOCK_TRANSITION:
        return True
    if not TRANSITION_MARKER or not os.path.isfile(TRANSITION_MARKER):
        return False
    try:
        with open(TRANSITION_MARKER) as stream:
            marker = json.load(stream)
    except (OSError, json.JSONDecodeError):
        return False
    return (marker.get("for_user_gesture") == 1 and
            marker.get("gesture_sequence_confirmed") is not False and
            not (isinstance(marker.get("dismissal_gesture"), dict) and
                 marker["dismissal_gesture"].get("confirmed") is False))


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else 0


def _read(process, address, size):
    if not address or not size:
        return b""
    error = lldb.SBError()
    data = process.ReadMemory(address, min(size, 0x200), error)
    return data if error.Success() else b""


def _u64(process, address):
    data = _read(process, address, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else 0


def _u32(process, address):
    data = _read(process, address, 4)
    return int.from_bytes(data, "little") if len(data) == 4 else 0


def _progname(process):
    pointer = _u64(process, SLIDE + PROGNAME_PTR)
    string = _u64(process, pointer) if pointer else 0
    data = _read(process, string, 96)
    return data.split(b"\0", 1)[0].decode("ascii", "replace") if data else ""


def _process_name(frame):
    process = frame.GetThread().GetProcess()
    thread_pointer = _reg(frame, "tpidr_el1")
    if thread_pointer in THREAD_NAMES:
        return THREAD_NAMES[thread_pointer]
    group = _u64(process, thread_pointer + 0x290) if thread_pointer else 0
    if group in GROUP_NAMES:
        result = (GROUP_NAMES[group], group)
        if thread_pointer:
            THREAD_NAMES[thread_pointer] = result
        return result
    name = _progname(process)
    if group:
        GROUP_NAMES[group] = name
    result = (name, group)
    if thread_pointer:
        THREAD_NAMES[thread_pointer] = result
    return result


def _header(process, address, requested=0x40):
    raw = _read(process, address, min(max(requested, 24), 0x100))
    if len(raw) < 24:
        return {"address": address, "requested": requested,
                "read": len(raw), "hex": raw.hex()}
    return {
        "address": address,
        "requested": requested,
        "read": len(raw),
        "bits": int.from_bytes(raw[0:4], "little"),
        "size": int.from_bytes(raw[4:8], "little"),
        "remote_port": int.from_bytes(raw[8:12], "little"),
        "local_port": int.from_bytes(raw[12:16], "little"),
        "voucher_port": int.from_bytes(raw[16:20], "little"),
        "message_id": int.from_bytes(raw[20:24], "little"),
        "prefix_hex": raw[:128].hex(),
    }


def _write_event(payload, name="ca-message-send.json"):
    if not EVENT_DIR:
        return
    os.makedirs(EVENT_DIR, exist_ok=True)
    path = os.path.join(EVENT_DIR, name)
    temporary = path + ".%d.tmp" % os.getpid()
    with open(temporary, "w") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _mutator_hint():
    """Best-effort same-boot hint from render_gate LAST_MUT if imported."""
    try:
        import render_gate_callbacks as rgc
        return getattr(rgc, "LAST_MUT", None)
    except Exception:
        return None


def _emit(frame, site, message_id, header, extra=None):
    global HITS
    process = frame.GetThread().GetProcess()
    name, group = _process_name(frame)
    HITS += 1
    thread = frame.GetThread()
    tpidr = _reg(frame, "tpidr_el1")
    payload = {
        "label": "CA_MSG_SEND",
        "site": site,
        "hit": HITS,
        "time": time.time(),
        "process": name,
        "thread_group": group,
        "thread": thread.GetThreadID(),
        "tpidr_el1": tpidr,
        "pc": _reg(frame, "pc"),
        "lr": _reg(frame, "lr"),
        "sp": _reg(frame, "sp"),
        "message_id": message_id,
        "header": header,
        "last_mutator": _mutator_hint(),
        "backtrace": [thread.GetFrameAtIndex(i).GetPC()
                      for i in range(min(thread.GetNumFrames(), 12))],
    }
    if (site == "send_wrapper" and message_id == 0x9c42 and
            os.environ.get("DVM_CA_MSG_FULL_BODY_9C42") == "1"):
        # The message-ID store runs before the encoder has filled the packet
        # body. At the send-wrapper entry, the complete message is ready but
        # has not yet been handed to mach_msg; capture it here before server
        # decoding can obscure whether geometry was already empty on the wire.
        address = header.get("address")
        size = header.get("size", 0)
        if address and size >= 24:
            error = lldb.SBError()
            raw = process.ReadMemory(address, min(size, 4096), error)
            if error.Success() and len(raw) >= 24:
                payload["body_full_hex"] = raw[24:].hex()
                payload["body_full_bytes"] = len(raw) - 24
    if extra:
        payload.update(extra)
    LAST_SEND[tpidr] = payload
    print("CA_MSG_SEND_JSON " + json.dumps(payload, sort_keys=True), flush=True)
    _write_event(payload)
    if message_id == 0x36:
        _write_event(payload, "ca-message-0x36-send.json")
    if HITS >= 64:
        for breakpoint_id in list(BP_IDS):
            process.GetTarget().FindBreakpointByID(breakpoint_id).SetEnabled(False)
        print("bounded-disabled CA_MSG_SEND after=%d" % HITS, flush=True)
    return False


def on_msgid_store(frame, bp_loc, _dict):
    """Break at stp wzr,w9,[x24,#0x10] — w9 is the chosen msgh_id."""
    if not _transition_gate_open():
        return False
    name, group = _process_name(frame)
    if PROCESS_NAMES and name not in PROCESS_NAMES:
        return False
    message_id = _reg(frame, "x9") & 0xffffffff
    x24 = _reg(frame, "x24")
    sample_key = ("msgid_store", name)
    samples = SAMPLES.get(sample_key, 0)
    SAMPLES[sample_key] = samples + 1
    header = _header(frame.GetThread().GetProcess(), x24) if x24 else {}
    # Header may not yet contain the store (BP is on the STP); prefer w9.
    header["message_id_register"] = message_id
    if message_id not in INTERESTING_IDS:
        if samples < 4:
            print("CA_MSGID_STORE_SAMPLE process=%s group=0x%x id=0x%x x24=0x%x" % (
                name, group, message_id, x24), flush=True)
        return False
    return _emit(frame, "msgid_store", message_id, header, {
        "x24": x24,
        "w8_bits_or_size": _reg(frame, "x8") & 0xffffffff,
        "w23_size": _reg(frame, "x23") & 0xffffffff,
    })


def on_send_wrapper(frame, bp_loc, _dict):
    """Break at send wrapper entry; x0 is the message buffer."""
    if not _transition_gate_open():
        return False
    name, group = _process_name(frame)
    if PROCESS_NAMES and name not in PROCESS_NAMES:
        return False
    process = frame.GetThread().GetProcess()
    address = _reg(frame, "x0")
    header = _header(process, address)
    message_id = header.get("message_id")
    sample_key = ("send_wrapper", name)
    samples = SAMPLES.get(sample_key, 0)
    SAMPLES[sample_key] = samples + 1
    if message_id not in INTERESTING_IDS:
        if samples < 4:
            print("CA_SEND_WRAPPER_SAMPLE process=%s group=0x%x id=%s size=%s" % (
                name, group,
                hex(message_id) if message_id is not None else None,
                header.get("size")), flush=True)
        return False
    return _emit(frame, "send_wrapper", message_id, header)


def on_walk360_ret(frame, bp_loc, _dict):
    """After bl 0x18469aadc(ctx+0x360); x0 is walk node (or null)."""
    if not _transition_gate_open():
        return False
    name, group = _process_name(frame)
    if PROCESS_NAMES and name not in PROCESS_NAMES:
        return False
    process = frame.GetThread().GetProcess()
    x0 = _reg(frame, "x0")
    x19 = _reg(frame, "x19")
    sample_key = ("walk360_ret", name)
    samples = SAMPLES.get(sample_key, 0)
    SAMPLES[sample_key] = samples + 1
    walk14 = _u32(process, x0 + 0x14) if x0 else None
    slot_360 = _u64(process, x19 + 0x360) if x19 else None
    extra = {
        "x0_walk": x0,
        "walk_plus_14": walk14,
        "ctx_360": slot_360,
        "null_walk": not bool(x0),
    }
    if samples < 16 or x0:
        return _emit(frame, "walk360_ret", walk14 or 0, {"message_id": walk14 or 0}, extra)
    return False


def on_commit_call(frame, bp_loc, _dict):
    """Break at BL to msg builder — dump ctx slots that feed the send."""
    if not _transition_gate_open():
        return False
    name, group = _process_name(frame)
    if PROCESS_NAMES and name not in PROCESS_NAMES:
        return False
    process = frame.GetThread().GetProcess()
    x19 = _reg(frame, "x19")
    x0 = _reg(frame, "x0")  # port object / ring
    w1 = _reg(frame, "x1") & 0xffffffff
    x2 = _reg(frame, "x2")
    x3 = _reg(frame, "x3")
    w4 = _reg(frame, "x4") & 0xffffffff
    sample_key = ("commit_call", name)
    samples = SAMPLES.get(sample_key, 0)
    SAMPLES[sample_key] = samples + 1
    slot_1e8 = _u32(process, x19 + 0x1e8) if x19 else None
    slot_360 = _u64(process, x19 + 0x360) if x19 else None
    ctx_210 = _u64(process, x19 + 0x210) if x19 else None
    ctx_228 = _u64(process, x19 + 0x228) if x19 else None
    extra = {
        "x19": x19,
        "x0_port": x0,
        "w1": w1,
        "x2": x2,
        "x3_cmd": x3,
        "w4_ok": w4,
        "ctx_1e8": slot_1e8,
        "ctx_360": slot_360,
        "ctx_210": ctx_210,
        "ctx_228": ctx_228,
    }
    if os.environ.get("DVM_CA_ENCODER_QUEUE_DUMP") == "1" and x0:
        # send_message serializes the encoder's pending byte queue from
        # [encoder+0x18, encoder+0x20). Snapshot it at the call site, before
        # the builder consumes/resets that queue. Cap reads to keep LLDB
        # callback latency and event size bounded.
        queue_begin = _u64(process, x0 + 0x18)
        queue_end = _u64(process, x0 + 0x20)
        queue_bytes = (queue_end - queue_begin
                       if queue_end >= queue_begin else None)
        extra.update({
            "encoder_object": x0,
            "encoder_queue_begin": queue_begin,
            "encoder_queue_end": queue_end,
            "encoder_queue_bytes": queue_bytes,
            "encoder_queue_limit": _u64(process, x0 + 0x28),
            "encoder_aux_pointer": _u64(process, x0 + 0x30),
            "encoder_aux_count": _u64(process, x0 + 0x38),
        })
        if queue_begin and queue_bytes is not None and queue_bytes <= 0x1000:
            error = lldb.SBError()
            raw = process.ReadMemory(queue_begin, queue_bytes, error)
            if error.Success():
                extra["encoder_queue_hex"] = raw.hex()
                extra["encoder_queue_captured_bytes"] = len(raw)
    # Always emit a few samples so we learn whether commit is reached at all.
    if samples < 8 or slot_1e8 not in (None, 0, 0xffffffff):
        return _emit(frame, "commit_call", w1, {"message_id": w1}, extra)
    return False


def on_send_global(frame, bp_loc, _dict):
    """Legacy global syscall filter — only when DVM_CA_MSG36_SEND_GLOBAL=1."""
    if not _transition_gate_open():
        return False
    process = frame.GetThread().GetProcess()
    name, group = _process_name(frame)
    if PROCESS_NAMES and name not in PROCESS_NAMES:
        return False
    trap = "mach_msg2_trap" if BP_IDS.get(
        bp_loc.GetBreakpoint().GetID()) == MACH_MSG2_TRAP + SLIDE else "mach_msg_trap"
    options = _reg(frame, "x1")
    if not options & 1:
        return False
    address = _reg(frame, "x0")
    if trap == "mach_msg2_trap":
        packed_send = _reg(frame, "x2") >> 32
        header = _header(process, address, packed_send)
        header["arg_message_id"] = _reg(frame, "x4") >> 32
    else:
        header = _header(process, address, _reg(frame, "x2"))
    message_id = header.get("message_id")
    argument_id = header.get("arg_message_id")
    sample_key = (name, trap)
    samples = SAMPLES.get(sample_key, 0)
    SAMPLES[sample_key] = samples + 1
    if message_id not in INTERESTING_IDS and argument_id not in INTERESTING_IDS:
        if samples < 2:
            print("CA_SEND_SAMPLE process=%s trap=%s group=0x%x id=%s arg_id=%s" % (
                name, trap, group,
                hex(message_id) if message_id is not None else None,
                hex(argument_id) if argument_id is not None else None), flush=True)
        return False
    return _emit(frame, trap, message_id or argument_id, header)


def _install_one(debugger, slide, static_address, label, callback_name):
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    address = static_address + int(slide)
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint set --address 0x%x" % address, result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed %s breakpoint: %s" % (label, result.GetError()))
    breakpoint = target.GetBreakpointAtIndex(before)
    BP_IDS[breakpoint.GetID()] = address
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand(
        "breakpoint command add -F ca_msg36_send_callbacks.%s %d" %
        (callback_name, breakpoint.GetID()), result)
    if not result.Succeeded():
        raise RuntimeError("failed %s callback: %s" % (label, result.GetError()))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" % (
        breakpoint.GetID(), label, address), flush=True)


def install(debugger, slide):
    global SLIDE, HITS
    global AFTER_LOCK_TRANSITION, TRANSITION_MARKER
    SLIDE = int(slide)
    HITS = 0
    AFTER_LOCK_TRANSITION = os.environ.get(
        "DVM_CA_MSG_AFTER_LOCK_TRANSITION", "0") == "1"
    TRANSITION_MARKER = os.environ.get(
        "DVM_LOCK_GESTURE_TRANSITION_MARKER", "")
    GROUP_NAMES.clear()
    THREAD_NAMES.clear()
    SAMPLES.clear()
    LAST_SEND.clear()
    BP_IDS.clear()
    _install_one(debugger, slide, WALK360_RET, "CA_WALK360_RET", "on_walk360_ret")
    _install_one(debugger, slide, COMMIT_CALL, "CA_COMMIT_CALL", "on_commit_call")
    _install_one(debugger, slide, MSGID_STORE, "CA_MSGID_STORE", "on_msgid_store")
    _install_one(debugger, slide, SEND_WRAPPER, "CA_SEND_WRAPPER", "on_send_wrapper")
    if os.environ.get("DVM_CA_MSG36_SEND_GLOBAL") == "1":
        _install_one(debugger, slide, MACH_MSG2_TRAP, "CA_MSG36_SEND_MACH_MSG2",
                     "on_send_global")
        _install_one(debugger, slide, MACH_MSG_TRAP, "CA_MSG36_SEND_MACH_MSG",
                     "on_send_global")

"""Observe the BackBoard DataMigrationCheckIn handler on a live boot.

Resolve backboardd's PIE base from a frame in its own address space before
arming its static offsets. This is read-only and does not alter the check-in.
"""

import json
import time

import lldb

import ccs_ls_seeding_correlation_callbacks as base
import display_query_callbacks as display_query


ANCHOR = 0x237CCFCCC  # mach_msg2_trap, used only to find backboardd's PIE
CLIENT_CALL = 0x18A1476A0
CLIENT_RETURN = 0x18A1476A4
SERVER_MESSAGE = 0x18F8BAF64  # -[BSXPCServiceConnectionEventHandler connection:handleMessage:]
SERVER_ERROR = 0x18F8CCE28   # -[BSXPCServiceConnectionEventHandler connection:handleError:]
CLIENT_XPC_SEND = 0x1805B29D4  # xpc_connection_send_message_with_reply_sync
SERVER_XPC_MACH_EVENT = 0x1805B5DD0  # x1 == 2: dispatch-Mach receive
SERVER_XPC_HANDLER = 0x1805B3A44  # __xpc_connection_call_event_handler
SERVER_INCOMING_BS_CONNECTION = 0x18F8D4044  # incoming XPC -> BoardServices connection
SERVER_BS_CONNECTION_INITIALIZED = 0x18F8CB654  # BS connection + active XPC connection
XPC_DICT_HANDOFF = 0x1805BCAF4
XPC_MACH_HANDOFF = 0x18059A8D0
XPC_MACH_DISPATCH_ARM = 0x18059A94C
XPC_CHANNEL_DISPATCH_ARM = 0x18059A98C
DISPATCH_MACH_HANDOFF = 0x1AE8D571C
SERVER_BS_DISPATCH_SITES = (
    (0x18F8D697C, "SERVER_BS_ACTIVATION_MESSAGE"),
    (0x18F8D66F8, "SERVER_BS_CONNECTION_ACTIVATED"),
    (0x18F8D72D4, "SERVER_BS_HANDLE_CONNECTION"),
    (0x18F8B90BC, "SERVER_BS_HANDLE_MESSAGE"),
    (0x18F8B9120, "SERVER_BS_MESSAGE_INVALIDATED_ARM"),
    (0x18F8B9180, "SERVER_BS_MESSAGE_CHILD_CHECK"),
    (0x18F8B927C, "SERVER_BS_MESSAGE_ORDINARY_ARM"),
    (0x18F8B9294, "SERVER_BS_MESSAGE_DID_ACTIVATE_CALL"),
    (0x18F8B92A4, "SERVER_BS_MESSAGE_EVENT_HANDLER_CALL"),
    (0x18F8B9374, "SERVER_BS_MESSAGE_CHILD_QUEUE_CALL"),
    (0x18F8BB140, "SERVER_BS_CHILD_ENQUEUE"),
    (0x18F8BB1C4, "SERVER_BS_CHILD_ENQUEUE_VALID"),
    (0x18F8BB1DC, "SERVER_BS_CHILD_IMMEDIATE"),
    (0x18F8BB210, "SERVER_BS_CHILD_PENDING"),
    (0x18F8BAB88, "SERVER_BS_RUN_PENDED_EVENTS"),
    (0x18F8B9848, "SERVER_BS_CHILD_BLOCK_INVOKE"),
    (0x18F8BFC98, "SERVER_BS_ASYNC_DISPATCH_QUEUE"),
    (0x18F8ECA80, "SERVER_BS_ASYNC_MAIN_RUNLOOP"),
    (0x18F8F31EC, "SERVER_BS_ASYNC_REPLY_FALLBACK"),
    (0x18F907028, "SERVER_BS_ASYNC_MAIN_THREAD"),
    (0x18F90742C, "SERVER_BS_ASYNC_COMPOUND"),
    (0x18F907DA0, "SERVER_BS_ASYNC_BASE"),
    (0x18F8CD114, "SERVER_BS_INVALIDATE_MESSAGE"),
    (0x18F8CD684, "SERVER_BS_HANDLE_ERROR"),
)
SERVER_SITES = (
    (0x164D4, "SERVER_MIGRATION_LISTENER"),
    (0x16610, "SERVER_CONFIGURE_CONNECTION"),
    (0x41944, "SERVER_DATA_MIGRATOR_COMPLETE"),
    (0x433B4, "SERVER_CHECKIN_AFTER_MIGRATION"),
    (0x43274, "SERVER_LOCK_CHECKIN"),
    (0x42FFC, "SERVER_COMPLETE_CHECKIN"),
    (0x430B4, "SERVER_CHECKIN_DECISION"),
    (0x43130, "SERVER_CHECKIN_PENDING"),
    (0x431D8, "SERVER_CHECKIN_IMMEDIATE"),
    (0x4342C, "SERVER_CHECKIN_METHOD_RETURN"),
)

PIE_BASE = None
SITE_LABELS = {}
HITS = {}
ANCHOR_HITS = 0
RETURNS = {}
CLIENT_TIME = None
GENERIC_IDS = []
CLIENT_THREAD = None
MIGRATION_WRAPPER = None
MIGRATION_CONFIG_ARG = None
CHILD_BLOCK = None
HANDOFF_THREAD = None
HANDOFF_TIME = None
CALLBACK_MODULE = "bks_server_receive_callbacks"
DIH_TO_BLOCK = {}
HANDOFF_DIH = None
HANDOFF_QUEUE = None
ENABLE_DRAIN_PROBES = False
DRAIN_ARMED = False
DRAIN_HITS = {}
DRAIN_SITES = (
    (0x1AE8C4BD4, "DISPATCH_LANE_PUSH"),
    (0x1AE8C4A2C, "DISPATCH_LANE_WAKEUP"),
    (0x1AE8C2338, "DISPATCH_LANE_INVOKE"),
    (0x1AE8C15F8, "DISPATCH_LANE_SERIAL_DRAIN"),
    (0x1AE8C3070, "DISPATCH_WORKLOOP_WAKEUP"),
    (0x1AE8C31D4, "DISPATCH_WORKLOOP_INVOKE"),
)


def _connection_name(process, connection):
    """Mirror xpc_connection_get_name's read-only +0xa8 field access."""
    if not connection:
        return None
    raw = base._read(process, connection + 0xA8, 8)
    if len(raw) != 8:
        return None
    pointer = int.from_bytes(raw, "little") & 0x0000FFFFFFFFFFFF
    if not pointer:
        return None
    value = base._read(process, pointer, 160)
    if not value:
        return None
    value = value.split(b"\0", 1)[0]
    try:
        decoded = value.decode("ascii")
    except UnicodeDecodeError:
        return None
    return decoded if decoded.isprintable() else None


def _peer_pid(process, connection):
    """Mirror xpc_connection_get_pid's read-only +0x7c field access."""
    raw = base._read(process, connection + 0x7C, 4) if connection else b""
    return int.from_bytes(raw, "little", signed=True) if len(raw) == 4 else None


def _pointer_fields(process, pointer, size=0x200):
    raw = base._read(process, pointer, size) if pointer else b""
    return {"0x%x" % offset: "0x%x" % value
            for offset in range(0, len(raw) - 7, 8)
            if 0x100000000 <= (value := int.from_bytes(raw[offset:offset + 8], "little"))
            < 0x800000000000}


def _queue_chain(process, queue, limit=16):
    """Read a bounded lane do_next chain without calling guest code."""
    raw = base._read(process, queue + 0x68, 8) if queue else b""
    item = int.from_bytes(raw, "little") if len(raw) == 8 else 0
    chain = []
    seen = set()
    while item and item not in seen and len(chain) < limit:
        seen.add(item)
        data = base._read(process, item, 0x40)
        if len(data) < 0x18:
            chain.append({"item": item, "bytes": data.hex(),
                          "read_failed": True})
            break
        next_item = int.from_bytes(data[0x10:0x18], "little")
        target_queue = int.from_bytes(data[0x18:0x20], "little")
        context = int.from_bytes(data[0x28:0x30], "little")
        chain.append({"item": item, "next": next_item,
                      "bytes": data.hex(), "target_queue": target_queue,
                      "context": context,
                      "context_bytes": base._read(process, context, 0x60).hex()
                      if context else ""})
        item = next_item
    return chain


def _emit(label, frame, **extra):
    thread = frame.GetThread()
    process = thread.GetProcess()
    payload = {
        "label": label,
        "time": time.time(),
        "progname": base._progname(process),
        "pc": base._reg(frame, "pc"),
        "lr": base._reg(frame, "lr"),
        "thread_pointer": base._reg(frame, "tpidr_el1"),
        "x0": base._reg(frame, "x0"),
        "x1": base._reg(frame, "x1"),
        "x2": base._reg(frame, "x2"),
        "x3": base._reg(frame, "x3"),
        "x19": base._reg(frame, "x19"),
        "x20": base._reg(frame, "x20"),
        "x21": base._reg(frame, "x21"),
        "x22": base._reg(frame, "x22"),
        "backtrace": [thread.GetFrameAtIndex(i).GetPC()
                      for i in range(min(thread.GetNumFrames(), 14))],
    }
    payload.update(extra)
    print("BKS_SERVER_RECEIVE_JSON " + json.dumps(payload, sort_keys=True),
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
    SITE_LABELS[breakpoint.GetID()] = label
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand(
        "breakpoint command add -F %s.%s %d" %
        (CALLBACK_MODULE, callback, breakpoint.GetID()), result)
    if not result.Succeeded():
        raise RuntimeError("failed callback %s: %s" %
                           (label, result.GetError()))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
          (breakpoint.GetID(), label, address), flush=True)
    return breakpoint


def _resolve_base(frame):
    process = frame.GetThread().GetProcess()
    thread = frame.GetThread()
    for index in range(min(thread.GetNumFrames(), 24)):
        address = thread.GetFrameAtIndex(index).GetPC() & 0x0000FFFFFFFFFFFF
        if not 0x100000000 <= address < 0x140000000:
            continue
        found = display_query._find_macho_base(process, address)
        if found is not None:
            return found, address, index
    return None, None, None


def on_anchor(frame, bp_loc, context):
    global PIE_BASE, ANCHOR_HITS
    if PIE_BASE is not None or base._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    ANCHOR_HITS += 1
    if ANCHOR_HITS > 64:
        bp_loc.GetBreakpoint().SetEnabled(False)
        print("BKS_SERVER_ANCHOR_EXHAUSTED", flush=True)
        return False
    found, address, index = _resolve_base(frame)
    if found is None:
        if ANCHOR_HITS in (1, 8, 32, 64):
            _emit("SERVER_BASE_UNRESOLVED", frame, anchor_hits=ANCHOR_HITS)
        return False
    PIE_BASE = found
    _emit("SERVER_BASE_RESOLVED", frame, pie_base=found,
          executable_frame=address, frame_index=index,
          anchor_hits=ANCHOR_HITS)
    debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
    for offset, label in SERVER_SITES:
        _install(debugger, found + offset, label, "on_site")
    bp_loc.GetBreakpoint().SetEnabled(False)
    return False


def on_site(frame, bp_loc, context):
    global MIGRATION_WRAPPER, MIGRATION_CONFIG_ARG
    if base._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    label = SITE_LABELS[bp_loc.GetBreakpoint().GetID()]
    count = HITS.get(label, 0) + 1
    HITS[label] = count
    if count <= 12:
        extra = {"pie_base": PIE_BASE, "hit": count}
        if label == "SERVER_MIGRATION_LISTENER":
            MIGRATION_WRAPPER = base._reg(frame, "x3")
            extra["migration_wrapper"] = MIGRATION_WRAPPER
            extra["migration_wrapper_fields"] = _pointer_fields(
                frame.GetThread().GetProcess(), MIGRATION_WRAPPER)
        elif label == "SERVER_CONFIGURE_CONNECTION":
            MIGRATION_CONFIG_ARG = base._reg(frame, "x1")
            extra["config_arg"] = MIGRATION_CONFIG_ARG
            extra["config_arg_fields"] = _pointer_fields(
                frame.GetThread().GetProcess(), MIGRATION_CONFIG_ARG)
        if label in ("SERVER_MIGRATION_LISTENER", "SERVER_CONFIGURE_CONNECTION",
                     "SERVER_CHECKIN_AFTER_MIGRATION"):
            process = frame.GetThread().GetProcess()
            pointer = base._read(process, PIE_BASE + 0xAA668, 8)
            sentinel = int.from_bytes(pointer, "little") if len(pointer) == 8 else 0
            state = base._read(process, sentinel + 0x20, 0x60) if sentinel else b""
            extra.update(sentinel=sentinel, sentinel_state=state.hex(),
                         system_app_blocked=(state[0x30] if len(state) > 0x30
                                             else None))
            if count == 1:
                address = base._reg(frame, "lr") & 0x0000FFFFFFFFFFFF
                debugger = process.GetTarget().GetDebugger()
                ret = _install(debugger, address, label + "_RETURN", "on_site_return")
                RETURNS[ret.GetID()] = (label, base._reg(frame, "tpidr_el1"), time.time())
        _emit(label, frame, **extra)
    if count >= 12:
        bp_loc.GetBreakpoint().SetEnabled(False)
    return False


def on_site_return(frame, bp_loc, context):
    breakpoint = bp_loc.GetBreakpoint()
    source, thread_pointer, started = RETURNS[breakpoint.GetID()]
    if (base._progname(frame.GetThread().GetProcess()) == "backboardd" and
            base._reg(frame, "tpidr_el1") == thread_pointer):
        _emit(source + "_RETURN", frame, elapsed=time.time() - started,
              pie_base=PIE_BASE)
        breakpoint.SetEnabled(False)
    return False


def on_client(frame, bp_loc, context):
    global CLIENT_TIME, CLIENT_THREAD
    if base._progname(frame.GetThread().GetProcess()) != "SpringBoard":
        return False
    label = SITE_LABELS[bp_loc.GetBreakpoint().GetID()]
    if label == "CLIENT_CHECKIN_CALL":
        CLIENT_TIME = time.time()
        CLIENT_THREAD = base._reg(frame, "tpidr_el1")
        target = frame.GetThread().GetProcess().GetTarget()
        for breakpoint_id in GENERIC_IDS:
            target.FindBreakpointByID(breakpoint_id).SetEnabled(True)
    _emit(label, frame, server_sites_armed=PIE_BASE is not None,
          pie_base=PIE_BASE)
    return False


def on_client_xpc_send(frame, bp_loc, context):
    if (CLIENT_TIME is None or
            base._progname(frame.GetThread().GetProcess()) != "SpringBoard" or
            base._reg(frame, "tpidr_el1") != CLIENT_THREAD):
        return False
    count = HITS.get("CLIENT_XPC_SEND", 0) + 1
    HITS["CLIENT_XPC_SEND"] = count
    if count <= 5:
        _emit("CLIENT_XPC_SEND", frame, hit=count,
              since_client=time.time() - CLIENT_TIME)
    if count >= 5:
        bp_loc.GetBreakpoint().SetEnabled(False)
    return False


def on_server_xpc_event(frame, bp_loc, context):
    if base._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    event = base._reg(frame, "x1")
    phase = "PRE" if CLIENT_TIME is None else "POST"
    key = "SERVER_XPC_RECV_" + phase if event == 2 else "SERVER_XPC_OTHER_" + phase
    count = HITS.get(key, 0) + 1
    HITS[key] = count
    limit = 5 if CLIENT_TIME is None else 24
    connection = base._reg(frame, "x0")
    name = _connection_name(frame.GetThread().GetProcess(), connection)
    peer_pid = _peer_pid(frame.GetThread().GetProcess(), connection)
    interesting = name in ("com.apple.backboard.display.services",
                           "com.apple.backboard.system-app-server")
    if count <= limit or interesting:
        _emit(key, frame, hit=count, event_code=event,
              connection=connection, connection_name=name, peer_pid=peer_pid,
              since_client=(None if CLIENT_TIME is None
                            else time.time() - CLIENT_TIME),
              pie_base=PIE_BASE)
    return False


def on_server_xpc_handler(frame, bp_loc, context):
    if base._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    phase = "PRE" if CLIENT_TIME is None else "POST"
    key = "SERVER_XPC_HANDLER_" + phase
    count = HITS.get(key, 0) + 1
    HITS[key] = count
    connection = base._reg(frame, "x0")
    name = _connection_name(frame.GetThread().GetProcess(), connection)
    peer_pid = _peer_pid(frame.GetThread().GetProcess(), connection)
    interesting = name in ("com.apple.backboard.display.services",
                           "com.apple.backboard.system-app-server")
    if count <= (5 if CLIENT_TIME is None else 24) or interesting:
        _emit(key, frame, hit=count, connection=connection,
              connection_name=name, peer_pid=peer_pid,
              since_client=(None if CLIENT_TIME is None
                            else time.time() - CLIENT_TIME),
              pie_base=PIE_BASE)
    return False


def on_incoming_bs_connection(frame, bp_loc, context):
    if base._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    count = HITS.get("SERVER_INCOMING_BS_CONNECTION", 0) + 1
    HITS["SERVER_INCOMING_BS_CONNECTION"] = count
    if count <= 24:
        process = frame.GetThread().GetProcess()
        _emit("SERVER_INCOMING_BS_CONNECTION", frame, hit=count,
              arg1_peer_pid=_peer_pid(process, base._reg(frame, "x1")),
              arg2_peer_pid=_peer_pid(process, base._reg(frame, "x2")),
              arg3_peer_pid=_peer_pid(process, base._reg(frame, "x3")),
              since_client=(None if CLIENT_TIME is None
                            else time.time() - CLIENT_TIME))
    return False


def on_bs_connection_initialized(frame, bp_loc, context):
    if base._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    count = HITS.get("SERVER_BS_CONNECTION_INITIALIZED", 0) + 1
    HITS["SERVER_BS_CONNECTION_INITIALIZED"] = count
    if count <= 32:
        process = frame.GetThread().GetProcess()
        _emit("SERVER_BS_CONNECTION_INITIALIZED", frame, hit=count,
              bs_connection=base._reg(frame, "x1"),
              xpc_connection=base._reg(frame, "x3"),
              xpc_peer_pid=_peer_pid(process, base._reg(frame, "x3")),
              migration_wrapper=MIGRATION_WRAPPER,
              migration_wrapper_fields=(
                  _pointer_fields(process, MIGRATION_WRAPPER)
                  if MIGRATION_WRAPPER and CLIENT_TIME is not None else {}),
              config_arg=MIGRATION_CONFIG_ARG,
              config_arg_fields=(
                  _pointer_fields(process, MIGRATION_CONFIG_ARG)
                  if MIGRATION_CONFIG_ARG and CLIENT_TIME is not None else {}),
              since_client=(None if CLIENT_TIME is None
                            else time.time() - CLIENT_TIME))
    return False


def on_bs_dispatch(frame, bp_loc, context):
    global CHILD_BLOCK
    if base._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    label = SITE_LABELS[bp_loc.GetBreakpoint().GetID()]
    if (CLIENT_TIME is None and label in
            ("SERVER_BS_HANDLE_MESSAGE", "SERVER_BS_CHILD_ENQUEUE",
             "SERVER_BS_ASYNC_DISPATCH_QUEUE", "SERVER_BS_CHILD_BLOCK_INVOKE")):
        return False
    count = HITS.get(label, 0) + 1
    HITS[label] = count
    if count <= 24:
        process = frame.GetThread().GetProcess()
        if label == "SERVER_BS_CHILD_ENQUEUE":
            CHILD_BLOCK = base._reg(frame, "x1")
            # The parent BS handler may have entered before the client check-in
            # hook fired. Its caller return is frame 2 while enqueue is active;
            # arm that exact same-thread return here, after matching the child.
            if count == 1 and frame.GetThread().GetNumFrames() > 2:
                address = (frame.GetThread().GetFrameAtIndex(2).GetPC()
                           & 0x0000FFFFFFFFFFFF)
                debugger = process.GetTarget().GetDebugger()
                ret = _install(debugger, address,
                               "SERVER_BS_HANDLE_MESSAGE_RETURN",
                               "on_site_return")
                RETURNS[ret.GetID()] = ("SERVER_BS_HANDLE_MESSAGE",
                                        base._reg(frame, "tpidr_el1"), time.time())
        state = base._read(process, base._reg(frame, "x0") + 0xA6, 6)
        pending = base._read(process, base._reg(frame, "x0") + 0x50, 8)
        block_fields = (_pointer_fields(process, base._reg(frame, "x0"), 0x40)
                        if label == "SERVER_BS_CHILD_BLOCK_INVOKE" else {})
        _emit(label, frame, hit=count,
              object_state_a6_ab=state.hex(),
              object_pending_50=(int.from_bytes(pending, "little")
                                 if len(pending) == 8 else None),
              child_block=CHILD_BLOCK,
              child_block_fields=block_fields,
              migration_wrapper=MIGRATION_WRAPPER,
              migration_bs_connection=(
                  int(_pointer_fields(process,
                                      MIGRATION_WRAPPER, 0x10).get("0x8", "0"), 16)
                  if MIGRATION_WRAPPER else None),
              since_client=(None if CLIENT_TIME is None
                            else time.time() - CLIENT_TIME))
    return False


def on_xpc_handoff(frame, bp_loc, context):
    global HANDOFF_THREAD, HANDOFF_TIME, HANDOFF_DIH, HANDOFF_QUEUE, DRAIN_ARMED
    if base._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    label = SITE_LABELS[bp_loc.GetBreakpoint().GetID()]
    thread_pointer = base._reg(frame, "tpidr_el1")
    if label == "XPC_IPC_INVOKE":
        dih = base._reg(frame, "x0")
        if dih in DIH_TO_BLOCK:
            _emit(label, frame, dih=dih, child_block=DIH_TO_BLOCK[dih],
                  continuation_bytes=base._read(
                      frame.GetThread().GetProcess(), dih, 0x58).hex())
        return False
    if label == "XPC_DICT_HANDOFF":
        if CHILD_BLOCK is None or base._reg(frame, "x2") != CHILD_BLOCK:
            return False
        HANDOFF_THREAD = thread_pointer
        HANDOFF_TIME = time.time()
        HANDOFF_QUEUE = base._reg(frame, "x1")
        HANDOFF_DIH = None
        if ENABLE_DRAIN_PROBES and not DRAIN_ARMED:
            debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
            for address, drain_label in DRAIN_SITES:
                _install(debugger, base.SLIDE + address, drain_label,
                         "on_queue_drain")
            DRAIN_ARMED = True
    elif HANDOFF_THREAD is None or thread_pointer != HANDOFF_THREAD:
        return False
    process = frame.GetThread().GetProcess()
    extra = {}
    if label == "XPC_DICT_HANDOFF":
        extra["queue_bytes"] = base._read(process, base._reg(frame, "x1"), 0x90).hex()
        extra["queue_chain"] = _queue_chain(process, HANDOFF_QUEUE)
        extra["dict_bytes"] = base._read(process, base._reg(frame, "x0"), 0x90).hex()
        extra["block_bytes"] = base._read(process, CHILD_BLOCK, 0x40).hex()
    elif label == "DISPATCH_HANDOFF_SET_WLH":
        HANDOFF_DIH = base._reg(frame, "x0")
        DIH_TO_BLOCK[HANDOFF_DIH] = CHILD_BLOCK
        extra["queue_bytes"] = base._read(process, base._reg(frame, "x1"), 0x90).hex()
        extra["dih_bytes"] = base._read(process, base._reg(frame, "x0"), 0x58).hex()
    elif label == "DISPATCH_HANDOFF_SET_WLH_RETURN":
        dih = base._reg(frame, "x19")
        extra["dih"] = dih
        extra["dih_bytes"] = base._read(process, dih, 0x58).hex()
    elif label == "XPC_OUTER_CALLOUT_RETURN":
        extra["handoff_context"] = base._reg(frame, "x9")
    elif label == "XPC_ASYNC_ENTRY":
        extra["context_bytes"] = base._read(process, base._reg(frame, "x0"), 0x40).hex()
        raw = base._read(process, base._reg(frame, "x0") + 0x10, 8)
        extra["dih"] = int.from_bytes(raw, "little") if len(raw) == 8 else None
        extra["continuation_bytes"] = base._read(
            process, extra["dih"], 0x58).hex() if extra["dih"] else ""
    if label in ("XPC_ASYNC_ENQUEUE_RETURN", "XPC_OUTER_CALLOUT_RETURN"):
        extra["handoff_dih"] = HANDOFF_DIH
        extra["handoff_queue"] = HANDOFF_QUEUE
        extra["continuation_bytes"] = base._read(
            process, HANDOFF_DIH, 0x58).hex() if HANDOFF_DIH else ""
        extra["queue_bytes"] = base._read(
            process, HANDOFF_QUEUE, 0x90).hex() if HANDOFF_QUEUE else ""
        extra["queue_chain"] = _queue_chain(process, HANDOFF_QUEUE)
        raw = base._read(process, HANDOFF_QUEUE + 0x18, 8) if HANDOFF_QUEUE else b""
        target = int.from_bytes(raw, "little") if len(raw) == 8 else None
        extra["queue_target"] = target
        extra["queue_target_bytes"] = base._read(
            process, target, 0x90).hex() if target else ""
    _emit(label, frame, child_block=CHILD_BLOCK,
          elapsed_from_handoff=(None if HANDOFF_TIME is None
                                else time.time() - HANDOFF_TIME), **extra)
    if label in ("XPC_DICT_HANDOFF", "XPC_MACH_HANDOFF", "DISPATCH_MACH_HANDOFF"):
        address = base._reg(frame, "lr") & 0x0000FFFFFFFFFFFF
        process = frame.GetThread().GetProcess()
        debugger = process.GetTarget().GetDebugger()
        ret = _install(debugger, address, label + "_RETURN", "on_site_return")
        RETURNS[ret.GetID()] = (label, thread_pointer, time.time())
    return False


def on_queue_drain(frame, bp_loc, context):
    if base._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    if not HANDOFF_QUEUE:
        return False
    process = frame.GetThread().GetProcess()
    raw = base._read(process, HANDOFF_QUEUE + 0x18, 8)
    target = int.from_bytes(raw, "little") if len(raw) == 8 else None
    queue_arg = base._reg(frame, "x0")
    if queue_arg not in (HANDOFF_QUEUE, target):
        return False
    label = SITE_LABELS[bp_loc.GetBreakpoint().GetID()]
    key = (label, queue_arg)
    DRAIN_HITS[key] = DRAIN_HITS.get(key, 0) + 1
    count = DRAIN_HITS[key]
    if count <= 20 or count % 100 == 0:
        _emit(label, frame, hit=count, queue_arg=queue_arg,
              migration_queue=HANDOFF_QUEUE, queue_target=target,
              handoff_dih=HANDOFF_DIH,
              elapsed_from_handoff=(None if HANDOFF_TIME is None
                                    else time.time() - HANDOFF_TIME),
              queue_bytes=base._read(process, HANDOFF_QUEUE, 0x90).hex(),
              target_bytes=base._read(process, target, 0x90).hex()
              if target else "",
              continuation_bytes=base._read(process, HANDOFF_DIH, 0x58).hex()
              if HANDOFF_DIH else "")
    return False


def on_generic_server(frame, bp_loc, context):
    if base._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    label = SITE_LABELS[bp_loc.GetBreakpoint().GetID()]
    key = label + ("_PRE" if CLIENT_TIME is None else "_POST")
    count = HITS.get(key, 0) + 1
    HITS[key] = count
    limit = 3 if CLIENT_TIME is None else 20
    if count <= limit:
        _emit(key, frame, hit=count,
              since_client=(None if CLIENT_TIME is None
                            else time.time() - CLIENT_TIME),
              pie_base=PIE_BASE)
    if count >= limit:
        bp_loc.GetBreakpoint().SetEnabled(False)
    return False


def install(debugger, slide):
    global PIE_BASE, ANCHOR_HITS, CLIENT_TIME, CLIENT_THREAD
    global MIGRATION_WRAPPER, MIGRATION_CONFIG_ARG, CHILD_BLOCK
    global HANDOFF_THREAD, HANDOFF_TIME
    PIE_BASE = None
    ANCHOR_HITS = 0
    CLIENT_TIME = None
    CLIENT_THREAD = None
    MIGRATION_WRAPPER = None
    MIGRATION_CONFIG_ARG = None
    CHILD_BLOCK = None
    HANDOFF_THREAD = None
    HANDOFF_TIME = None
    SITE_LABELS.clear()
    HITS.clear()
    RETURNS.clear()
    GENERIC_IDS.clear()
    base.SLIDE = int(slide)
    display_query.SLIDE[0] = int(slide)
    _install(debugger, base.SLIDE + ANCHOR, "SERVER_BASE_ANCHOR", "on_anchor")
    _install(debugger, base.SLIDE + CLIENT_CALL, "CLIENT_CHECKIN_CALL", "on_client")
    _install(debugger, base.SLIDE + CLIENT_RETURN, "CLIENT_CHECKIN_RETURN", "on_client")
    _install(debugger, base.SLIDE + CLIENT_XPC_SEND, "CLIENT_XPC_SEND",
             "on_client_xpc_send")
    _install(debugger, base.SLIDE + SERVER_XPC_MACH_EVENT,
             "SERVER_XPC_MACH_EVENT", "on_server_xpc_event")
    _install(debugger, base.SLIDE + SERVER_XPC_HANDLER,
             "SERVER_XPC_HANDLER", "on_server_xpc_handler")
    _install(debugger, base.SLIDE + SERVER_INCOMING_BS_CONNECTION,
             "SERVER_INCOMING_BS_CONNECTION", "on_incoming_bs_connection")
    _install(debugger, base.SLIDE + SERVER_BS_CONNECTION_INITIALIZED,
             "SERVER_BS_CONNECTION_INITIALIZED", "on_bs_connection_initialized")
    for address, label in SERVER_BS_DISPATCH_SITES:
        _install(debugger, base.SLIDE + address, label, "on_bs_dispatch")
    for address, label in ((XPC_DICT_HANDOFF, "XPC_DICT_HANDOFF"),
                           (XPC_MACH_HANDOFF, "XPC_MACH_HANDOFF"),
                           (XPC_MACH_DISPATCH_ARM, "XPC_MACH_DISPATCH_ARM"),
                           (XPC_CHANNEL_DISPATCH_ARM, "XPC_CHANNEL_DISPATCH_ARM"),
                           (DISPATCH_MACH_HANDOFF, "DISPATCH_MACH_HANDOFF")):
        _install(debugger, base.SLIDE + address, label, "on_xpc_handoff")
    for address, label in ((SERVER_MESSAGE, "SERVER_HANDLE_MESSAGE"),
                           (SERVER_ERROR, "SERVER_HANDLE_ERROR")):
        breakpoint = _install(debugger, base.SLIDE + address, label,
                              "on_generic_server")
        GENERIC_IDS.append(breakpoint.GetID())

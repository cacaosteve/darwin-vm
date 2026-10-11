"""Follow a delivered SystemAppMigrator kmsg through wrapper-side XPC dispatch.

This composes the proven sender/object-equality probe, then adds read-only
samples at shared libxpc dispatch-Mach/event-handler boundaries and the
BoardServices message handler. Event-handler candidates are sampled before
the request join as well as after it; only same-thread/connection/request
evidence attributes one to the matched delivery. A handler/event hit is not
itself a terminal reply; the existing kernel probe still requires a
reply-object send/handoff and same-owner sync return to close that edge.
"""

import json
import time

import lldb

import bks_checkin_callbacks as bks
import dm_lock_checkin_plugin_callbacks as transport
import dm_lock_checkin_plugin_kernelpath_callbacks as kernelpath

SYSTEMAPP_ID = "com.apple.MobileInstallation.SystemAppMigrator"


# These shared-cache offsets are also used by bks_server_receive_callbacks.
# The callback filters to migrationpluginwrapper and records argument bytes
# and frame PCs, rather than assuming that the XPC event is the app request.
SITES = (
    (0x1805B5DD0, "WRAPPER_XPC_MACH_EVENT"),
    (0x1805B3A44, "WRAPPER_XPC_EVENT_HANDLER"),
    (0x18059A8D0, "WRAPPER_XPC_MACH_HANDOFF"),
    (0x18059A94C, "WRAPPER_XPC_MACH_DISPATCH_ARM"),
    (0x18059A98C, "WRAPPER_XPC_CHANNEL_DISPATCH_ARM"),
    # Duplicate the existing lifecycle send hook with a callback that can
    # retain the full matched message even when its generic raw-send quota is
    # exhausted by unrelated connections.
    (0x1805B2480, "WRAPPER_XPC_SEND"),
    (0x18F8BAF64, "WRAPPER_BS_HANDLE_MESSAGE"),
    # DataMigration.framework's matched client method. The completion block
    # supplied by __DMPerformMigration is x5 at method entry; this method's
    # synchronous plugin-send call returns at +0x1f8c, then invokes its local
    # result callback at +0x2068. These are observation boundaries, not proof
    # that the local callback is the caller's block until the captured block
    # invoke/capture is linked at runtime.
    (0x25E8F1A10, "DM_WRAPPER_RUN_PLUGIN_ENTRY"),
    (0x25E8F1F8C, "DM_WRAPPER_PLUGIN_SYNC_RETURN"),
    (0x25E8F2068, "DM_WRAPPER_RESULT_CALLBACK_INVOKE"),
)
META = {}
ARMED = False
HANDOFF_EVENT = None
DM_OUTBOUND_HANDOFF = None
SYSTEMAPP_CALLBACK_CONTEXT = None
MSU_CALLBACK_CONTEXT = None
MIGRATION_RESULT_RECEIVER_CONTEXT = None
ORIGINAL_EVENT = None
HITS = {}
HANDLER_INVOKES = {}
HANDLER_BREAKPOINTS = set()
MAX_HANDLER_BREAKPOINTS = 64
REGISTERED_HITS = 0
MATCHED_SYNC_OWNER = None
MATCHED_REQUEST = None
MATCHED_REPLY_OBJECT = None
MATCHED_DEST_OBJECT = None
DECODED_SENDS = 0
DECODED_CLIENT_MESSAGES = 0
RUN_PLUGIN_CALLS = {}
ACTIVE_SLIDE = 0
OPEN_SITE_BP = None
OPEN_RETURN_META = {}
OPEN_RETURN_BY_ADDRESS = {}
WRAPPER_MACH_SITE_BPS = {}
WRAPPER_MACH_RETURN_META = {}
WRAPPER_MACH_RETURN_BPS = {}
# PLUGIN59 saved this runtime PC as _open$NOCANCEL+40 after applying the
# matching shared-cache slide. It is an observed syscall boundary, not a
# guessed Mach/XPC site. At that PC x0/x1 held pathname/flags in the snapshot.
OPEN_NOCANCEL_SYSCALL_PC = 0x237CD5734


def _hex(value):
    return "0x%x" % int(value)


def _memory(process, address, size=0x100):
    if not address or address >= (1 << 43):
        return ""
    try:
        return bks._read(process, address, size).hex()
    except Exception:
        return ""


def _u64(process, address):
    try:
        raw = bks._read(process, address, 8)
    except Exception:
        return 0
    return int.from_bytes(raw, "little") if len(raw) == 8 else 0


def _user_pointer(value):
    # libxpc stores arm64e-signed block/invoke pointers. The low 48 bits are
    # the address used for read-only inspection and LLDB breakpoint placement.
    return int(value) & 0x0000ffffffffffff


def _graph_contains_pointer(rows, pointer):
    """Check raw graph bytes for an exact little-endian pointer candidate."""
    pointer = _user_pointer(pointer)
    if not pointer:
        return False
    needle = pointer.to_bytes(8, "little")
    for row in rows:
        try:
            raw = bytes.fromhex(row.get("bytes", ""))
        except ValueError:
            continue
        for offset in range(0, max(0, len(raw) - 7), 8):
            if _user_pointer(int.from_bytes(raw[offset:offset + 8], "little")) == pointer:
                return True
    return False


def _bounded_pointer_graph(process, root, max_nodes=48, depth_limit=4):
    """Read a small pointer graph from an XPC event without assuming layout."""
    pending = [(int(root), 0)] if root else []
    seen = set()
    rows = []
    needle = SYSTEMAPP_ID.encode("ascii")
    while pending and len(rows) < max_nodes:
        address, depth = pending.pop(0)
        address = _user_pointer(address)
        if (address in seen or address < 0x100000000 or address >= (1 << 43)):
            continue
        seen.add(address)
        raw_hex = _memory(process, address, 0x100)
        raw = bytes.fromhex(raw_hex) if raw_hex else b""
        if not raw:
            continue
        row = {"address": _hex(address), "depth": depth,
               "bytes": raw_hex,
               "systemapp_identifier_present": needle in raw}
        rows.append(row)
        if depth >= depth_limit:
            continue
        # This is candidate discovery only: retain exact source offsets and
        # never interpret a pointer as an XPC field without a later hit.
        for offset in range(0, min(len(raw) - 7, 0x100), 8):
            value = int.from_bytes(raw[offset:offset + 8], "little")
            pointer = _user_pointer(value)
            if 0x100000000 <= pointer < (1 << 43) and pointer not in seen:
                pending.append((pointer, depth + 1))
                row.setdefault("pointer_candidates", []).append({
                    "offset": "+0x%x" % offset, "raw": _hex(value),
                    "address": _hex(pointer)})
                if len(pending) + len(rows) >= max_nodes:
                    break
    return rows


def _prog(process):
    try:
        return bks._progname(process)
    except Exception:
        return "<unreadable>"


def _stack(thread, limit=12):
    out = []
    for i in range(min(thread.GetNumFrames(), limit)):
        frame = thread.GetFrameAtIndex(i)
        out.append({"pc": _hex(frame.GetPC()), "symbol": frame.GetFunctionName() or ""})
    return out


def _key_strings(rows):
    """Report only recognized protocol-key text present in captured bytes."""
    needles = (b"pluginIdentifier", b"migrationResult", b"checkNecessity",
               b"msgID", b"result", b"error", b"reply", b"success",
               b"SystemAppMigrator", b"MergeBuddyProvisioningResponse.migrator")
    found = set()
    for row in rows:
        raw_hex = row.get("bytes", "")
        try:
            raw = bytes.fromhex(raw_hex)
        except ValueError:
            continue
        for needle in needles:
            if needle in raw or needle.decode("ascii").encode("utf-16le") in raw:
                found.add(needle.decode("ascii"))
    return sorted(found)


def _install_one(debugger, slide, static, label):
    target = debugger.GetSelectedTarget()
    address = int(slide) + static
    bp = target.BreakpointCreateByAddress(address)
    if not bp.IsValid():
        raise RuntimeError("failed to create %s at 0x%x" % (label, address))
    bp.SetScriptCallbackFunction(__name__ + ".on_wrapper_hit")
    META[bp.GetID()] = label
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command list %d" % bp.GetID(), result)
    proof = result.GetOutput() or result.GetError() or ""
    if __name__ + ".on_wrapper_hit" not in proof:
        raise RuntimeError("callback command missing for %s: %s" % (label, proof))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x callback=%s" %
          (bp.GetID(), label, address, __name__ + ".on_wrapper_hit"), flush=True)


def _arm_open_watch(debugger, slide):
    """Arm the observed open$NOCANCEL boundary after a matched callback only."""
    global OPEN_SITE_BP
    if OPEN_SITE_BP is not None:
        return
    bp = debugger.GetSelectedTarget().BreakpointCreateByAddress(
        int(slide) + OPEN_NOCANCEL_SYSCALL_PC)
    if not bp.IsValid():
        raise RuntimeError("failed to arm observed open$NOCANCEL PC")
    bp.SetScriptCallbackFunction(__name__ + ".on_tips_open_site")
    OPEN_SITE_BP = bp.GetID()
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command list %d" % OPEN_SITE_BP, result)
    proof = result.GetOutput() or result.GetError() or ""
    if __name__ + ".on_tips_open_site" not in proof:
        raise RuntimeError("open callback command missing: %s" % proof)
    print("COMMAND_LIST_PROOF id=%d label=SYSTEMAPP_CALLBACK_OPEN_NOCANCEL_PC "
          "address=0x%x callback=%s" %
          (OPEN_SITE_BP, int(slide) + OPEN_NOCANCEL_SYSCALL_PC,
           __name__ + ".on_tips_open_site"), flush=True)


def on_tips_open_site(frame, bp_loc, _context):
    """Record any open on the matched callback thread and arm its return."""
    process = frame.GetThread().GetProcess()
    thread = frame.GetThread()
    owner = bks._reg(frame, "tpidr_el1")
    context = SYSTEMAPP_CALLBACK_CONTEXT
    if (_prog(process) != "com.apple.migrationpluginwrapper" or not context or
            owner != int(context.get("receiver_tpidr", "0"), 0)):
        return False
    path_ptr = bks._reg(frame, "x0")
    path_hex = _memory(process, path_ptr, 0x200)
    try:
        path = bytes.fromhex(path_hex).split(b"\0", 1)[0].decode(
            "utf-8", "replace")
    except ValueError:
        path = "<unreadable>"
    is_app_placeholder = path.startswith("/System/Library/AppPlaceholders/")
    flags = bks._reg(frame, "x1")
    return_pc = bks._reg(frame, "x30")
    row = {
        "label": "SYSTEMAPP_WRAPPER_OPEN_CALL",
        "time": time.time(),
        "request_owner_tpidr": _hex(MATCHED_SYNC_OWNER or 0),
        "request": _hex(MATCHED_REQUEST or 0),
        "wrapper_tpidr": _hex(owner),
        "thread": thread.GetThreadID(),
        "path_pointer": _hex(path_ptr),
        "path": path,
        "flags": _hex(flags),
        "mode": _hex(bks._reg(frame, "x2")),
        "return_pc": _hex(return_pc),
        "app_placeholders_path": is_app_placeholder,
        "pc": _hex(bks._reg(frame, "pc")),
        "lr": _hex(bks._reg(frame, "lr")),
        "stack": _stack(thread),
        "interpretation": "observed open call on the matched SystemApp wrapper callback thread; syscall return is not yet known",
    }
    _write("progress.SYSTEMAPP_WRAPPER_OPEN_CALL.%s.json" %
           ("%d" % int(time.time() * 1000)), row)
    if is_app_placeholder:
        app_row = dict(row)
        app_row["label"] = "SYSTEMAPP_APPPLACEHOLDERS_OPEN_CALL"
        _write("progress.SYSTEMAPP_APPPLACEHOLDERS_OPEN_CALL.%s.json" %
               ("%d" % int(time.time() * 1000)), app_row)
    if not return_pc or return_pc >= (1 << 48):
        row["return_arm"] = "unavailable LR"
        _write("progress.SYSTEMAPP_APPPLACEHOLDERS_OPEN_RETURN_ARM_FAILED.json",
               row)
        return False
    key = (thread.GetThreadID(), return_pc)
    bp_id = OPEN_RETURN_BY_ADDRESS.get(key)
    if bp_id is None:
        bp = process.GetTarget().BreakpointCreateByAddress(return_pc)
        if not bp.IsValid():
            row["return_arm"] = "breakpoint creation failed"
            _write("progress.SYSTEMAPP_APPPLACEHOLDERS_OPEN_RETURN_ARM_FAILED.json",
                   row)
            return False
        bp.SetScriptCallbackFunction(__name__ + ".on_tips_open_return")
        bp_id = bp.GetID()
        OPEN_RETURN_BY_ADDRESS[key] = bp_id
        result = lldb.SBCommandReturnObject()
        process.GetTarget().GetDebugger().GetCommandInterpreter().HandleCommand(
            "breakpoint command list %d" % bp_id, result)
        proof = result.GetOutput() or result.GetError() or ""
        if __name__ + ".on_tips_open_return" not in proof:
            raise RuntimeError("open return callback command missing: %s" % proof)
    OPEN_RETURN_META.setdefault(bp_id, []).append(row)
    print("SYSTEMAPP_WRAPPER_OPEN_RETURN_ARMED " +
          json.dumps({"thread": thread.GetThreadID(), "tpidr_el1": _hex(owner),
                      "return_pc": _hex(return_pc), "path": path,
                      "app_placeholders_path": is_app_placeholder},
                     sort_keys=True), flush=True)
    return False


def on_tips_open_return(frame, bp_loc, _context):
    """Capture raw open return value on the same matched wrapper thread."""
    process = frame.GetThread().GetProcess()
    thread = frame.GetThread()
    owner = bks._reg(frame, "tpidr_el1")
    pending = OPEN_RETURN_META.get(bp_loc.GetBreakpoint().GetID(), [])
    index = next((i for i in range(len(pending) - 1, -1, -1)
                  if pending[i].get("thread") == thread.GetThreadID() and
                  int(pending[i].get("wrapper_tpidr", "0"), 0) == owner), None)
    if index is None:
        return False
    call = pending.pop(index)
    raw_result = bks._reg(frame, "x0")
    low32 = raw_result & 0xffffffff
    signed32 = low32 if low32 < 0x80000000 else low32 - 0x100000000
    is_app_placeholder = call.get("app_placeholders_path", False)
    row = {
        "label": "SYSTEMAPP_WRAPPER_OPEN_RETURN",
        "time": time.time(),
        "elapsed_s": time.time() - call["time"],
        "request_owner_tpidr": _hex(MATCHED_SYNC_OWNER or 0),
        "request": _hex(MATCHED_REQUEST or 0),
        "wrapper_tpidr": _hex(owner),
        "thread": thread.GetThreadID(),
        "path": call["path"],
        "app_placeholders_path": is_app_placeholder,
        "flags": call["flags"],
        "raw_x0": _hex(raw_result),
        "low32": _hex(low32),
        "signed_low32": signed32,
        "pc": _hex(bks._reg(frame, "pc")),
        "lr": _hex(bks._reg(frame, "lr")),
        "stack": _stack(thread),
        "interpretation": "same-thread return boundary for the recorded open; raw return captured without assigning errno semantics",
    }
    _write("progress.SYSTEMAPP_WRAPPER_OPEN_RETURN.%s.json" %
           ("%d" % int(time.time() * 1000)), row)
    if is_app_placeholder:
        app_row = dict(row)
        app_row["label"] = "SYSTEMAPP_APPPLACEHOLDERS_OPEN_RETURN"
        _write("progress.SYSTEMAPP_APPPLACEHOLDERS_OPEN_RETURN.%s.json" %
               ("%d" % int(time.time() * 1000)), app_row)
    return False


def _arm_wrapper_mach_watch(debugger, slide):
    """Trace mach_msg trap entry/return on the matched wrapper callback thread."""
    if WRAPPER_MACH_SITE_BPS:
        return
    target = debugger.GetSelectedTarget()
    for static, label in ((transport.MACH_MSG2_TRAP, "mach_msg2"),
                          (transport.MACH_MSG_TRAP, "mach_msg")):
        bp = target.BreakpointCreateByAddress(int(slide) + int(static))
        if not bp.IsValid():
            raise RuntimeError("failed to arm wrapper %s trap" % label)
        bp.SetScriptCallbackFunction(__name__ + ".on_wrapper_mach_site")
        WRAPPER_MACH_SITE_BPS[label] = bp.GetID()
        result = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command list %d" % bp.GetID(), result)
        proof = result.GetOutput() or result.GetError() or ""
        if __name__ + ".on_wrapper_mach_site" not in proof:
            raise RuntimeError("wrapper %s trap callback command missing" % label)
        print("COMMAND_LIST_PROOF id=%d label=SYSTEMAPP_WRAPPER_%s_TRAP "
              "address=0x%x callback=%s" %
              (bp.GetID(), label.upper(), int(slide) + int(static),
               __name__ + ".on_wrapper_mach_site"), flush=True)


def on_wrapper_mach_site(frame, bp_loc, _context):
    """Log wrapper callback-thread Mach trap arguments and arm the return."""
    process = frame.GetThread().GetProcess()
    thread = frame.GetThread()
    context = SYSTEMAPP_CALLBACK_CONTEXT
    owner = bks._reg(frame, "tpidr_el1")
    if (_prog(process) != "com.apple.migrationpluginwrapper" or not context or
            owner != int(context.get("receiver_tpidr", "0"), 0)):
        return False
    label = next((name for name, bp_id in WRAPPER_MACH_SITE_BPS.items()
                  if bp_id == bp_loc.GetBreakpoint().GetID()), "unknown")
    registers = {"x%d" % i: _hex(bks._reg(frame, "x%d" % i))
                 for i in range(8)}
    if label == "mach_msg2":
        try:
            args = transport._mach2_arguments(process, frame)
        except Exception as error:
            args = {"decode_error": type(error).__name__ + ": " + str(error),
                    "registers": registers}
    else:
        msg = bks._reg(frame, "x0")
        send_size = bks._reg(frame, "x2")
        try:
            header = transport._mach_header(process, msg, min(send_size, 0x1000))
        except Exception as error:
            header = {"decode_error": type(error).__name__ + ": " + str(error)}
        args = {"registers": registers, "message": header,
                "options": _hex(bks._reg(frame, "x1")),
                "send_size": send_size,
                "receive_size": bks._reg(frame, "x3"),
                "receive_name": _hex(bks._reg(frame, "x4")),
                "timeout": _hex(bks._reg(frame, "x5"))}
    return_pc = bks._reg(frame, "x30")
    row = {
        "label": "SYSTEMAPP_WRAPPER_MACH_CALL",
        "trap": label,
        "time": time.time(),
        "request_owner_tpidr": _hex(MATCHED_SYNC_OWNER or 0),
        "request": _hex(MATCHED_REQUEST or 0),
        "wrapper_tpidr": _hex(owner),
        "thread": thread.GetThreadID(),
        "pc": _hex(bks._reg(frame, "pc")),
        "lr": _hex(return_pc),
        "args": args,
        "stack": _stack(thread),
        "interpretation": "Mach trap entered on the matched wrapper callback thread; task-local port names are not cross-task peer identity",
    }
    _write("progress.SYSTEMAPP_WRAPPER_MACH_CALL.%s.json" %
           int(time.time() * 1000), row)
    if not return_pc or return_pc >= (1 << 48):
        return False
    key = (thread.GetThreadID(), return_pc)
    bp_id = WRAPPER_MACH_RETURN_BPS.get(key)
    if bp_id is None:
        bp = process.GetTarget().BreakpointCreateByAddress(return_pc)
        if not bp.IsValid():
            row["return_arm_error"] = "breakpoint creation failed"
            _write("progress.SYSTEMAPP_WRAPPER_MACH_RETURN_ARM_FAILED.json", row)
            return False
        bp.SetScriptCallbackFunction(__name__ + ".on_wrapper_mach_return")
        bp_id = bp.GetID()
        WRAPPER_MACH_RETURN_BPS[key] = bp_id
        proof = lldb.SBCommandReturnObject()
        process.GetTarget().GetDebugger().GetCommandInterpreter().HandleCommand(
            "breakpoint command list %d" % bp_id, proof)
        proof_text = proof.GetOutput() or proof.GetError() or ""
        if __name__ + ".on_wrapper_mach_return" not in proof_text:
            raise RuntimeError("wrapper Mach return callback command missing")
    WRAPPER_MACH_RETURN_META.setdefault(bp_id, []).append(row)
    return False


def on_wrapper_mach_return(frame, bp_loc, _context):
    """Capture a matched wrapper Mach trap's return and raw result."""
    process = frame.GetThread().GetProcess()
    thread = frame.GetThread()
    owner = bks._reg(frame, "tpidr_el1")
    pending = WRAPPER_MACH_RETURN_META.get(bp_loc.GetBreakpoint().GetID(), [])
    index = next((i for i in range(len(pending) - 1, -1, -1)
                  if pending[i].get("thread") == thread.GetThreadID() and
                  int(pending[i].get("wrapper_tpidr", "0"), 0) == owner), None)
    if index is None:
        return False
    call = pending.pop(index)
    row = {
        "label": "SYSTEMAPP_WRAPPER_MACH_RETURN",
        "time": time.time(),
        "elapsed_s": time.time() - call["time"],
        "trap": call["trap"],
        "request_owner_tpidr": _hex(MATCHED_SYNC_OWNER or 0),
        "request": _hex(MATCHED_REQUEST or 0),
        "wrapper_tpidr": _hex(owner),
        "thread": thread.GetThreadID(),
        "raw_x0": _hex(bks._reg(frame, "x0")),
        "pc": _hex(bks._reg(frame, "pc")),
        "lr": _hex(bks._reg(frame, "lr")),
        "call": call,
        "stack": _stack(thread),
        "interpretation": "same-thread return from the captured Mach trap; result and object identity require its call tuple and downstream receive evidence",
    }
    _write("progress.SYSTEMAPP_WRAPPER_MACH_RETURN.%s.json" %
           int(time.time() * 1000), row)
    return False


def _arm_registered_handler(frame, connection, event):
    """Resolve the live connection's event block and arm its invoke target.

    Bootkc shared-cache disassembly of __xpc_connection_call_event_handler
    shows conn+0x20 -> signed block, block+0x10 -> signed invoke pointer, and
    the call passes (block, event). Treat each value as a measured candidate
    until the invoke breakpoint fires with those same arguments.
    """
    process = frame.GetThread().GetProcess()
    debugger = process.GetTarget().GetDebugger()
    block_raw = _u64(process, connection + 0x20)
    block = _user_pointer(block_raw)
    block_bytes = _memory(process, block, 0x40) if block else ""
    invoke_raw = _u64(process, block + 0x10) if block else 0
    invoke = _user_pointer(invoke_raw)
    code_bytes = _memory(process, invoke, 0x20) if invoke else ""
    current_owner = bks._reg(frame, "tpidr_el1")
    handoff = HANDOFF_EVENT
    if (handoff is None or
            int(handoff.get("receiver_tpidr", "0"), 0) != current_owner):
        handoff = None
    dm_handoff = DM_OUTBOUND_HANDOFF
    if (dm_handoff is None or
            int(dm_handoff.get("receiver_tpidr", "0"), 0) != current_owner):
        dm_handoff = None
    details = {
        "connection": _hex(connection),
        "connection_event_handler_field": "+0x20",
        "block_raw": _hex(block_raw),
        "block": _hex(block),
        "block_bytes": block_bytes,
        "invoke_field": "+0x10",
        "invoke_raw": _hex(invoke_raw),
        "invoke": _hex(invoke),
        "invoke_bytes": code_bytes,
        "event": _hex(event),
        "event_bytes": _memory(process, event, 0x100),
        "event_pointer_graph": _bounded_pointer_graph(process, event),
        "handoff": handoff,
        "dm_outbound_handoff": dm_handoff,
        "time": time.time(),
    }
    _write("progress.WRAPPER_REGISTERED_EVENT_CALLBACK_CANDIDATE.json", details)
    if not (0x100000000 <= invoke < (1 << 48)) or not code_bytes:
        print("WRAPPER_REGISTERED_EVENT_CALLBACK_UNREADABLE " +
              json.dumps(details, sort_keys=True), flush=True)
        return
    HANDLER_INVOKES.setdefault(invoke, []).append({
        "block": block, "connection": connection, "event": event,
        "handoff": handoff, "dm_outbound_handoff": dm_handoff,
    })
    if invoke in HANDLER_BREAKPOINTS:
        return
    if len(HANDLER_BREAKPOINTS) >= MAX_HANDLER_BREAKPOINTS:
        print("WRAPPER_REGISTERED_EVENT_CALLBACK_LIMIT " +
              json.dumps({"limit": MAX_HANDLER_BREAKPOINTS,
                          "invoke": _hex(invoke),
                          "connection": _hex(connection)}, sort_keys=True),
              flush=True)
        return
    bp = debugger.GetSelectedTarget().BreakpointCreateByAddress(invoke)
    if not bp.IsValid():
        return
    bp.SetScriptCallbackFunction(__name__ + ".on_registered_handler_hit")
    HANDLER_BREAKPOINTS.add(invoke)
    HANDLER_META[bp.GetID()] = invoke
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command list %d" % bp.GetID(), result)
    proof = result.GetOutput() or result.GetError() or ""
    if __name__ + ".on_registered_handler_hit" not in proof:
        raise RuntimeError("registered callback breakpoint lacks command proof: %s" %
                           proof)
    print("COMMAND_LIST_PROOF id=%d label=WRAPPER_REGISTERED_EVENT_CALLBACK "
          "address=0x%x callback=%s" %
          (bp.GetID(), invoke, __name__ + ".on_registered_handler_hit"),
          flush=True)


def _write(name, row):
    print("WRAPPER_REPLY_TRACE " + json.dumps(row, sort_keys=True), flush=True)
    bks._write_event(name, row)


HANDLER_META = {}


def on_registered_handler_hit(frame, bp_loc, _context):
    global REGISTERED_HITS, SYSTEMAPP_CALLBACK_CONTEXT, MSU_CALLBACK_CONTEXT
    invoke = HANDLER_META.get(bp_loc.GetBreakpoint().GetID())
    if not invoke:
        return False
    process = frame.GetThread().GetProcess()
    block_arg = _user_pointer(bks._reg(frame, "x0"))
    event_arg = _user_pointer(bks._reg(frame, "x1"))
    matches = []
    for candidate in HANDLER_INVOKES.get(invoke, []):
        if block_arg == candidate["block"] and event_arg == candidate["event"]:
            matches.append(candidate)
    if not matches:
        return False
    is_dm_outbound = any(candidate.get("dm_outbound_handoff")
                         for candidate in matches)
    progname = _prog(process)
    if progname != "com.apple.migrationpluginwrapper" and not (
            is_dm_outbound and progname == "com.apple.datamigrator"):
        return False
    thread = frame.GetThread()
    try:
        fields = kernelpath.plugin._xpc_fields(process, event_arg)
    except Exception:
        fields = {}
    event_bytes = _memory(process, event_arg, 0x400)
    systemapp_payload = SYSTEMAPP_ID.encode("ascii").hex() in event_bytes
    pointer_graph = _bounded_pointer_graph(process, event_arg)
    systemapp_pointer_payload = any(
        item.get("systemapp_identifier_present") for item in pointer_graph)
    graph_bytes = b"".join(
        bytes.fromhex(item.get("bytes", ""))
        for item in pointer_graph if item.get("bytes"))
    msu_payload = (b"MSUDataMigrator" in graph_bytes or
                   "MSUDataMigrator".encode("utf-16le") in graph_bytes)
    systemapp_callback_payload = systemapp_payload or systemapp_pointer_payload
    if (progname == "com.apple.migrationpluginwrapper" and
            (systemapp_callback_payload or msu_payload)):
        candidate = matches[0]
        event_context = {
            "time": time.time(),
            "receiver_tpidr": _hex(bks._reg(frame, "tpidr_el1")),
            "receiver_thread": thread.GetThreadID(),
            "receiver_pid": process.GetProcessID(),
            "connection": _hex(candidate.get("connection", 0)),
            "event": _hex(event_arg),
            "block": _hex(block_arg),
            "registered_invoke": _hex(invoke),
            "systemapp_identifier_present": systemapp_callback_payload,
            "msu_identifier_present": msu_payload,
            "event_plugin_markers": [name for name, present in (
                (SYSTEMAPP_ID, systemapp_callback_payload),
                ("com.apple.MSUDataMigrator", msu_payload)) if present],
            "outbound_send_seen": False,
            "basis": "same wrapper thread/connection's registered event carries the matched SystemAppMigrator identifier; this is a send candidate, not kernel delivery or reply proof",
        }
        if systemapp_callback_payload:
            SYSTEMAPP_CALLBACK_CONTEXT = event_context
            bks._write_event(
                "progress.SYSTEMAPP_WRAPPER_CALLBACK_CONTEXT_CANDIDATE.json",
                event_context)
            # PLUGIN59 found this callback thread opening the Tips
            # AppPlaceholder directory. Arm that observed syscall PC only
            # after a positive SystemApp callback context exists.
            try:
                _arm_open_watch(process.GetTarget().GetDebugger(), ACTIVE_SLIDE)
                _arm_wrapper_mach_watch(
                    process.GetTarget().GetDebugger(), ACTIVE_SLIDE)
            except Exception as error:
                event_context["post_callback_watch_error"] = (
                    type(error).__name__ + ": " + str(error))
                _write("progress.SYSTEMAPP_POST_CALLBACK_WATCH_ARM_FAILED.json",
                       event_context)
        if msu_payload:
            MSU_CALLBACK_CONTEXT = event_context
            event_context["basis"] = (
                "registered wrapper event pointer graph contains MSUDataMigrator; "
                "same-thread send is a candidate and is not linked to the "
                "SystemApp reply without ipc_object/transaction evidence")
            bks._write_event(
                "progress.MSU_WRAPPER_CALLBACK_CONTEXT_CANDIDATE.json",
                event_context)
        # Arm a narrow, owner-conditioned kernel helper breakpoint now. MSU
        # callback traffic can precede the later SystemApp request and cannot
        # be recovered by arming only after CLIENT_SYNC_CALL.
        try:
            transport.register_callback_send_context(
                process.GetTarget().GetDebugger(),
                bks._reg(frame, "tpidr_el1"), event_context)
        except Exception as error:
            event_context["kernel_send_probe_error"] = (
                type(error).__name__ + ": " + str(error))
    row = {
        "label": ("DATAMIGRATOR_REGISTERED_EVENT_CALLBACK_INVOKE"
                  if is_dm_outbound else
                  "WRAPPER_REGISTERED_EVENT_CALLBACK_INVOKE"),
        "time": time.time(),
        "progname": _prog(process),
        "pid": process.GetProcessID(),
        "thread": thread.GetThreadID(),
        "tpidr_el1": _hex(bks._reg(frame, "tpidr_el1")),
        "pc": _hex(bks._reg(frame, "pc")),
        "lr": _hex(bks._reg(frame, "lr")),
        "registers": {"x%d" % i: _hex(bks._reg(frame, "x%d" % i))
                      for i in range(8)},
        "block_arg": _hex(block_arg),
        "event_arg": _hex(event_arg),
        "block_bytes": _memory(process, block_arg, 0x40),
        "event_bytes": event_bytes,
        "event_pointer_graph": pointer_graph,
        "fields": fields,
        "systemapp_identifier_bytes_present":
            systemapp_payload or systemapp_pointer_payload,
        "matches": matches,
        "stack": _stack(thread),
        "secondary_dm_handoff": next((candidate.get("dm_outbound_handoff")
                                       for candidate in matches
                                       if candidate.get("dm_outbound_handoff")),
                                      None),
        "interpretation": "registered XPC event block invoked on the matched secondary DataMigrator receive thread; handler and terminal reply semantics remain distinct" if is_dm_outbound else "registered XPC event block invoked; app-level work and terminal reply remain separate",
    }
    REGISTERED_HITS += 1
    _write("progress.WRAPPER_REGISTERED_EVENT_CALLBACK_INVOKE.%03d.json" %
           REGISTERED_HITS, row)
    return False


def _wrapped_transport_event(frame, label, extra):
    """Keep transport logging intact and remember the actual wrapper handoff."""
    global HANDOFF_EVENT, MATCHED_SYNC_OWNER, MATCHED_REQUEST
    global MATCHED_REPLY_OBJECT, MATCHED_DEST_OBJECT
    global DECODED_SENDS, DECODED_CLIENT_MESSAGES, DM_OUTBOUND_HANDOFF
    global MIGRATION_RESULT_RECEIVER_CONTEXT
    row = ORIGINAL_EVENT(frame, label, extra)
    if label == "SYSTEMAPP_MACH_WINDOW_CLOSED":
        coverage = transport.pinned_reply_path_coverage()
        close_row = {
            "label": "PINNED_REPLY_SEND_AND_HANDOFF_COVERAGE_AT_WINDOW_CLOSE",
            "time": row.get("time"),
            "window_seconds": row.get("window_seconds"),
            "coverage": coverage,
            "interpretation": "counts confirm which global post-copyin and independent ith_kmsg store callbacks executed; zero reply matches is scoped to these observed paths",
        }
        _write("success.PINNED_REPLY_SEND_AND_HANDOFF_COVERAGE_AT_WINDOW_CLOSE.json",
               close_row)
        print("PINNED_REPLY_PATH_COVERAGE " +
              json.dumps(close_row, sort_keys=True), flush=True)
    process = frame.GetThread().GetProcess()
    owner = bks._reg(frame, "tpidr_el1")
    if label in ("IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS",
                 "IPC_SYSTEMAPP_SEND_OBJECTS_CANDIDATE"):
        MATCHED_SYNC_OWNER = owner
        MATCHED_REQUEST = int(extra.get("request", "0"), 0)
        MATCHED_REPLY_OBJECT = int(extra.get("resolved_reply_candidate", "0"), 0)
        MATCHED_DEST_OBJECT = int(extra.get("resolved_destination_candidate", "0"), 0)
        _write("progress.WRAPPER_REPLY_IDENTITY.json", {
            "label": "WRAPPER_REPLY_IDENTITY", "time": row["time"],
            "owner_tpidr": _hex(owner),
            "request": _hex(MATCHED_REQUEST),
            "message_id": extra.get("header", {}).get("message_id"),
            "identity_level": extra.get("identity_level",
                                         "resolved-user-send-and-kmsg"),
            "destination_object": _hex(MATCHED_DEST_OBJECT),
            "reply_object": _hex(MATCHED_REPLY_OBJECT),
            "terminal_reply_object_is_candidate":
                label != "IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS",
        })
        for handoff in transport.CALLBACK_DM_HANDOFFS:
            _write("progress.CALLBACK_DM_HANDOFF_VS_SYSTEMAPP_REPLY.json", {
                "time": time.time(),
                "target_request": _hex(MATCHED_REQUEST),
                "target_owner_tpidr": _hex(MATCHED_SYNC_OWNER),
                "target_reply_object": _hex(MATCHED_REPLY_OBJECT),
                "callback_dm_handoff": handoff,
                "destination_equals_systemapp_reply": (
                    int(handoff.get("sender_candidate", {}).get(
                        "destination_object_key", "0"), 0) ==
                    (transport.KERNEL_TARGET or {}).get("reply_key")),
                "interpretation": "cross-event timing/object comparison only; a non-equal destination is not the terminal reply, and a prior MSU event is not attributed to SystemApp without a transaction link",
            })
    if label == "IPC_ITH_KMSG_HANDOFF_CANDIDATE" and \
            extra.get("matched_systemapp_request_message") and \
            extra.get("progname") == "com.apple.migrationpluginwrapper":
        HANDOFF_EVENT = {
            "time": row["time"],
            "receiver_thread": extra.get("receiver_thread"),
            "receiver_tpidr": extra.get("current_tpidr"),
            "request": extra.get("target_request"),
            "destination_object": (extra.get("matched_systemapp_request_headers") or
                                    [{}])[0].get("remote_object_key"),
            "receiver_pid": extra.get("task_pid"),
        }
        bks._write_event("progress.WRAPPER_REQUEST_ITH_KMSG.json", HANDOFF_EVENT)
        print("WRAPPER_REPLY_TRACE_START " + json.dumps(HANDOFF_EVENT, sort_keys=True),
              flush=True)
    if label == "IPC_ITH_KMSG_HANDOFF_CANDIDATE" and \
            extra.get("matched_wrapper_outbound_destination_message") and \
            extra.get("progname") == "com.apple.datamigrator":
        exact = next((item for item in
                      extra.get("matched_wrapper_outbound_destination_headers", [])
                      if item.get("destination_object_match") and
                      item.get("outbound_message_id_match") and
                      item.get("outbound_size_match") and
                      item.get("body_exact_match")), None)
        if exact is not None:
            DM_OUTBOUND_HANDOFF = {
                "time": row["time"],
                "receiver_thread": extra.get("receiver_thread"),
                "receiver_tpidr": extra.get("current_tpidr"),
                "receiver_pid": extra.get("task_pid"),
                "receiver_progname": extra.get("progname"),
                "request": extra.get("target_request"),
                "kmsg": exact.get("candidate_address"),
                "destination_object": exact.get("remote_object_key"),
                "message_id": exact.get("message_id"),
                "size": exact.get("receiver_size"),
                "body_exact_match": True,
                "outbound_candidate_index": exact.get("outbound_candidate_index"),
                "original_systemapp_reply_object": _hex(MATCHED_REPLY_OBJECT or 0),
                "original_systemapp_reply_is_this_destination": (
                    int(exact.get("remote_object_key", "0"), 0) ==
                    (transport.KERNEL_TARGET or {}).get("reply_key")),
            }
            kernel_target = getattr(transport, "KERNEL_TARGET", None)
            if kernel_target is not None:
                kernel_target["wrapper_outbound_receiver_tpidr"] = int(
                    extra.get("current_tpidr", "0"), 0)
                kernel_target["wrapper_outbound_receiver"] = DM_OUTBOUND_HANDOFF
            bks._write_event(
                "success.IPC_SECONDARY_DM_DEST_ITH_KMSG.json", DM_OUTBOUND_HANDOFF)
            print("IPC_SECONDARY_DM_DEST_ITH_KMSG " +
                  json.dumps(DM_OUTBOUND_HANDOFF, sort_keys=True), flush=True)
    if label == "IPC_ITH_KMSG_HANDOFF_CANDIDATE" and \
            extra.get("matched_migration_result_destination_headers") and \
            extra.get("progname") == "com.apple.migrationpluginwrapper":
        exact = next((item for item in
                      extra.get("matched_migration_result_destination_headers", [])
                      if item.get("destination_object_match") and
                      item.get("message_id_match") and item.get("size_match") and
                      item.get("body_exact_match")), None)
        if exact is not None:
            MIGRATION_RESULT_RECEIVER_CONTEXT = {
                "time": row["time"],
                "receiver_tpidr": extra.get("current_tpidr"),
                "receiver_thread": extra.get("receiver_thread"),
                "receiver_pid": extra.get("task_pid"),
                "receiver_progname": extra.get("progname"),
                "request": extra.get("target_request"),
                "pinned_reply_object": _hex(MATCHED_REPLY_OBJECT or 0),
                "handoff_site": extra.get("site"),
                "migration_result_receive": exact,
                "interpretation": "exact migrationResult header/body received by wrapper; next same-TPIDR send is a candidate, kernel object equality decides whether it targets the original DM reply",
            }
            bks._write_event(
                "success.WRAPPER_MIGRATION_RESULT_RECEIVER_CONTEXT.json",
                MIGRATION_RESULT_RECEIVER_CONTEXT)
            try:
                receiver_owner = int(extra.get("current_tpidr", "0"), 0)
            except (TypeError, ValueError):
                receiver_owner = 0
            if receiver_owner:
                transport.KERNEL_SEND_HELPER_OWNERS.add(receiver_owner)
                transport._refresh_kernel_send_helper_condition(lldb.debugger)
            print("WRAPPER_MIGRATION_RESULT_RECEIVER_CONTEXT " + json.dumps(
                MIGRATION_RESULT_RECEIVER_CONTEXT, sort_keys=True), flush=True)
    elif label == "WRAPPER_XPC_SEND_RAW":
        # Keep callback-side message bytes for a send on either the exact
        # matched request handoff or the same wrapper thread/connection whose
        # registered event carried SystemAppMigrator. The latter is only an
        # app-level send candidate; kernel object equality is still required
        # before relating it to the original request's reply object.
        handoff = HANDOFF_EVENT
        callback_context = (SYSTEMAPP_CALLBACK_CONTEXT
                            if SYSTEMAPP_CALLBACK_CONTEXT and owner == int(
                                SYSTEMAPP_CALLBACK_CONTEXT.get(
                                    "receiver_tpidr", "0"), 0) else
                            MSU_CALLBACK_CONTEXT if MSU_CALLBACK_CONTEXT and
                            owner == int(MSU_CALLBACK_CONTEXT.get(
                                "receiver_tpidr", "0"), 0) else
                            MIGRATION_RESULT_RECEIVER_CONTEXT
                            if MIGRATION_RESULT_RECEIVER_CONTEXT and owner == int(
                                MIGRATION_RESULT_RECEIVER_CONTEXT.get(
                                    "receiver_tpidr", "0"), 0) else None)
        endpoint = int(extra.get("endpoint_candidate") or "0", 0)
        matched_handoff_connection = bool(
            handoff and owner == int(handoff.get("receiver_tpidr", "0"), 0) and
            endpoint == int(handoff.get("connection", "0"), 0))
        matched_post_result_thread = bool(
            MIGRATION_RESULT_RECEIVER_CONTEXT and
            owner == int(MIGRATION_RESULT_RECEIVER_CONTEXT.get(
                "receiver_tpidr", "0"), 0))
        matched_callback_connection = bool(
            callback_context and
            endpoint == int(callback_context.get("connection", "0"), 0))
        matched_connection = (matched_handoff_connection or
                              matched_post_result_thread or
                              matched_callback_connection)
        if matched_connection:
            DECODED_SENDS += 1
            message = int(extra.get("message", "0"), 0)
            try:
                fields = kernelpath.plugin._xpc_fields(process, message)
            except Exception:
                fields = {}
            graph = _bounded_pointer_graph(process, message, max_nodes=48,
                                           depth_limit=4)
            decoded = {
                "label": "MATCHED_WRAPPER_XPC_SEND_DECODE",
                "time": row["time"], "send_index": DECODED_SENDS,
                "request_owner_tpidr": _hex(MATCHED_SYNC_OWNER or 0),
                "request": _hex(MATCHED_REQUEST or 0),
                "reply_object": _hex(MATCHED_REPLY_OBJECT or 0),
                "destination_object": _hex(MATCHED_DEST_OBJECT or 0),
                "wrapper_owner_tpidr": _hex(owner),
                "connection": _hex(endpoint), "message": _hex(message),
                "same_connection_as_matched_event": bool(matched_connection),
                "same_thread_as_matched_event_or_handoff": True,
                "correlation_basis": (
                    "same wrapper TPIDR as exact migrationResult receiver; endpoint may differ and send remains a candidate"
                    if matched_post_result_thread else
                    "matched request handoff"
                    if matched_handoff_connection else
                    "same wrapper TPIDR and connection as registered event "
                    "carrying SystemAppMigrator; send remains a candidate"),
                "xpc_fields": fields,
                "message_bytes": _memory(process, message, 0x400),
                "message_pointer_graph": graph,
                "recognized_key_strings": _key_strings(graph),
                "transport_event": row,
                "interpretation": "callback-associated XPC send; reply-object equality requires kernel object evidence",
            }
            _write("progress.MATCHED_WRAPPER_XPC_SEND_DECODE.%03d.json" %
                   DECODED_SENDS, decoded)
    elif label == "CLIENT_WRAPPER_MESSAGE_RAW" and \
            extra.get("identifier") == SYSTEMAPP_ID:
        receiver_owner = int(extra.get("receiver_owner", "0"), 0)
        if MATCHED_SYNC_OWNER and receiver_owner == MATCHED_SYNC_OWNER:
            DECODED_CLIENT_MESSAGES += 1
            message = int(extra.get("message", "0"), 0)
            try:
                fields = kernelpath.plugin._xpc_fields(process, message)
            except Exception:
                fields = {}
            graph = _bounded_pointer_graph(process, message, max_nodes=48,
                                           depth_limit=4)
            decoded = {
                "label": "MATCHED_SYSTEMAPP_CLIENT_HANDLE_MESSAGE_DECODE",
                "time": row["time"], "message_index": DECODED_CLIENT_MESSAGES,
                "request_owner_tpidr": _hex(MATCHED_SYNC_OWNER),
                "request": _hex(MATCHED_REQUEST or 0),
                "reply_object": _hex(MATCHED_REPLY_OBJECT or 0),
                "identifier": SYSTEMAPP_ID,
                "handler_pc": extra.get("pc"),
                "message": _hex(message),
                "receiver": extra.get("receiver"),
                "receiver_owner_tpidr": _hex(receiver_owner),
                "xpc_fields": fields,
                "message_bytes": _memory(process, message, 0x400),
                "message_pointer_graph": graph,
                "recognized_key_strings": _key_strings(graph),
                "transport_event": row,
                "interpretation": "plugin-attributed client handler; terminal-result semantics require decoded payload and reply-object match",
            }
            _write("progress.MATCHED_SYSTEMAPP_CLIENT_HANDLE_MESSAGE.%03d.json" %
                   DECODED_CLIENT_MESSAGES, decoded)
    return row


def on_wrapper_hit(frame, bp_loc, _context):
    global DECODED_SENDS
    label = META.get(bp_loc.GetBreakpoint().GetID())
    if not label:
        return False
    process = frame.GetThread().GetProcess()
    owner = bks._reg(frame, "tpidr_el1")
    progname = _prog(process)
    thread = frame.GetThread()

    # Keep the request-side completion scheduler separate from wrapper-server
    # dispatch. runPluginAtPath: executes in DataMigrator; its x5 completion
    # block is the exact block whose +0x10 invoke the transport probe arms.
    # The following two PCs are after the synchronous plugin request and at
    # the framework's local result-block invoke, respectively. They never
    # stop/modify execution and are correlated by thread, owner, and plugin id.
    if label == "DM_WRAPPER_RUN_PLUGIN_ENTRY":
        if progname != "com.apple.datamigrator":
            return False
        identifier_ptr = bks._reg(frame, "x3")
        try:
            identifier = transport.plugin._nsstring(process, identifier_ptr)
        except Exception:
            identifier = "<unreadable>"
        if identifier != SYSTEMAPP_ID:
            return False
        block = bks._reg(frame, "x5")
        invoke_raw = _u64(process, block + 0x10) if block else 0
        invoke = _user_pointer(invoke_raw)
        item = {
            "time": time.time(), "thread": thread.GetThreadID(),
            "owner_tpidr": _hex(owner), "identifier": identifier,
            "receiver": _hex(bks._reg(frame, "x0")),
            "selector": _hex(bks._reg(frame, "x1")),
            "path": _hex(bks._reg(frame, "x2")),
            "parameters": _hex(bks._reg(frame, "x4")),
            "completion_block": _hex(block),
            "completion_invoke_raw": _hex(invoke_raw),
            "completion_invoke": _hex(invoke),
            "request": _hex(transport.SYNC_CALLS.get(owner, {}).get(
                "request", 0) or 0),
            "call_bytes": _memory(process, block, 0x60),
        }
        RUN_PLUGIN_CALLS[(thread.GetThreadID(), owner)] = item
        _write("progress.DM_WRAPPER_RUN_PLUGIN_SYSTEMAPP_ENTRY.json", item)
        print("DM_WRAPPER_COMPLETION_TRACE " + json.dumps({
            "label": label, **item}, sort_keys=True), flush=True)
        return False

    if label in ("DM_WRAPPER_PLUGIN_SYNC_RETURN",
                 "DM_WRAPPER_RESULT_CALLBACK_INVOKE"):
        if progname != "com.apple.datamigrator":
            return False
        key = (thread.GetThreadID(), owner)
        item = RUN_PLUGIN_CALLS.get(key)
        # Deferred gate installation can occur while the matched call is
        # already stopped at this function's entry PC. The gate seeds the
        # completion block, while this later boundary supplies the request
        # identity captured by the transport callback.
        if item is not None and not item.get("request"):
            sync = transport.SYNC_CALLS.get(owner, {})
            if sync.get("request"):
                item["request"] = _hex(sync["request"])
        if item is None:
            return False
        if label == "DM_WRAPPER_PLUGIN_SYNC_RETURN":
            result_ptr = bks._reg(frame, "x0")
            extra = {
                "sync_result": _hex(result_ptr),
                "sync_result_bytes": _memory(process, result_ptr, 0x300),
                "sync_result_graph": _bounded_pointer_graph(
                    process, result_ptr, max_nodes=24, depth_limit=3),
            }
        else:
            local_block = bks._reg(frame, "x22")
            result_ptr = bks._reg(frame, "x24")
            local_invoke_raw = (_u64(process, local_block + 0x10)
                                if local_block else 0)
            graph = _bounded_pointer_graph(
                process, local_block, max_nodes=24, depth_limit=3)
            extra = {
                "local_result_block": _hex(local_block),
                "local_result_block_invoke_raw": _hex(local_invoke_raw),
                "local_result_block_invoke": _hex(
                    _user_pointer(local_invoke_raw)),
                "local_result_block_bytes": _memory(
                    process, local_block, 0x100),
                "callback_argument": _hex(result_ptr),
                "callback_argument_bytes": _memory(process, result_ptr, 0x300),
                "callback_argument_graph": _bounded_pointer_graph(
                    process, result_ptr, max_nodes=24, depth_limit=3),
                "block_graph_contains_original_block":
                    _graph_contains_pointer(
                        graph, int(item["completion_block"], 0)),
                "block_graph_contains_original_invoke":
                    _graph_contains_pointer(
                        graph, int(item["completion_invoke"], 0)),
            }
        row = {
            "label": label, "time": time.time(),
            "elapsed_from_call_s": time.time() - item["time"],
            "thread": thread.GetThreadID(), "owner_tpidr": _hex(owner),
            "identifier": item["identifier"],
            "request": item.get("request"),
            "completion_block": item["completion_block"],
            "completion_invoke": item["completion_invoke"],
            "registers": {"x%d" % i: _hex(bks._reg(frame, "x%d" % i))
                          for i in range(8)},
            **extra,
            "interpretation": (
                "post-sync result callback boundary; linkage to the caller's "
                "completion block requires matching captured invoke or a "
                "later exact-block invoke event"),
        }
        _write("progress.%s.json" % label, row)
        print("DM_WRAPPER_COMPLETION_TRACE " + json.dumps(row, sort_keys=True),
              flush=True)
        if label == "DM_WRAPPER_RESULT_CALLBACK_INVOKE":
            RUN_PLUGIN_CALLS.pop(key, None)
        return False

    callback_dm_handoff = next((item for item in reversed(
        transport.CALLBACK_DM_HANDOFFS)
        if owner == int(item.get("receiver_tpidr", "0"), 0)), None)
    dm_outbound_match = bool(
        (DM_OUTBOUND_HANDOFF and
         owner == int(DM_OUTBOUND_HANDOFF.get("receiver_tpidr", "0"), 0) or
         callback_dm_handoff is not None) and
        progname == "com.apple.datamigrator")
    systemapp_callback_match = bool(
        SYSTEMAPP_CALLBACK_CONTEXT and
        owner == int(SYSTEMAPP_CALLBACK_CONTEXT.get("receiver_tpidr", "0"), 0) and
        progname == "com.apple.migrationpluginwrapper")
    msu_callback_match = bool(
        MSU_CALLBACK_CONTEXT and
        owner == int(MSU_CALLBACK_CONTEXT.get("receiver_tpidr", "0"), 0) and
        progname == "com.apple.migrationpluginwrapper")
    if progname != "com.apple.migrationpluginwrapper" and not dm_outbound_match:
        return False
    # The kernel object's destination join is useful corroboration, but it is
    # not a prerequisite for collecting candidate dispatch events. Attribute
    # them only when the handoff's receiving thread and this event's
    # connection/callback lineage agree.
    if (HANDOFF_EVENT is None and DM_OUTBOUND_HANDOFF is None and
            not systemapp_callback_match and not msu_callback_match and label not in (
                "WRAPPER_XPC_EVENT_HANDLER", "WRAPPER_BS_HANDLE_MESSAGE")):
        return False
    thread = frame.GetThread()
    handoff_match = (HANDOFF_EVENT if HANDOFF_EVENT and
                     int(HANDOFF_EVENT.get("receiver_tpidr", "0"), 0) == owner
                     else None)
    if handoff_match is None and dm_outbound_match:
        handoff_match = DM_OUTBOUND_HANDOFF or callback_dm_handoff
    if handoff_match is None and systemapp_callback_match:
        handoff_match = SYSTEMAPP_CALLBACK_CONTEXT
    if handoff_match is None and msu_callback_match:
        handoff_match = MSU_CALLBACK_CONTEXT
    count = HITS.get(label, 0) + 1
    HITS[label] = count
    # Bound log volume but preserve early dispatch edges and occasional later
    # progress samples. No callback mutates guest state or stops the boot.
    if count > 64 and count % 32:
        return False
    regs = {"x%d" % i: _hex(bks._reg(frame, "x%d" % i)) for i in range(8)}
    pointers = {}
    for name in ("x0", "x1", "x2", "x3"):
        pointer = bks._reg(frame, name)
        pointers[name] = _memory(process, pointer, 0x100)
    row = {
        "label": label,
        "time": time.time(),
        "after_matched_ith_kmsg_window": HANDOFF_EVENT is not None or
                                           DM_OUTBOUND_HANDOFF is not None,
        "after_systemapp_identifier_event_candidate":
            systemapp_callback_match,
        "after_msu_identifier_event_candidate": msu_callback_match,
        "handoff": handoff_match,
        "progname": _prog(process),
        "pid": process.GetProcessID(),
        "thread": thread.GetThreadID(),
        "tpidr_el1": _hex(owner),
        "pc": _hex(bks._reg(frame, "pc")),
        "lr": _hex(bks._reg(frame, "lr")),
        "registers": regs,
        "argument_bytes": pointers,
        "stack": _stack(thread),
        "interpretation": ("same-thread DataMigrator boundary after exact secondary kmsg handoff; not a terminal reply" if dm_outbound_match else "wrapper dispatch boundary only; not a reply"),
    }
    if label == "WRAPPER_XPC_SEND":
        connection = bks._reg(frame, "x0")
        message = bks._reg(frame, "x1")
        try:
            fields = kernelpath.plugin._xpc_fields(process, message)
        except Exception:
            fields = {}
        graph = _bounded_pointer_graph(process, message, max_nodes=48,
                                       depth_limit=4)
        # Do not restrict this to the originally matched callback TPIDR. A
        # migrationResult XPC send can be emitted on a different wrapper
        # thread; register that exact sender for a bounded kernel send-helper
        # sample while leaving the request's pinned reply object untouched.
        migration_result_candidate = transport.register_migration_result_xpc_candidate(
            lldb.debugger, process, frame, connection, message, fields, graph,
            event_time=row["time"])
        callback_context = (SYSTEMAPP_CALLBACK_CONTEXT
                            if systemapp_callback_match else
                            MSU_CALLBACK_CONTEXT if msu_callback_match else None)
        callback_age = (row["time"] - float(callback_context.get("time", 0))
                        if callback_context else -1)
        same_callback_thread_window = bool(
            callback_context and 0 <= callback_age <= 5.0)
        same_handoff_connection = bool(
            handoff_match is not None and
            connection == int(handoff_match.get("connection", "0"), 0))
        if same_callback_thread_window or same_handoff_connection:
            if callback_context:
                callback_context["outbound_send_seen"] = True
                callback_context["outbound_xpc_message"] = _hex(message)
                callback_context["outbound_xpc_send_time"] = row["time"]
            DECODED_SENDS += 1
            # Seed the existing kernel object-equality probe with the wrapper
            # callback thread. This is only a thread/time candidate: the XPC
            # message pointer is not a kernel kmsg and its destination must be
            # read from a later mach_msg2 send header.
            kernel_target = getattr(transport, "KERNEL_TARGET", None)
            if kernel_target is not None:
                pinned_wrapper_owner = kernel_target.get(
                    "wrapper_callback_owner")
                accept_wrapper_thread = (
                    pinned_wrapper_owner is None or
                    owner == pinned_wrapper_owner)
                if accept_wrapper_thread:
                    kernel_target["wrapper_callback_owner"] = owner
                    kernel_target["wrapper_callback_send_time"] = row["time"]
                    kernel_target["wrapper_callback_connection"] = connection
                    kernel_target["wrapper_callback_message"] = message
                    kernel_target["wrapper_callback_send_correlation"] = (
                        "same registered SystemApp event thread within 5s; "
                        "connection may differ and send remains a candidate"
                        if same_callback_thread_window else
                        "same thread and connection as matched request handoff")
                    transport.KERNEL_SEND_HELPER_OWNERS.add(owner)
                    transport._refresh_kernel_send_helper_condition(
                        lldb.debugger)
                else:
                    _write("progress.WRAPPER_XPC_SEND_OTHER_THREAD.json", {
                        "label": "WRAPPER_XPC_SEND_OTHER_THREAD",
                        "request": _hex(MATCHED_REQUEST or 0),
                        "identifier": SYSTEMAPP_ID,
                        "candidate_owner": _hex(owner),
                        "pinned_wrapper_callback_owner":
                            _hex(pinned_wrapper_owner),
                        "connection": _hex(connection),
                        "message": _hex(message),
                        "interpretation": "other wrapper send; retained as traffic, does not replace the matched callback thread",
                    })
                # Re-key the existing pre-send kmsg breakpoint to this exact
                # wrapper thread after the original DM request has seeded the
                # object filters. This avoids a global mach_msg2 stop storm.
                bp_id = transport.KERNEL_BREAKPOINTS.get(
                    "KERNEL_MACH2_SEND_HELPER_CALL")
                if bp_id and accept_wrapper_thread:
                    bp = process.GetTarget().FindBreakpointByID(bp_id)
                    if bp and bp.IsValid():
                        condition = bp.GetCondition() or ""
                        actual = condition
                        kernel_target["wrapper_send_bp_condition"] = {
                            "requested": condition, "actual": actual,
                            "installed": "$x19 == 0x%x" % owner in condition}
            decoded = {
                "label": "MATCHED_WRAPPER_XPC_SEND_DECODE",
                "time": row["time"], "send_index": DECODED_SENDS,
                "request_owner_tpidr": _hex(MATCHED_SYNC_OWNER or 0),
                "request": _hex(MATCHED_REQUEST or 0),
                "reply_object": _hex(MATCHED_REPLY_OBJECT or 0),
                "destination_object": _hex(MATCHED_DEST_OBJECT or 0),
                "wrapper_owner_tpidr": _hex(owner),
                "connection": _hex(connection), "message": _hex(message),
                "same_connection_as_matched_event": True,
                "same_thread_as_registered_event_or_handoff": True,
                "same_connection_as_registered_event": (
                    connection == int(handoff_match.get("connection", "0"), 0)
                    if handoff_match else False),
                "correlation_basis": kernel_target.get(
                    "wrapper_callback_send_correlation") if kernel_target else None,
                "wrapper_send_bp_condition": (kernel_target.get(
                    "wrapper_send_bp_condition") if kernel_target else None),
                "xpc_fields": fields,
                "message_bytes": _memory(process, message, 0x800),
                "message_pointer_graph": graph,
                "recognized_key_strings": _key_strings(graph),
                "wrapper_send_context": row,
                "interpretation": "same-thread short-window registered-event XPC send candidate; request object fields are comparison candidates only; outbound destination requires kernel send-object evidence",
            }
            if migration_result_candidate:
                decoded["migration_result_kernel_join_candidate"] = \
                    migration_result_candidate
            _write("progress.MATCHED_WRAPPER_XPC_SEND_DECODE.%03d.json" %
                   DECODED_SENDS, decoded)
        return False
    if label == "WRAPPER_XPC_EVENT_HANDLER":
        connection = bks._reg(frame, "x0")
        event = bks._reg(frame, "x1")
        if handoff_match is not None:
            handoff_match["connection"] = _hex(connection)
            handoff_match["event"] = _hex(event)
        event_bytes = row["argument_bytes"].get("x1", "")
        event_pointer_graph = _bounded_pointer_graph(process, event)
        try:
            event_fields = kernelpath.plugin._xpc_fields(process, event)
        except Exception:
            event_fields = {}
        systemapp_payload = (
            SYSTEMAPP_ID.encode("ascii").hex() in event_bytes or
            "SystemAppMigrator" in json.dumps(event_fields, sort_keys=True) or
            any(item.get("systemapp_identifier_present")
                for item in event_pointer_graph))
        row["event_fields"] = event_fields
        row["event_pointer_graph"] = event_pointer_graph
        row["systemapp_identifier_present"] = systemapp_payload
        row["registered_callback_candidate"] = {
            "status": "candidate only; event identity not established"
                      if not systemapp_payload else
                      "candidate; invocation breakpoint will confirm this event block",
        }
        print("WRAPPER_REPLY_TRACE " + json.dumps(row, sort_keys=True), flush=True)
        event_file = ("progress.DATAMIGRATOR_XPC_EVENT_HANDLER_CANDIDATE.json"
                      if dm_outbound_match else
                      "progress.WRAPPER_XPC_EVENT_HANDLER_CANDIDATE.json")
        bks._write_event(event_file, row)
        # Capture the actual registered block/invoke for each bounded event.
        # A callback hit is not attributed to SystemApp until its event or
        # same-thread app-level message carries the matched plugin identity.
        _arm_registered_handler(frame, connection, event)
        return False
    if label == "WRAPPER_BS_HANDLE_MESSAGE":
        message = bks._reg(frame, "x2")
        try:
            fields = kernelpath.plugin._xpc_fields(process, message)
        except Exception:
            fields = {}
        message_bytes = _memory(process, message, 0x400)
        row.update({
            "app_message": _hex(message),
            "app_message_bytes": message_bytes,
            "app_message_fields": fields,
            "systemapp_identifier_present":
                SYSTEMAPP_ID.encode("ascii").hex() in message_bytes or
                SYSTEMAPP_ID in json.dumps(fields, sort_keys=True),
            "interpretation": "app-level handler boundary; reply remains open",
        })
        _write("progress.WRAPPER_APP_HANDLE_MESSAGE.json", row)
        return False
    print("WRAPPER_REPLY_TRACE " + json.dumps(row, sort_keys=True), flush=True)
    bks._write_event("progress.%s.%03d.json" % (label, count), row)
    return False


def install(debugger, slide):
    global ARMED, HANDOFF_EVENT, ORIGINAL_EVENT
    global DM_OUTBOUND_HANDOFF, SYSTEMAPP_CALLBACK_CONTEXT, MSU_CALLBACK_CONTEXT
    global MATCHED_SYNC_OWNER, MATCHED_REQUEST, MATCHED_REPLY_OBJECT
    global MATCHED_DEST_OBJECT, DECODED_SENDS, DECODED_CLIENT_MESSAGES
    global ACTIVE_SLIDE, OPEN_SITE_BP
    global MIGRATION_RESULT_RECEIVER_CONTEXT
    ARMED = False
    ACTIVE_SLIDE = int(slide)
    OPEN_SITE_BP = None
    OPEN_RETURN_META.clear()
    OPEN_RETURN_BY_ADDRESS.clear()
    WRAPPER_MACH_SITE_BPS.clear()
    WRAPPER_MACH_RETURN_META.clear()
    WRAPPER_MACH_RETURN_BPS.clear()
    HANDOFF_EVENT = None
    DM_OUTBOUND_HANDOFF = None
    SYSTEMAPP_CALLBACK_CONTEXT = None
    MSU_CALLBACK_CONTEXT = None
    MIGRATION_RESULT_RECEIVER_CONTEXT = None
    MATCHED_SYNC_OWNER = MATCHED_REQUEST = None
    MATCHED_REPLY_OBJECT = MATCHED_DEST_OBJECT = None
    DECODED_SENDS = DECODED_CLIENT_MESSAGES = 0
    HITS.clear()
    HANDLER_INVOKES.clear()
    HANDLER_BREAKPOINTS.clear()
    HANDLER_META.clear()
    RUN_PLUGIN_CALLS.clear()
    # The setup script imports this composed module, but LLDB does not
    # automatically register Python modules imported transitively by it.
    # kernelpath.install dynamically registers its own callback name with
    # LLDB, so explicitly import that module before it arms any breakpoints.
    interpreter = debugger.GetCommandInterpreter()
    for module in (kernelpath, transport):
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("command script import %s" % module.__file__,
                                  result)
        if not result.Succeeded():
            raise RuntimeError("failed LLDB import %s: %s" %
                               (module.__name__, result.GetError()))
    # Install the established DM call-time and kernel object-identity ladder.
    kernelpath.install(debugger, slide)
    # kernelpath.install narrows the composed lifecycle callbacks to its
    # historical Passbook/SpringBoard targets. This probe's matched request
    # is SystemAppMigrator, so explicitly extend that filter before the first
    # DataMigrator method call; otherwise CLIENT_SYNC_CALL is visible only to
    # an outer gate observer and the post-copyin reply-object resolver never
    # arms.
    kernelpath.plugin.TARGET_IDS.add(SYSTEMAPP_ID)
    kernelpath.plugin.TARGET_PATHS = tuple(kernelpath.plugin.TARGET_PATHS) + (
        "SystemAppMigrator.migrator",)
    print("WRAPPER_REPLY_SYSTEMAPP_LIFECYCLE_FILTER_READY id=%s path=%s" %
          (SYSTEMAPP_ID, "SystemAppMigrator.migrator"), flush=True)
    ORIGINAL_EVENT = transport._event
    transport._event = _wrapped_transport_event
    for static, label in SITES:
        _install_one(debugger, slide, static, label)
    ARMED = True
    print("DM_LOCK_WRAPPER_REPLY_READY slide=0x%x sites=%d; "
          "event-handler candidates are bounded and request attribution is explicit" %
          (int(slide), len(SITES)), flush=True)

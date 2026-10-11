"""Pair the matched msgID-0 migration with its plugin-launch XPC call.

This composes the existing check-in/reply probe with the generic plugin
lifecycle tracer. It arms the DataMigrator launch call/return only after the
matched DM_START_MIGRATION_CALL, captures this boot's path/identifier/params,
then keys wrapper receive/reply observations to that identifier. Read-only.
"""

import json
import os
import time

import lldb

import bks_checkin_callbacks as bks
import dm_lock_checkin_reply_callbacks as reply
import dm_migration_server_inner_callbacks as inner
import dm_migration_server_migrate_callbacks as migration
import dm_perform_roundtrip_callbacks as common
import dm_plugin_lifecycle_callbacks as plugin


CALL = 0x13768
RETURN = 0x1376C
SITES = {}
CALLS = {}
RECEIVERS = {}
ARMED_FOR_REQUEST = False
PLUGIN_ORIGINAL_ON_HIT = None
RAW_WRAPPER_HITS = 0
SYNC_CALLS = {}
MACH_MSG2_TRAP = 0x237CCFCCC
MACH_MSG_TRAP = 0x237CCFC0C
MACH_TRAPS_ARMED = False
MACH_WINDOW_START = 0.0
# Keep the bounded Mach-trap capture alive long enough to catch delayed
# wrapper-side receive/reply traffic (PLUGIN11's same-receiver intermediate
# message arrived ~158 s after CLIENT_SYNC_CALL). Events remain filtered by
# the call-time port tuple rather than becoming a general message survey.
MACH_WINDOW_SECONDS = float(os.environ.get("DVM_PLUGIN_MACH_WINDOW_SECONDS", "600"))
MACH_DEST_PORT = 0
MACH_REPLY_PORT = 0
# Mach port names are local to a task's IPC space. Keep numeric names scoped
# to the process that supplied them; equal numbers in another process are not
# a transport join without resolving both names to the same ipc_port.
MACH_TARGET_PORTS = {}
# Save the actual user-side mach_msg2 send from the matched sync owner. The
# copied-in kernel kmsg may use KDATA_OOL, so plugin identity is joined from
# this same-owner vector/header sample rather than guessed from inline bytes.
MACH_USER_SENDS = {}
# The matched high-level call is the correlation anchor, but XPC can issue its
# underlying mach_msg2 on another thread in the same DataMigrator process.
# Keep this process/request identity separately from per-TPIDR observations.
SYSTEMAPP_ACTIVE_CALL = None
SYSTEMAPP_ID = "com.apple.MobileInstallation.SystemAppMigrator"
KERNEL_SLIDE = int(os.environ.get("DVM_KERNEL_SLIDE", "0x20000000"), 0)
# These older static PCs were from the adjacent mach_msg trap/copyin helper,
# not this build's mach_msg2_trap send path; retained only for comparison.
LEGACY_KMSG_COPYIN_RETURN = 0xFFFFfff00AAA3900
LEGACY_MQUEUE_COPYIN_RESOLVED = 0xFFFFfff00AA8FA18
# Start on the live mach_msg2_trap path after it saves TPIDR_EL1 (x19), then
# arm the copyin return PCs in that same function for the matched owner.
MACH2_OWNER_PROLOGUE = 0xFFFFfff00AAA3B8C
RECEIVE_COPYIN_CALL = 0xFFFFfff00AAA402C
RECEIVE_OBJECT_LOADED = 0xFFFFfff00AAA4040
# +0x40bc is before the outgoing-send block in mach_msg2_trap. It is the
# csel result of a secondary ipc-object copyin at +0x40b0, not a dequeue.
# x8 is the translated object candidate; x23 is written by this instruction.
SECONDARY_COPYIN_RESULT = 0xFFFFfff00AAA40BC
# The outgoing mach_msg2 send helper is called at +0x3f50 and returns at
# +0x3f54. The later +0x4160 call is not a send: bootkc disassembly passes
# (waitq, timeout, options, current thread, flags) to ipc_mqueue_receive.
MACH2_SEND_HELPER_CALL = 0xFFFFfff00AAA3F50
MACH2_SEND_HELPER_RETURN = 0xFFFFfff00AAA3F54
MACH2_MQUEUE_RECEIVE_CALL = 0xFFFFfff00AAA4160
MACH2_MQUEUE_RECEIVE_RETURN = 0xFFFFfff00AAA4164
MACH2_RECEIVE_RESULTS_CALL_RETURN = 0xFFFFfff00AAA417C
# Successful ipc_mqueue_receive unlinks the selected queue entry, clears its
# links, then PAC-stores that entry into receiver thread +0x3b0 (ith_kmsg).
# Other statically found +0x3b0 stores cover direct-delivery paths, so arm
# those too and classify only with queue/object + kmsg-header evidence.
MQUEUE_ITH_KMSG_QUEUE_STORE = 0xFFFFfff00AA8F44C
MQUEUE_ITH_KMSG_DIRECT_STORE_A = 0xFFFFfff00AA8E638
MQUEUE_ITH_KMSG_DIRECT_STORE_B = 0xFFFFfff00AA8ECA0
# The former 0x38fc/0x3900 pair belongs to an adjacent Mach trap helper, not
# this build's mach_msg2_trap (which starts at 0x3b38). These are return PCs
# after calls to 0xaa89f50 and 0xaa8dd58. On the live mach_msg2 path, the
# kmsg is kept in x21 across the second helper; x23 is packed/tagged metadata.
MACH2_AA89F50_RETURN = 0xFFFFfff00AAA3EF0
MACH2_AA8DD58_RETURN = 0xFFFFfff00AAA3F54
KERNEL_META = {}
KERNEL_BREAKPOINTS = {}
KERNEL_SYNC_OWNER = 0
KERNEL_TARGET = None
KERNEL_RECEIVE_CONDITIONED = False
KERNEL_PATH_ARMED = False
KERNEL_PENDING_REPLY_RECEIVE = {}
WRAPPER_THREAD_SEND_SAMPLES = 0
# Request-window XPC messages containing migrationResult may be emitted by a
# wrapper thread other than the registered callback thread. Keep those XPC
# send observations separate from the immutable SystemApp request object pair;
# the kernel send-helper hook below resolves the actual Mach destination.
MIGRATION_RESULT_XPC_CANDIDATES = {}
MIGRATION_RESULT_JOIN_WINDOW = float(os.environ.get(
    "DVM_MIGRATION_RESULT_JOIN_WINDOW", "30"))
POST_MIGRATION_RESULT_SEND_WINDOW = float(os.environ.get(
    "DVM_POST_MIGRATION_RESULT_SEND_WINDOW", "7200"))
KERNEL_SEND_HELPER_OWNERS = set()
# Optional, request-scoped all-sender watch. The existing global helper-return
# BP inspects every mach_msg2 sender after copyin; when enabled, it emits only
# destinations object-equal to the pinned reply. This avoids another breakpoint
# on the hot send-helper call site.
TRACE_ANY_PINNED_REPLY_DEST = os.environ.get(
    "DVM_TRACE_ANY_PINNED_REPLY_DEST", "0") == "1"
ANY_PINNED_REPLY_SEND_WATCH_ARMED = False
ANY_PINNED_REPLY_SEND_HITS = 0
ANY_PINNED_REPLY_SEND_HITS = 0
# Coverage accounting for the independent post-copyin and receive-handoff
# paths. A zero equality result is useful only alongside evidence that the
# relevant callbacks were live for the matched window.
PIN_SEND_PATH_STATS = {"postcopy_return_hits": 0,
                       "postcopy_return_readable": 0,
                       "postcopy_return_bad_kmsg": 0}
PIN_ITH_KMSG_STATS = {
    label: {"hits": 0, "candidate_headers": 0, "reply_matches": 0}
    for label in ("KERNEL_ITH_KMSG_QUEUE_STORE",
                  "KERNEL_ITH_KMSG_DIRECT_STORE_A",
                  "KERNEL_ITH_KMSG_DIRECT_STORE_B")}
# Short-lived, same-thread candidates for wrapper sends emitted from registered
# MSU/SystemApp callback events. MSU can run immediately before the matched
# SystemApp request, so its send may precede KERNEL_TARGET initialization.
CALLBACK_SEND_CONTEXTS = {}
CALLBACK_SEND_BPS = {}
CALLBACK_OUTBOUND_CANDIDATES = []
CALLBACK_HANDOFF_BPS = {}
CALLBACK_DM_HANDOFFS = []
# SystemAppMigrator's completion block is per-call heap state.  Resolve its
# invoke pointer from that exact block at the matched runPluginAtPath call,
# then add a return breakpoint from the invoke's live LR.  These are kept
# separate from the global plugin lifecycle sites so unrelated migrators do
# not stop or contaminate the matched request.
COMPLETION_INVOKE_BPS = {}
COMPLETION_INVOKE_META = {}
COMPLETION_RETURN_META = {}
SYSTEMAPP_PLUGIN_RETURN_BPS = {}
SYSTEMAPP_PLUGIN_RETURN_META = {}


def _user_pointer_key(value):
    """Mask arm64e high PAC bits for executable/block pointer comparison."""
    return int(value) & 0x0000ffffffffffff


def _dynamic_breakpoint(debugger, address, callback, label):
    target = debugger.GetSelectedTarget()
    bp = target.BreakpointCreateByAddress(int(address))
    if not bp.IsValid():
        raise RuntimeError("failed to arm %s at 0x%x" % (label, address))
    bp.SetScriptCallbackFunction(__name__ + "." + callback)
    proof = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command list %d" % bp.GetID(), proof)
    proof_text = proof.GetOutput() or proof.GetError() or ""
    if __name__ + "." + callback not in proof_text:
        bp.SetEnabled(False)
        raise RuntimeError("LLDB did not attach callback for %s: %s" %
                           (label, proof_text))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x callback=%s" %
          (bp.GetID(), label, address, __name__ + "." + callback),
          flush=True)
    return bp


def _arm_systemapp_completion(debugger, process, frame, call):
    """Arm the exact call's block invoke and, when invoked, its live LR return."""
    block = int(call.get("completion", "0"), 0)
    raw = bks._read(process, block + 0x10, 8) if block else b""
    if len(raw) != 8:
        call["completion_invoke_read"] = "short/unreadable block+0x10"
        return
    invoke_raw = int.from_bytes(raw, "little")
    invoke = _user_pointer_key(invoke_raw)
    call.update(completion_invoke_raw="0x%x" % invoke_raw,
                completion_invoke="0x%x" % invoke,
                completion_invoke_source="this call's completion block +0x10")
    if not 0x100000000 <= invoke < (1 << 43):
        call["completion_invoke_read"] = "not a canonical user executable pointer"
        return

    # One address may serve several blocks; retain each call's exact block so
    # the callback accepts only x0 == that call-time block.
    invoke_bp_id = COMPLETION_INVOKE_BPS.get(invoke)
    if invoke_bp_id is None:
        bp = _dynamic_breakpoint(
            debugger, invoke, "on_completion_invoke",
            "SYSTEMAPP_COMPLETION_INVOKE")
        invoke_bp_id = bp.GetID()
        COMPLETION_INVOKE_BPS[invoke] = invoke_bp_id
        COMPLETION_INVOKE_META[invoke_bp_id] = []
    call_meta = {
        "block": block, "owner": int(call["owner"]),
        "identifier": call.get("identifier"), "path": call.get("path"),
        "invoke": invoke, "call_time": time.time(),
        "request": SYNC_CALLS.get(int(call["owner"]), {}).get("request"),
    }
    COMPLETION_INVOKE_META[invoke_bp_id].append(call_meta)

    # The call-site BP is at the matched `bl`; static +0x1376c is re-derived
    # from the current image base and captured in the event. It is filtered by
    # owner + SystemApp identifier, unlike the deliberately omitted global
    # runPluginAtPath return probe.
    base = migration.MAIN_BASE
    plugin_return = base + RETURN if base else 0
    if plugin_return:
        existing = SYSTEMAPP_PLUGIN_RETURN_BPS.get(plugin_return)
        if existing is None:
            bp = _dynamic_breakpoint(
                debugger, plugin_return, "on_systemapp_plugin_return",
                "SYSTEMAPP_RUN_PLUGIN_RETURN")
            SYSTEMAPP_PLUGIN_RETURN_BPS[plugin_return] = bp.GetID()
            SYSTEMAPP_PLUGIN_RETURN_META[bp.GetID()] = []
        SYSTEMAPP_PLUGIN_RETURN_META[
            SYSTEMAPP_PLUGIN_RETURN_BPS[plugin_return]].append(call_meta)
        call["plugin_return_pc"] = "0x%x" % plugin_return


def on_completion_invoke(frame, bp_loc, _context):
    """Accept only the completion block whose +0x10 supplied this PC."""
    bp_id = bp_loc.GetBreakpoint().GetID()
    block_arg = bks._reg(frame, "x0")
    owner = bks._reg(frame, "tpidr_el1")
    selected = None
    for candidate in COMPLETION_INVOKE_META.get(bp_id, []):
        if (block_arg == candidate["block"] and
                candidate.get("identifier") == SYSTEMAPP_ID):
            selected = candidate
            break
    if selected is None:
        return False

    thread = frame.GetThread()
    lr = bks._reg(frame, "lr")
    row = {
        "label": "DM_SYSTEMAPP_COMPLETION_INVOKE",
        "time": time.time(), "thread": thread.GetThreadID(),
        "tpidr_el1": "0x%x" % owner,
        "call_owner_tpidr": "0x%x" % selected["owner"],
        "identifier": selected["identifier"],
        "path": selected.get("path"),
        "request": ("0x%x" % selected["request"]
                    if selected.get("request") else None),
        "completion_block": "0x%x" % selected["block"],
        "invoke_pc": "0x%x" % selected["invoke"],
        "x0_block": "0x%x" % block_arg,
        "x0_equals_call_block": block_arg == selected["block"],
        "lr_return_pc": "0x%x" % lr,
        "registers": _registers(frame),
    }
    print("DM_SYSTEMAPP_COMPLETION " + json.dumps(row, sort_keys=True),
          flush=True)
    bks._write_event("progress.DM_SYSTEMAPP_COMPLETION_INVOKE.json", row)

    # Arm only after the exact block invoke is observed. A return breakpoint
    # may be shared by unrelated calls; the callback filters thread + owner +
    # block metadata and disables itself after the matched return.
    if 0x100000000 <= lr < (1 << 43):
        try:
            ret_bp = _dynamic_breakpoint(
                frame.GetThread().GetProcess().GetTarget().GetDebugger(), lr,
                "on_completion_return", "SYSTEMAPP_COMPLETION_RETURN")
            COMPLETION_RETURN_META[ret_bp.GetID()] = dict(
                selected, invoke_thread=thread.GetThreadID(),
                invoke_owner=owner, invoke_lr=lr,
                invoke_time=row["time"])
        except Exception as error:
            row["return_arm_error"] = (
                type(error).__name__ + ": " + str(error))
            bks._write_event(
                "progress.DM_SYSTEMAPP_COMPLETION_RETURN_ARM_ERROR.json", row)
    return False


def on_completion_return(frame, bp_loc, _context):
    bp = bp_loc.GetBreakpoint()
    meta = COMPLETION_RETURN_META.get(bp.GetID())
    if not meta:
        return False
    thread = frame.GetThread()
    owner = bks._reg(frame, "tpidr_el1")
    if (thread.GetThreadID() != meta["invoke_thread"] or
            owner != meta["invoke_owner"]):
        return False
    row = {
        "label": "DM_SYSTEMAPP_COMPLETION_RETURN",
        "time": time.time(), "elapsed_s": time.time() - meta["invoke_time"],
        "thread": thread.GetThreadID(), "tpidr_el1": "0x%x" % owner,
        "identifier": meta["identifier"], "path": meta.get("path"),
        "request": ("0x%x" % meta["request"]
                    if meta.get("request") else None),
        "completion_block": "0x%x" % meta["block"],
        "invoke_pc": "0x%x" % meta["invoke"],
        "return_pc": "0x%x" % meta["invoke_lr"],
        "x0_result": "0x%x" % bks._reg(frame, "x0"),
        "registers": _registers(frame),
    }
    print("DM_SYSTEMAPP_COMPLETION " + json.dumps(row, sort_keys=True),
          flush=True)
    bks._write_event("progress.DM_SYSTEMAPP_COMPLETION_RETURN.json", row)
    bp.SetEnabled(False)
    COMPLETION_RETURN_META.pop(bp.GetID(), None)
    return False


def on_systemapp_plugin_return(frame, bp_loc, _context):
    """Per-call return marker at this boot's resolved runPluginAtPath return."""
    bp = bp_loc.GetBreakpoint()
    owner = bks._reg(frame, "tpidr_el1")
    for candidate in SYSTEMAPP_PLUGIN_RETURN_META.get(bp.GetID(), []):
        if (owner == candidate["owner"] and
                candidate.get("identifier") == SYSTEMAPP_ID):
            row = {
                "label": "DM_SYSTEMAPP_RUN_PLUGIN_RETURN_1376C",
                "time": time.time(), "thread": frame.GetThread().GetThreadID(),
                "tpidr_el1": "0x%x" % owner,
                "identifier": candidate["identifier"],
                "path": candidate.get("path"),
                "request": ("0x%x" % candidate["request"]
                            if candidate.get("request") else None),
                "return_pc": "0x%x" % bks._reg(frame, "pc"),
                "result_x0": "0x%x" % bks._reg(frame, "x0"),
                "registers": _registers(frame),
            }
            print("DM_SYSTEMAPP_PLUGIN_RETURN " +
                  json.dumps(row, sort_keys=True), flush=True)
            bks._write_event(
                "progress.DM_SYSTEMAPP_RUN_PLUGIN_RETURN_1376C.json", row)
            bp.SetEnabled(False)
            return False
    return False


def _secondary_dm_match(owner, process):
    """Return this boot's exact DataMigrator receiver join, if established."""
    try:
        if bks._progname(process) != "com.apple.datamigrator":
            return None
    except Exception:
        return None
    target = KERNEL_TARGET or {}
    receiver_owner = target.get("wrapper_outbound_receiver_tpidr")
    if receiver_owner is not None and owner == receiver_owner:
        return target.get("wrapper_outbound_receiver")
    for handoff in reversed(CALLBACK_DM_HANDOFFS):
        try:
            if owner == int(handoff.get("receiver_tpidr", "0"), 0):
                return handoff
        except (TypeError, ValueError):
            continue
    return None


def register_callback_send_context(debugger, owner, context):
    """Watch one registered wrapper callback thread's immediate Mach sends."""
    owner = int(owner)
    CALLBACK_SEND_CONTEXTS[owner] = dict(context)
    if owner in CALLBACK_SEND_BPS:
        return
    bp = _kernel_bp(debugger, MACH2_SEND_HELPER_CALL,
                    "KERNEL_CALLBACK_SEND_HELPER_%x" % owner)
    _kernel_condition(bp, "$x19 == 0x%x" % owner,
                      "callback-send-owner-%x" % owner,
                      disable_on_failure=False)
    CALLBACK_SEND_BPS[owner] = bp.GetID()


def _arm_callback_handoff_bps(debugger, kmsg, candidate_id):
    for static, reg, suffix in (
            (MQUEUE_ITH_KMSG_QUEUE_STORE, "x20", "queue"),
            (MQUEUE_ITH_KMSG_DIRECT_STORE_A, "x23", "directa"),
            (MQUEUE_ITH_KMSG_DIRECT_STORE_B, "x21", "directb")):
        key = (candidate_id, suffix)
        if key in CALLBACK_HANDOFF_BPS:
            continue
        bp = _kernel_bp(debugger, static,
                        "KERNEL_CALLBACK_ITH_%s_%x" % (suffix, candidate_id))
        _kernel_condition(bp, "$%s == 0x%x" % (reg, kmsg),
                          "callback-ith-%s-%x" % (suffix, candidate_id),
                          disable_on_failure=False)
        CALLBACK_HANDOFF_BPS[key] = bp.GetID()


def _log_secondary_dm_userland(frame, label, message=0, endpoint=0,
                               selector=0):
    """Capture XPC/message boundaries only on the proven secondary receiver."""
    process = frame.GetThread().GetProcess()
    owner = bks._reg(frame, "tpidr_el1")
    matched = _secondary_dm_match(owner, process)
    if not matched:
        return False
    message = int(message or 0)
    try:
        fields = plugin._xpc_fields(process, message) if message else {}
    except Exception:
        fields = {}
    stack = []
    thread = frame.GetThread()
    for index in range(min(thread.GetNumFrames(), 16)):
        item = thread.GetFrameAtIndex(index)
        stack.append({"pc": "0x%x" % item.GetPC(),
                      "symbol": item.GetFunctionName() or ""})
    row = {
        "label": label,
        "time": time.time(),
        "target_identifier": SYSTEMAPP_ID,
        "systemapp_request": "0x%x" % (KERNEL_TARGET or {}).get("request", 0),
        "systemapp_reply_object": "0x%x" % (KERNEL_TARGET or {}).get("reply_object", 0),
        "receiver_progname": "com.apple.datamigrator",
        "receiver_pid": process.GetProcessID(),
        "receiver_thread": thread.GetThreadID(),
        "receiver_tpidr_el1": "0x%x" % owner,
        "matched_kernel_handoff": matched,
        "message": "0x%x" % message if message else None,
        "message_fields": fields,
        "message_bytes": _user_bytes(process, message, 0x800) if message else "",
        "message_pointer_bytes": _user_bytes(process, message, 0x180) if message else "",
        "endpoint_candidate": "0x%x" % endpoint if endpoint else None,
        "endpoint_bytes": _user_bytes(process, endpoint, 0x100) if endpoint else "",
        "selector_candidate": "0x%x" % selector if selector else None,
        "selector_text_candidate": plugin._cstring(process, selector, 128) if selector else "",
        "registers": _registers(frame),
        "stack": stack,
        "interpretation": "userspace boundary on the exact thread whose ith_kmsg matched the secondary DataMigrator destination; handler semantics and relation to the original terminal reply require payload/reply-object evidence",
    }
    bks._write_event("progress.IPC_SECONDARY_DM_USERSPACE.%s.json" % label, row)
    print("IPC_SECONDARY_DM_USERSPACE " + json.dumps(row, sort_keys=True), flush=True)
    return True


def _user_bytes(process, address, size=0x100):
    """Capture a bounded raw user object for transport-identity analysis."""
    if not 0 < address < (1 << 43):
        return ""
    try:
        return bks._read(process, address, size).hex()
    except Exception:
        return ""


def _registers(frame, names=tuple("x%d" % i for i in range(9))):
    return {name: "0x%x" % bks._reg(frame, name) for name in names}


def _mach_header(process, address, size=0x80):
    raw = _user_bytes(process, address, min(max(int(size), 24), 0x100))
    data = bytes.fromhex(raw) if raw else b""
    if len(data) < 24:
        return {"address": "0x%x" % address, "raw": raw}
    return {
        "address": "0x%x" % address,
        "bits": "0x%x" % int.from_bytes(data[0:4], "little"),
        "size": int.from_bytes(data[4:8], "little"),
        "remote_port": int.from_bytes(data[8:12], "little"),
        "local_port": int.from_bytes(data[12:16], "little"),
        "voucher_port": int.from_bytes(data[16:20], "little"),
        "message_id": int.from_bytes(data[20:24], "little", signed=True),
        "raw": raw,
    }


def _kernel_u8(process, address):
    raw = bks._read(process, address, 1)
    return raw[0] if len(raw) == 1 else None


def _kernel_u64(process, address):
    raw = bks._read(process, address, 8)
    return int.from_bytes(raw, "little") if len(raw) == 8 else None


def _ipc_object_key(value):
    """Strip top PAC/sign-extension bits for a kernel-address comparison."""
    low = value & 0x0000ffffffffffff
    # This boot's kernel heap/stack pointers include low-48 values in the
    # 0xffd... and 0xffe... ranges (for example the live mach_msg2 kmsg at
    # 0xffffffdcea033b68). Requiring only the 0xfff... image range silently
    # discarded valid ipc_object candidates.
    return low if low >= 0x0000ff0000000000 else 0


def _kernel_header(process, kmsg):
    """Read the measured 32-byte inline header in this bootkc's ipc_kmsg."""
    kind_byte = _kernel_u8(process, kmsg + 0x3f) if kmsg else None
    if kind_byte is None:
        return {"kmsg": "0x%x" % kmsg, "header_error": "kmsg unreadable"}
    ikm_type = kind_byte >> 6
    # For IKM_TYPE_ALL_INLINED / IKM_TYPE_KDATA_OOL, ikm_header() is kmsg+0x40.
    # Do not strip/authenticate the out-of-line signed pointer speculatively.
    if ikm_type > 1:
        return {"kmsg": "0x%x" % kmsg, "ikm_type": ikm_type,
                "header_error": "non-inline signed header not decoded"}
    header = kmsg + 0x40
    # The live mach_msg2 copyin sample measured a 32-byte kernel header:
    # bits/size at +0/+4, widened destination/local ipc_port* fields at +8/+16,
    # then the 32-bit voucher name and Mach msgh_id at +0x18/+0x1c. The
    # 508-byte user message became a 516-byte kmsg (+8), so its body starts at
    # kmsg+0x60 (header +0x20), not +0x68.
    raw = bks._read(process, header, 32)
    if len(raw) != 32:
        return {"kmsg": "0x%x" % kmsg, "ikm_type": ikm_type,
                "header": "0x%x" % header, "header_error": "short read"}
    return {
        "kmsg": "0x%x" % kmsg, "ikm_type": ikm_type,
        "header": "0x%x" % header, "header_raw": raw.hex(),
        "bits": "0x%x" % int.from_bytes(raw[0:4], "little"),
        "size": int.from_bytes(raw[4:8], "little"),
        "remote_object": "0x%x" % int.from_bytes(raw[8:16], "little"),
        "local_object": "0x%x" % int.from_bytes(raw[16:24], "little"),
        "voucher_name": "0x%x" % int.from_bytes(raw[24:28], "little"),
        "message_id": int.from_bytes(raw[28:32], "little", signed=True),
        "remote_field_u64": "0x%x" % int.from_bytes(raw[8:16], "little"),
        "local_field_u64": "0x%x" % int.from_bytes(raw[16:24], "little"),
    }


def _dequeue_header_candidates(process, link_or_kmsg):
    """Read conservative header candidates for an ith_kmsg handoff sample.

    The queue path stores the selected queue entry in thread+0x3b0. The entry
    may be the kmsg base or its embedded link; report both address hypotheses
    rather than silently assuming an offset. A candidate becomes useful only
    when its decoded header/message identity is coherent.
    """
    candidates = []
    for label, address in (("stored_value", int(link_or_kmsg)),
                           ("stored_value_minus_0x10", int(link_or_kmsg) - 0x10)):
        if not _ipc_object_key(address):
            candidates.append({"candidate": label, "address": "0x%x" % address,
                               "header_error": "not a canonical kernel pointer"})
            continue
        try:
            header = _kernel_header(process, address)
        except Exception as error:
            header = {"kmsg": "0x%x" % address,
                      "header_error": type(error).__name__ + ": " + str(error)}
        candidates.append({"candidate": label, "address": "0x%x" % address,
                           "decoded": header})
    return candidates


def _kernel_bp(debugger, static, label):
    target = debugger.GetSelectedTarget()
    address = static + KERNEL_SLIDE
    bp = target.BreakpointCreateByAddress(address)
    if not bp.IsValid():
        raise RuntimeError("failed to create kernel breakpoint %s at 0x%x" %
                           (label, address))
    bp.SetScriptCallbackFunction(__name__ + ".on_kernel_identity_hit")
    KERNEL_META[bp.GetID()] = label
    KERNEL_BREAKPOINTS[label] = bp.GetID()
    proof = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command list %d" % bp.GetID(), proof)
    proof_text = proof.GetOutput() or proof.GetError() or ""
    if __name__ + ".on_kernel_identity_hit" not in proof_text:
        raise RuntimeError("LLDB did not attach kernel callback for %s: %s" %
                           (label, proof_text))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x callback=%s" %
          (bp.GetID(), label, address,
           __name__ + ".on_kernel_identity_hit"), flush=True)
    return bp


def _kernel_arm(debugger):
    # Callback-side send probes may populate KERNEL_BREAKPOINTS before the
    # SystemApp sync is reached.  Only the mach_msg2 owner prologue proves
    # that the matched send-path capture has been armed; any other kernel
    # breakpoint here must not suppress it.
    if "KERNEL_MACH2_OWNER_PROLOGUE" in KERNEL_BREAKPOINTS:
        return
    entry_bp = _kernel_bp(debugger, MACH2_OWNER_PROLOGUE,
                          "KERNEL_MACH2_OWNER_PROLOGUE")
    # At this instruction x19 was just loaded from TPIDR_EL1 in the exact
    # bootkc mach_msg2_trap prologue. A condition avoids stopping every guest
    # syscall; the callback still verifies the register before accepting it.
    condition = "$x19 == 0x%x" % KERNEL_SYNC_OWNER
    _kernel_condition(entry_bp, condition, "matched-mach2-thread",
                      disable_on_failure=False)
    print("DM_LOCK_PLUGIN_KERNEL_IDENTITY_READY slide=0x%x entry=0x%x owner=0x%x" %
          (KERNEL_SLIDE, MACH2_OWNER_PROLOGUE + KERNEL_SLIDE,
           KERNEL_SYNC_OWNER), flush=True)


def _kernel_path_arm(debugger):
    global KERNEL_PATH_ARMED
    if KERNEL_PATH_ARMED:
        return
    sites = (
        # x19 is TPIDR_EL1 saved by this mach_msg2_trap prologue. The old
        # 0x38fc/0x3900 hooks were in the adjacent mach_msg trap function.
        (MACH2_AA89F50_RETURN, "KERNEL_MACH2_AA89F50_RETURN", True),
        # The matched sender seeds KERNEL_TARGET; later helper returns are
        # global so a peer's send to the captured reply ipc_object can be
        # recognized. The callback emits only the object-equal reply candidate.
        (MACH2_AA8DD58_RETURN, "KERNEL_MACH2_AA8DD58_RETURN", False),
        (RECEIVE_COPYIN_CALL, "KERNEL_RECEIVE_COPYIN_CALL", True),
        (RECEIVE_OBJECT_LOADED, "KERNEL_RECEIVE_OBJECT_LOADED", False),
        (SECONDARY_COPYIN_RESULT, "KERNEL_SECONDARY_COPYIN_RESULT", False),
        (MACH2_SEND_HELPER_CALL, "KERNEL_MACH2_SEND_HELPER_CALL", True),
        (MACH2_MQUEUE_RECEIVE_CALL, "KERNEL_MACH2_MQUEUE_RECEIVE_CALL", True),
        (MACH2_MQUEUE_RECEIVE_RETURN, "KERNEL_MACH2_MQUEUE_RECEIVE_RETURN", True),
        (MACH2_RECEIVE_RESULTS_CALL_RETURN,
         "KERNEL_MACH2_RECEIVE_RESULTS_RETURN", True),
        (MQUEUE_ITH_KMSG_QUEUE_STORE, "KERNEL_ITH_KMSG_QUEUE_STORE", False),
        (MQUEUE_ITH_KMSG_DIRECT_STORE_A,
         "KERNEL_ITH_KMSG_DIRECT_STORE_A", False),
        (MQUEUE_ITH_KMSG_DIRECT_STORE_B,
         "KERNEL_ITH_KMSG_DIRECT_STORE_B", False),
    )
    for static, label, owner_only in sites:
        bp = _kernel_bp(debugger, static, label)
        if owner_only:
            if label == "KERNEL_MACH2_SEND_HELPER_CALL":
                KERNEL_SEND_HELPER_OWNERS.add(KERNEL_SYNC_OWNER)
                _refresh_kernel_send_helper_condition(debugger)
            else:
                _kernel_condition(bp, "$x19 == 0x%x" % KERNEL_SYNC_OWNER,
                                  label, disable_on_failure=False)
    KERNEL_PATH_ARMED = True
    print("DM_LOCK_PLUGIN_KERNEL_PATH_READY owner=0x%x mach2_aa89f50_return=0x%x "
          "mach2_send_call=0x%x mach2_send_return=0x%x "
          "aa8dd58_return=0x%x mqueue_receive_call=0x%x "
          "mqueue_receive_return=0x%x receive_results_return=0x%x "
          "ith_kmsg_queue_store=0x%x direct_store_a=0x%x direct_store_b=0x%x "
          "receive_object=0x%x secondary_copyin_result=0x%x "
          "callback_filters=object-equality" % (
              KERNEL_SYNC_OWNER, MACH2_AA89F50_RETURN + KERNEL_SLIDE,
              MACH2_SEND_HELPER_CALL + KERNEL_SLIDE,
              MACH2_SEND_HELPER_RETURN + KERNEL_SLIDE,
              MACH2_AA8DD58_RETURN + KERNEL_SLIDE,
              MACH2_MQUEUE_RECEIVE_CALL + KERNEL_SLIDE,
              MACH2_MQUEUE_RECEIVE_RETURN + KERNEL_SLIDE,
              MACH2_RECEIVE_RESULTS_CALL_RETURN + KERNEL_SLIDE,
              MQUEUE_ITH_KMSG_QUEUE_STORE + KERNEL_SLIDE,
              MQUEUE_ITH_KMSG_DIRECT_STORE_A + KERNEL_SLIDE,
              MQUEUE_ITH_KMSG_DIRECT_STORE_B + KERNEL_SLIDE,
              RECEIVE_OBJECT_LOADED + KERNEL_SLIDE,
              SECONDARY_COPYIN_RESULT + KERNEL_SLIDE), flush=True)


def _refresh_kernel_send_helper_condition(debugger):
    """Filter the send-helper hook to the sync owner and observed XPC senders."""
    bp_id = KERNEL_BREAKPOINTS.get("KERNEL_MACH2_SEND_HELPER_CALL")
    if bp_id is None:
        return False
    bp = debugger.GetSelectedTarget().FindBreakpointByID(bp_id)
    if not bp or not bp.IsValid():
        return False
    owners = {int(value) for value in KERNEL_SEND_HELPER_OWNERS if value}
    if KERNEL_SYNC_OWNER:
        owners.add(int(KERNEL_SYNC_OWNER))
    if KERNEL_TARGET and KERNEL_TARGET.get("wrapper_callback_owner"):
        owners.add(int(KERNEL_TARGET["wrapper_callback_owner"]))
    expression = " || ".join("$x19 == 0x%x" % value
                             for value in sorted(owners)) or "false"
    return _kernel_condition(bp, expression,
                             "migrationResult-sender-owner-set",
                             disable_on_failure=False)


def pinned_reply_path_coverage():
    """Return the actual callback/site counts for this pinned request."""
    return {
        "request": "0x%x" % ((KERNEL_TARGET or {}).get("request", 0)),
        "pinned_reply_object": "0x%x" %
            ((KERNEL_TARGET or {}).get("reply_object", 0)),
        "pinned_reply_object_key": "0x%x" %
            ((KERNEL_TARGET or {}).get("reply_key", 0)),
        "postcopy_send_helper_return": dict(PIN_SEND_PATH_STATS),
        "ith_kmsg_store_sites": {
            key: dict(value) for key, value in PIN_ITH_KMSG_STATS.items()},
        "post_migration_result_receiver_tpidr": (
            "0x%x" % (KERNEL_TARGET or {}).get(
                "wrapper_migration_result_receiver_tpidr", 0)
            if (KERNEL_TARGET or {}).get(
                "wrapper_migration_result_receiver_tpidr") else None),
        "post_migration_result_receiver_contexts": list(
            (KERNEL_TARGET or {}).get(
                "wrapper_migration_result_receiver_contexts", [])),
        "post_migration_result_send_window_seconds":
            POST_MIGRATION_RESULT_SEND_WINDOW,
        "sites_armed": [
            {"label": label,
             "runtime_pc": "0x%x" % (static + KERNEL_SLIDE)}
            for static, label in (
                (MACH2_AA8DD58_RETURN, "KERNEL_MACH2_AA8DD58_RETURN"),
                (MQUEUE_ITH_KMSG_QUEUE_STORE, "KERNEL_ITH_KMSG_QUEUE_STORE"),
                (MQUEUE_ITH_KMSG_DIRECT_STORE_A,
                 "KERNEL_ITH_KMSG_DIRECT_STORE_A"),
                (MQUEUE_ITH_KMSG_DIRECT_STORE_B,
                 "KERNEL_ITH_KMSG_DIRECT_STORE_B"))
            if label in KERNEL_BREAKPOINTS],
    }


def _key_strings(graph):
    """Return protocol key names visible in an XPC field graph."""
    try:
        rendered = json.dumps(graph, sort_keys=True, default=str).encode("utf-8")
    except Exception:
        rendered = repr(graph).encode("utf-8", "replace")
    keys = ("pluginIdentifier", "migrationResult", "checkNecessity",
            "msgID", "result", "error", "reply", "success",
            "SystemAppMigrator", "MergeBuddyProvisioningResponse.migrator")
    return sorted(key for key in keys if key.encode("ascii") in rendered)


def register_migration_result_xpc_candidate(debugger, process, frame,
                                            connection, message, fields, graph,
                                            event_time=None):
    """Remember one XPC migrationResult send and arm its TPIDR at send-helper.

    This records only the high-level XPC candidate. A later kernel kmsg from
    that TPIDR is still a short-window candidate unless its payload/transport
    identity also matches; destination equality itself is always read from
    the kernel kmsg, never copied from the request or XPC graph.
    """
    owner = bks._reg(frame, "tpidr_el1")
    target = KERNEL_TARGET or {}
    if (not target.get("request_object_pair_pinned") or
            "migrationResult" not in fields or not owner):
        return None
    now = float(event_time if event_time is not None else time.time())
    candidate = {
        "label": "WRAPPER_MIGRATION_RESULT_XPC_SEND_CANDIDATE",
        "time": now,
        "request": "0x%x" % target.get("request", 0),
        "identifier": SYSTEMAPP_ID,
        "reply_object_key": "0x%x" % target.get("reply_key", 0),
        "destination_object_key": "0x%x" % target.get("remote_key", 0),
        "sender_tpidr": "0x%x" % owner,
        "sender_pid": process.GetProcessID(),
        "sender_progname": bks._progname(process),
        "connection": "0x%x" % connection,
        "xpc_message": "0x%x" % message,
        "xpc_fields": fields,
        "recognized_key_strings": _key_strings(graph),
        "systemapp_identifier_present": SYSTEMAPP_ID.encode("ascii") in
            json.dumps(graph, sort_keys=True).encode("utf-8"),
        "correlation_basis": "request-window XPC payload contains migrationResult; kernel destination unresolved until a send-helper kmsg from this TPIDR",
        "kernel_send_join_window_seconds": MIGRATION_RESULT_JOIN_WINDOW,
    }
    key = int(owner)
    MIGRATION_RESULT_XPC_CANDIDATES.setdefault(key, []).append(candidate)
    # Retain a bounded history per thread so repeated sends can each be
    # correlated without allowing unrelated owners to overwrite the reply pin.
    MIGRATION_RESULT_XPC_CANDIDATES[key] = \
        MIGRATION_RESULT_XPC_CANDIDATES[key][-32:]
    KERNEL_SEND_HELPER_OWNERS.add(key)
    _refresh_kernel_send_helper_condition(debugger)
    bks._write_event(
        "progress.WRAPPER_MIGRATION_RESULT_XPC_SEND_CANDIDATE.json",
        candidate)
    return candidate


def _kernel_condition(bp, expression, label, disable_on_failure=True):
    """Install a register/memory predicate; preserve callback fallback on failure."""
    try:
        bp.SetCondition(expression)
        output = bp.GetCondition() or ""
        ok = output == expression
    except Exception as error:
        output = str(error)
        ok = False
    print("DM_LOCK_PLUGIN_KERNEL_CONDITION label=%s bp=%d condition=%s result=%s" %
          (label, bp.GetID(), expression, output or "ok"), flush=True)
    if not ok and disable_on_failure:
        # Do not leave an unfiltered kernel IPC breakpoint running over the
        # rest of the boot if this LLDB build cannot compile the predicate.
        bp.SetEnabled(False)
    return ok


def on_kernel_identity_hit(frame, bp_loc, _context):
    global KERNEL_TARGET, KERNEL_RECEIVE_CONDITIONED, KERNEL_SYNC_OWNER
    global WRAPPER_THREAD_SEND_SAMPLES, ANY_PINNED_REPLY_SEND_HITS
    label = KERNEL_META.get(bp_loc.GetBreakpoint().GetID())
    if not label:
        return False
    owner = bks._reg(frame, "tpidr_el1")
    process = frame.GetThread().GetProcess()
    if label == "KERNEL_MACH2_AA8DD58_RETURN" and TRACE_ANY_PINNED_REPLY_DEST:
        # This BP is already global to all mach_msg2 senders and runs after
        # copyin. x21 is the kmsg passed to the helper; check the resolved
        # destination for every sender without relying on per-task port names.
        target = KERNEL_TARGET or {}
        if (target.get("request_object_pair_pinned") and
                target.get("reply_key")):
            PIN_SEND_PATH_STATS["postcopy_return_hits"] += 1
            kmsg = bks._reg(frame, "x21")
            try:
                header = _kernel_header(process, kmsg)
                destination = int(header.get("remote_field_u64", "0"), 0)
                destination_key = _ipc_object_key(destination)
            except Exception:
                header, destination, destination_key = {}, 0, 0
            if destination_key:
                PIN_SEND_PATH_STATS["postcopy_return_readable"] += 1
            else:
                PIN_SEND_PATH_STATS["postcopy_return_bad_kmsg"] += 1
            if destination_key and destination_key == target["reply_key"]:
                size = int(header.get("size", 0) or 0)
                body_size = min(max(size - 32, 0), 0x2000)
                try:
                    body = bks._read(process, kmsg + 0x60, body_size) \
                        if body_size else b""
                except Exception:
                    body = b""
                ANY_PINNED_REPLY_SEND_HITS += 1
                record = {
                    "label": "IPC_ANY_SEND_DEST_PINNED_REPLY_OBJECT",
                    "time": time.time(),
                    "request": "0x%x" % target.get("request", 0),
                    "original_sync_owner_tpidr": "0x%x" % target.get("owner", 0),
                    "pinned_reply_object": "0x%x" % target.get("reply_object", 0),
                    "pinned_reply_object_key": "0x%x" % target["reply_key"],
                    "sender_tpidr_el1": "0x%x" % owner,
                    "sender_pid": process.GetProcessID(),
                    "sender_progname": bks._progname(process),
                    "kmsg": "0x%x" % kmsg,
                    "header": header,
                    "destination_object": "0x%x" % destination,
                    "destination_object_key": "0x%x" % destination_key,
                    "destination_equals_pinned_reply": True,
                    "send_helper_result_x0": "0x%x" % bks._reg(frame, "x0"),
                    "body_hex": body.hex(),
                    "body_contains_migrationResult": b"migrationResult" in body,
                    "registers": _registers(
                        frame, tuple("x%d" % i for i in range(9)) +
                        tuple("x19", "x20", "x21", "x22", "x23", "x30", "sp", "pc")),
                    "identity_limit": "resolved send-helper return kmsg destination equals the pinned ipc_object; delivery and same-owner sync return are separate evidence",
                }
                bks._write_event(
                    "success.IPC_ANY_SEND_DEST_PINNED_REPLY_OBJECT.%03d.json" %
                    ANY_PINNED_REPLY_SEND_HITS, record)
                print("IPC_ANY_SEND_DEST_PINNED_REPLY_OBJECT " +
                      json.dumps(record, sort_keys=True), flush=True)

    if label.startswith("KERNEL_CALLBACK_SEND_HELPER_"):
        context = CALLBACK_SEND_CONTEXTS.get(owner)
        if not context or bks._progname(process) != "com.apple.migrationpluginwrapper":
            return False
        kmsg = bks._reg(frame, "x0")
        try:
            header = _kernel_header(process, kmsg)
            size = int(header.get("size", 0) or 0)
            body_size = min(max(size - 32, 0), 0x800)
            body = bks._read(process, kmsg + 0x60, body_size) if body_size else b""
        except Exception as error:
            header = {"header_error": type(error).__name__ + ": " + str(error)}
            body = b""
            size = 0
        # Keep only the compact CPX pid/msgID callback shape used by the
        # already-observed DataMigrator secondary message. This is a sender
        # candidate; receipt still requires the full object/header/body join.
        if (int(header.get("message_id", 0) or 0) != 0x10000000 or
                size < 32 or b"CPX@" not in body or
                b"pid" not in body or b"msgID" not in body):
            return False
        destination = int(header.get("remote_field_u64", "0"), 0)
        candidate_id = len(CALLBACK_OUTBOUND_CANDIDATES) + 1
        candidate = {
            "candidate_id": candidate_id,
            "time": time.time(),
            "event_context": context,
            "sender_tpidr": "0x%x" % owner,
            "sender_pid": process.GetProcessID(),
            "sender_progname": bks._progname(process),
            "xpc_message_candidate": context.get("outbound_xpc_message"),
            "kmsg": "0x%x" % kmsg,
            "header": header,
            "destination_object": "0x%x" % destination,
            "destination_object_key": "0x%x" % _ipc_object_key(destination),
            "body_hex": body.hex(),
            "message_id": header.get("message_id"),
            "size": size,
            "kernel_registers": _registers(
                frame, tuple("x%d" % i for i in range(9)) + ("sp", "pc")),
            "identity_level": "same-thread registered-event send candidate; awaiting exact DataMigrator ith_kmsg join",
        }
        CALLBACK_OUTBOUND_CANDIDATES.append(candidate)
        bks._write_event(
            "progress.IPC_WRAPPER_EVENT_OUTBOUND_KMSG_CANDIDATE.%03d.json" %
            candidate_id, candidate)
        _arm_callback_handoff_bps(process.GetTarget().GetDebugger(), kmsg,
                                  candidate_id)
        return False

    if label.startswith("KERNEL_CALLBACK_ITH_"):
        try:
            candidate_id = int(label.rsplit("_", 1)[1], 16)
            candidate = next(item for item in CALLBACK_OUTBOUND_CANDIDATES
                             if item.get("candidate_id") == candidate_id)
        except (ValueError, StopIteration):
            return False
        if bks._progname(process) != "com.apple.datamigrator":
            return False
        if label.startswith("KERNEL_CALLBACK_ITH_queue_"):
            kmsg = bks._reg(frame, "x20")
            receiver_thread_reg = "x19"
        elif label.startswith("KERNEL_CALLBACK_ITH_directa_"):
            kmsg = bks._reg(frame, "x23")
            receiver_thread_reg = "x24"
        else:
            kmsg = bks._reg(frame, "x21")
            receiver_thread_reg = "x23"
        try:
            header = _kernel_header(process, kmsg)
            size = int(header.get("size", 0) or 0)
            body_size = min(max(size - 32, 0), 0x800)
            body = bks._read(process, kmsg + 0x60, body_size) if body_size else b""
            exact = (
                _ipc_object_key(int(header.get("remote_field_u64", "0"), 0)) ==
                int(candidate.get("destination_object_key", "0"), 0) and
                int(header.get("message_id", -1)) == int(candidate.get("message_id", -2)) and
                size == int(candidate.get("size", -1)) and
                body.hex() == candidate.get("body_hex"))
        except Exception:
            exact = False
            header = {"header_error": "could not decode candidate receiver kmsg"}
            body = b""
        if not exact:
            return False
        thread = bks._reg(frame, receiver_thread_reg)
        handoff = {
            "label": "IPC_SECONDARY_DM_DEST_ITH_KMSG",
            "time": time.time(),
            "candidate_id": candidate_id,
            "receiver_progname": bks._progname(process),
            "receiver_pid": process.GetProcessID(),
            "receiver_tpidr": "0x%x" % owner,
            "receiver_thread": "0x%x" % thread,
            "receiver_kmsg": "0x%x" % kmsg,
            "sender_candidate": candidate,
            "receiver_header": header,
            "body_exact_match": True,
            "destination_object_match": True,
            "full_message_match": True,
            "interpretation": "exact callback-send candidate was handed to DataMigrator ith_kmsg; terminal SystemApp reply requires comparison to the original reply object and same-owner return",
        }
        CALLBACK_DM_HANDOFFS.append(handoff)
        bks._write_event(
            "success.IPC_SECONDARY_DM_DEST_ITH_KMSG.%03d.json" % candidate_id,
            handoff)
        print("IPC_SECONDARY_DM_DEST_ITH_KMSG " +
              json.dumps(handoff, sort_keys=True), flush=True)
        return False

    if label in {"CLIENT_SYNC_RETURN_FOLLOWUP",
                 "DM_RUN_PLUGIN_RETURN_FOLLOWUP"}:
        if owner == KERNEL_SYNC_OWNER:
            _event(frame, label, {
                "identifier": SYSTEMAPP_ID,
                "request": "0x%x" % (KERNEL_TARGET["request"]
                                      if KERNEL_TARGET else 0),
                "same_owner": True,
                "result_x0": "0x%x" % bks._reg(frame, "x0"),
                "registers": _registers(frame),
            })
            bks._write_event("success.%s.json" % label, {
                "label": label, "time": time.time(),
                "owner": "0x%x" % owner,
                "request": "0x%x" % (KERNEL_TARGET["request"]
                                      if KERNEL_TARGET else 0),
                "result_x0": "0x%x" % bks._reg(frame, "x0"),
            })
        return False

    if label == "KERNEL_MACH2_OWNER_PROLOGUE":
        # x19 is the live TPIDR_EL1 value loaded immediately before this PC.
        saved_owner = bks._reg(frame, "x19")
        if owner != KERNEL_SYNC_OWNER or saved_owner != KERNEL_SYNC_OWNER:
            return False
        _event(frame, "KERNEL_MACH2_OWNER_ENTRY", {
            "identifier": SYSTEMAPP_ID,
            "owner_match": True,
            "tpidr_el1": "0x%x" % owner,
            "x19_saved_tpidr": "0x%x" % saved_owner,
            "kernel_registers": _registers(frame),
            "next_static_pc": "0x%x" % (MACH2_OWNER_PROLOGUE + 4),
            "kernel_slide": "0x%x" % KERNEL_SLIDE,
        })
        bp = frame.GetThread().GetProcess().GetTarget().FindBreakpointByID(
            bp_loc.GetBreakpoint().GetID())
        bp.SetEnabled(False)
        _kernel_path_arm(process.GetTarget().GetDebugger())
        return False

    if label in {"KERNEL_RECEIVE_COPYIN_CALL"}:
        if owner != KERNEL_SYNC_OWNER:
            return False
        _event(frame, label, {
            "identifier": SYSTEMAPP_ID,
            "owner_match": True,
            "tpidr_el1": "0x%x" % owner,
            "kernel_registers": _registers(frame),
            "relevant_registers": _registers(frame, tuple("x%d" % i for i in range(8))),
            "static_pc": "0x%x" % (bks._reg(frame, "pc") - KERNEL_SLIDE),
            "kernel_slide": "0x%x" % KERNEL_SLIDE,
        })
        return False

    if label == "KERNEL_MACH2_SEND_HELPER_CALL":
        active = SYSTEMAPP_ACTIVE_CALL or {}
        user_send = MACH_USER_SENDS.get(owner, {})
        active_thread_send = bool(
            user_send.get("request") == active.get("request") and
            user_send.get("identifier") == SYSTEMAPP_ID and
            user_send.get("active_request_same_process"))
        migration_result_candidates = [
            item for item in MIGRATION_RESULT_XPC_CANDIDATES.get(owner, [])
            if 0 <= time.time() - float(item.get("time", 0)) <=
                MIGRATION_RESULT_JOIN_WINDOW]
        if owner == KERNEL_SYNC_OWNER or active_thread_send or \
                migration_result_candidates:
            kmsg = bks._reg(frame, "x0")
            try:
                header = _kernel_header(process, kmsg)
            except Exception as error:
                header = {"header_error": type(error).__name__ + ": " + str(error)}
            sync = SYNC_CALLS.get(owner) or (active if active_thread_send else {})
            user_send_matches = bool(
                sync.get("identifier") == SYSTEMAPP_ID and
                user_send.get("request") == sync.get("request") and
                user_send.get("identifier") == SYSTEMAPP_ID and
                user_send.get("message_id") == 0x40000000 and
                header.get("message_id") == 0x40000000 and
                (user_send.get("contains_plugin_identifier") or
                 owner == KERNEL_SYNC_OWNER) and
                header.get("size") in (508, 516))
            same_process_user_send_candidate = bool(
                sync.get("identifier") == SYSTEMAPP_ID and
                user_send.get("request") == sync.get("request") and
                user_send.get("identifier") == SYSTEMAPP_ID and
                user_send.get("active_request_same_process") and
                user_send.get("message_id") == 0x40000000 and
                header.get("message_id") == 0x40000000 and
                header.get("size") in (508, 516))
            same_owner_expanded_candidate = bool(
                owner == KERNEL_SYNC_OWNER and
                sync.get("identifier") == SYSTEMAPP_ID and
                sync.get("request_plugin_identifier") == SYSTEMAPP_ID and
                header.get("message_id") == 0x40000000 and
                header.get("size") == 516)
            send_candidate_matches = (user_send_matches or
                                      same_process_user_send_candidate or
                                      same_owner_expanded_candidate)
            remote_obj = int(header.get("remote_field_u64", "0"), 0)
            local_obj = int(header.get("local_field_u64", "0"), 0)
            remote_key = _ipc_object_key(remote_obj)
            local_key = _ipc_object_key(local_obj)
            if migration_result_candidates and KERNEL_TARGET and \
                    KERNEL_TARGET.get("request_object_pair_pinned"):
                size = int(header.get("size", 0) or 0)
                body_size = min(max(size - 32, 0), 0x2000)
                try:
                    body = bks._read(process, kmsg + 0x60, body_size)
                except Exception:
                    body = b""
                body_hex = body.hex()
                body_ascii = body.decode("latin-1", "replace")
                for xpc_candidate in migration_result_candidates:
                    delta = time.time() - float(xpc_candidate.get("time", 0))
                    migration_dest_candidate = {
                        "candidate_id": len(KERNEL_TARGET.setdefault(
                            "migration_result_dest_candidates", [])) + 1,
                        "time": time.time(),
                        "xpc_candidate": xpc_candidate,
                        "sender_tpidr": "0x%x" % owner,
                        "sender_pid": process.GetProcessID(),
                        "sender_progname": bks._progname(process),
                        "kmsg": "0x%x" % kmsg,
                        "header": header,
                        "message_id": header.get("message_id"),
                        "size": size,
                        "destination_object": "0x%x" % remote_obj,
                        "destination_object_key": "0x%x" % remote_key,
                        "body_hex": body_hex,
                    }
                    KERNEL_TARGET["migration_result_dest_candidates"].append(
                        migration_dest_candidate)
                    # Every row is explicitly a same-TPIDR, bounded-time
                    # candidate. Do not promote it to an XPC transaction join
                    # from temporal proximity alone.
                    _event(frame,
                           "IPC_MIGRATION_RESULT_KERNEL_SEND_CANDIDATE", {
                        "identifier": SYSTEMAPP_ID,
                        "request": xpc_candidate.get("request"),
                        "xpc_sender_tpidr": xpc_candidate.get("sender_tpidr"),
                        "kernel_sender_tpidr": "0x%x" % owner,
                        "xpc_connection": xpc_candidate.get("connection"),
                        "xpc_message": xpc_candidate.get("xpc_message"),
                        "xpc_time": xpc_candidate.get("time"),
                        "seconds_after_xpc_send": delta,
                        "join_basis": "same TPIDR plus bounded XPC-send-to-kernel-send-helper interval; transaction identity not established by this alone",
                        "kmsg": "0x%x" % kmsg,
                        "kmsg_header": header,
                        "message_id": header.get("message_id"),
                        "size": size,
                        "destination_object": "0x%x" % remote_obj,
                        "destination_object_key": "0x%x" % remote_key,
                        "local_object": "0x%x" % local_obj,
                        "local_object_key": "0x%x" % local_key,
                        "pinned_request_reply_object": "0x%x" %
                            KERNEL_TARGET.get("reply_object", 0),
                        "destination_equals_pinned_reply": bool(
                            remote_key and remote_key ==
                            KERNEL_TARGET.get("reply_key")),
                        "kmsg_body_hex": body_hex,
                        "kmsg_body_contains_migrationResult":
                            b"migrationResult" in body,
                        "kmsg_body_contains_systemapp_identifier":
                            SYSTEMAPP_ID in body_ascii,
                        "xpc_message_fields": xpc_candidate.get("xpc_fields"),
                        "xpc_recognized_keys":
                            xpc_candidate.get("recognized_key_strings"),
                        "progname": bks._progname(process),
                        "task_pid": process.GetProcessID(),
                        "registers": _registers(
                            frame, tuple("x%d" % i for i in range(9)) +
                            tuple("x%d" % i for i in range(19, 26)) +
                            ("sp", "pc")),
                        "interpretation": "kernel destination is resolved from this send-helper kmsg; XPC-to-kmsg relation remains a candidate unless body/transport identity corroborates it",
                    })
            # The first exact same-owner user-send/kmsg join is the request
            # whose reply right we are tracing. Pin it immediately, before
            # receiver handoff; later SystemApp-shaped sends can be emitted by
            # other DM threads and must never replace this object pair.
            target_already_pinned = bool(
                KERNEL_TARGET and
                KERNEL_TARGET.get("request") == sync.get("request") and
                KERNEL_TARGET.get("request_object_pair_pinned"))
            pin_this_send = bool(
                user_send_matches and remote_key and local_key and
                owner == KERNEL_SYNC_OWNER and
                not target_already_pinned)
            if pin_this_send:
                # This is the kmsg passed to the send helper after copyin.
                # It is sufficient to seed later object-equality probes, but
                # is not itself proof that the send completed or was dequeued.
                KERNEL_TARGET = {
                    "owner": KERNEL_SYNC_OWNER or owner,
                    "send_owner": owner,
                    "request": sync["request"],
                    "identifier": SYSTEMAPP_ID,
                    "remote_object": remote_obj,
                    "reply_object": local_obj,
                    "remote_key": remote_key,
                    "reply_key": local_key,
                    "header": header,
                    "capture_stage": "send_helper_call_candidate",
                    "request_object_pair_pinned": True,
                    "identity_level": (
                        "user-send-and-kernel-kmsg-match" if user_send_matches
                        else "same-process-user-send-or-matched-sync-owner-plus-wire-id-size"),
                }
                if TRACE_ANY_PINNED_REPLY_DEST:
                    ANY_PINNED_REPLY_SEND_WATCH_ARMED = True
                    print("DM_LOCK_PLUGIN_ANY_PINNED_REPLY_DEST_WATCH_READY "
                          "request=0x%x reply_key=0x%x pc=0x%x "
                          "site=global-post-copyin-helper-return" %
                          (KERNEL_TARGET["request"], KERNEL_TARGET["reply_key"],
                           MACH2_AA8DD58_RETURN + KERNEL_SLIDE), flush=True)
                candidate = _event(
                    frame, "IPC_SYSTEMAPP_SEND_HELPER_CALL_OBJECT_CANDIDATE", {
                        "identifier": SYSTEMAPP_ID,
                        "request": "0x%x" % sync["request"],
                        "owner_tpidr": "0x%x" % owner,
                        "task_pid": process.GetProcessID(),
                        "progname": bks._progname(process),
                        "message_id": "0x%x" % header["message_id"],
                        "size": header.get("size"),
                        "kmsg_header": header,
                        "destination_object_candidate": "0x%x" % remote_obj,
                        "reply_object_candidate": "0x%x" % local_obj,
                        "candidate_source": "post-copyin kmsg at send-helper call",
                        "identity_level": KERNEL_TARGET["identity_level"],
                        "user_send_candidate": user_send,
                        "send_completion_or_peer_dequeue_proven": False,
                    })
                bks._write_event(
                    "progress.IPC_SYSTEMAPP_SEND_HELPER_CALL_OBJECT_CANDIDATE.json",
                    candidate)
            elif send_candidate_matches and remote_key and local_key:
                # Preserve the candidate for later analysis without mutating
                # KERNEL_TARGET, which is pinned to the original sync's
                # destination/reply objects after its receiver handoff.
                candidate = _event(
                    frame, "IPC_SYSTEMAPP_SEND_OBJECTS_CANDIDATE", {
                        "identifier": SYSTEMAPP_ID,
                        "request": "0x%x" % sync["request"],
                        "candidate_owner_tpidr": "0x%x" % owner,
                        "original_sync_owner_tpidr": "0x%x" % KERNEL_SYNC_OWNER,
                        "task_pid": process.GetProcessID(),
                        "progname": bks._progname(process),
                        "message_id": "0x%x" % header.get("message_id", 0),
                        "size": header.get("size"),
                        "destination_object_candidate": "0x%x" % remote_obj,
                        "reply_object_candidate": "0x%x" % local_obj,
                        "candidate_source": "later SystemApp-shaped send; original request object pair retained",
                        "original_reply_object_key": "0x%x" %
                            (KERNEL_TARGET or {}).get("reply_key", 0),
                    })
                bks._write_event(
                    "progress.IPC_SYSTEMAPP_SEND_OBJECTS_CANDIDATE.json",
                    candidate)
            _event(frame, "IPC_SAME_OWNER_SEND_HELPER_CALL", {
                "identifier": SYSTEMAPP_ID,
                "target_request": "0x%x" % (sync.get("request", 0)),
                "owner_tpidr": "0x%x" % owner,
                "kmsg_x0": "0x%x" % kmsg,
                "kmsg_header": header,
                "same_owner_user_send_match": send_candidate_matches,
                "registers": _registers(
                    frame, tuple("x%d" % i for i in range(9)) + ("sp", "pc")),
                "site_semantics": "call to bootkc helper at 0xaa8dd58; return is +0x3f54",
            })
        elif KERNEL_TARGET and (post_mr_context := next((context for context in
                reversed(KERNEL_TARGET.get(
                    "wrapper_migration_result_receiver_contexts", []))
                if int(context.get("receiver_tpidr", "0"), 0) == owner and
                context.get("receiver_progname") == bks._progname(process) and
                time.time() - float(context.get("time", 0)) <=
                    POST_MIGRATION_RESULT_SEND_WINDOW), None)):
            # Attribute follow-up calls only while the exact receiving
            # process/thread context is still live. TPIDR values can be
            # recycled; do not call a later UserEventAgent/CommCenter send a
            # wrapper continuation just because the numeric TPIDR repeats.
            kmsg = bks._reg(frame, "x0")
            try:
                header = _kernel_header(process, kmsg)
            except Exception as error:
                header = {"header_error": type(error).__name__ + ": " + str(error)}
            destination = int(header.get("remote_field_u64", "0"), 0)
            local = int(header.get("local_field_u64", "0"), 0)
            size = int(header.get("size", 0) or 0)
            body_size = min(max(size - 32, 0), 0x1000)
            try:
                body = bks._read(process, kmsg + 0x60, body_size) if body_size else b""
            except Exception:
                body = b""
            reply_match = _ipc_object_key(destination) == KERNEL_TARGET.get("reply_key")
            post_result = {
                "label": "IPC_WRAPPER_POST_MIGRATION_RESULT_SEND_CALL",
                "request": "0x%x" % KERNEL_TARGET["request"],
                "sender_tpidr": "0x%x" % owner,
                "sender_thread": "0x%x" % frame.GetThread().GetThreadID(),
                "sender_pid": process.GetProcessID(),
                "sender_progname": bks._progname(process),
                "receiver_handoff": post_mr_context,
                "seconds_after_migration_result_receive": time.time() -
                    float(post_mr_context.get("time", time.time())),
                "kmsg": "0x%x" % kmsg,
                "header": header,
                "destination_object": "0x%x" % destination,
                "destination_object_key": "0x%x" % _ipc_object_key(destination),
                "pinned_reply_object": "0x%x" %
                    KERNEL_TARGET.get("reply_object", 0),
                "destination_equals_pinned_reply": bool(reply_match),
                "local_object": "0x%x" % local,
                "local_object_key": "0x%x" % _ipc_object_key(local),
                "body_hex": body.hex(),
                "body_contains_migrationResult": b"migrationResult" in body,
                "site_semantics": "send-helper call after exact migrationResult receive in the same wrapper TPIDR; send attempt only, kernel return/handoff and DM sync return are separate",
                "registers": _registers(
                    frame, tuple("x%d" % i for i in range(9)) +
                    tuple("x%d" % i for i in range(19, 26)) + ("sp", "pc")),
            }
            _event(frame, "IPC_WRAPPER_POST_MIGRATION_RESULT_SEND_CALL", post_result)
            bks._write_event(
                "progress.IPC_WRAPPER_POST_MIGRATION_RESULT_SEND_CALL.json",
                post_result)
            # Retain each same-receiver send as a concrete destination
            # candidate. The send is only associated with the preceding
            # migrationResult receive by this TPIDR and ordering; a later
            # ith_kmsg object/header/body match names a receiver for that
            # candidate without inventing a transaction ID.
            if (_ipc_object_key(destination) and header.get("message_id") is not None
                    and size >= 32):
                post_mr_candidates = KERNEL_TARGET.setdefault(
                    "post_migration_result_send_candidates", [])
                outbound_candidate = {
                    "candidate_index": len(post_mr_candidates) + 1,
                    "time": time.time(),
                    "seconds_after_migration_result_receive":
                        post_result["seconds_after_migration_result_receive"],
                    "sender_tpidr": "0x%x" % owner,
                    "sender_pid": process.GetProcessID(),
                "sender_progname": bks._progname(process),
                "same_process_as_migration_result_receiver": True,
                "post_migration_result_send_window_seconds":
                    POST_MIGRATION_RESULT_SEND_WINDOW,
                    "kmsg": "0x%x" % kmsg,
                    "destination_object": "0x%x" % destination,
                    "destination_object_key": "0x%x" %
                        _ipc_object_key(destination),
                    "original_reply_object": "0x%x" %
                        KERNEL_TARGET.get("reply_object", 0),
                    "destination_equals_original_reply": bool(reply_match),
                    "local_object": "0x%x" % local,
                    "message_id": header.get("message_id"),
                    "size": size,
                    "body_hex": body.hex(),
                    "basis": "post-exact-migrationResult receive send-helper candidate on same receiver TPIDR; transaction identity not established",
                }
                post_mr_candidates.append(outbound_candidate)
                bks._write_event(
                    "progress.IPC_POST_MIGRATION_RESULT_SEND_CANDIDATE.%03d.json" %
                    outbound_candidate["candidate_index"], {
                        "label": "IPC_POST_MIGRATION_RESULT_SEND_CANDIDATE",
                        "request": "0x%x" % KERNEL_TARGET["request"],
                        "candidate": outbound_candidate,
                    })
            if reply_match:
                bks._write_event(
                    "success.IPC_WRAPPER_POST_MIGRATION_RESULT_SEND_TO_PIN.json",
                    post_result)
        elif KERNEL_TARGET and owner == KERNEL_TARGET.get(
                "wrapper_callback_owner"):
            # After the matched wrapper callback calls xpc_connection_send_message,
            # this call-site sample reads the actual post-copyin kmsg pointer
            # passed to the send helper. Keep the join deliberately narrow:
            # same wrapper TPIDR plus the callback's connection/message/time;
            # do not substitute the DM request's object candidates for this
            # kmsg's fields.
            kmsg = bks._reg(frame, "x0")
            try:
                header = _kernel_header(process, kmsg)
            except Exception as error:
                header = {"header_error": type(error).__name__ + ": " + str(error)}
            size = int(header.get("size", 0) or 0)
            body_size = min(max(size - 32, 0), 0x800)
            try:
                body = (bks._read(process, kmsg + 0x60, body_size)
                        if body_size else b"")
            except Exception:
                body = b""
            destination = int(header.get("remote_field_u64", "0"), 0)
            local = int(header.get("local_field_u64", "0"), 0)
            sample = _event(frame, "IPC_WRAPPER_CALLBACK_SEND_KMSG", {
                "target_identifier": SYSTEMAPP_ID,
                "target_request": "0x%x" % KERNEL_TARGET["request"],
                "wrapper_thread_tpidr": "0x%x" % owner,
                "wrapper_connection": "0x%x" %
                    KERNEL_TARGET.get("wrapper_callback_connection", 0),
                "wrapper_xpc_message": "0x%x" %
                    KERNEL_TARGET.get("wrapper_callback_message", 0),
                "seconds_after_xpc_send": time.time() - KERNEL_TARGET.get(
                    "wrapper_callback_send_time", time.time()),
                "send_task_pid": process.GetProcessID(),
                "send_progname": bks._progname(process),
                "same_thread_as_matched_wrapper_xpc_send": True,
                "kmsg_pointer_from_send_helper_x0": "0x%x" % kmsg,
                "header": header,
                "destination_object": "0x%x" % destination,
                "destination_object_key": "0x%x" %
                    _ipc_object_key(destination),
                "reply_candidate_match": _ipc_object_key(destination) ==
                    KERNEL_TARGET["reply_key"],
                "local_object": "0x%x" % local,
                "local_object_key": "0x%x" % _ipc_object_key(local),
                "body_hex_prefix": body[:0x400].hex(),
                "systemapp_identifier_in_body":
                    SYSTEMAPP_ID.encode("ascii") in body,
                "mergebuddy_name_in_body":
                    b"MergeBuddyProvisioningResponse.migrator" in body or
                    "MergeBuddyProvisioningResponse.migrator".encode(
                        "utf-16le") in body,
                "registers": _registers(
                    frame, tuple("x%d" % i for i in range(9)) +
                    tuple("x%d" % i for i in range(19, 26)) +
                    ("sp", "pc")),
                "interpretation": "actual kernel kmsg candidate at the send-helper call; destination is usable only if the header is valid and the callback-thread/transport join remains consistent",
            })
            # Keep this boot's callback-side destination candidate for a
            # receiver-side object-equality join. The XPC-message-to-kmsg
            # relation remains a candidate (same connection/thread, short
            # interval, and payload shape); a matching ith_kmsg proves the
            # receiver took a message addressed to this ipc_object, but does
            # not by itself prove a transaction-level XPC identity.
            callback_delta = float(sample.get("seconds_after_xpc_send", 0) or 0)
            body_has_cpx_envelope = b"CPX@" in body
            body_has_pid_and_msgid = b"pid" in body and b"msgID" in body
            if (0 <= callback_delta <= 5.0 and body_has_cpx_envelope and
                    body_has_pid_and_msgid and _ipc_object_key(destination)):
                candidates = KERNEL_TARGET.setdefault(
                    "wrapper_outbound_dest_candidates", [])
                outbound_candidate = {
                    "label": "IPC_WRAPPER_OUTBOUND_DEST_CANDIDATE",
                    "time": time.time(),
                    "candidate_index": len(candidates) + 1,
                    "destination_object": "0x%x" % destination,
                    "destination_object_key": "0x%x" %
                        _ipc_object_key(destination),
                    "local_object": "0x%x" % local,
                    "local_object_key": "0x%x" % _ipc_object_key(local),
                    "kmsg": "0x%x" % kmsg,
                    "message_id": header.get("message_id"),
                    "size": header.get("size"),
                    "body_hex": body.hex(),
                    "callback_delta_seconds": callback_delta,
                    "wrapper_tpidr": "0x%x" % owner,
                    "wrapper_connection": sample.get("wrapper_connection"),
                    "wrapper_xpc_message": sample.get("wrapper_xpc_message"),
                    "xpc_to_kmsg_identity": "candidate only; thread/time/payload shape, no transaction id",
                }
                candidates.append(outbound_candidate)
                bks._write_event(
                    "progress.IPC_WRAPPER_OUTBOUND_DEST_CANDIDATE.json", {
                        "target_identifier": SYSTEMAPP_ID,
                        "target_request": "0x%x" % KERNEL_TARGET["request"],
                        "candidate": outbound_candidate,
                        "original_systemapp_reply_object": "0x%x" %
                            KERNEL_TARGET["reply_object"],
                        "destination_is_original_reply":
                            outbound_candidate["destination_object_key"] ==
                            KERNEL_TARGET["reply_key"],
                    })
            bks._write_event("progress.IPC_WRAPPER_CALLBACK_SEND_KMSG.json",
                             sample)
        return False

    if label in {"KERNEL_MACH2_MQUEUE_RECEIVE_CALL",
                 "KERNEL_MACH2_MQUEUE_RECEIVE_RETURN",
                 "KERNEL_MACH2_RECEIVE_RESULTS_RETURN"}:
        if owner == KERNEL_SYNC_OWNER:
            receive_object = bks._reg(frame, "x22")
            queue = bks._reg(frame, "x0") if label == \
                "KERNEL_MACH2_MQUEUE_RECEIVE_CALL" else receive_object + 0x10
            # Independent fallback for the reply right: at this exact
            # same-owner synchronous receive, x22 is the resolved receive
            # ipc_object and x0 is its waitq. This is not send-side
            # destination resolution and does not prove a peer reply.
            receive_key = _ipc_object_key(receive_object)
            sync_call = SYNC_CALLS.get(owner, {})
            if (label == "KERNEL_MACH2_MQUEUE_RECEIVE_CALL" and
                    not KERNEL_TARGET and
                    sync_call.get("identifier") == SYSTEMAPP_ID and
                    receive_key and queue == receive_object + 0x10):
                KERNEL_TARGET = {
                    "owner": owner, "send_owner": owner,
                    "request": sync_call["request"],
                    "identifier": SYSTEMAPP_ID,
                    "remote_object": 0, "reply_object": receive_object,
                    "remote_key": 0, "reply_key": receive_key,
                    "header": None,
                    "identity_level": "matched-sync receive object plus its waitq",
                    "reply_object_source": "mach_msg2 ipc_mqueue_receive entry",
                }
                resolved_reply = _event(
                    frame, "IPC_RESOLVED_SYSTEMAPP_REPLY_OBJECT", {
                        "identifier": SYSTEMAPP_ID,
                        "request": "0x%x" % sync_call["request"],
                        "owner_tpidr": "0x%x" % owner,
                        "receive_object_x22": "0x%x" % receive_object,
                        "reply_object_key": "0x%x" % receive_key,
                        "waitq_x0": "0x%x" % queue,
                        "waitq_is_object_plus_0x10": True,
                        "source": "matched sync owner's receive call",
                        "destination_object_resolved": False,
                        "interpretation": "reply ipc_object resolved at receive path; request destination remains unpaired",
                    })
                bks._write_event(
                    "success.IPC_RESOLVED_SYSTEMAPP_REPLY_OBJECT.json",
                    resolved_reply)
            queue_match = None
            if KERNEL_TARGET:
                queue_key = _ipc_object_key(queue)
                queue_match = {
                    "destination_waitq_match":
                        queue_key == KERNEL_TARGET["remote_key"] + 0x10,
                    "reply_waitq_match":
                        queue_key == KERNEL_TARGET["reply_key"] + 0x10,
                    "waitq_key": "0x%x" % queue_key,
                }
            thread = bks._reg(frame, "x19")
            try:
                ith_kmsg = _kernel_u64(process, thread + 0x3b0)
            except Exception:
                ith_kmsg = None
            _event(frame, label.replace("KERNEL_", "IPC_SAME_OWNER_"), {
                "identifier": SYSTEMAPP_ID,
                "target_request": "0x%x" % (KERNEL_TARGET["request"]
                                              if KERNEL_TARGET else 0),
                "owner_tpidr": "0x%x" % owner,
                "thread_x19": "0x%x" % thread,
                "receive_object_x22": "0x%x" % receive_object,
                "receive_waitq_x0_or_object_plus_0x10": "0x%x" % queue,
                "target_waitq_comparison": queue_match,
                "result_x0": "0x%x" % bks._reg(frame, "x0"),
                "ith_kmsg_slot_before_later_trap_processing":
                    ("0x%x" % ith_kmsg if ith_kmsg is not None else None),
                "registers": _registers(
                    frame, tuple("x%d" % i for i in range(9)) +
                    ("x19", "x20", "x21", "x22", "x23", "x30", "sp", "pc")),
                "site_semantics": {
                    "KERNEL_MACH2_MQUEUE_RECEIVE_CALL":
                        "mach_msg2_trap calls ipc_mqueue_receive at +0x4160",
                    "KERNEL_MACH2_MQUEUE_RECEIVE_RETURN":
                        "return boundary after ipc_mqueue_receive at +0x4164",
                    "KERNEL_RECEIVE_RESULTS_RETURN":
                        "return boundary after mach_msg_receive_results call at +0x417c",
                }[label],
            })
            if (label == "KERNEL_MACH2_MQUEUE_RECEIVE_CALL" and
                    KERNEL_TARGET and
                    _ipc_object_key(receive_object) ==
                    KERNEL_TARGET["reply_key"]):
                reply_receive = _event(
                    frame, "IPC_SYSTEMAPP_REPLY_RECEIVE_CALL", {
                        "identifier": SYSTEMAPP_ID,
                        "request": "0x%x" % KERNEL_TARGET["request"],
                        "send_owner_tpidr": "0x%x" % KERNEL_TARGET["owner"],
                        "receive_owner_tpidr": "0x%x" % owner,
                        "receive_object": "0x%x" % receive_object,
                        "receive_object_key": "0x%x" %
                            _ipc_object_key(receive_object),
                        "same_owner": owner == KERNEL_TARGET["owner"],
                        "stage": "reply-object receive call; no dequeue/result yet",
                    })
                bks._write_event(
                    "progress.IPC_SYSTEMAPP_REPLY_RECEIVE_CALL.json",
                    reply_receive)
        return False

    if label in {"KERNEL_ITH_KMSG_QUEUE_STORE",
                 "KERNEL_ITH_KMSG_DIRECT_STORE_A",
                 "KERNEL_ITH_KMSG_DIRECT_STORE_B"}:
        # A callback candidate may have been emitted before the matched
        # SystemApp request initialized KERNEL_TARGET; it still needs the
        # same exact destination/message/body test at ith_kmsg.
        if not KERNEL_TARGET:
            return False
        site_stats = PIN_ITH_KMSG_STATS.setdefault(label, {
            "hits": 0, "candidate_headers": 0, "reply_matches": 0})
        site_stats["hits"] += 1
        thread_reg = {"KERNEL_ITH_KMSG_QUEUE_STORE": "x19",
                      "KERNEL_ITH_KMSG_DIRECT_STORE_A": "x24",
                      "KERNEL_ITH_KMSG_DIRECT_STORE_B": "x23"}[label]
        thread = bks._reg(frame, thread_reg)
        candidate = bks._reg(frame, "x20") if label == \
            "KERNEL_ITH_KMSG_QUEUE_STORE" else bks._reg(frame, "x17")
        # At the queue-removal store, x17 is PAC-signed from x20 immediately
        # before the store. Direct-delivery stores similarly PAC-sign x23/x21
        # into x17. Prefer the unsigned source where disassembly exposes it.
        if label == "KERNEL_ITH_KMSG_DIRECT_STORE_A":
            candidate = bks._reg(frame, "x23")
        elif label == "KERNEL_ITH_KMSG_DIRECT_STORE_B":
            candidate = bks._reg(frame, "x21")
        headers = _dequeue_header_candidates(process, candidate)
        site_stats["candidate_headers"] += len(headers)
        matched_headers = []
        endpoint_headers = []
        outbound_dest_headers = []
        post_mr_peer_headers = []
        migration_result_dest_headers = []
        for item in headers:
            decoded = item.get("decoded", {})
            try:
                message_id = int(decoded.get("message_id"))
                remote_key = _ipc_object_key(
                    int(decoded.get("remote_field_u64", "0"), 0))
                local_key = _ipc_object_key(
                    int(decoded.get("local_field_u64", "0"), 0))
            except (TypeError, ValueError):
                continue
            expected_header = KERNEL_TARGET.get("header") or {}
            expected_size = expected_header.get("size")
            if (message_id == 0x40000000 and
                    remote_key == KERNEL_TARGET["remote_key"] and
                    int(decoded.get("size", 0) or 0) == int(expected_size or 0)):
                matched_headers.append({
                    "candidate": item.get("candidate"),
                    "message_id": "0x%x" % message_id,
                    "size": decoded.get("size"),
                    "size_match": True,
                    "destination_object_match": True,
                    "remote_object_key": "0x%x" % remote_key,
                    "local_object_key": "0x%x" % local_key,
                })
            if remote_key == KERNEL_TARGET["reply_key"]:
                site_stats["reply_matches"] += 1
                endpoint_headers.append({
                    "candidate": item.get("candidate"),
                    "message_id": "0x%x" % message_id,
                    "reply_object_match": True,
                    "remote_object_key": "0x%x" % remote_key,
                    "local_object_key": "0x%x" % local_key,
                })
            for outbound in KERNEL_TARGET.get(
                    "wrapper_outbound_dest_candidates", []):
                try:
                    outbound_key = int(
                        outbound.get("destination_object_key", "0"), 0)
                    outbound_id = int(outbound.get("message_id"))
                    outbound_size = int(outbound.get("size"))
                except (TypeError, ValueError):
                    continue
                if remote_key != outbound_key:
                    continue
                candidate_address = int(item.get("address", "0"), 0)
                body_size = min(max(int(decoded.get("size", 0)) - 32, 0),
                                0x800)
                try:
                    receiver_body = (bks._read(
                        process, candidate_address + 0x60, body_size)
                        if body_size else b"")
                except Exception:
                    receiver_body = b""
                outbound_body = bytes.fromhex(outbound.get("body_hex", ""))
                outbound_dest_headers.append({
                    "candidate": item.get("candidate"),
                    "candidate_address": item.get("address"),
                    "receiver_progname": bks._progname(process),
                    "receiver_pid": process.GetProcessID(),
                    "receiver_tpidr": "0x%x" % owner,
                    "receiver_thread": "0x%x" % thread,
                    "message_id": "0x%x" % message_id,
                    "receiver_size": decoded.get("size"),
                    "outbound_candidate_index": outbound.get("candidate_index"),
                    "outbound_candidate_kmsg": outbound.get("kmsg"),
                    "destination_object_match": True,
                    "outbound_message_id_match": message_id == outbound_id,
                    "outbound_size_match": int(decoded.get("size", 0)) ==
                        outbound_size,
                    "body_exact_match": bool(outbound_body) and
                        receiver_body == outbound_body,
                    "receiver_body_hex": receiver_body.hex(),
                    "outbound_body_hex": outbound.get("body_hex"),
                    "remote_object_key": "0x%x" % remote_key,
                    "local_object_key": "0x%x" % local_key,
                })
            # Join the wrapper's post-migrationResult send candidates to
            # receiver-side kmsgs by resolved destination object. Add the
            # header/body comparison and receiver identity so a destination
            # match alone is not mistaken for this exact message.
            for outbound in KERNEL_TARGET.get(
                    "post_migration_result_send_candidates", []):
                try:
                    outbound_key = int(
                        outbound.get("destination_object_key", "0"), 0)
                    outbound_id = int(outbound.get("message_id"))
                    outbound_size = int(outbound.get("size"))
                except (TypeError, ValueError):
                    continue
                if remote_key != outbound_key:
                    continue
                candidate_address = int(item.get("address", "0"), 0)
                body_size = min(max(int(decoded.get("size", 0)) - 32, 0),
                                0x2000)
                try:
                    receiver_body = (bks._read(
                        process, candidate_address + 0x60, body_size)
                        if body_size else b"")
                except Exception:
                    receiver_body = b""
                outbound_body = bytes.fromhex(outbound.get("body_hex", ""))
                post_mr_peer_headers.append({
                    "candidate": item.get("candidate"),
                    "candidate_address": item.get("address"),
                    "receiver_progname": bks._progname(process),
                    "receiver_pid": process.GetProcessID(),
                    "receiver_tpidr": "0x%x" % owner,
                    "receiver_thread": "0x%x" % thread,
                    "message_id": "0x%x" % message_id,
                    "receiver_size": decoded.get("size"),
                    "post_migration_result_candidate_index":
                        outbound.get("candidate_index"),
                    "sender_tpidr": outbound.get("sender_tpidr"),
                    "sender_progname": outbound.get("sender_progname"),
                    "destination_object_match": True,
                    "message_id_match": message_id == outbound_id,
                    "size_match": int(decoded.get("size", 0)) == outbound_size,
                    "body_exact_match": bool(outbound_body) and
                        receiver_body == outbound_body,
                    "receiver_body_hex": receiver_body.hex(),
                    "send_body_hex": outbound.get("body_hex"),
                    "original_reply_object": outbound.get(
                        "original_reply_object"),
                    "destination_equals_original_reply": outbound.get(
                        "destination_equals_original_reply"),
                    "interpretation": "receiver took a kmsg addressed to the post-migrationResult destination candidate; exact header/body equality strengthens the send/receive join, but no transaction ID is present",
                })
            for migration_candidate in KERNEL_TARGET.get(
                    "migration_result_dest_candidates", []):
                try:
                    migration_key = int(
                        migration_candidate.get("destination_object_key", "0"), 0)
                    migration_id = int(migration_candidate.get("message_id"))
                    migration_size = int(migration_candidate.get("size"))
                except (TypeError, ValueError):
                    continue
                if remote_key != migration_key:
                    continue
                candidate_address = int(item.get("address", "0"), 0)
                body_size = min(max(int(decoded.get("size", 0)) - 32, 0),
                                0x2000)
                try:
                    receiver_body = (bks._read(
                        process, candidate_address + 0x60, body_size)
                        if body_size else b"")
                except Exception:
                    receiver_body = b""
                migration_body = bytes.fromhex(
                    migration_candidate.get("body_hex", ""))
                migration_result_dest_headers.append({
                    "candidate": item.get("candidate"),
                    "candidate_address": item.get("address"),
                    "receiver_progname": bks._progname(process),
                    "receiver_pid": process.GetProcessID(),
                    "receiver_tpidr": "0x%x" % owner,
                    "receiver_thread": "0x%x" % thread,
                    "message_id": "0x%x" % message_id,
                    "receiver_size": decoded.get("size"),
                    "migration_result_candidate_id":
                        migration_candidate.get("candidate_id"),
                    "migration_result_sender_tpidr":
                        migration_candidate.get("sender_tpidr"),
                    "migration_result_sender_progname":
                        migration_candidate.get("sender_progname"),
                    "migration_result_xpc_candidate":
                        migration_candidate.get("xpc_candidate"),
                    "destination_object_match": True,
                    "message_id_match": message_id == migration_id,
                    "size_match": int(decoded.get("size", 0)) == migration_size,
                    "body_exact_match": bool(migration_body) and
                        receiver_body == migration_body,
                    "receiver_body_hex": receiver_body.hex(),
                    "send_body_hex": migration_candidate.get("body_hex"),
                    "interpretation": "receiver kmsg has the same resolved destination object; exact header/body fields determine whether this is the recorded migrationResult send candidate",
                })
        if (not matched_headers and not endpoint_headers and
                not outbound_dest_headers and not post_mr_peer_headers and
                not migration_result_dest_headers):
            return False
        sample = _event(frame, "IPC_ITH_KMSG_HANDOFF_CANDIDATE", {
            "target_identifier": SYSTEMAPP_ID,
            "target_request": "0x%x" % KERNEL_TARGET["request"],
            "send_owner_tpidr": "0x%x" % KERNEL_TARGET["owner"],
            "current_tpidr": "0x%x" % owner,
            "task_pid": process.GetProcessID(),
            "progname": bks._progname(process),
            "site": label,
            "site_static_pc": "0x%x" % (bks._reg(frame, "pc") - KERNEL_SLIDE),
            "receiver_thread_register": thread_reg,
            "receiver_thread": "0x%x" % thread,
            "queue_waitq": None,
            "queue_waitq_object_match": None,
            "queue_identity_note": (
                "not inferred from x21 at this store; match the dequeued "
                "kmsg header's remote object instead"),
            "ith_kmsg_source_candidate": "0x%x" % candidate,
            "ith_kmsg_source_candidates": headers,
            "matched_systemapp_request_headers": matched_headers,
            "matched_systemapp_reply_object_headers": endpoint_headers,
            "matched_wrapper_outbound_destination_headers":
                outbound_dest_headers,
            "matched_post_migration_result_peer_headers":
                post_mr_peer_headers,
            "matched_migration_result_destination_headers":
                migration_result_dest_headers,
            "matched_systemapp_request_message": bool(matched_headers),
            "matched_systemapp_reply_object_message": bool(endpoint_headers),
            "matched_wrapper_outbound_destination_message":
                bool(outbound_dest_headers),
            "matched_post_migration_result_peer_message":
                bool(post_mr_peer_headers),
            "registers": _registers(
                frame, tuple("x%d" % i for i in range(31)) +
                ("sp", "pc", "tpidr_el1")),
            "site_semantics": (
                "ipc_mqueue_receive queue unlink followed by PAC store to "
                "receiver thread+0x3b0" if label == "KERNEL_ITH_KMSG_QUEUE_STORE"
                else "alternate ith_kmsg store; direct-delivery candidate, "
                     "requires header/object correlation"),
        })
        bks._write_event("progress.IPC_ITH_KMSG_HANDOFF_CANDIDATE.json", sample)
        for peer in post_mr_peer_headers:
            peer_event = {
                "label": "IPC_POST_MIGRATION_RESULT_OUTBOUND_PEER",
                "time": time.time(),
                "request": "0x%x" % KERNEL_TARGET["request"],
                "pinned_reply_object": "0x%x" %
                    KERNEL_TARGET.get("reply_object", 0),
                "handoff_site": label,
                "peer": peer,
                "matched_systemapp_request_owner_tpidr": "0x%x" %
                    KERNEL_TARGET.get("owner", 0),
            }
            bks._write_event(
                "progress.IPC_POST_MIGRATION_RESULT_OUTBOUND_PEER.%03d.json" %
                int(peer.get("post_migration_result_candidate_index", 0)),
                peer_event)
            print("IPC_POST_MIGRATION_RESULT_OUTBOUND_PEER " +
                  json.dumps(peer_event, sort_keys=True), flush=True)
        if endpoint_headers:
            pin_handoff = {
                "label": "IPC_PINNED_REPLY_ITH_KMSG_HANDOFF",
                "request": "0x%x" % KERNEL_TARGET["request"],
                "pinned_reply_object": "0x%x" %
                    KERNEL_TARGET.get("reply_object", 0),
                "pinned_reply_object_key": "0x%x" %
                    KERNEL_TARGET.get("reply_key", 0),
                "handoff": sample,
                "interpretation": "kmsg receive/store header destination object equals the immutable SystemApp reply ipc_object; same-owner sync return remains separate",
            }
            bks._write_event(
                "success.IPC_PINNED_REPLY_ITH_KMSG_HANDOFF.json", pin_handoff)
            print("IPC_PINNED_REPLY_ITH_KMSG_HANDOFF " +
                  json.dumps(pin_handoff, sort_keys=True), flush=True)
        # A full-body, ID, size, and destination-object match identifies an
        # actual migrationResult receive in migrationpluginwrapper. Seed a
        # short, same-receiver-thread send watch so the next Mach message can
        # be resolved without assuming it uses the same XPC connection.
        exact_wrapper_migration_result = next((item for item in
            migration_result_dest_headers
            if item.get("body_exact_match") and
            item.get("message_id_match") and item.get("size_match") and
            bks._progname(process) == "com.apple.migrationpluginwrapper"),
            None)
        if exact_wrapper_migration_result:
            receiver_tpidr = int(owner)
            KERNEL_TARGET["wrapper_migration_result_receiver_tpidr"] = \
                receiver_tpidr
            receiver_context = {
                "time": time.time(), "receiver_tpidr": "0x%x" % owner,
                "receiver_thread": "0x%x" % thread,
                "receiver_pid": process.GetProcessID(),
                "receiver_progname": bks._progname(process),
                "request": "0x%x" % KERNEL_TARGET["request"],
                "pinned_reply_object": "0x%x" %
                    KERNEL_TARGET.get("reply_object", 0),
                "migration_result_match": exact_wrapper_migration_result,
                "handoff_site": label,
            }
            KERNEL_TARGET["wrapper_migration_result_handoff"] = receiver_context
            contexts = KERNEL_TARGET.setdefault(
                "wrapper_migration_result_receiver_contexts", [])
            contexts.append(receiver_context)
            KERNEL_SEND_HELPER_OWNERS.add(receiver_tpidr)
            _refresh_kernel_send_helper_condition(
                frame.GetThread().GetProcess().GetTarget().GetDebugger())
            bks._write_event(
                "success.IPC_WRAPPER_MIGRATION_RESULT_RECEIVER.json",
                receiver_context)
            print("IPC_WRAPPER_MIGRATION_RESULT_RECEIVER " + json.dumps(
                receiver_context,
                sort_keys=True), flush=True)
        for result_match in migration_result_dest_headers:
            bks._write_event(
                "success.IPC_MIGRATION_RESULT_DEST_ITH_KMSG.%03d.json" %
                int(result_match.get("migration_result_candidate_id", 0)), {
                    "label": "IPC_MIGRATION_RESULT_DEST_ITH_KMSG",
                    "target_request": "0x%x" % KERNEL_TARGET["request"],
                    "site": label,
                    "match": result_match,
                    "full_handoff_sample": sample,
                })
        if matched_headers:
            bks._write_event("success.IPC_SYSTEMAPP_MATCHED_DEQUEUE.json", sample)
            if (bks._progname(process) == "com.apple.migrationpluginwrapper" and
                    KERNEL_TARGET.get("reply_key") and
                    KERNEL_TARGET.get("remote_key")):
                KERNEL_TARGET["request_delivery_confirmed"] = True
                resolved = _event(
                    frame, "IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS", {
                        "identifier": SYSTEMAPP_ID,
                        "request": "0x%x" % KERNEL_TARGET["request"],
                        "send_owner_tpidr": "0x%x" %
                            KERNEL_TARGET.get("send_owner", KERNEL_TARGET["owner"]),
                        "sync_owner_tpidr": "0x%x" % KERNEL_TARGET["owner"],
                        "resolved_destination_candidate": "0x%x" %
                            KERNEL_TARGET["remote_object"],
                        "resolved_reply_candidate": "0x%x" %
                            KERNEL_TARGET["reply_object"],
                        "destination_object_key": "0x%x" %
                            KERNEL_TARGET["remote_key"],
                        "reply_object_key": "0x%x" % KERNEL_TARGET["reply_key"],
                        "identity_level": KERNEL_TARGET.get("identity_level"),
                        "delivery_receiver": bks._progname(process),
                        "delivery_pid": process.GetProcessID(),
                        "matched_kmsg_header": matched_headers[0],
                        "send_helper_candidate_header": KERNEL_TARGET.get("header"),
                        "destination_object_equal_at_receiver": True,
                        "interpretation": "SystemApp send objects resolved from copied-in kmsg and confirmed by same destination object + msgID/size at wrapper ith_kmsg; reply object is now eligible for equality checks, but this is not a terminal reply or migration completion",
                    })
                bks._write_event(
                    "success.IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS.json",
                    resolved)
        if endpoint_headers:
            bks._write_event(
                "success.IPC_SYSTEMAPP_REPLY_OBJECT_DEQUEUE.json", sample)
        if outbound_dest_headers:
            bks._write_event(
                "success.IPC_WRAPPER_OUTBOUND_DEST_ITH_KMSG.json", sample)
        return False

    if label == "KERNEL_SECONDARY_COPYIN_RESULT":
        if not KERNEL_TARGET:
            return False
        # The +0x40bc breakpoint fires before csel writes x23. Disassembly
        # proves the preceding helper at +0x40b0 is a secondary object
        # copyin, so x8 is the translated object candidate, not a kmsg.
        status = bks._reg(frame, "x0") & 0xffffffff
        output_candidate = bks._reg(frame, "x8")
        prior_x23 = bks._reg(frame, "x23")
        object_ptr = bks._reg(frame, "x22")
        object_key = _ipc_object_key(object_ptr)
        output_key = _ipc_object_key(output_candidate)
        pending = KERNEL_PENDING_REPLY_RECEIVE.pop(owner, None)
        if owner == KERNEL_TARGET["owner"]:
            sample = _event(frame, "IPC_SAME_OWNER_SECONDARY_COPYIN_RESULT", {
                "target_identifier": SYSTEMAPP_ID,
                "target_request": "0x%x" % KERNEL_TARGET["request"],
                "target_send_owner_tpidr": "0x%x" % KERNEL_TARGET["owner"],
                "owner_tpidr": "0x%x" % owner,
                "task_pid": process.GetProcessID(),
                "progname": bks._progname(process),
                "prior_receive_right_resolve": pending,
                "copyin_status_x0": "0x%x" % status,
                "prior_receive_object_x22": "0x%x" % object_ptr,
                "prior_receive_object_x22_key": "0x%x" % object_key,
                "x22_matches_destination":
                    object_key == KERNEL_TARGET["remote_key"],
                "x22_matches_reply": object_key == KERNEL_TARGET["reply_key"],
                "secondary_copyin_input_x24": "0x%x" % bks._reg(frame, "x24"),
                "secondary_copyin_output_x8": "0x%x" % output_candidate,
                "secondary_copyin_output_x8_key": "0x%x" % output_key,
                "x8_matches_destination":
                    output_key == KERNEL_TARGET["remote_key"],
                "x8_matches_reply": output_key == KERNEL_TARGET["reply_key"],
                "x23_before_csel": "0x%x" % prior_x23,
                "site_semantics": "secondary object-copyin result; not message dequeue",
                "all_gprs": _registers(
                    frame, tuple("x%d" % i for i in range(31)) +
                    ("sp", "pc", "tpidr_el1")),
            })
            bks._write_event("success.IPC_SAME_OWNER_SECONDARY_COPYIN_RESULT.json",
                             sample)
        return False

    if label == "KERNEL_RECEIVE_OBJECT_LOADED":
        if not KERNEL_TARGET:
            return False
        object_ptr = bks._reg(frame, "x22")
        object_key = _ipc_object_key(object_ptr)
        side = "destination" if object_key == KERNEL_TARGET["remote_key"] else (
            "reply" if object_key == KERNEL_TARGET["reply_key"] else None)
        if side:
            received = _event(frame, "IPC_SHARED_RIGHT_RECEIVE", {
                "side": side,
                "target_identifier": SYSTEMAPP_ID,
                "target_request": "0x%x" % KERNEL_TARGET["request"],
                "target_send_owner_tpidr": "0x%x" % KERNEL_SYNC_OWNER,
                "receive_owner_tpidr": "0x%x" % owner,
                "receive_task_pid": process.GetProcessID(),
                "receive_progname": bks._progname(process),
                "local_name": "0x%x" % bks._reg(frame, "x21"),
                "resolved_object": "0x%x" % object_ptr,
                "resolved_object_key": "0x%x" % object_key,
                "same_shared_object": True,
            })
            bks._write_event("progress.IPC_SHARED_RIGHT_RECEIVE.json", received)
            if side == "reply" and owner == KERNEL_TARGET["owner"]:
                KERNEL_PENDING_REPLY_RECEIVE[owner] = {
                    "resolved_object": "0x%x" % object_ptr,
                    "resolved_object_key": "0x%x" % object_key,
                    "task_local_name": "0x%x" % bks._reg(frame, "x21"),
                    "resolve_pc": "0x%x" % bks._reg(frame, "pc"),
                }
        return False

    if label in {
            "KERNEL_MACH2_AA89F50_RETURN",
            "KERNEL_MACH2_AA8DD58_RETURN"}:
        # The exact SystemApp call is the only unfiltered send sample. Once
        # its rights are resolved, a LLDB expression limits later callbacks
        # to replies addressed to this request's task-local reply right.
        if owner == KERNEL_SYNC_OWNER:
            _event(frame, label, {
                "identifier": SYSTEMAPP_ID,
                "owner_match": True,
                "tpidr_el1": "0x%x" % owner,
                "kernel_registers": _registers(frame),
                "static_pc": "0x%x" % (bks._reg(frame, "pc") - KERNEL_SLIDE),
                "kernel_slide": "0x%x" % KERNEL_SLIDE,
            })
        user_send_for_owner = MACH_USER_SENDS.get(owner, {})
        active = SYSTEMAPP_ACTIVE_CALL or {}
        active_thread_send = bool(
            user_send_for_owner.get("request") == active.get("request") and
            user_send_for_owner.get("identifier") == SYSTEMAPP_ID and
            user_send_for_owner.get("active_request_same_process"))
        sync = SYNC_CALLS.get(owner)
        if not sync and active_thread_send:
            sync = active
        if sync and sync.get("identifier") == SYSTEMAPP_ID:
            if label == "KERNEL_MACH2_AA89F50_RETURN":
                # This is only the return from the first helper call. Record
                # its stack out-slot without assuming it already contains a
                # readable kmsg; the next helper return is the better sample.
                sp = frame.GetSP()
                slot = sp + 0x58
                _event(frame, "MACH2_AA89F50_RETURN_CANDIDATE", {
                    "identifier": SYSTEMAPP_ID,
                    "request": "0x%x" % sync["request"],
                    "sp": "0x%x" % sp,
                    "out_slot": "0x%x" % slot,
                    "out_slot_raw": bks._read(process, slot, 8).hex(),
                    "x5_candidate": "0x%x" % bks._reg(frame, "x5"),
                    "kernel_registers": _registers(frame),
                })
                return False

            # At mach_msg2_trap+0x41c, x21 is the kmsg passed as x0 to
            # helper aa8dd58. It is callee-saved across that helper; x23 is a
            # packed/tagged value (e.g. 0x8004070500100231), not a pointer.
            kmsg = bks._reg(frame, "x21")
            header = _kernel_header(process, kmsg)
            body_size = min(max(int(header.get("size", 0)) - 32, 0), 0x400)
            body = (bks._read(process, kmsg + 0x60, body_size)
                    if kmsg and body_size else b"")
            identity_in_body = SYSTEMAPP_ID.encode("ascii") in body
            user_send = user_send_for_owner
            user_send_matches = bool(
                user_send.get("request") == sync["request"] and
                user_send.get("identifier") == SYSTEMAPP_ID and
                user_send.get("message_id") == 0x40000000 and
                header.get("message_id") == user_send.get("message_id") and
                (user_send.get("contains_plugin_identifier") or
                 owner == KERNEL_SYNC_OWNER) and
                header.get("size") in (508, 516))
            same_process_user_send_candidate = bool(
                user_send.get("request") == sync["request"] and
                user_send.get("identifier") == SYSTEMAPP_ID and
                user_send.get("active_request_same_process") and
                user_send.get("message_id") == 0x40000000 and
                header.get("message_id") == 0x40000000 and
                header.get("size") in (508, 516))
            # Some launches expose a small outer mach_msg2 header to the user
            # trap hook while the kernel helper receives the expanded 516-byte
            # CPX kmsg (OOL body). Keep the strict user-header join above as
            # the resolved path. This alternate is only a candidate: same
            # TPIDR/request, pluginIdentifier decoded at CLIENT_SYNC_CALL,
            # and exact SystemApp wire ID/size on the kernel kmsg. Do not call
            # it a resolved transport identity until the receiver body/object
            # comparison supplies the next independent join.
            same_owner_expanded_candidate = bool(
                sync.get("identifier") == SYSTEMAPP_ID and
                sync.get("request_plugin_identifier") == SYSTEMAPP_ID and
                header.get("message_id") == 0x40000000 and
                header.get("size") == 516)
            send_identity_candidate = (user_send_matches or
                                       same_process_user_send_candidate or
                                       same_owner_expanded_candidate)
            _event(frame, "IPC_KMSG_COPYIN_SYSTEMAPP_CANDIDATE", {
                "identifier": SYSTEMAPP_ID, "request": "0x%x" % sync["request"],
                "header": header, "systemapp_id_in_kmsg_body": identity_in_body,
                "same_owner_user_send_match": user_send_matches,
                "user_send": user_send,
                "kmsg_body_prefix": body[:128].hex(),
                "kernel_registers": _registers(frame),
            })
            _event(frame, "IPC_SAME_OWNER_SEND_HELPER_RETURN", {
                "identifier": SYSTEMAPP_ID,
                "request": "0x%x" % sync["request"],
                "owner_tpidr": "0x%x" % owner,
                "mach_send_helper_result_x0": "0x%x" % bks._reg(frame, "x0"),
                "kmsg_x21": "0x%x" % kmsg,
                "kmsg_header": header,
                "same_owner_user_send_match": user_send_matches,
                "return_static_pc": "0x%x" % MACH2_SEND_HELPER_RETURN,
                "site_semantics": "return from bootkc helper aa8dd58; status only, not remote dequeue",
            })
            if label == "KERNEL_MACH2_AA8DD58_RETURN" and \
                    send_identity_candidate:
                remote_obj = int(header.get("remote_field_u64", "0"), 0)
                local_obj = int(header.get("local_field_u64", "0"), 0)
                remote_key = _ipc_object_key(remote_obj)
                local_key = _ipc_object_key(local_obj)
                valid_object_fields = bool(remote_key and local_key)
                target_already_pinned = bool(
                    KERNEL_TARGET and
                    KERNEL_TARGET.get("request") == sync.get("request") and
                    KERNEL_TARGET.get("request_object_pair_pinned"))
                pin_this_send = bool(
                    valid_object_fields and user_send_matches and
                    owner == KERNEL_SYNC_OWNER and
                    not target_already_pinned)
                if pin_this_send:
                    KERNEL_TARGET = {"owner": KERNEL_SYNC_OWNER or owner,
                                     "send_owner": owner,
                                     "request": sync["request"],
                                     "identifier": SYSTEMAPP_ID,
                                     "remote_object": remote_obj,
                                     "reply_object": local_obj,
                                     "remote_key": remote_key,
                                     "reply_key": local_key,
                                     "header": header,
                                     "request_object_pair_pinned": True,
                                     "identity_level": (
                                         "same-thread-user-send-and-kmsg-match"
                                         if user_send_matches else
                                         "candidate-same-process-user-send-or-sync-request-plus-kernel-id-size; receiver handoff required")}
                if valid_object_fields:
                    identity_level = ((KERNEL_TARGET or {}).get("identity_level")
                                      if pin_this_send else
                                      "candidate-only; original reply pair retained")
                    label_out = ("IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS"
                                 if pin_this_send else
                                 "IPC_SYSTEMAPP_SEND_OBJECTS_CANDIDATE")
                    resolved = _event(frame, label_out, {
                        "identifier": SYSTEMAPP_ID,
                        "request": "0x%x" % sync["request"],
                        "owner_match": owner == KERNEL_SYNC_OWNER,
                        "send_owner_tpidr": "0x%x" % owner,
                        "original_sync_owner_tpidr": "0x%x" % KERNEL_SYNC_OWNER,
                        "identity_level": identity_level,
                        "task_pid": process.GetProcessID(),
                        "task_progname": bks._progname(process),
                        "task_local_destination_name": "0x%x" % MACH_DEST_PORT,
                        "task_local_reply_name": "0x%x" % MACH_REPLY_PORT,
                        "header": header,
                        "systemapp_id_in_same_owner_user_send": user_send_matches,
                        "kernel_inline_body_contains_plugin_identifier": identity_in_body,
                        "identity_join": ("same-thread user send + kernel kmsg header"
                                          if pin_this_send else
                                          "candidate only; pinned request object pair retained"),
                        "kmsg_body_prefix": body[:128].hex(),
                        "resolved_destination_candidate": "0x%x" % remote_obj,
                        "resolved_reply_candidate": "0x%x" % local_obj,
                        "original_pinned_reply_object_key": "0x%x" %
                            ((KERNEL_TARGET or {}).get("reply_key", 0)),
                        "candidate_fields_are_kernel_addresses": valid_object_fields,
                        "user_send_header_match": user_send_matches,
                        "user_send_candidate": user_send,
                        "kernel_registers": _registers(frame),
                    })
                    event_file = ("success.IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS.json"
                                  if pin_this_send else
                                  "progress.IPC_SYSTEMAPP_SEND_OBJECTS_CANDIDATE.json")
                    bks._write_event(event_file, resolved)
                # Receive/dequeue and copyin-return sites stay unconditioned
                # in LLDB. Their callbacks compare PAC-masked ipc_object keys
                # so peer tasks remain observable without expression/JIT
                # predicate failure or cross-process numeric-name matching.
                if not valid_object_fields:
                    _event(frame, "IPC_KERNEL_OBJECT_CANDIDATE_REJECTED", {
                        "reason": "header fields are not canonical kernel object addresses",
                        "destination_candidate": "0x%x" % remote_obj,
                        "reply_candidate": "0x%x" % local_obj,
                        "request": "0x%x" % sync["request"],
                    })
        elif label == "KERNEL_MACH2_AA8DD58_RETURN" and KERNEL_TARGET:
            # Global post-copyin sampling: callback-side comparison against
            # the captured reply ipc_object keeps peer tasks visible. This is
            # a send candidate at helper return, not proof of queue delivery;
            # receive/dequeue and same-owner sync return remain separate.
            try:
                kmsg = bks._reg(frame, "x21")
                header = _kernel_header(process, kmsg)
            except Exception:
                # This breakpoint also sees unrelated owners. A stale or
                # non-kmsg x21 must be a silent non-match, never a debugger
                # stop that truncates the live SystemApp reply window.
                return False
            destination = int(header.get("remote_field_u64", "0"), 0)
            wrapper_owner = KERNEL_TARGET.get("wrapper_callback_owner")
            if wrapper_owner is not None and owner == wrapper_owner and \
                    WRAPPER_THREAD_SEND_SAMPLES < 32:
                WRAPPER_THREAD_SEND_SAMPLES += 1
                size = int(header.get("size", 0) or 0)
                body_size = min(max(size - 32, 0), 0x800)
                try:
                    body = (bks._read(process, kmsg + 0x60, body_size)
                            if body_size else b"")
                except Exception:
                    body = b""
                _event(frame, "IPC_WRAPPER_CALLBACK_THREAD_SEND_CANDIDATE", {
                    "target_identifier": SYSTEMAPP_ID,
                    "target_request": "0x%x" % KERNEL_TARGET["request"],
                    "wrapper_thread_tpidr": "0x%x" % wrapper_owner,
                    "wrapper_xpc_connection": "0x%x" %
                        KERNEL_TARGET.get("wrapper_callback_connection", 0),
                    "wrapper_xpc_message": "0x%x" %
                        KERNEL_TARGET.get("wrapper_callback_message", 0),
                    "time_after_wrapper_xpc_send_seconds": time.time() -
                        KERNEL_TARGET.get("wrapper_callback_send_time", time.time()),
                    "send_task_pid": process.GetProcessID(),
                    "send_progname": bks._progname(process),
                    "same_thread_as_callback_xpc_send": True,
                    "header": header,
                    "destination_object": "0x%x" % destination,
                    "reply_object_match":
                        _ipc_object_key(destination) == KERNEL_TARGET["reply_key"],
                    "kmsg_body_prefix": body[:0x400].hex(),
                    "systemapp_id_in_kmsg_body":
                        SYSTEMAPP_ID.encode("ascii") in body,
                    "mergebuddy_name_in_kmsg_body":
                        b"MergeBuddyProvisioningResponse.migrator" in body or
                        "MergeBuddyProvisioningResponse.migrator".encode(
                            "utf-16le") in body,
                    "kernel_registers": _registers(frame),
                    "interpretation": "actual mach_msg2 kmsg destination from the callback thread; temporal/thread candidate, not XPC-message identity unless payload/stack also joins",
                })
            if _ipc_object_key(destination) == KERNEL_TARGET["reply_key"]:
                candidate = _event(frame, "IPC_REPLY_DEST_COPYIN_CANDIDATE", {
                    "side": "reply",
                    "stage": "mach_msg2 copyin helper return; delivery not established",
                    "target_identifier": SYSTEMAPP_ID,
                    "target_request": "0x%x" % KERNEL_TARGET["request"],
                    "send_owner_tpidr": "0x%x" % owner,
                    "send_task_pid": process.GetProcessID(),
                    "send_progname": bks._progname(process),
                    "resolved_destination": "0x%x" % destination,
                    "resolved_destination_key": "0x%x" %
                        _ipc_object_key(destination),
                    "same_shared_object": True,
                    "header": header,
                    "kernel_registers": _registers(frame),
                })
                bks._write_event(
                    "success.IPC_REPLY_DEST_COPYIN_CANDIDATE.json", candidate)
        return False

    # This PC is immediately after ipc_mqueue_copyin stored its resolved
    # ipc_object through the out slot; x22 is that object, x21 is only the
    # receiving task's local name.
    if KERNEL_TARGET:
        object_ptr = bks._reg(frame, "x22")
        object_key = _ipc_object_key(object_ptr)
        side = "destination" if object_key == KERNEL_TARGET["remote_key"] else (
            "reply" if object_key == KERNEL_TARGET["reply_key"] else None)
        if side:
            _event(frame, "IPC_SHARED_RIGHT_RECEIVE", {
                "side": side, "target_identifier": SYSTEMAPP_ID,
                "target_request": "0x%x" % KERNEL_TARGET["request"],
                "target_send_owner_tpidr": "0x%x" % KERNEL_SYNC_OWNER,
                "receive_owner_tpidr": "0x%x" % owner,
                "receive_task_pid": process.GetProcessID(),
                "receive_task_progname": bks._progname(process),
                "local_name": "0x%x" % bks._reg(frame, "x21"),
                "resolved_object": "0x%x" % object_ptr,
                "resolved_object_key": "0x%x" % object_key,
                "same_shared_object": True,
                "kernel_registers": _registers(frame),
            })
    return False


def _mach2_arguments(process, frame):
    """Decode mach_msg2_trap's packed register ABI, including vectors."""
    registers = [bks._reg(frame, "x%d" % i) for i in range(8)]
    options, bits_send = registers[1], registers[2]
    remote_local, voucher_id = registers[3], registers[4]
    desc_receive, recv_size_priority = registers[5], registers[6]
    result = {
        "registers": {"x%d" % i: "0x%x" % value
                      for i, value in enumerate(registers)},
        "options": "0x%x" % options,
        "vector": bool(options & (1 << 32)),
        "msgh_bits": "0x%x" % (bits_send & 0xffffffff),
        "send_count": bits_send >> 32,
        "remote_port": remote_local & 0xffffffff,
        "local_port": remote_local >> 32,
        "voucher_port": voucher_id & 0xffffffff,
        "message_id": voucher_id >> 32,
        "descriptor_count": desc_receive & 0xffffffff,
        "receive_name": desc_receive >> 32,
        "receive_count": recv_size_priority & 0xffffffff,
        "priority": recv_size_priority >> 32,
        "timeout": "0x%x" % registers[7],
    }
    if result["vector"]:
        count = max(result["send_count"], result["receive_count"])
        vector_raw = _user_bytes(process, registers[0], min(count, 2) * 24)
        vectors = []
        data = bytes.fromhex(vector_raw) if vector_raw else b""
        for offset in range(0, len(data) - 23, 24):
            vectors.append({
                "data": int.from_bytes(data[offset:offset + 8], "little"),
                "receive_address": int.from_bytes(
                    data[offset + 8:offset + 16], "little"),
                "send_size": int.from_bytes(
                    data[offset + 16:offset + 20], "little"),
                "receive_size": int.from_bytes(
                    data[offset + 20:offset + 24], "little"),
            })
        result["vectors_raw"] = vector_raw
        result["vectors"] = vectors
        first = vectors[0] if vectors else {}
        result["message"] = _mach_header(
            process, first.get("data", 0), first.get("send_size", 0))
    else:
        result["message"] = _mach_header(
            process, registers[0], result["send_count"])
    return result


def _import(debugger, module):
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % module.__file__, result)
    if not result.Succeeded():
        raise RuntimeError("failed LLDB import %s: %s" %
                           (module.__name__, result.GetError()))


def _arm(debugger, address, label):
    common._install(debugger, address, label,
                    "dm_lock_checkin_plugin_callbacks.on_hit")
    bp = debugger.GetSelectedTarget().GetBreakpointAtIndex(
        debugger.GetSelectedTarget().GetNumBreakpoints() - 1)
    SITES[bp.GetID()] = label


def _event(frame, label, extra):
    row = {"label": label, "time": time.time(),
           "thread": frame.GetThread().GetThreadID(),
           "tpidr_el1": "0x%x" % bks._reg(frame, "tpidr_el1"),
           "pc": "0x%x" % bks._reg(frame, "pc"),
           "lr": "0x%x" % bks._reg(frame, "lr")}
    row.update(extra)
    print("DM_LOCK_PLUGIN " + json.dumps(row, sort_keys=True), flush=True)
    safe = "".join(c if c.isalnum() or c in "_.-" else "_" for c in label)
    bks._write_event("progress.%s.json" % safe, row)
    return row


def _plugin_success(label, identifier, row):
    """Write an identifier-scoped marker so unrelated plugin edges do not stop a run."""
    safe_id = "".join(c if c.isalnum() or c in "_.-" else "_"
                      for c in (identifier or "unknown"))
    bks._write_event("success.%s.%s.json" % (label, safe_id), row)


def _read_string(process, address):
    # The existing lifecycle tracer has the correct NSString layouts for the
    # call arguments on this build; the generic Swift/CF string decoder does
    # not handle the path object's runtime representation here.
    return plugin._nsstring(process, address)


def _arm_plugin_sites(debugger):
    global ARMED_FOR_REQUEST
    if ARMED_FOR_REQUEST:
        return
    base = migration.MAIN_BASE
    if not base:
        raise RuntimeError("DataMigrator main base unavailable at start call")
    _arm(debugger, base + CALL, "DM_RUN_PLUGIN_CALL_13768")
    _arm(debugger, base + RETURN, "DM_RUN_PLUGIN_RETURN_1376C")
    ARMED_FOR_REQUEST = True
    print("DM_LOCK_PLUGIN_CALL_SITES_READY base=0x%x call=0x%x return=0x%x" %
          (base, base + CALL, base + RETURN), flush=True)


def on_hit(frame, bp_loc, context):
    label = SITES.get(bp_loc.GetBreakpoint().GetID())
    if label is None:
        # The underlying msgID-0 ladder owns the client/handler correlation.
        # After it records the matched start call, arm the plugin callsites
        # before the asynchronous migration starter can reach them.
        underlying_label = common.SITES.get(bp_loc.GetBreakpoint().GetID())
        result = inner.on_hit(frame, bp_loc, context)
        if underlying_label == "DM_START_MIGRATION_CALL":
            _arm_plugin_sites(frame.GetThread().GetProcess().GetTarget().GetDebugger())
        elif underlying_label in {
                "DM_START_MIGRATION_RETURN", "DM_MIGRATION_RETURN",
                "DM_REPLY_BLOCK_CALL", "DM_CLIENT_SYNC_RETURN"}:
            # Let the runner stop at the first matched unwind edge. The
            # underlying callbacks still own the detailed progress record.
            bks._write_event("success.%s.json" % underlying_label, {
                "label": underlying_label, "time": time.time(),
                "thread": frame.GetThread().GetThreadID(),
                "tpidr_el1": "0x%x" % bks._reg(frame, "tpidr_el1"),
                "pc": "0x%x" % bks._reg(frame, "pc")})
        return result

    if label in ("SYSTEMAPP_MACH_MSG2", "SYSTEMAPP_MACH_MSG"):
        global MACH_DEST_PORT, MACH_REPLY_PORT, MACH_TRAPS_ARMED
        process = frame.GetThread().GetProcess()
        owner = bks._reg(frame, "tpidr_el1")
        options = bks._reg(frame, "x1")
        target_call = SYNC_CALLS.get(owner)
        if label == "SYSTEMAPP_MACH_MSG2":
            mach = _mach2_arguments(process, frame)
            header = mach.get("message", {})
            remote = mach.get("remote_port", 0)
            local = mach.get("local_port", 0)
            receive_name = mach.get("receive_name", 0)
            message_id = mach.get("message_id", 0)
        else:
            header = _mach_header(process, bks._reg(frame, "x0"))
            remote = int(header.get("remote_port", 0) or 0)
            local = int(header.get("local_port", 0) or 0)
            receive_name = 0
            message_id = header.get("message_id", 0)
            mach = {"options": "0x%x" % options,
                    "registers": _registers(frame), "message": header}
        target_call = (target_call if target_call and
                       target_call.get("identifier") == SYSTEMAPP_ID else None)
        progname = bks._progname(process)
        task_ports = MACH_TARGET_PORTS.get(progname, set())
        same_task_name_match = bool(
            (remote and remote in task_ports) or
            (local and local in task_ports) or
            (receive_name and receive_name in task_ports) or
            (header.get("local_port", 0) in task_ports) or
            (header.get("remote_port", 0) in task_ports))
        elapsed = time.time() - MACH_WINDOW_START if MACH_WINDOW_START else None
        active = SYSTEMAPP_ACTIVE_CALL or {}
        same_process_active = bool(
            active.get("pid") == process.GetProcessID() and
            MACH_WINDOW_START and elapsed is not None and
            elapsed < MACH_WINDOW_SECONDS)
        raw_header = header.get("raw", "")
        try:
            raw_bytes = bytes.fromhex(raw_header)
        except (TypeError, ValueError):
            raw_bytes = b""
        raw_contains_id = SYSTEMAPP_ID.encode("ascii") in raw_bytes
        interesting_systemapp_send = (
            bool(options & 1) and message_id == 0x40000000 and
            (header.get("size") in (508, 516) or raw_contains_id))
        if (target_call or same_task_name_match or
                (same_process_active and interesting_systemapp_send)):
            _event(frame, "SYSTEMAPP_MACH_TRANSPORT", {
                "identifier": target_call.get("identifier") if target_call else None,
                "request": ("0x%x" % target_call["request"]
                            if target_call else None),
                "owner_match": bool(target_call),
                "progname": progname,
                "mach_site": label,
                "options": "0x%x" % options,
                "mach2": mach,
                "header": header,
                "target_destination_port": "0x%x" % MACH_DEST_PORT,
                "target_reply_port": "0x%x" % MACH_REPLY_PORT,
                "receive_name": "0x%x" % receive_name,
                "same_task_name_match": same_task_name_match,
                "same_process_as_active_systemapp_request": same_process_active,
                "active_request_owner_tpidr": (
                    "0x%x" % active.get("owner", 0) if active else None),
                "window_elapsed_s": elapsed,
            })
        if (target_call or (same_process_active and
                            interesting_systemapp_send)) and options & 1:
            # mach_msg{2}_trap send header: remote_port is destination and
            # local_port is the reply receive right. Preserve exactly what
            # the matched SystemApp thread passed; do not infer peer identity
            # from the process name.
            task_ports = MACH_TARGET_PORTS.setdefault(progname, set())
            if remote:
                task_ports.add(remote)
                if not MACH_DEST_PORT:
                    MACH_DEST_PORT = remote
            if local:
                task_ports.add(local)
                MACH_REPLY_PORT = local
            if receive_name:
                task_ports.add(receive_name)
                MACH_REPLY_PORT = receive_name
            selected_call = target_call or active
            # Keep the exact caller path when available. For a worker thread,
            # retain only the measured SystemApp wire ID plus its known
            # 508/516-byte shape (or an explicit plugin identifier in bytes).
            # This remains a candidate until the copied-in kmsg and object
            # handoff provide their independent joins.
            MACH_USER_SENDS[owner] = {
                "identifier": selected_call.get("identifier"),
                "request": selected_call["request"],
                "active_request_same_process": bool(
                    same_process_active and not target_call),
                "active_request_owner_tpidr": "0x%x" % active.get("owner", 0),
                "mach_site": label,
                "message_id": message_id,
                "size": header.get("size"),
                "destination_name": remote,
                "reply_local_name": local or receive_name,
                "header_address": header.get("address"),
                "contains_plugin_identifier": raw_contains_id,
                "header_prefix": raw_header[:512],
                "sender_tpidr": "0x%x" % owner,
                "sender_pid": process.GetProcessID(),
            }
            _event(frame, ("SYSTEMAPP_MACH_SEND_TUPLE" if target_call else
                           "SYSTEMAPP_PROCESS_THREAD_MACH_SEND_CANDIDATE"), {
                "identifier": selected_call.get("identifier"),
                "request": "0x%x" % selected_call["request"],
                "destination_port": "0x%x" % remote,
                "reply_local_port": "0x%x" % local,
                "receive_name": "0x%x" % receive_name,
                "message_id": message_id,
                "mach_site": label,
                "options": "0x%x" % options,
                "mach2": mach,
                "header": header,
                "same_thread_as_client_sync_call": bool(target_call),
                "candidate_only": not bool(target_call or raw_contains_id),
            })
        if MACH_WINDOW_START and elapsed is not None and elapsed >= MACH_WINDOW_SECONDS:
            for bp_id, bp_label in list(SITES.items()):
                if bp_label in ("SYSTEMAPP_MACH_MSG2", "SYSTEMAPP_MACH_MSG"):
                    frame.GetThread().GetProcess().GetTarget().FindBreakpointByID(
                        bp_id).SetEnabled(False)
            MACH_TRAPS_ARMED = False
            closed = _event(frame, "SYSTEMAPP_MACH_WINDOW_CLOSED", {
                "reason": "bounded port-match window expired",
                "window_seconds": MACH_WINDOW_SECONDS,
                "destination_port": "0x%x" % MACH_DEST_PORT,
                "reply_port": "0x%x" % MACH_REPLY_PORT,
            })
            bks._write_event("success.SYSTEMAPP_MACH_WINDOW_CLOSED.json", closed)
            for bp_id in KERNEL_BREAKPOINTS.values():
                frame.GetThread().GetProcess().GetTarget().FindBreakpointByID(
                    bp_id).SetEnabled(False)
        return False

    process = frame.GetThread().GetProcess()
    owner = bks._reg(frame, "tpidr_el1")
    if label == "DM_RUN_PLUGIN_CALL_13768":
        path_ptr, identifier_ptr, params_ptr, completion_ptr = (
            bks._reg(frame, name) for name in ("x2", "x3", "x4", "x5"))
        path = _read_string(process, path_ptr)
        identifier = _read_string(process, identifier_ptr)
        # The lifecycle probe normally filters two known IDs. Add only the
        # identity measured on this exact call so its send/receive/reply hooks
        # follow this launch without importing a prior boot's plugin name.
        if identifier and not identifier.startswith("<"):
            plugin.TARGET_IDS.add(identifier)
        if path and not path.startswith("<"):
            plugin.TARGET_PATHS = tuple(plugin.TARGET_PATHS) + (path.rsplit("/", 1)[-1],)
        call = {"owner": owner, "path": path, "identifier": identifier,
                "path_ptr": "0x%x" % path_ptr,
                "identifier_ptr": "0x%x" % identifier_ptr,
                "parameters": "0x%x" % params_ptr,
                "parameters_bytes": bks._read(process, params_ptr, 0x80).hex(),
                "completion": "0x%x" % completion_ptr,
                "completion_bytes": bks._read(process, completion_ptr, 0x40).hex(),
                "receiver": "0x%x" % bks._reg(frame, "x0"),
                "receiver_bytes": _user_bytes(
                    process, bks._reg(frame, "x0"), 0x200),
                "selector": "0x%x" % bks._reg(frame, "x1")}
        if identifier == SYSTEMAPP_ID:
            try:
                _arm_systemapp_completion(
                    frame.GetThread().GetProcess().GetTarget().GetDebugger(),
                    process, frame, call)
            except Exception as error:
                call["completion_probe_arm_error"] = (
                    type(error).__name__ + ": " + str(error))
                _event(frame, "DM_SYSTEMAPP_COMPLETION_ARM_ERROR", {
                    "identifier": identifier,
                    "completion_block": call.get("completion"),
                    "error": call["completion_probe_arm_error"],
                })
        CALLS[owner] = call
        RECEIVERS[call["receiver"]] = {
            "path": path, "identifier": identifier, "owner": owner}
        event = _event(frame, label, call)
        bks._write_event("success.DM_RUN_PLUGIN_ENTRY.json", event)
        _plugin_success("DM_RUN_PLUGIN_ENTRY", identifier, event)
        return False

    call = CALLS.get(owner)
    if not call:
        return False
    result = _event(frame, label, {
        "path": call["path"], "identifier": call["identifier"],
        "parameters": call["parameters"], "result": "0x%x" % bks._reg(frame, "x0"),
        "call_owner": "0x%x" % owner})
    bks._write_event("success.DM_RUN_PLUGIN_RETURN.json", result)
    _plugin_success("DM_RUN_PLUGIN_RETURN", call["identifier"], result)
    CALLS.pop(owner, None)
    # TARGET_IDS grows as each migration plugin is discovered. Only the
    # requested SystemAppMigrator return may stop this boot; stopping on an
    # earlier plugin (for example ContainerMigrator) detaches LLDB before
    # the transport tuple we are trying to capture.
    if (call["identifier"] == SYSTEMAPP_ID and
            os.environ.get("DVM_CONTINUE_SYSTEMAPP_PLUGIN_RETURN") != "1"):
        return True
    return False


def _plugin_on_hit(frame, bp_loc, context):
    """Keep lifecycle tracing intact and expose a matched wrapper reply stop."""
    global RAW_WRAPPER_HITS, MACH_TRAPS_ARMED, MACH_WINDOW_START
    global MACH_DEST_PORT, MACH_REPLY_PORT
    global SYSTEMAPP_ACTIVE_CALL
    label = plugin.META.get(bp_loc.GetBreakpoint().GetID(), "")
    process = frame.GetThread().GetProcess()
    owner = bks._reg(frame, "tpidr_el1")
    owner_item = plugin.OWNERS.get(owner)

    # Start low-level XPC send/reply capture as soon as the matched plugin's
    # synchronous call is observed. Do not wait for a decoded
    # pluginIdentifier on a later receive: wrapper messages may omit it.
    if label == "CLIENT_SYNC_CALL" and owner_item and \
            owner_item.get("identifier") in plugin.TARGET_IDS and \
            not plugin.DYNAMIC_ARMED:
        debugger = process.GetTarget().GetDebugger()
        for static, dynamic_label in plugin.DYNAMIC_SITES:
            plugin._install_one(debugger, plugin.SLIDE[0] + static,
                                dynamic_label)
        plugin.DYNAMIC_ARMED = True
        print("DM_LOCK_PLUGIN_XPC_TRANSPORT_ARMED identifier=%s" %
              owner_item.get("identifier"), flush=True)

    if label == "CLIENT_SYNC_CALL" and owner_item and \
            owner_item.get("identifier") == SYSTEMAPP_ID and \
            not MACH_TRAPS_ARMED:
        # A bounded syscall window obtains the actual Mach ports/message ID
        # at the matched caller. The handler keeps only this caller and traffic
        # matching its captured ports, then disables the system-wide trap
        # breakpoints after MACH_WINDOW_SECONDS to bound perturbation.
        debugger = process.GetTarget().GetDebugger()
        _arm(debugger, bks.SLIDE[0] + MACH_MSG2_TRAP, "SYSTEMAPP_MACH_MSG2")
        _arm(debugger, bks.SLIDE[0] + MACH_MSG_TRAP, "SYSTEMAPP_MACH_MSG")
        MACH_TRAPS_ARMED = True
        MACH_WINDOW_START = time.time()

    # Preserve the actual sync-call self/arguments alongside the request
    # dictionary. x21 remains the request pointer used by the existing probe;
    # x0 and the receiver object are candidates for the transport lineage,
    # not assumed endpoint fields.
    if label == "CLIENT_SYNC_CALL" and owner_item and \
            owner_item.get("identifier") == SYSTEMAPP_ID:
        request = bks._reg(frame, "x21")
        try:
            request_fields = plugin._xpc_fields(process, request)
        except Exception:
            request_fields = {}
        request_identifier = (request_fields.get("pluginIdentifier", {})
                              .get("text"))
        call = CALLS.get(owner, {})
        receiver = int(call.get("receiver", "0"), 0) if call else 0
        SYNC_CALLS[owner] = {
            "identifier": owner_item.get("identifier"),
            "path": owner_item.get("path"),
            "request": request,
            "receiver": receiver,
            "request_plugin_identifier": request_identifier,
        }
        SYSTEMAPP_ACTIVE_CALL = {
            "identifier": SYSTEMAPP_ID,
            "request": request,
            "owner": owner,
            "pid": process.GetProcessID(),
            "path": owner_item.get("path"),
            "started": time.time(),
        }
        global KERNEL_SYNC_OWNER
        KERNEL_SYNC_OWNER = owner
        _kernel_arm(process.GetTarget().GetDebugger())
        _event(frame, "CLIENT_SYNC_TRANSPORT_SAMPLE", {
            "identifier": owner_item.get("identifier"),
            "request": "0x%x" % request,
            "request_bytes": _user_bytes(process, request, 0x180),
            "x_registers": _registers(frame),
            "x0_object_bytes": _user_bytes(process, bks._reg(frame, "x0"), 0x200),
            "run_plugin_receiver": "0x%x" % receiver,
            "run_plugin_receiver_bytes": _user_bytes(process, receiver, 0x200),
        })

    # The generic DMXPCConnection receive hook can execute without a
    # pluginIdentifier field. Keep a small raw window after the target call so
    # remote receive candidates are not discarded by the identity filter.
    if label == "WRAPPER_XPC_RECEIVE":
        RAW_WRAPPER_HITS += 1
        if RAW_WRAPPER_HITS <= 128:
            message = bks._reg(frame, "x2")
            # Preserve the receiver/connection candidate alongside the XPC
            # object. Its layout is intentionally left undecoded until a
            # field or pointer is validated against the paired send path.
            endpoint = bks._reg(frame, "x0")
            fields = plugin._xpc_fields(process, message)
            sync_call = SYNC_CALLS.get(owner)
            _event(frame, "WRAPPER_XPC_RECEIVE_RAW", {
                "sync_owner_match": bool(sync_call),
                "sync_identifier": sync_call.get("identifier") if sync_call else None,
                "sync_request": ("0x%x" % sync_call["request"]
                                 if sync_call else None),
                "message": "0x%x" % message,
                "endpoint_candidate": "0x%x" % endpoint,
                "endpoint_candidate_bytes": _user_bytes(process, endpoint, 0x200),
                "fields": fields,
                "progname": bks._progname(process),
                "x_registers": _registers(frame),
                "message_bytes": _user_bytes(process, message, 0x180),
            })

    # Capture the receiver/message pair and payload bytes before the
    # underlying callback filters or normalizes the message.
    if label == "CLIENT_WRAPPER_MESSAGE":
        receiver = bks._reg(frame, "x0")
        receiver_item = RECEIVERS.get("0x%x" % receiver)
        message = bks._reg(frame, "x2")
        if _secondary_dm_match(owner, process):
            _log_secondary_dm_userland(
                frame, "CLIENT_WRAPPER_MESSAGE", message=message,
                endpoint=receiver, selector=bks._reg(frame, "x1"))
        if receiver_item and receiver_item.get("identifier") in plugin.TARGET_IDS:
            _event(frame, "CLIENT_WRAPPER_MESSAGE_RAW", {
                "identifier": receiver_item.get("identifier"),
                "receiver_owner": "0x%x" % receiver_item.get("owner", 0),
                "receiver": "0x%x" % receiver,
                "selector": "0x%x" % bks._reg(frame, "x1"),
                "message": "0x%x" % message,
                "x_registers": _registers(frame),
                "receiver_bytes": _user_bytes(process, receiver, 0x200),
                "message_bytes": _user_bytes(process, message, 0x180),
            })

    if label in ("WRAPPER_XPC_RECEIVE", "WRAPPER_XPC_SEND", "WRAPPER_XPC_REPLY"):
        RAW_WRAPPER_HITS += 1
        register = {"WRAPPER_XPC_RECEIVE": "x2", "WRAPPER_XPC_SEND": "x1",
                    "WRAPPER_XPC_REPLY": "x0"}[label]
        message = bks._reg(frame, register)
        # For send/receive, x0 is the connection/receiver candidate on the
        # observed call shape. For the reply wrapper x0 is itself the message,
        # so do not mislabel it as an endpoint.
        endpoint = bks._reg(frame, "x0") if label != "WRAPPER_XPC_REPLY" else 0
        if _secondary_dm_match(owner, process):
            _log_secondary_dm_userland(
                frame, label, message=message, endpoint=endpoint)
        fields = plugin._xpc_fields(process, message)
        identifier = fields.get("pluginIdentifier", {}).get("text")
        sync_call = SYNC_CALLS.get(owner)
        if identifier in plugin.TARGET_IDS or RAW_WRAPPER_HITS <= 32:
            _event(frame, label + "_RAW", {
                "sync_owner_match": bool(sync_call),
                "sync_identifier": sync_call.get("identifier") if sync_call else None,
                "sync_request": ("0x%x" % sync_call["request"]
                                 if sync_call else None),
                "identifier": identifier, "message": "0x%x" % message,
                "endpoint_candidate": "0x%x" % endpoint if endpoint else None,
                "endpoint_candidate_bytes": _user_bytes(process, endpoint, 0x200),
                "fields": fields, "progname": bks._progname(process),
                "register": register,
                "x_registers": _registers(frame),
                "message_bytes": _user_bytes(process, message, 0x180)})
    if label == "CLIENT_SYNC_RETURN" and owner_item and \
            owner_item.get("identifier") == SYSTEMAPP_ID:
        _event(frame, "CLIENT_SYNC_RETURN_RAW", {
            "identifier": owner_item.get("identifier"),
            "result": "0x%x" % bks._reg(frame, "x0"),
            "x_registers": _registers(frame),
        })
    result = PLUGIN_ORIGINAL_ON_HIT(frame, bp_loc, context)
    if label == "CLIENT_SYNC_RETURN":
        item = owner_item
        if item and item.get("identifier") == SYSTEMAPP_ID:
            sync_return = {
                "label": label, "time": time.time(),
                "thread": frame.GetThread().GetThreadID(),
                "tpidr_el1": "0x%x" % owner,
                "path": item.get("path"),
                "identifier": item.get("identifier"),
                "result": "0x%x" % bks._reg(frame, "x0")}
            bks._write_event("success.PLUGIN_SYNC_RETURN.json", sync_return)
            _plugin_success("PLUGIN_SYNC_RETURN", item.get("identifier"),
                            sync_return)
            SYNC_CALLS.pop(owner, None)
            # Post-sync ladder runs need this callback to log and auto-continue
            # so the same boot can reach DM_PERFORM_RETURN and BKS check-in.
            # Preserve the historical stop for reply-only investigations.
            if os.environ.get(
                    "DVM_CONTINUE_AFTER_SYSTEMAPP_SYNC_RETURN") != "1":
                return True
            print("DM_LOCK_PLUGIN_CONTINUE_AFTER_SYNC_RETURN owner=0x%x" %
                  owner, flush=True)
            return False
    if label == "WRAPPER_XPC_REPLY":
        process = frame.GetThread().GetProcess()
        message = bks._reg(frame, "x0")
        fields = plugin._xpc_fields(process, message)
        identifier = fields.get("pluginIdentifier", {}).get("text")
        if identifier in plugin.TARGET_IDS:
            reply = {
                "label": label, "time": time.time(),
                "thread": frame.GetThread().GetThreadID(),
                "tpidr_el1": "0x%x" % bks._reg(frame, "tpidr_el1"),
                "identifier": identifier,
                "message": "0x%x" % message, "fields": fields}
            bks._write_event("success.WRAPPER_XPC_REPLY.json", reply)
            _plugin_success("WRAPPER_XPC_REPLY", identifier, reply)
    return result


def install_frozen_followup(debugger, slide, main_base, owner, identifier,
                            path, parameters, receiver=0, request=0):
    """Re-arm one still-open plugin launch on an already-frozen same boot."""
    global ARMED_FOR_REQUEST, PLUGIN_ORIGINAL_ON_HIT
    SITES.clear()
    CALLS.clear()
    RECEIVERS.clear()
    SYNC_CALLS.clear()
    ARMED_FOR_REQUEST = True
    bks.SLIDE[0] = int(slide)
    plugin.TARGET_IDS.clear()
    plugin.TARGET_IDS.add(str(identifier))
    plugin.TARGET_PATHS = (str(path).rsplit("/", 1)[-1],)
    plugin.META.clear()
    plugin.OWNERS.clear()
    plugin.COUNTS.clear()
    plugin.DYNAMIC_ARMED = False

    _import(debugger, plugin)
    PLUGIN_ORIGINAL_ON_HIT = plugin.on_hit
    plugin.on_hit = _plugin_on_hit
    plugin.install(debugger, int(slide))
    # These shared XPC functions are normally armed after a filtered receive.
    # Arm them directly here because this follow-up starts mid-request.
    for static, label in plugin.DYNAMIC_SITES:
        plugin._install_one(debugger, int(slide) + static, label)
    plugin.DYNAMIC_ARMED = True

    _arm(debugger, int(main_base) + RETURN,
         "DM_RUN_PLUGIN_RETURN_1376C_FOLLOWUP")
    CALLS[int(owner)] = {
        "owner": int(owner), "path": str(path), "identifier": str(identifier),
        "parameters": "0x%x" % int(parameters),
        "receiver": "0x%x" % int(receiver), "followup_seed": True}
    if receiver:
        RECEIVERS["0x%x" % int(receiver)] = {
            "path": str(path), "identifier": str(identifier),
            "owner": int(owner)}
    plugin.OWNERS[int(owner)] = {"path": str(path), "identifier": str(identifier)}
    process = debugger.GetSelectedTarget().GetProcess()
    seed = {
        "label": "FROZEN_REQUEST_TRANSPORT_SEED",
        "time": time.time(), "owner": "0x%x" % int(owner),
        "identifier": str(identifier), "path": str(path),
        "request": "0x%x" % int(request),
        "request_bytes": _user_bytes(process, int(request), 0x180),
        "receiver": "0x%x" % int(receiver),
        "receiver_bytes": _user_bytes(process, int(receiver), 0x200),
        "parameters": "0x%x" % int(parameters),
        "parameters_bytes": _user_bytes(process, int(parameters), 0x100),
    }
    bks._write_event("progress.FROZEN_REQUEST_TRANSPORT_SEED.json", seed)
    print("DM_LOCK_PLUGIN_FROZEN_REQUEST_TRANSPORT_SEED " +
          json.dumps(seed, sort_keys=True), flush=True)
    print("DM_LOCK_PLUGIN_FOLLOWUP_READY slide=0x%x base=0x%x owner=0x%x "
          "identifier=%s path=%s" %
          (int(slide), int(main_base), int(owner), identifier, path), flush=True)


def install_frozen_ipc_followup(debugger, slide, owner, request,
                               remote_object, reply_object,
                               sync_return_pc=0x26C211F8C,
                               plugin_return_pc=0x104F6F76C):
    """Observe rights for one already-resolved SystemApp request.

    The +0x40bc site is now known to be a secondary object-copyin result,
    not a post-mqueue_receive/dequeue point. Request/reply object candidates
    are explicit values captured by IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS; no
    task-local port-name equality is used.
    """
    global KERNEL_SLIDE, KERNEL_SYNC_OWNER, KERNEL_TARGET
    global KERNEL_RECEIVE_CONDITIONED, KERNEL_PATH_ARMED
    KERNEL_SLIDE = int(slide)
    KERNEL_SYNC_OWNER = int(owner)
    remote_key = _ipc_object_key(int(remote_object))
    reply_key = _ipc_object_key(int(reply_object))
    if not remote_key or not reply_key:
        raise ValueError("resolved ipc_object arguments are not canonical kernel pointers")
    KERNEL_TARGET = {
        "owner": int(owner), "request": int(request),
        "identifier": SYSTEMAPP_ID,
        "remote_object": int(remote_object),
        "reply_object": int(reply_object),
        "remote_key": remote_key, "reply_key": reply_key,
        "header": {"source": "PLUGIN29 IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS"},
    }
    KERNEL_META.clear()
    KERNEL_BREAKPOINTS.clear()
    target = debugger.GetSelectedTarget()
    receive_bp = _kernel_bp(debugger, RECEIVE_OBJECT_LOADED,
                            "KERNEL_RECEIVE_OBJECT_LOADED")
    receive_done_bp = _kernel_bp(debugger, SECONDARY_COPYIN_RESULT,
                                 "KERNEL_SECONDARY_COPYIN_RESULT")
    send_bp = _kernel_bp(debugger, MACH2_AA8DD58_RETURN,
                         "KERNEL_MACH2_AA8DD58_RETURN")
    # Do not install LLDB expression conditions here. On this LLDB/GDB-stub
    # pair, pointer-mask and kmsg-memory conditions repeatedly fail to JIT
    # materialize while mach_msg2 is active. Keep the three breakpoints
    # unconditioned for this bounded follow-up; the Python callback compares
    # the resolved object keys before emitting any event.
    KERNEL_RECEIVE_CONDITIONED = False
    for address, label in (
            (int(sync_return_pc), "CLIENT_SYNC_RETURN_FOLLOWUP"),
            (int(plugin_return_pc), "DM_RUN_PLUGIN_RETURN_FOLLOWUP")):
        bp = target.BreakpointCreateByAddress(address)
        if not bp.IsValid():
            raise RuntimeError("failed to arm %s at 0x%x" % (label, address))
        bp.SetScriptCallbackFunction(__name__ + ".on_kernel_identity_hit")
        KERNEL_META[bp.GetID()] = label
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x callback=%s" %
              (bp.GetID(), label, address,
               __name__ + ".on_kernel_identity_hit"), flush=True)
    KERNEL_PATH_ARMED = True
    print("DM_LOCK_PLUGIN_FROZEN_IPC_FOLLOWUP_READY filter=callback-only "
          "owner=0x%x request=0x%x "
          "destination_object=0x%x reply_object=0x%x mach2_send=0x%x "
          "receive_right=0x%x secondary_copyin_result=0x%x" % (
              int(owner), int(request), int(remote_object), int(reply_object),
              MACH2_AA8DD58_RETURN + KERNEL_SLIDE,
              RECEIVE_OBJECT_LOADED + KERNEL_SLIDE,
              SECONDARY_COPYIN_RESULT + KERNEL_SLIDE), flush=True)


def install(debugger, slide):
    global ARMED_FOR_REQUEST, PLUGIN_ORIGINAL_ON_HIT, RAW_WRAPPER_HITS
    global MACH_TRAPS_ARMED, MACH_WINDOW_START, MACH_DEST_PORT, MACH_REPLY_PORT
    global KERNEL_SYNC_OWNER, KERNEL_TARGET, KERNEL_RECEIVE_CONDITIONED
    global MACH_USER_SENDS
    global SYSTEMAPP_ACTIVE_CALL
    global WRAPPER_THREAD_SEND_SAMPLES
    global ANY_PINNED_REPLY_SEND_WATCH_ARMED, ANY_PINNED_REPLY_SEND_HITS
    global PIN_SEND_PATH_STATS, PIN_ITH_KMSG_STATS
    SITES.clear()
    CALLS.clear()
    RECEIVERS.clear()
    ARMED_FOR_REQUEST = False
    RAW_WRAPPER_HITS = 0
    SYNC_CALLS.clear()
    MACH_TRAPS_ARMED = False
    MACH_WINDOW_START = 0.0
    MACH_DEST_PORT = 0
    MACH_REPLY_PORT = 0
    MACH_TARGET_PORTS.clear()
    MACH_USER_SENDS.clear()
    SYSTEMAPP_ACTIVE_CALL = None
    KERNEL_META.clear()
    KERNEL_BREAKPOINTS.clear()
    KERNEL_SYNC_OWNER = 0
    KERNEL_TARGET = None
    WRAPPER_THREAD_SEND_SAMPLES = 0
    ANY_PINNED_REPLY_SEND_WATCH_ARMED = False
    ANY_PINNED_REPLY_SEND_HITS = 0
    PIN_SEND_PATH_STATS = {"postcopy_return_hits": 0,
                           "postcopy_return_readable": 0,
                           "postcopy_return_bad_kmsg": 0}
    PIN_ITH_KMSG_STATS = {
        label: {"hits": 0, "candidate_headers": 0, "reply_matches": 0}
        for label in ("KERNEL_ITH_KMSG_QUEUE_STORE",
                      "KERNEL_ITH_KMSG_DIRECT_STORE_A",
                      "KERNEL_ITH_KMSG_DIRECT_STORE_B")}
    KERNEL_PENDING_REPLY_RECEIVE.clear()
    KERNEL_RECEIVE_CONDITIONED = False
    plugin.TARGET_IDS.clear()
    plugin.TARGET_IDS.update({"com.apple.PassbookDataMigrator", "com.apple.sbmigrator"})
    plugin.TARGET_PATHS = ("PassbookDataMigrator.migrator", "SpringBoard.migrator")
    plugin.META.clear()
    plugin.OWNERS.clear()
    plugin.COUNTS.clear()
    plugin.DYNAMIC_ARMED = False

    # The reply composition installs existing modules; route its dynamically
    # armed migration callbacks through this dispatcher so the matched start
    # call can arm 0x13768/0x1376c.
    _import(debugger, plugin)
    PLUGIN_ORIGINAL_ON_HIT = plugin.on_hit
    plugin.on_hit = _plugin_on_hit
    reply.install(debugger, int(slide))
    # reply.install imports the inner module through LLDB; that module's
    # import-time alias resets the wrapped implementation's callback target.
    # Set it after composition so newly armed msgID-0 sites dispatch here.
    migration.CALLBACK_MODULE = __name__
    plugin.install(debugger, int(slide))
    # The historical receive filter did not hit on every launch. Capture the
    # bounded send/reply surface from boot so a same-ID pairing is not lost
    # behind that coverage assumption.
    for static, label in plugin.DYNAMIC_SITES:
        plugin._install_one(debugger, int(slide) + static, label)
    plugin.DYNAMIC_ARMED = True
    print("DM_LOCK_CHECKIN_PLUGIN_READY slide=0x%x" % int(slide), flush=True)

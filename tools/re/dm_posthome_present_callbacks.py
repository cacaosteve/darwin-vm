"""Read-only post-home CA commit to first IOMFB handoff ladder.

The repaired-parent ADFL/home probe supplies the controls. These extra sites
are logged only after a non-null SBHomeScreenEntity init return; unrelated
early CA traffic does not count as post-home render progress.
"""

import json
import os
import struct
import time
from pathlib import Path

import lldb

import bks_checkin_callbacks as bks
import dm_post_shell_home_min_callbacks as home
import first_surface_callbacks as surface_capture
from capture_marker_gate import accepted_transition_marker, marker_is_open
from posthome_return_context import return_context_status
from posthome_surface_status import classify_iosurface_x3


USER_SITES = (
    # Setup's welcome UI has its own scene lifecycle.  Include its commits so
    # a visible client-side transaction can be distinguished from SB-only CA
    # activity when the display server receives no Update.
    (0x1843FF180, "CA_COMMIT_ENTRY", ("SpringBoard", "Setup")),
    # P276's offline QuartzCore disassembly places this callsite in
    # CA::Render::Message::send_message, immediately before the imported
    # mach_msg stub. At this PC x0 is the Mach message buffer, x1 options,
    # x2 send size, and x3-x6 are the remaining mach_msg arguments. This is
    # an AP-side serialized-message boundary, not proof of server receipt.
    (0x184404FAC, "CA_RENDER_MACH_MSG_SEND_CALL", ("SpringBoard", "Setup")),
    (0x184776174, "SERVER_RENDER_UPDATE_ENTRY", "backboardd"),
    (0x1844510F0, "DISPLAY_RENDER_DISPLAY_ENTRY", "backboardd"),
    (0x18444B954, "SERVER_OGL_RENDER", "backboardd"),
    (0x18446BB14, "FINISH_UPDATE_ENTRY", "backboardd"),
    (0x18446CA68, "FINISH_UPDATE_SURFACE_BL", "backboardd"),
    (0x18446D0C0, "FINISH_SWAP_X22", "backboardd"),
    (0x18446D0CC, "FINISH_SWAP_X22_RETURN", "backboardd"),
    (0x1847AE2CC, "CREATE_SURFACE_ENTRY", "backboardd"),
    (0x1847AE314, "ALLOCATE_IOSURFACE_RETURN", "backboardd"),
    (0x18447238C, "FB_SWAP_SET_LAYER_ENTRY", "backboardd"),
    (0x184472488, "FB_SWAP_SENDER_CALL", "backboardd"),
)
# Historical event label is retained for consumers, but this address is only
# the observed kernel-side dispatch boundary. Static bootkc disassembly shows
# an authenticated indirect branch through vtable slot +0xd8 here; it is not
# yet identified as the A408 builder/submission body.
KERNEL_SITES = (
    # P281's measured path puts H17P swap_submit ahead of generic mapping and
    # the A408/A407 builders. Keep these kernel cuts gated on the confirmed
    # lock-transition marker so ordinary boot traffic does not consume them.
    (0xFFFFFFF00918E3C4, "H17P_SWAP_SUBMIT"),
    (0xFFFFFFF00A0C366C, "IOMFB_GENERIC_MAP"),
    (0xFFFFFFF00A0C4664, "IOMFB_A408_BUILD"),
    (0xFFFFFFF00A0C477C, "IOMFB_A407_BUILD"),
)
KERNEL_ACCEPTED_GATE_LABELS = {
    "H17P_SWAP_SUBMIT", "IOMFB_GENERIC_MAP", "IOMFB_A408_BUILD",
    "IOMFB_A407_BUILD",
}
KERNEL_SLIDE = 0x20000000
META = {}
ARMED = set()
COUNTS = {}
SURFACE_SAMPLES = 0
# Keep several post-shell swaps so a later ordinary touch transition can be
# observed without restarting LLDB. Captures remain read-only and bounded.
MAX_SURFACE_SAMPLES = 16
START_FILE = ""
GATED_SURFACE_SAMPLES = 0
SURFACE_X3_STATUS_COUNTS = {
    "no-surface-pointer-at-x3": 0,
    "x3-not-a-valid-user-pointer": 0,
    "captured-or-read-attempted": 0,
    "valid-user-pointer-sample-limit-reached": 0,
}
SURFACE_START_FILE = ""
ALLOCATED_SURFACES = []
ALLOCATED_SURFACE_SAMPLES = 0
RECENT_LAYER_HEADERS = {}
MAX_ALLOCATED_SURFACE_SAMPLES = 2
OGL_INFLIGHT = {}
OGL_STACK_SAMPLES = set()
CA_COMMIT_PENDING = {}
SURFACE_LINEAGE_TRACE = False
SURFACE_LINEAGE_HITS = 0
MAX_SURFACE_LINEAGE_HITS = 32
DYNAMIC_DISPATCH_TARGETS = {}
MAX_DYNAMIC_DISPATCH_TARGETS = 4
RETURN_CONTEXTS = {}
STRICT_RETURN_ARMED = set()
MAX_RETURN_CONTEXT_DIAGNOSTICS = 8
# A typical post-transition display burst can exceed the generic 32-row cap
# before the Home swipe is even sent. Keep the AP-side serialized CA messages
# covered through the accepted lock transition, but start counting only once
# the gesture probe has written its positive marker.
CA_MSG_START_FILE = ""
MAX_CA_RENDER_MACH_MSG_SAMPLES = 8192


def _canonical_kernel_code_candidate(raw):
    """Return a 48-bit sign-extended kernel VA candidate without PAC claims."""
    if not isinstance(raw, int):
        return None
    candidate = raw & 0x0000FFFFFFFFFFFF
    if candidate & (1 << 47):
        candidate |= 0xFFFF000000000000
    if (candidate & 3 or
            not 0xFFFFFFF000000000 <= candidate <= 0xFFFFFFF03FFFFFFF):
        return None
    return candidate


def _register(debugger, address, label, expected, thread=0):
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint set --address 0x%x" % address, result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed %s: %s" % (label, result.GetError()))
    bp = target.GetBreakpointAtIndex(before)
    META[bp.GetID()] = label, expected, thread
    bp.SetAutoContinue(True)
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand(
        "breakpoint command add -F dm_posthome_present_callbacks.on_hit %d" %
        bp.GetID(), result)
    if not result.Succeeded():
        raise RuntimeError("callback failure %s: %s" % (label, result.GetError()))
    proof = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(), proof)
    proof_text = proof.GetOutput() or proof.GetError() or ""
    callback_name = "dm_posthome_present_callbacks.on_hit"
    if (not proof.Succeeded() or callback_name not in proof_text or
            bp.GetNumLocations() < 1):
        bp.SetEnabled(False)
        raise RuntimeError("callback/location proof failed for %s: %s" %
                           (label, proof_text))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
          (bp.GetID(), label, address), flush=True)
    return bp


def _arm_return(frame, label, expected, thread):
    address = bks._reg(frame, "lr")
    if not address:
        print("POSTHOME_RETURN_UNARMED label=%s reason=null-lr" % label,
              flush=True)
        return
    key = label, expected, thread, address
    if key in ARMED:
        return
    ARMED.add(key)
    debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
    _register(debugger, address, label, expected, thread)


def _arm_context_return(frame, label, expected, source_label):
    """Arm a one-invocation return witness keyed by TPIDR and entry SP.

    Kernel text PCs are shared by many callers. The breakpoint is deliberately
    not thread-filtered in LLDB; the callback compares the live TPIDR and SP
    against this saved entry so unrelated executions at the same return PC
    cannot be scored as this invocation's return.
    """
    thread = bks._reg(frame, "tpidr_el1")
    entry_sp = bks._reg(frame, "sp")
    address = bks._reg(frame, "lr")
    if not address or thread is None or entry_sp is None:
        print("POSTHOME_CONTEXT_RETURN_UNARMED label=%s lr=%s thread=%s sp=%s" %
              (label, address, thread, entry_sp), flush=True)
        return None
    key = label, expected, thread, entry_sp, address
    if key in STRICT_RETURN_ARMED:
        return None
    debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
    bp = _register(debugger, address, label, expected, 0)
    context = {
        "label": label,
        "entry_label": source_label,
        "expected_process": expected,
        "expected_thread": thread,
        "entry_sp": entry_sp,
        "return_pc": address,
        "entry_pc": bks._reg(frame, "pc"),
        "entry_lr": address,
        "entry_time": time.time(),
        "entry_registers": {
            "x0": bks._reg(frame, "x0"),
            "x1": bks._reg(frame, "x1"),
            "x2": bks._reg(frame, "x2"),
            "x3": bks._reg(frame, "x3"),
            "x8": bks._reg(frame, "x8"),
        },
        "diagnostics": 0,
    }
    RETURN_CONTEXTS[bp.GetID()] = context
    STRICT_RETURN_ARMED.add(key)
    print("POSTHOME_CONTEXT_RETURN_ARMED " + json.dumps({
        "breakpoint_id": bp.GetID(),
        "label": label,
        "entry_label": source_label,
        "expected_process": expected,
        "expected_thread": thread,
        "entry_sp": entry_sp,
        "entry_pc": context["entry_pc"],
        "return_pc": address,
        "entry_registers": context["entry_registers"],
    }, sort_keys=True), flush=True)
    return bp


def _record_ca_commit_entry(row):
    # The return PC and restored SP let the later callback pair a return to
    # this invocation, rather than treating one global return hit as coverage
    # for every CA transaction. CA::Context::commit_transaction is entered
    # with the Context in x0 and Transaction in x1.
    key = (row.get("thread"), row.get("sp"), row.get("lr"))
    CA_COMMIT_PENDING.setdefault(key, []).append({
        "count": row.get("count"),
        "time": row.get("time"),
        "context_x0": row.get("x0"),
        "transaction_x1": row.get("x1"),
        "entry_pc": row.get("pc"),
        "entry_lr": row.get("lr"),
        "entry_sp": row.get("sp"),
    })


def _pair_ca_commit_return(row):
    key = (row.get("thread"), row.get("sp"), row.get("pc"))
    entries = CA_COMMIT_PENDING.get(key, [])
    if not entries:
        row["ca_commit_entry_pair"] = None
        row["ca_commit_return_pair_status"] = "no-pending-entry-at-same-thread-sp-lr"
        return
    entry = entries.pop(0)
    if not entries:
        CA_COMMIT_PENDING.pop(key, None)
    row["ca_commit_entry_pair"] = entry
    row["ca_commit_return_pair_status"] = "matched-thread-sp-and-entry-lr"


def _read_mach_msg_call(process, row):
    """Read a bounded header/prefix at QuartzCore's pre-mach_msg callsite.

    When explicitly enabled, preserve the complete 0x9c42 body (up to the
    existing 4 KiB message cap) so it can be compared with the server-side
    receive buffer. Port names remain task-local; body equality is a payload
    join, not an IPC-right identity proof.
    """
    address = row.get("x0")
    if not isinstance(address, int) or address <= 0x10000:
        return {"header_read": False, "reason": "invalid-x0"}
    error = lldb.SBError()
    data = process.ReadMemory(address, 0x20, error)
    if not error.Success() or not data or len(data) < 0x18:
        return {"header_read": False,
                "reason": error.GetCString() or "short-read",
                "read_bytes": len(data) if data else 0}
    bits, size, remote, local, voucher, msg_id = struct.unpack(
        "<IIIIII", data[:0x18])
    preview_length = min(0x100, max(size, 0x18))
    preview_error = lldb.SBError()
    preview = process.ReadMemory(address, preview_length, preview_error)
    result = {
        "header_read": True,
        "header_address": address,
        "header_bytes_0x00_0x1f": data.hex(),
        "msgh_bits": bits,
        "msgh_size": size,
        "msgh_remote_port_task_local_name": remote,
        "msgh_local_port_task_local_name": local,
        "msgh_voucher_port_task_local_name": voucher,
        "msgh_id": msg_id,
        "message_prefix": (preview.hex()
                            if preview_error.Success() and preview else None),
        "message_prefix_bytes": (len(preview)
                                 if preview_error.Success() and preview else 0),
        "mach_msg_args": {
            "options_x1": row.get("x1"),
            "send_size_x2": row.get("x2"),
            "receive_size_x3": row.get("x3"),
            "receive_name_x4": row.get("x4"),
            "timeout_x5": row.get("x5"),
            "notify_x6": row.get("x6"),
        },
        "interpretation": (
            "QuartzCore AP-side mach_msg callsite; port fields are task-local "
            "names and require kernel-right/object resolution for peer joins"),
    }
    if (msg_id == 0x9c42 and
            os.environ.get("DVM_CA_MSG_FULL_BODY_9C42") == "1"):
        capture_length = min(size, 0x1000)
        full_error = lldb.SBError()
        full = process.ReadMemory(address, capture_length, full_error)
        if full_error.Success() and full:
            result["message_full_hex"] = full.hex()
            result["message_full_bytes"] = len(full)
            result["message_full_complete"] = len(full) == size
            result["message_full_body_hex"] = full[0x18:].hex()
            result["message_full_body_bytes"] = max(0, len(full) - 0x18)
    return result


def _resolve_load_address(process, value):
    """Annotate a captured code-pointer candidate without dereferencing it."""
    if not isinstance(value, int) or value <= 0x10000:
        return {"address": value, "resolved": False}
    try:
        target = process.GetTarget()
        address = target.ResolveLoadAddress(value)
        if not address.IsValid():
            return {"address": value, "resolved": False}
        module = address.GetModule()
        section = address.GetSection()
        function = address.GetFunction()
        symbol = address.GetSymbol()
        module_path = ""
        if module.IsValid():
            module_path = module.GetFileSpec().GetPath() or ""
        section_name = section.GetName() if section.IsValid() else ""
        function_name = ((function.GetName() or "")
                         if function.IsValid() else "")
        symbol_name = ((symbol.GetName() or "")
                       if symbol.IsValid() else "")
        return {
            "address": value,
            "resolved": bool(module_path or section_name or function_name or
                              symbol_name),
            "module": module_path,
            "section": section_name,
            "function": function_name,
            "symbol": symbol_name,
        }
    except Exception as error:
        return {"address": value, "resolved": False,
                "resolve_error": repr(error)}


def _read_header(process, address, offsets=(0, 8, 0x10, 0x18)):
    """Read a small object header and resolve its first word as a code/data symbol."""
    if not isinstance(address, int) or address <= 0x10000:
        return {"address": address, "readable": False}
    words = [surface_capture._u64(process, address + offset)
             for offset in offsets]
    first = words[0] if words else None
    return {
        "address": address,
        "offsets": list(offsets),
        "qwords": words,
        "first_word_resolution": _resolve_load_address(process, first),
    }


def _looks_like_user_pointer(value):
    # The captured arm64e user heaps in this guest use the 0x7x... range.
    # Keep this a conservative candidate filter, not a pointer-validity claim.
    return isinstance(value, int) and 0x7000000000 <= value < 0x800000000000


def _iosurface_x3_status(value):
    """Classify the known fb_swap_set_layer x3 surface argument."""
    if _looks_like_user_pointer(value):
        return "captured-or-read-attempted"
    if not value:
        return "no-surface-pointer-at-x3"
    return "x3-not-a-valid-user-pointer"


def _surface_candidate_keys():
    keys = {}
    for item in ALLOCATED_SURFACES:
        metadata = item.get("metadata", {})
        for field, value in (("allocation_result", item.get("surface")),
                             ("metadata_internal", metadata.get("internal")),
                             ("metadata_base", metadata.get("base_address"))):
            if _looks_like_user_pointer(value):
                keys.setdefault(value, []).append({
                    "allocation_result": item.get("surface"),
                    "allocation_time": item.get("time"),
                    "field": field,
                })
    return keys


def _descriptor_surface_lineage(process, descriptor, descriptor_words):
    """Look for a previously returned surface among descriptor pointers/children.

    This is an opt-in diagnostic. A match is an address-identity observation,
    not proof the descriptor represents visible pixels or reaches scanout.
    """
    keys = _surface_candidate_keys()
    matches = []
    direct_refs = []
    nested_reads = []
    seen = set()
    for index, value in enumerate(descriptor_words):
        offset = index * 8
        if value in keys:
            matches.append({"depth": 0, "descriptor_offset": offset,
                            "value": value, "candidates": keys[value]})
        if not _looks_like_user_pointer(value) or value in seen:
            continue
        seen.add(value)
        direct_refs.append({"offset": offset, "value": value})
        # At most eight immediate user-pointer children, eight qwords each.
        if len(nested_reads) >= 8:
            continue
        words = [surface_capture._u64(process, value + child_offset)
                 for child_offset in range(0, 0x40, 8)]
        nested = {"descriptor_offset": offset, "pointer": value,
                  "qwords_0x00_0x38": words}
        for child_index, child_value in enumerate(words):
            if child_value in keys:
                matches.append({
                    "depth": 1,
                    "descriptor_offset": offset,
                    "pointer": value,
                    "child_offset": child_index * 8,
                    "value": child_value,
                    "candidates": keys[child_value],
                })
        nested_reads.append(nested)
    return {
        "descriptor": descriptor,
        "candidate_addresses": [
            {"address": address, "sources": sources}
            for address, sources in keys.items()],
        "direct_user_pointer_fields": direct_refs,
        "nested_first_0x40_reads": nested_reads,
        "allocation_identity_matches": matches,
        "interpretation": (
            "address identity only; no visible-pixel or scanout claim"),
    }


def on_hit(frame, bp_loc, _context):
    global SURFACE_SAMPLES, GATED_SURFACE_SAMPLES
    global ALLOCATED_SURFACE_SAMPLES
    global SURFACE_LINEAGE_HITS
    global SURFACE_X3_STATUS_COUNTS
    bp = bp_loc.GetBreakpoint()
    label, expected, thread_filter = META[bp.GetID()]
    if not home.HOME_RETURNED:
        return False
    process = frame.GetThread().GetProcess()
    name = bks._progname(process)
    if expected != "kernel" and name not in (
            expected if isinstance(expected, tuple) else (expected,)):
        return False
    if (label == "CA_RENDER_MACH_MSG_SEND_CALL" and
            not marker_is_open(CA_MSG_START_FILE)):
        return False
    accepted_marker = None
    if label in KERNEL_ACCEPTED_GATE_LABELS:
        accepted_marker = accepted_transition_marker(SURFACE_START_FILE)
        if accepted_marker is None:
            return False
    thread = bks._reg(frame, "tpidr_el1")
    if thread_filter and thread != thread_filter:
        return False
    strict_return = RETURN_CONTEXTS.get(bp.GetID())
    strict_return_status = None
    if strict_return is not None:
        strict_return_status = return_context_status(
            strict_return.get("expected_thread"),
            strict_return.get("entry_sp"),
            thread, bks._reg(frame, "sp"))
        if strict_return_status != "matched-entry-thread-and-restored-sp":
            strict_return["diagnostics"] += 1
            if strict_return["diagnostics"] <= MAX_RETURN_CONTEXT_DIAGNOSTICS:
                print("POSTHOME_CONTEXT_RETURN_DIAGNOSTIC " + json.dumps({
                    "breakpoint_id": bp.GetID(),
                    "label": label,
                    "status": strict_return_status,
                    "expected_thread": strict_return.get("expected_thread"),
                    "actual_thread": thread,
                    "expected_sp": strict_return.get("entry_sp"),
                    "actual_sp": bks._reg(frame, "sp"),
                    "pc": bks._reg(frame, "pc"),
                    "process": name,
                    "diagnostic_count": strict_return["diagnostics"],
                }, sort_keys=True), flush=True)
            return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    # Swap calls are commonly a batch of null-surface layer-state updates
    # before a real IOSurface arrives. Keep observing well beyond that batch;
    # a small fixed cap made P105's later surface status unknowable.
    limit = (
        MAX_CA_RENDER_MACH_MSG_SAMPLES
        if label == "CA_RENDER_MACH_MSG_SEND_CALL" else
        256 if label in ("FB_SWAP_SET_LAYER_ENTRY", "FB_SWAP_SENDER_CALL") else
        512 if label in ("CA_COMMIT_ENTRY", "CA_COMMIT_RETURN") else 32)
    if count > limit:
        bp.SetEnabled(False)
        return False
    row = {
        "label": label, "count": count, "time": time.time(),
        "process": name, "thread": thread, "pc": bks._reg(frame, "pc"),
        "lr": bks._reg(frame, "lr"), "sp": bks._reg(frame, "sp"),
        **{"x%d" % index: bks._reg(frame, "x%d" % index)
           for index in range(9)},
    }
    if strict_return is not None:
        row["return_pair"] = {
            "status": strict_return_status,
            "entry": strict_return,
            "actual_thread": thread,
            "actual_sp": row["sp"],
        }
    if accepted_marker is not None:
        row["accepted_transition_time"] = accepted_marker.get("time")
    if label == "CA_COMMIT_ENTRY":
        _record_ca_commit_entry(row)
    elif label == "CA_COMMIT_RETURN":
        _pair_ca_commit_return(row)
    elif label == "CA_RENDER_MACH_MSG_SEND_CALL":
        row["mach_msg_call"] = _read_mach_msg_call(process, row)
    elif label == "IOMFB_GENERIC_MAP":
        descriptor = row.get("x1")
        if isinstance(descriptor, int) and descriptor > 0x10000:
            row["swap_descriptor_sample"] = {
                "address_x1": descriptor,
                "layer_mask_plus_0x14c": surface_capture._u32(
                    process, descriptor + 0x14c),
                "surface_slots_plus_0x78": [
                    surface_capture._u64(
                        process, descriptor + 0x78 + index * 8)
                    for index in range(4)],
                "semantics": (
                    "read-only descriptor fields; the mask/slots do not by "
                    "themselves prove mapping success or scanout"),
            }
    if label == "IOMFB_A408_BUILD":
        # P273's matching on-disk bootkc disassembly loads a vtable word into
        # x8 before copying it to x16 for BRAA. P274's live register pattern
        # differs from that saved instruction sequence, so do not assign the
        # P273 mnemonic or PAC role to every boot. x8 is retained as a target
        # candidate (not a proven branch destination); preserve the adjacent
        # raw registers until the exact loaded bootkc bytes are tied to a run.
        target_candidate = bks._reg(frame, "x8")
        va_candidate = _canonical_kernel_code_candidate(target_candidate)
        row.update({
            "dispatch_boundary": True,
            "static_site_semantics": "unresolved-for-this-boot-image",
            "p273_on_disk_instruction": "braa x16,x17",
            "dispatch_x8_target_candidate_raw": target_candidate,
            "dispatch_x16_raw": bks._reg(frame, "x16"),
            "dispatch_x17_raw": bks._reg(frame, "x17"),
            "dispatch_x2_raw": bks._reg(frame, "x2"),
            "dispatch_target_48bit_va_candidate": va_candidate,
        })
        # Arm a non-stopping entry candidate from x8. A same-thread hit can
        # support the target association; it does not prove branch completion,
        # a return value, or the target's operation.
        if (va_candidate is not None and
                va_candidate not in DYNAMIC_DISPATCH_TARGETS and
                len(DYNAMIC_DISPATCH_TARGETS) < MAX_DYNAMIC_DISPATCH_TARGETS):
            DYNAMIC_DISPATCH_TARGETS[va_candidate] = {
                "level": 1,
                "source_site": row["pc"],
                "source_x8_candidate_raw": target_candidate,
                "source_x16_raw": row["dispatch_x16_raw"],
                "source_x17_raw": row["dispatch_x17_raw"],
                "source_x2_raw": row["dispatch_x2_raw"],
                "source_tpidr_el1": thread,
                "source_time": row["time"],
            }
            try:
                candidate_bp = _register(
                    process.GetTarget().GetDebugger(), va_candidate,
                    "IOMFB_A408_DISPATCH_TARGET_CANDIDATE_HIT", "kernel")
                row["dynamic_target_candidate_bp_id"] = candidate_bp.GetID()
            except Exception as error:
                DYNAMIC_DISPATCH_TARGETS.pop(va_candidate, None)
                row["dynamic_target_candidate_arm_error"] = repr(error)
    elif label == "IOMFB_A408_DISPATCH_TARGET_CANDIDATE_HIT":
        source = DYNAMIC_DISPATCH_TARGETS.get(row["pc"])
        row["dispatch_target_source"] = source
        same_tpidr_as_source = False
        if source:
            same_tpidr_as_source = (
                thread == source.get("source_tpidr_el1"))
            row["same_tpidr_as_dispatch_source"] = same_tpidr_as_source
            row["seconds_after_dispatch_source"] = (
                row["time"] - source["source_time"]
                if isinstance(source.get("source_time"), (int, float))
                else None)
            if same_tpidr_as_source:
                # The x8 candidate is an instruction address, not a named
                # function. At this live hit, however, its own LR/SP form a
                # better return candidate than the LR sampled at the source
                # BRAA/BLRAA dispatch instruction. Keep the branch mnemonic
                # unresolved until matched boot bytes establish it.
                _arm_context_return(
                    frame,
                    "IOMFB_DISPATCH_TARGET_RETURN_LEVEL%d" %
                    source.get("level", 0),
                    expected,
                    "IOMFB_A408_DISPATCH_TARGET_CANDIDATE_HIT")
        # P273's first target candidate is itself an indirect callsite:
        # x8 carries its callee candidate and x16 the BLRAA modifier. Log and
        # optionally arm exactly one further level, tied to this hit's thread
        # and receiver. A later address hit is still only code-location
        # evidence; it is not a method name or proof of a successful call.
        raw_callee = row.get("x8")
        row["nested_indirect_callee_raw_x8"] = raw_callee
        row["nested_indirect_pac_modifier_x16"] = row.get("x16")
        nested = _canonical_kernel_code_candidate(raw_callee)
        row["nested_indirect_callee_48bit_va_candidate"] = nested
        if (source and source.get("level") == 1 and nested is not None and
                nested not in DYNAMIC_DISPATCH_TARGETS and
                len(DYNAMIC_DISPATCH_TARGETS) < MAX_DYNAMIC_DISPATCH_TARGETS):
            DYNAMIC_DISPATCH_TARGETS[nested] = {
                "level": 2,
                "source_site": row["pc"],
                "source_target_raw_x8": raw_callee,
                "source_pac_modifier_x16": row.get("x16"),
                "source_receiver_x0": row.get("x0"),
                "source_tpidr_el1": thread,
                "source_time": row["time"],
                "parent_dispatch_source": source,
            }
            try:
                candidate_bp = _register(
                    process.GetTarget().GetDebugger(), nested,
                    "IOMFB_A408_DISPATCH_TARGET_CANDIDATE_HIT", "kernel")
                row["nested_indirect_candidate_bp_id"] = candidate_bp.GetID()
            except Exception as error:
                DYNAMIC_DISPATCH_TARGETS.pop(nested, None)
                row["nested_indirect_candidate_arm_error"] = repr(error)
    if label == "SERVER_OGL_RENDER_RETURN":
        OGL_INFLIGHT.pop(thread, None)
    # At fb_swap_set_layer x1 is the framebuffer/layer reference. Preserve a
    # small read-only header when it is readable so successive null updates
    # can be distinguished by object state, without guessing pixel contents.
    if label == "FB_SWAP_SET_LAYER_ENTRY" and row.get("x1"):
        words = []
        for offset in range(0, 0x100, 8):
            words.append(surface_capture._u64(process, row["x1"] + offset))
        row["x1_words_0x00_0xf8"] = words
        RECENT_LAYER_HEADERS[thread] = {
            "time": row["time"], "x1": row["x1"],
            "word0": words[0] if words else None,
        }
        if (SURFACE_LINEAGE_TRACE and
                SURFACE_LINEAGE_HITS < MAX_SURFACE_LINEAGE_HITS):
            SURFACE_LINEAGE_HITS += 1
            lineage = _descriptor_surface_lineage(
                process, row["x1"], words)
            row["surface_allocation_descriptor_lineage"] = lineage
            print("POSTHOME_SURFACE_DESCRIPTOR_LINEAGE " +
                  json.dumps({"hit": SURFACE_LINEAGE_HITS,
                              "time": row["time"],
                              "process": name, "thread": thread,
                              **lineage}, sort_keys=True), flush=True)
    if label == "ALLOCATE_IOSURFACE_RETURN":
        surface = row.get("x0")
        marker_open = marker_is_open(SURFACE_START_FILE)
        if (surface and surface > 0x10000 and marker_open and
                ALLOCATED_SURFACE_SAMPLES < MAX_ALLOCATED_SURFACE_SAMPLES):
            ALLOCATED_SURFACE_SAMPLES += 1
            try:
                metadata = surface_capture._surface_metadata(process, surface)
                capture = surface_capture._capture_surface(
                    process, surface, metadata, ALLOCATED_SURFACE_SAMPLES)
                metadata["surface_header_qwords_0x00_0x78"] = [
                    surface_capture._u64(process, surface + offset)
                    for offset in range(0, 0x80, 8)]
                metadata["surface_object_header"] = _read_header(
                    process, surface)
                internal = metadata.get("internal")
                if isinstance(internal, int) and internal > 0x10000:
                    metadata["internal_header_qwords_0x00_0xf8"] = [
                        surface_capture._u64(process, internal + offset)
                        for offset in range(0, 0x100, 8)]
                    metadata["internal_object_header"] = _read_header(
                        process, internal)
            except Exception as error:
                metadata = {"metadata_error": repr(error)}
                capture = {"capture_error": repr(error)}
            allocation = {
                "surface": surface, "time": row["time"],
                "process": name, "thread": thread,
                "metadata": metadata, "capture": capture,
                "sample_semantics": (
                    "pointer returned at the create_surface allocation site; "
                    "candidate until its identity is matched at fb_swap"),
            }
            ALLOCATED_SURFACES.append(allocation)
            # Keep the in-memory join key small; full capture metadata is
            # already persisted in the event and raw-pixel record.
            ALLOCATED_SURFACES[:] = ALLOCATED_SURFACES[-8:]
            print("POSTHOME_ALLOCATED_IOSURFACE " +
                  json.dumps(allocation, sort_keys=True), flush=True)
    # fb_swap_set_layer receives the IOSurface in x3 (confirmed by the
    # first-surface call-site analysis). The later 0x184472488 boundary is
    # after loading *(x24+0x18) as an Objective-C receiver and branching to
    # objc_msgSend$sender. The selector has no colon; x2 is not established as
    # a formal surface argument there. Keep that site as a sender-call
    # observation, not a handoff claim. Surface sampling happens at entry.
    if (label == "FB_SWAP_SET_LAYER_ENTRY" and SURFACE_START_FILE and
            not marker_is_open(SURFACE_START_FILE)):
        GATED_SURFACE_SAMPLES += 1
        if GATED_SURFACE_SAMPLES in (1, 16, 128):
            print("POSTHOME_SURFACE_CAPTURE_GATED count=%d marker=%s" %
                  (GATED_SURFACE_SAMPLES, SURFACE_START_FILE), flush=True)
    if (label == "FB_SWAP_SET_LAYER_ENTRY" and
            marker_is_open(SURFACE_START_FILE)):
        iosurface = row.get("x3")
        row["x3_matches_allocated_surface"] = bool(
            iosurface and any(item["surface"] == iosurface
                              for item in ALLOCATED_SURFACES))
        status = classify_iosurface_x3(
            iosurface, SURFACE_SAMPLES < MAX_SURFACE_SAMPLES)
        row["iosurface_sample_status"] = status
        SURFACE_X3_STATUS_COUNTS[status] += 1
        print("POSTHOME_IOSURFACE_ARGUMENT_STATUS " + json.dumps({
            "time": row["time"],
            "process": name,
            "thread": thread,
            "layer_x1": row.get("x1"),
            "surface_x3": iosurface,
            "status": status,
            "cumulative_counts": SURFACE_X3_STATUS_COUNTS,
        }, sort_keys=True), flush=True)
        if status == "captured-or-read-attempted":
            SURFACE_SAMPLES += 1
            try:
                metadata = surface_capture._surface_metadata(process, iosurface)
                capture = surface_capture._capture_surface(
                    process, iosurface, metadata, SURFACE_SAMPLES)
            except Exception as error:
                metadata = {"metadata_error": repr(error)}
                capture = {"capture_error": repr(error)}
            surface_row = {
                "label": "POSTHOME_IOSURFACE_PIXELS",
                "sample": SURFACE_SAMPLES,
                "time": time.time(),
                "process": name,
                "thread": thread,
                "surface": iosurface,
                "surface_metadata": metadata,
                "surface_capture": capture,
            }
            surface_capture._write_event(
                "posthome-iosurface-%02d.json" % SURFACE_SAMPLES,
                surface_row)
            print("POSTHOME_IOSURFACE_JSON " +
                  json.dumps(surface_row, sort_keys=True), flush=True)
    print("POSTHOME_PRESENT_JSON " + json.dumps(row, sort_keys=True),
          flush=True)
    if label == "FB_SWAP_SENDER_CALL":
        object_ptr = row.get("x0")
        dispatch_slot = (surface_capture._u64(process, object_ptr + 0xB38)
                         if object_ptr else None)
        object_header = ([surface_capture._u64(process, object_ptr + offset)
                          for offset in (0, 8, 0x10, 0x18, 0x20, 0x28,
                                         0x30, 0x38, 0xB30, 0xB38, 0xB40)]
                         if object_ptr else [])
        previous = RECENT_LAYER_HEADERS.get(thread)
        row["sender_receiver_slot_0xb38"] = dispatch_slot
        # The slot's dynamic target was not identified by the prior numeric
        # pointer comparison. Ask LLDB to resolve it against this process's
        # loaded images; keep unresolved values as raw candidates.
        row["sender_receiver_slot_0xb38_resolution"] = \
            _resolve_load_address(process, dispatch_slot)
        row["sender_receiver_header_offsets"] = [
            0, 8, 0x10, 0x18, 0x20, 0x28, 0x30, 0x38, 0xB30, 0xB38,
            0xB40]
        row["sender_receiver_header_qwords"] = object_header
        row["sender_x0_matches_recent_set_layer_x1_word0"] = bool(
            previous and object_ptr and previous.get("word0") == object_ptr)
        row["recent_set_layer_header"] = previous
        print("POSTHOME_SENDER_RECEIVER_OBJECT " + json.dumps({
            "time": row["time"], "thread": thread,
            "x0": object_ptr, "x2": row.get("x2"),
            "dispatch_slot_0xb38": dispatch_slot,
            "dispatch_slot_0xb38_resolution":
                row["sender_receiver_slot_0xb38_resolution"],
            "object_header_offsets": row["sender_receiver_header_offsets"],
            "object_header_qwords": object_header,
            "x0_matches_recent_set_layer_x1_word0":
                row["sender_x0_matches_recent_set_layer_x1_word0"],
            "recent_set_layer_header": previous,
        }, sort_keys=True), flush=True)
        # A render call with no observed return may still be active on another
        # backboardd thread while layer-state handoffs continue. Sample each
        # in-flight render's live register/frame chain once, from this natural
        # stop point; do not infer a stall from the missing return marker alone.
        for render_thread, render in list(OGL_INFLIGHT.items()):
            if render_thread in OGL_STACK_SAMPLES:
                continue
            stack_rows = []
            for index in range(process.GetNumThreads()):
                candidate = process.GetThreadAtIndex(index)
                if not candidate.IsValid() or candidate.GetNumFrames() < 1:
                    continue
                top = candidate.GetFrameAtIndex(0)
                candidate_thread = bks._reg(top, "tpidr_el1")
                frames = []
                for frame_index in range(min(candidate.GetNumFrames(), 16)):
                    candidate_frame = candidate.GetFrameAtIndex(frame_index)
                    frames.append({
                        "pc": bks._reg(candidate_frame, "pc"),
                        "lr": bks._reg(candidate_frame, "lr"),
                        "sp": bks._reg(candidate_frame, "sp"),
                        "x29": bks._reg(candidate_frame, "x29"),
                    })
                stack_rows.append({
                    "thread_id": candidate.GetThreadID(),
                    "tpidr_el1": candidate_thread,
                    "frames": frames,
                })
            sample = {
                "label": "POSTHOME_OGL_INFLIGHT_THREAD_SAMPLE",
                "time": time.time(), "process": name,
                "render_thread": render_thread,
                "render_entry": render,
                "trigger_thread": thread,
                "threads": stack_rows,
            }
            surface_capture._write_event(
                "posthome-ogl-inflight-stack.json", sample)
            print("POSTHOME_OGL_INFLIGHT_STACK " +
                  json.dumps(sample, sort_keys=True), flush=True)
            OGL_STACK_SAMPLES.add(render_thread)
    if label == "CA_COMMIT_ENTRY":
        _arm_return(frame, "CA_COMMIT_RETURN", expected, thread)
    elif label == "SERVER_RENDER_UPDATE_ENTRY":
        _arm_return(frame, "SERVER_RENDER_UPDATE_RETURN", expected, thread)
    elif label == "DISPLAY_RENDER_DISPLAY_ENTRY":
        _arm_return(frame, "DISPLAY_RENDER_DISPLAY_RETURN", expected, thread)
    elif label == "SERVER_OGL_RENDER":
        OGL_INFLIGHT[thread] = {
            "time": row["time"], "pc": row["pc"], "lr": row["lr"],
            "sp": row["sp"], "surface_candidate_x24": row.get("x24"),
        }
        _arm_return(frame, "SERVER_OGL_RENDER_RETURN", expected, thread)
    elif label == "FINISH_UPDATE_ENTRY":
        _arm_return(frame, "FINISH_UPDATE_RETURN", expected, thread)
    elif label == "CREATE_SURFACE_ENTRY":
        _arm_return(frame, "CREATE_SURFACE_RETURN", expected, thread)
    elif label.endswith("_RETURN") and thread_filter and label != "CA_COMMIT_RETURN":
        bp.SetEnabled(False)
    if label == "IOMFB_GENERIC_MAP":
        _arm_context_return(
            frame, "IOMFB_GENERIC_MAP_RETURN", expected,
            "IOMFB_GENERIC_MAP")
    if label == "IOMFB_A408_BUILD":
        print("POSTHOME_A408_DISPATCH_BOUNDARY reached; dynamic target logged; "
              "target return will be paired from candidate-hit LR/SP; "
              "builder/RPC/presentation still unproved", flush=True)
    if strict_return is not None:
        strict_key = (
            strict_return.get("label"),
            strict_return.get("expected_process"),
            strict_return.get("expected_thread"),
            strict_return.get("entry_sp"),
            strict_return.get("return_pc"))
        RETURN_CONTEXTS.pop(bp.GetID(), None)
        STRICT_RETURN_ARMED.discard(strict_key)
        bp.SetEnabled(False)
    return False


def install(debugger, slide, install_home=True):
    global SURFACE_SAMPLES, START_FILE, GATED_SURFACE_SAMPLES
    global SURFACE_START_FILE, ALLOCATED_SURFACE_SAMPLES
    global SURFACE_LINEAGE_TRACE, SURFACE_LINEAGE_HITS
    global DYNAMIC_DISPATCH_TARGETS, CA_COMMIT_PENDING, CA_MSG_START_FILE
    interpreter = debugger.GetCommandInterpreter()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("command script import %s" %
                              Path(home.__file__).resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("failed home import: %s" % result.GetError())
    if install_home:
        home.install(debugger, slide)
    META.clear()
    ARMED.clear()
    COUNTS.clear()
    CA_COMMIT_PENDING = {}
    SURFACE_SAMPLES = 0
    GATED_SURFACE_SAMPLES = 0
    for status in SURFACE_X3_STATUS_COUNTS:
        SURFACE_X3_STATUS_COUNTS[status] = 0
    SURFACE_START_FILE = os.environ.get(
        "DVM_POSTHOME_PRESENT_SURFACE_START_FILE", "")
    CA_MSG_START_FILE = os.environ.get(
        "DVM_POSTHOME_CA_MSG_START_FILE", SURFACE_START_FILE)
    ALLOCATED_SURFACES.clear()
    ALLOCATED_SURFACE_SAMPLES = 0
    SURFACE_LINEAGE_TRACE = os.environ.get(
        "DVM_POSTHOME_SURFACE_LINEAGE_TRACE", "0") == "1"
    SURFACE_LINEAGE_HITS = 0
    DYNAMIC_DISPATCH_TARGETS.clear()
    RETURN_CONTEXTS.clear()
    STRICT_RETURN_ARMED.clear()
    RECENT_LAYER_HEADERS.clear()
    START_FILE = os.environ.get("DVM_POSTHOME_PRESENT_START_FILE", "")
    surface_capture.SLIDE[0] = int(slide)
    surface_capture.EVENT_DIR = os.environ.get(
        "DVM_PROBE_EVENT_DIR", "/tmp/dvm/posthome-surface-events")
    armed = []
    for static, label, expected in USER_SITES:
        bp = _register(debugger, int(slide) + static, label, expected)
        armed.append({"id": bp.GetID(), "label": label,
                      "address": "0x%x" % (int(slide) + static),
                      "locations": bp.GetNumLocations(),
                      "callback": "dm_posthome_present_callbacks.on_hit"})
    for static, label in KERNEL_SITES:
        bp = _register(debugger, KERNEL_SLIDE + static, label, "kernel")
        armed.append({"id": bp.GetID(), "label": label,
                      "address": "0x%x" % (KERNEL_SLIDE + static),
                      "locations": bp.GetNumLocations(),
                      "callback": "dm_posthome_present_callbacks.on_hit"})
    print("POSTHOME_PRESENT_READY " + json.dumps(
        {"sites": len(armed), "armed": armed, "slide": "0x%x" % int(slide),
         "surface_capture_start_file": START_FILE,
         "surface_capture_gated": bool(SURFACE_START_FILE),
         "surface_capture_accepted_transition_file": SURFACE_START_FILE,
         "ca_message_capture_start_file": CA_MSG_START_FILE,
         "ca_message_capture_gated": bool(CA_MSG_START_FILE),
         "ca_render_message_limit": MAX_CA_RENDER_MACH_MSG_SAMPLES,
         "kernel_after_accepted_gated_labels": sorted(
             KERNEL_ACCEPTED_GATE_LABELS),
         "allocated_surface_capture_limit": MAX_ALLOCATED_SURFACE_SAMPLES},
        sort_keys=True), flush=True)

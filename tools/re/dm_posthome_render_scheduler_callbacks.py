"""Read-only scheduler and render_for_time decision trace after shell return.

This narrow observer samples QuartzCore display scheduler entry, Server
render_for_time, and its short/full branch. It performs no writes to guest
memory and logs sparse power-of-two samples so a long post-home run does not
produce an unbounded callback log.
"""

import json
import os
import time
from pathlib import Path

import lldb

import bks_checkin_callbacks as bks
import dm_post_shell_home_min_callbacks as home


SITES = (
    (0x1843EB030, "SCHED_UPDATE_A_ENTRY"),
    (0x18477E2D8, "SCHED_UPDATE_B_ENTRY"),
    (0x18440FC74, "RENDER_FOR_TIME_ENTRY"),
    (0x18440FDA0, "RFT_MAIN_AFTER_390"),
    (0x1844100E8, "RFT_TIME_DECISION"),
    (0x184410158, "RFT_SHORT_SKIP"),
    (0x18441015C, "RFT_FULL_PATH"),
    (0x184410280, "RFT_AFTER_V178"),
    # Branch target when the +0x178 method returns nonzero. This is the
    # precise early exit seen in P219; keep it distinct from a later V170
    # ignore/update decision.
    (0x184411B4C, "RFT_FULL_SKIP_V178"),
    (0x184410910, "RFT_AFTER_V248"),
    (0x184411560, "RFT_AFTER_V218"),
    (0x1844115B4, "RFT_AFTER_VF8"),
    # Immediately before the virtual IOMFBDisplay::ignore_update_p call.
    # At this call site x0 is the display receiver and x1 is the Update; do
    # not try to recover the receiver from x19 at the post-call site, where
    # x19 is a stack-like local on this build.
    (0x1844115E0, "RFT_BEFORE_V170"),
    (0x1844115E4, "RFT_AFTER_V170"),
    (0x184411630, "RFT_AFTER_V1B8"),
    (0x18441168C, "RFT_CMP648"),
    (0x184411694, "RFT_BEQ648"),
    (0x184411698, "RFT_NEQ648"),
    (0x184411D00, "RFT_ALT_11D00"),
    (0x184411710, "RFT_GFLAG"),
    (0x1844117BC, "RFT_20C"),
    # Branch at +0x7c0 chooses whether to invoke ignore_update_p. Capture
    # the actual byte loaded from display+0x20c and both sides of the branch.
    (0x1844117C0, "RFT_20C_TBNZ"),
    (0x1844117C4, "RFT_BEFORE_IGNORE_UPDATE"),
    (0x1844117F0, "RFT_IGNORE_UPDATE_RETURN"),
    (0x1844117F4, "RFT_AFTER_IGNORE_UPDATE_TRUE"),
    (0x184411818, "RFT_20C_BRANCH_JOIN"),
    (0x184411840, "RFT_AFTER_1F8"),
    (0x184411878, "RFT_AFTER_1D0"),
    (0x184413810, "RFT_ALT_MERGE_13810"),
    (0x1844134CC, "RFT_CALL_OUTER"),
    # Existing GATE237 writer sites. These are observed only after the
    # post-shell module arms; object equality to the V170 display is required
    # before attributing any later state transition to its predicate.
    (0x1845BD954, "DISPLAY_POWER_TARGET_STATE"),
    (0x1845BDAA4, "DISPLAY_POWER_STORE_STATE5"),
    (0x1846128F0, "DISPLAY_SET_POWER_STATE_LOCKED"),
)
META = {}
COUNTS = {}
ACTIVE_DISPLAY_BY_PID = {}
SLIDE = 0
OUT = None
SAMPLE_LIMIT = 64


def _reg(frame, name):
    try:
        value = frame.FindRegister(name)
        return value.GetValueAsUnsigned() if value.IsValid() else None
    except Exception:
        return None


def _read(process, address, size):
    if not address or size <= 0:
        return b""
    try:
        error = lldb.SBError()
        data = process.ReadMemory(address, size, error)
        return data if error.Success() else b""
    except Exception:
        return b""


def _u64(process, address):
    data = _read(process, address, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else None


def _u32(process, address):
    data = _read(process, address, 4)
    return int.from_bytes(data, "little") if len(data) == 4 else None


def _u8(process, address):
    data = _read(process, address, 1)
    return data[0] if data else None


def _emit(row):
    row["time"] = time.time()
    line = json.dumps(row, sort_keys=True)
    try:
        OUT.parent.mkdir(parents=True, exist_ok=True)
        with OUT.open("a") as stream:
            stream.write(line + "\n")
            stream.flush()
    except Exception as error:
        row["log_error"] = repr(error)
    print("POSTHOME_RENDER_SCHEDULER " + line, flush=True)


def on_hit(frame, bp_loc, _context):
    try:
        bp = bp_loc.GetBreakpoint()
        label = META.get(bp.GetID())
        if not label:
            return False
        process = frame.GetThread().GetProcess()
        # Scheduler and server sites are shared-cache backboardd functions.
        progname = bks._progname(process)
        if progname != "backboardd":
            return False
        # Prefer the initialized home/lock display, but allow an explicitly
        # marked post-input capture when SpringBoard never returns a home
        # entity. This distinguishes a dead home-entity gate from absent
        # compositor scheduling without admitting ordinary pre-input boot
        # traffic into the sample.
        post_input_marker = Path(os.environ.get(
            "DVM_POSTHOME_PRESENT_START_FILE", ""))
        post_input_started = bool(str(post_input_marker) and
                                  post_input_marker.is_file())
        if not home.HOME_RETURNED and not post_input_started:
            return False
        count = COUNTS.get(label, 0) + 1
        COUNTS[label] = count
        if count > SAMPLE_LIMIT and count & (count - 1):
            return False
        row = {
            "label": label, "count": count,
            "capture_gate": ("home-entity" if home.HOME_RETURNED
                             else "post-input-marker"),
            "pid": process.GetProcessID(),
            "thread_id": frame.GetThread().GetThreadID(),
            "tpidr_el1": _reg(frame, "tpidr_el1"),
            "pc": _reg(frame, "pc"), "lr": _reg(frame, "lr"),
            "sp": _reg(frame, "sp"),
            "registers": {"x%d" % i: _reg(frame, "x%d" % i)
                          for i in tuple(range(9)) + tuple(range(19, 29))},
        }
        if label == "RENDER_FOR_TIME_ENTRY":
            server = row["registers"].get("x0")
            context = _u64(process, server + 0x60) if server else None
            row["server"] = server
            row["server_390"] = _u8(process, server + 0x390) if server else None
            row["server_398"] = _u8(process, server + 0x398) if server else None
            row["context"] = context
            row["context_3b9"] = _u8(process, context + 0x3B9) if context else None
            row["context_2b8"] = _u64(process, context + 0x2B8) if context else None
        elif label == "RFT_BEFORE_V170":
            # Static call-site/ABI evidence from the render_gate_callbacks
            # trace: x0 is the IOMFBDisplay receiver; x1 is the Update passed
            # to ignore_update_p. Capture the actual predicate inputs before
            # the indirect call, including the pointed-to state bytes.
            display, update = row["registers"].get("x0"), row["registers"].get("x1")
            vtable = _u64(process, display) if display else None
            slot = _u64(process, vtable + 0x170) if vtable else None
            mode = _u64(process, display + 0x2B8) if display else None
            state = _u64(process, display + 0x330) if display else None
            flags = _u64(process, update + 0x140) if update else None
            state4 = _u8(process, state + 4) if state else None
            state5 = _u8(process, state + 5) if state else None
            pending = _u8(process, display + 0x7558) if display else None
            ignore_state = _u8(process, display + 0x7595) if display else None
            if display:
                ACTIVE_DISPLAY_BY_PID[process.GetProcessID()] = display
            row.update({
                "display_x0": display,
                "display_id_18": _u32(process, display + 0x18) if display else None,
                "update_x1": update,
                "display_vtable": vtable,
                "ignore_update_p_slot": slot,
                "display_mode_2b8": mode,
                "display_mode_byte_2bb": _u8(process, display + 0x2BB) if display else None,
                "update_flags_140": flags,
                "display_state_330": state,
                "state330_4": state4,
                "state330_5": state5,
                "display_7558": pending,
                "display_7595": ignore_state,
                "update_mask_0a": (flags & 0xA) if flags is not None else None,
            })
        elif label in ("DISPLAY_POWER_TARGET_STATE",
                       "DISPLAY_POWER_STORE_STATE5"):
            display = row["registers"].get("x20")
            active_display = ACTIVE_DISPLAY_BY_PID.get(process.GetProcessID())
            state = _u64(process, display + 0x330) if display else None
            row.update({
                "display_x20": display,
                "display_id": _u32(process, display + 0x18) if display else None,
                "active_display_x0": active_display,
                "matches_active_display": display == active_display,
                "mode_2b8": _u64(process, display + 0x2B8) if display else None,
                "power_assertions": _u32(process, display + 0x68E8 + 0x978) if display else None,
                "raw_power_state": _u8(process, display + 0x68E8 + 0x97C) if display else None,
                "target_state_w24": _reg(frame, "w24"),
                "state_330": state,
                "state330_4": _u8(process, state + 4) if state else None,
                "state330_5": _u8(process, state + 5) if state else None,
            })
        elif label == "DISPLAY_SET_POWER_STATE_LOCKED":
            display = row["registers"].get("x0")
            active_display = ACTIVE_DISPLAY_BY_PID.get(process.GetProcessID())
            state = _u64(process, display + 0x330) if display else None
            row.update({
                "display_x0": display,
                "display_id": _u32(process, display + 0x18) if display else None,
                "active_display_x0": active_display,
                "matches_active_display": display == active_display,
                "mode_2b8": _u64(process, display + 0x2B8) if display else None,
                "power_state_w1": _reg(frame, "w1"),
                "state_argument_w2": _reg(frame, "w2"),
                "state_330": state,
                "state330_4": _u8(process, state + 4) if state else None,
                "state330_5": _u8(process, state + 5) if state else None,
            })
        elif label == "RFT_TIME_DECISION":
            regs = row["registers"]
            x19, server = regs.get("x19"), regs.get("x25")
            slot = _u64(process, x19 + 0x1C8) if x19 else None
            bit17_count = regs.get("x26")
            temp_list = regs.get("x28")
            entries = _u64(process, server + 0x70) if server else None
            entry_count = _u64(process, server + 0x80) if server else None
            row.update({
                "decision_slot_1c8": slot,
                "bit17_count_x26": bit17_count,
                "temp_list_x28": temp_list,
                "server_x25": server,
                "display_list": entries,
                "display_list_count": entry_count,
                "full_path_predicate_slot_ne_bit17":
                    slot != bit17_count if slot is not None and bit17_count is not None else None,
                "temp_list_nonnull": bool(temp_list) if temp_list is not None else None,
            })
            if entries and entry_count:
                row["display_entries"] = []
                for index in range(min(int(entry_count), 8)):
                    entry = _u64(process, entries + index * 16)
                    flags = _u32(process, entry + 0xC) if entry else None
                    row["display_entries"].append({
                        "index": index, "entry": entry, "flags_0c": flags,
                        "bit17": ((flags >> 17) & 1) if flags is not None else None,
                    })
        elif label in ("RFT_AFTER_V178", "RFT_FULL_SKIP_V178"):
            # x20 remains the +0x178 method receiver across the call/branch.
            # x19 is stack-like here and must not be treated as a display.
            display = row["registers"].get("x20")
            vtable = _u64(process, display) if display else None
            state = _u64(process, display + 0x330) if display else None
            state_bytes = _read(process, state, 8) if state else b""
            mode = _u64(process, display + 0x2B8) if display else None
            row.update({
                "return_w0": _reg(frame, "w0"),
                "receiver_x20": display,
                "receiver_display_id_18": _u32(process, display + 0x18)
                    if display else None,
                "receiver_vtable": vtable,
                "receiver_vtable_slot_178": _u64(process, vtable + 0x178)
                    if vtable else None,
                "receiver_state_pointer_330": state,
                "receiver_state330_hex": state_bytes.hex(),
                "receiver_state330_bytes": list(state_bytes),
                "receiver_mode_2b8": mode,
                "receiver_mode_byte_2bb": _u8(process, display + 0x2BB)
                    if display else None,
            })
        elif label in ("RFT_AFTER_V248", "RFT_AFTER_V218", "RFT_AFTER_VF8",
                       "RFT_AFTER_V170", "RFT_AFTER_V1B8"):
            row["return_w0"] = _reg(frame, "w0")
            row["receiver_x20"] = _reg(frame, "x20")
            row["display_x19"] = _reg(frame, "x19")
            row["display_state_330"] = (
                _read(process, row["display_x19"] + 0x330, 8).hex()
                if row["display_x19"] else None)
            row["display_2b8"] = (
                _u64(process, row["display_x19"] + 0x2B8)
                if row["display_x19"] else None)
            row["display_648"] = (
                _u64(process, row["display_x19"] + 0x648)
                if row["display_x19"] else None)
            row["display_40"] = (
                _u64(process, row["display_x19"] + 0x40)
                if row["display_x19"] else None)
        elif label in ("RFT_CMP648", "RFT_BEQ648", "RFT_NEQ648"):
            x8, x9, x19 = (_reg(frame, "x8"), _reg(frame, "x9"),
                           _reg(frame, "x19"))
            row.update({
                "cmp_x8": x8, "cmp_x9": x9,
                "cmp_equal": x8 == x9 if x8 is not None and x9 is not None else None,
                "display_x19": x19,
                "display_648": _u64(process, x19 + 0x648) if x19 else None,
                "display_40": _u64(process, x19 + 0x40) if x19 else None,
                "display_640": _u64(process, x19 + 0x640) if x19 else None,
                "display_650": _u64(process, x19 + 0x650) if x19 else None,
            })
        elif label in ("RFT_ALT_11D00", "RFT_GFLAG", "RFT_20C",
                       "RFT_20C_TBNZ", "RFT_BEFORE_IGNORE_UPDATE",
                       "RFT_IGNORE_UPDATE_RETURN",
                       "RFT_AFTER_IGNORE_UPDATE_TRUE",
                       "RFT_20C_BRANCH_JOIN", "RFT_AFTER_1F8",
                       "RFT_AFTER_1D0", "RFT_ALT_MERGE_13810"):
            x19 = _reg(frame, "x19")
            flags_address = x19 + 0x20C if x19 else None
            row.update({"w8": _reg(frame, "w8"),
                        "x19": x19,
                        "x20": _reg(frame, "x20"),
                        "x25": _reg(frame, "x25"),
                        "flags_address": flags_address,
                        "flags_byte_20c": _u8(process, flags_address),
                        "flags_word_20c": _u32(process, flags_address),
                        "w0": _reg(frame, "w0"),
                        "w1": _reg(frame, "w1")})
        _emit(row)
    except Exception as error:
        _emit({"label": "RENDER_SCHEDULER_CALLBACK_ERROR",
               "error": repr(error), "pc": _reg(frame, "pc")})
    return False


def install(debugger, slide):
    global SLIDE, OUT
    SLIDE = int(slide)
    bks.SLIDE[0] = SLIDE
    event_dir = Path(os.environ.get(
        "DVM_PROBE_EVENT_DIR", "/tmp/dvm/posthome-render-scheduler-events"))
    OUT = event_dir / "render-scheduler.jsonl"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    META.clear()
    COUNTS.clear()
    ACTIVE_DISPLAY_BY_PID.clear()
    target = debugger.GetSelectedTarget()
    armed = []
    for offset, label in SITES:
        address = SLIDE + offset
        bp = target.BreakpointCreateByAddress(address)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("could not resolve %s at 0x%x" % (label, address))
        META[bp.GetID()] = label
        bp.SetScriptCallbackFunction(__name__ + ".on_hit")
        bp.SetAutoContinue(True)
        proof = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command list %d" % bp.GetID(), proof)
        text = proof.GetOutput() or proof.GetError() or ""
        if not proof.Succeeded() or __name__ + ".on_hit" not in text:
            bp.SetEnabled(False)
            raise RuntimeError("callback proof failed for %s: %s" % (label, text))
        armed.append({"id": bp.GetID(), "label": label,
                      "address": "0x%x" % address,
                      "locations": bp.GetNumLocations()})
    _emit({"label": "POSTHOME_RENDER_SCHEDULER_READY",
           "slide": "0x%x" % SLIDE, "armed": armed,
           "process_filter": "backboardd",
           "home_gate": "dm_post_shell_home_min_callbacks.HOME_RETURNED",
           "sample_policy": "first 64 then powers of two",
           "cmp648_registers": "x8/x9, read-only display+0x648/+0x40",
           "post_v170_sites": ["+0x18441168c", "+0x184411694",
                               "+0x184411698", "+0x184411d00",
                               "+0x184411710", "+0x1844117bc",
                               "+0x1844117c0", "+0x1844117c4",
                               "+0x1844117f0", "+0x1844117f4",
                               "+0x184411818", "+0x184411840",
                               "+0x184411878", "+0x184413810"],
           "log": str(OUT)})

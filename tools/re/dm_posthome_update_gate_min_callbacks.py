"""Read-only Update/V170 and immediate post-V170 branch trace.

This remains much narrower than the broad scheduler/renderer profile. It
samples Update flag construction, the V170 decision, the natural delta
compare and its branch successors, then the direct outer-update call sites.
Samples are sparse, marker-gated when requested, and never write guest state.
"""

import json
import os
import traceback
import time
from pathlib import Path

import lldb

import bks_checkin_callbacks as bks
import dm_post_shell_home_min_callbacks as home


SITES = (
    (0x184440E1C, "UPDATE_FLAGS_MASK_CHECK"),
    (0x1844115E0, "RFT_BEFORE_V170"),
    (0x1844115E4, "RFT_AFTER_V170"),
    # Follow the natural post-V170 path through the delta compare and both
    # exits. These are observation-only sites; no registers or guest memory
    # are modified.
    (0x1844115EC, "RFT_POST_V170_TIME"),
    (0x184411630, "RFT_POST_V170_V1B8"),
    (0x18441168C, "RFT_POST_V170_CMP648"),
    (0x184411694, "RFT_POST_V170_BEQ648"),
    (0x184411698, "RFT_POST_V170_NEQ648"),
    (0x184411D00, "RFT_POST_V170_ALT_11D00"),
    (0x184411710, "RFT_POST_V170_GFLAG"),
    (0x1844117C0, "RFT_POST_V170_20C_BRANCH"),
    (0x184411818, "RFT_POST_V170_20C_BIT3_PATH"),
    (0x18441182C, "RFT_POST_V170_CALL_1F8"),
    (0x184411840, "RFT_POST_V170_CALL_1F8_RETURN"),
    (0x184414DBC, "RFT_POST_V170_GFLAG_BIT_SET_PATH"),
    (0x184413810, "RFT_ALT_MERGE_13810"),
    (0x1844134CC, "CALL_OUTER_A"),
    # Verified by dyld symbolication: this is
    # CA::OGL::Renderer::render(Update const*, unsigned long, CASharedEvent*).
    (0x18444B954, "OGL_RENDER_ENTRY"),
    (0x184412000, "RFT_WP_12000"),
    (0x184413000, "RFT_WP_13000"),
    (0x184414D54, "RFT_FULL_EXIT_14D54"),
    (0x184414DA8, "RFT_FULL_EPILOG_14DA8"),
)
META = {}
COUNTS = {}
V170_UPDATES = {}
V170_THREAD_UPDATES = {}
LAST_TRANSITION_SAMPLE = {}
OUT = None
SAMPLE_LIMIT = 64
TRANSITION_SAMPLE_INTERVAL = 60.0
AFTER_LOCK_TRANSITION = os.environ.get(
    "DVM_POSTHOME_UPDATE_GATE_AFTER_LOCK_TRANSITION", "0") == "1"
TRANSITION_MARKER = Path(os.environ.get(
    "DVM_LOCK_GESTURE_TRANSITION_MARKER", ""))


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


def _uint(process, address, size):
    data = _read(process, address, size)
    return int.from_bytes(data, "little") if len(data) == size else None


def _sample(count):
    return count <= SAMPLE_LIMIT or count & (count - 1) == 0


def _path_key(frame, process):
    return (process.GetProcessID(), _reg(frame, "tpidr_el1"),
            _reg(frame, "sp"))


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
    print("POSTHOME_UPDATE_GATE_MIN " + line, flush=True)


def on_hit(frame, bp_loc, _context):
    try:
        label = META.get(bp_loc.GetBreakpoint().GetID())
        if not label:
            return False
        process = frame.GetThread().GetProcess()
        if bks._progname(process) != "backboardd":
            return False
        start_file = Path(os.environ.get("DVM_POSTHOME_PRESENT_START_FILE", ""))
        if not home.HOME_RETURNED and not (str(start_file) and start_file.is_file()):
            return False
        transition = None
        if AFTER_LOCK_TRANSITION:
            if not str(TRANSITION_MARKER) or not TRANSITION_MARKER.is_file():
                return False
            try:
                transition = json.loads(TRANSITION_MARKER.read_text())
            except (OSError, json.JSONDecodeError):
                return False
            if (transition.get("for_user_gesture") != 1 or
                    transition.get("gesture_sequence_confirmed") is False or
                    (isinstance(transition.get("dismissal_gesture"), dict) and
                     transition["dismissal_gesture"].get("confirmed") is False)):
                return False

        count = COUNTS.get(label, 0) + 1
        COUNTS[label] = count
        now = time.time()
        sample = _sample(count)
        time_bucket_sample = False
        if (transition and count > SAMPLE_LIMIT):
            bucket_key = (process.GetProcessID(),
                          _reg(frame, "tpidr_el1"), label)
            previous = LAST_TRANSITION_SAMPLE.get(bucket_key)
            if previous is None:
                LAST_TRANSITION_SAMPLE[bucket_key] = now
            elif now - previous >= TRANSITION_SAMPLE_INTERVAL:
                time_bucket_sample = True
                LAST_TRANSITION_SAMPLE[bucket_key] = now
        elif transition and count <= SAMPLE_LIMIT:
            bucket_key = (process.GetProcessID(),
                          _reg(frame, "tpidr_el1"), label)
            LAST_TRANSITION_SAMPLE.setdefault(bucket_key, now)
        # Preserve every V170 outcome, even when this row is not emitted.
        # Downstream branch callbacks are sampled much less often; pairing
        # them against only emitted V170 rows can silently reuse a result from
        # hundreds of seconds earlier.
        if label == "RFT_AFTER_V170":
            update = _reg(frame, "x1")
            flags = _uint(process, update + 0x140, 8) if update else None
            key = _path_key(frame, process)
            value = {
                "update": update,
                "flags_140": flags,
                "return_w0": _reg(frame, "w0"),
                "time": now,
            }
            V170_UPDATES[key] = value
            thread_key = (process.GetProcessID(), _reg(frame, "tpidr_el1"))
            V170_THREAD_UPDATES[thread_key] = value

        if not sample and not time_bucket_sample:
            return False

        sp = _reg(frame, "sp")
        row = {
            "label": label,
            "count": count,
            "pid": process.GetProcessID(),
            "thread_id": frame.GetThread().GetThreadID(),
            "tpidr_el1": _reg(frame, "tpidr_el1"),
            "pc": _reg(frame, "pc"),
            "lr": _reg(frame, "lr"),
            "sp": sp,
            "time": now,
            "x0": _reg(frame, "x0"),
            "x1": _reg(frame, "x1"),
            "x8": _reg(frame, "x8"),
            "x19": _reg(frame, "x19"),
            "x20": _reg(frame, "x20"),
            "x21": _reg(frame, "x21"),
            "x23": _reg(frame, "x23"),
            "x24": _reg(frame, "x24"),
            "x25": _reg(frame, "x25"),
            "x27": _reg(frame, "x27"),
            "w0": _reg(frame, "w0"),
            "w8": _reg(frame, "w8"),
            "x9": _reg(frame, "x9"),
        }
        if transition:
            transition_time = transition.get("time", row["time"])
            row["accepted_transition_time"] = transition_time
            row["seconds_after_accepted_transition"] = (
                row["time"] - transition_time)
            if time_bucket_sample:
                row["sample_reason"] = "post-transition-60s-bucket"
        if label == "UPDATE_FLAGS_MASK_CHECK":
            update = row["x21"]
            prepared = _uint(process, sp + 0xF0, 8) if sp else None
            mask = _uint(process, sp + 0x170, 8) if sp else None
            flags = _uint(process, update + 0x140, 8) if update else None
            state_bit = _uint(process, update + 0x5DA, 1) if update else None
            row.update({
                "update_x21": update,
                "prepared_flags_sp_f0": prepared,
                "prepared_bit22": ((prepared >> 22) & 1)
                    if prepared is not None else None,
                "mask_sp_170": mask,
                "mask_has_28000": bool(mask & 0x28000)
                    if mask is not None else None,
                "update_flags_140": flags,
                "update_state_5da": state_bit,
            })
        elif label == "RFT_AFTER_V170":
            # x0 is the virtual call's return value here, not the display.
            # Preserve the call-site Update from x1 and record its flags.
            update = row["x1"]
            flags = _uint(process, update + 0x140, 8) if update else None
            row.update({
                "ignore_return_w0": row["w0"],
                "update_x1": update,
                "update_flags_140": flags,
                "update_mask_0a": flags & 0xA if flags is not None else None,
            })
        elif label in ("RFT_BEFORE_V170", "RFT_V170_IMPL_ENTRY"):
            display = row["x0"]
            update = row["x1"]
            state = _uint(process, display + 0x330, 8) if display else None
            update_flags = _uint(process, update + 0x140, 8) if update else None
            mode = _uint(process, display + 0x2B8, 8) if display else None
            row.update({
                "display_x0": display,
                "display_id_18": _uint(process, display + 0x18, 4)
                    if display else None,
                "display_mode_2b8": mode,
                "display_mode_byte_2bb": _uint(process, display + 0x2BB, 1)
                    if display else None,
                "display_state_330": state,
                "state330_4": _uint(process, state + 4, 1)
                    if state else None,
                "state330_5": _uint(process, state + 5, 1)
                    if state else None,
                "update_x1": update,
                "update_flags_140": update_flags,
                "update_mask_0a": (update_flags & 0xA)
                    if update_flags is not None else None,
                "display_pending_7558": _uint(process, display + 0x7558, 1)
                    if display else None,
                "display_ignore_7595": _uint(process, display + 0x7595, 1)
                    if display else None,
            })
        else:
            # The RFT state object remains in x19 along these branch sites.
            # Record raw registers and the two compared fields; do not infer
            # branch outcome until a successor site confirms the path.
            x19 = row["x19"]
            fields = {}
            if x19:
                for offset, key in ((0x40, "state_40"),
                                    (0x158, "state_158"),
                                    (0x1F0, "state_1f0"),
                                    (0x20C, "state_20c"),
                                    (0x648, "state_648"),
                                    (0x660, "state_660"),
                                    (0xB20, "state_b20")):
                    fields[key] = _uint(process, x19 + offset, 8
                                        if offset in (0x40, 0x158, 0x1F0,
                                                      0x648, 0xB20) else 4)
            row.update(fields)
            paired = V170_UPDATES.get(_path_key(frame, process))
            if paired is None:
                paired = V170_THREAD_UPDATES.get(
                    (process.GetProcessID(), row["tpidr_el1"]))
            if paired:
                age = row["time"] - paired["time"]
                row.update({
                    "paired_v170_update": paired["update"],
                    "paired_v170_flags_140": paired["flags_140"],
                    "paired_v170_return_w0": paired["return_w0"],
                    "paired_v170_time": paired["time"],
                    "paired_v170_age_s": age,
                    "paired_v170_fresh": 0 <= age <= 5.0,
                })
            if label in ("RFT_POST_V170_CMP648",
                         "RFT_POST_V170_BEQ648",
                         "RFT_POST_V170_NEQ648"):
                row["cmp_x8_x9_equal"] = (
                    row["x8"] == row["x9"]
                    if row["x8"] is not None and row["x9"] is not None
                    else None)
            if label == "RFT_POST_V170_GFLAG":
                row["global_flag_bit0"] = (
                    row["w8"] & 1 if row["w8"] is not None else None)
            if label == "RFT_POST_V170_20C_BRANCH":
                row["state_20c_bit3"] = (
                    row["w8"] & 8 if row["w8"] is not None else None)
        _emit(row)
    except Exception as error:
        _emit({"label": "UPDATE_GATE_MIN_CALLBACK_ERROR",
               "error": repr(error),
               "traceback": traceback.format_exc(),
               "pc": _reg(frame, "pc")})
    return False


def install(debugger, slide):
    global OUT, AFTER_LOCK_TRANSITION, TRANSITION_MARKER
    bks.SLIDE[0] = int(slide)
    AFTER_LOCK_TRANSITION = os.environ.get(
        "DVM_POSTHOME_UPDATE_GATE_AFTER_LOCK_TRANSITION", "0") == "1"
    TRANSITION_MARKER = Path(os.environ.get(
        "DVM_LOCK_GESTURE_TRANSITION_MARKER", ""))
    event_dir = Path(os.environ.get(
        "DVM_PROBE_EVENT_DIR", "/tmp/dvm/posthome-update-gate-min-events"))
    OUT = event_dir / "update-gate-min.jsonl"
    OUT.parent.mkdir(parents=True, exist_ok=True)
    META.clear()
    COUNTS.clear()
    V170_UPDATES.clear()
    V170_THREAD_UPDATES.clear()
    LAST_TRANSITION_SAMPLE.clear()
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    armed = []
    for offset, label in SITES:
        address = int(slide) + offset
        bp = target.BreakpointCreateByAddress(address)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("could not resolve %s at 0x%x" % (label, address))
        META[bp.GetID()] = label
        callback_result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F %s.on_hit %d" %
            (__name__, bp.GetID()), callback_result)
        bp.SetAutoContinue(True)
        proof = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(), proof)
        proof_text = proof.GetOutput() or proof.GetError() or ""
        if (not callback_result.Succeeded() or not proof.Succeeded() or
                (__name__ + ".on_hit") not in proof_text):
            bp.SetEnabled(False)
            raise RuntimeError("callback proof failed for %s: %s %s" %
                               (label,
                                callback_result.GetError() or
                                callback_result.GetOutput(),
                                proof_text))
        armed.append({"label": label, "id": bp.GetID(),
                      "address": "0x%x" % address,
                      "locations": bp.GetNumLocations(),
                      "callback": __name__ + ".on_hit",
                      "auto_continue": True})
    _emit({"label": "POSTHOME_UPDATE_GATE_MIN_READY",
           "slide": "0x%x" % int(slide), "armed": armed,
           "sample_policy": ("first 64 then powers of two, plus one "
                             "post-transition row per 60s per thread/site; "
                             "V170 result pairing updates on every return"),
           "after_lock_transition": AFTER_LOCK_TRANSITION,
           "transition_marker": str(TRANSITION_MARKER),
           "update_pointer_register": "x21 at UPDATE_FLAGS_MASK_CHECK",
           "post_v170_sites": [label for _address, label in SITES
                               if label.startswith("RFT_POST_V170") or
                               label in ("CALL_OUTER_A",
                                         "SERVER_UPDATE_OUTER_ENTRY")],
           "log": str(OUT)})

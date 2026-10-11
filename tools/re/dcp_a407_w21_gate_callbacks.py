"""Read-only, ID-keyed A407 transfer/swallow discriminator on *real* DCP firmware.

This module requires an LLDB target that executes the recovered H17P DCP
task-2 firmware and exposes its CPU registers and memory. The current QEMU
IOMFB model does not execute these addresses. Address adjustment is an
explicit firmware slide, not the AP kernelcache slide.

After `command script import /absolute/path/to/this/file.py`, run
`script dcp_a407_w21_gate_callbacks.install(lldb.debugger, slide=0)`.
The callback only reads registers/memory, prints JSON, and stops on the first
matched swallow or transfer-scheduled verdict. No D-series sites are armed.
"""

import json
import os

import lldb


SWAP_ID = int(os.environ.get("DVM_DCP_A407_SWAP_ID", "1"), 0)
FB_FILTER = int(os.environ.get("DVM_DCP_A407_FB", "0"), 0)

SITES = (
    (0x04215DBC, "LOOKUP_OBJECT"),  # x3 loaded from [x29-0x70]
    (0x04216A28, "POLICY_ENTRY"),  # initial w21; x24=fb+0x3e94
    (0x04216A6C, "POLICY_520_RETURN"),  # w0 from virtual +0x520
    (0x04216A70, "POLICY_CLEAR"),
    (0x04216B4C, "POLICY_FORCE_ONE"),
    (0x04216D7C, "GATE"),  # w25/w21 before orn
    (0x04216D90, "TRANSFER_ARM"),
    (0x04216DA0, "CLEANUP_ARM"),  # also hit after a successful call returns
    (0x04213980, "TRANSFER_SCHEDULED"),
)

LABELS = {}
ACTIVE = {}


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else None


def _read_uint(process, address, size):
    if not address:
        return None
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return int.from_bytes(data, "little") if error.Success() and len(data) == size else None


def _hex(value):
    return None if value is None else "0x%x" % value


def _thread_key(frame):
    return frame.GetThread().GetThreadID()


def _emit(frame, label, details):
    row = {"site": label, "thread": _thread_key(frame)}
    row.update(details)
    print("DCP_A407_GATE " + json.dumps(row, sort_keys=True), flush=True)


def on_hit(frame, bp_loc, _context):
    """LLDB breakpoint callback: return True only for a final matched verdict."""
    label = LABELS.get(bp_loc.GetBreakpoint().GetID())
    if label is None:
        return False
    process = frame.GetThread().GetProcess()
    key = _thread_key(frame)

    if label == "LOOKUP_OBJECT":
        fb, request, obj = (_reg(frame, "x19"), _reg(frame, "x20"),
                            _reg(frame, "x3"))
        request_id = _read_uint(process, request + 0x98, 4) if request else None
        object_id = _read_uint(process, obj + 0x3D4, 4) if obj else None
        if request_id != SWAP_ID or object_id != SWAP_ID:
            return False
        if FB_FILTER and fb != FB_FILTER:
            return False
        ACTIVE[key] = {"fb": fb, "request": request, "object": obj,
                       "policy": [], "gate": None, "scheduled": False}
        _emit(frame, label, {"fb": _hex(fb), "request": _hex(request),
                             "object": _hex(obj), "request_id": request_id,
                             "object_id": object_id})
        return False

    state = ACTIVE.get(key)
    if not state:
        return False
    fb, obj = state["fb"], state["object"]

    if label == "POLICY_ENTRY":
        if _reg(frame, "x19") != fb:
            return False
        state["policy"].append({
            "site": label, "initial_w21": _reg(frame, "w21"),
            "x24": _hex(_reg(frame, "x24")),
            "fb_42bc": _read_uint(process, fb + 0x42BC, 4),
            "fb_4305": _read_uint(process, fb + 0x4305, 1),
            "fb_42c4": _read_uint(process, fb + 0x42C4, 4),
            "fb_42dc": _read_uint(process, fb + 0x42DC, 4),
        })
        return False

    if label in ("POLICY_520_RETURN", "POLICY_CLEAR", "POLICY_FORCE_ONE"):
        if _reg(frame, "x19") == fb:
            state["policy"].append({"site": label, "w0": _reg(frame, "w0"),
                                    "w21_before": _reg(frame, "w21")})
        return False

    if label == "GATE":
        fp = _reg(frame, "x29")
        current_obj = _read_uint(process, fp - 0x70, 8) if fp else None
        if _reg(frame, "x19") != fb or current_obj != obj:
            return False
        state["gate"] = {"w25": _reg(frame, "w25"),
                         "w21": _reg(frame, "w21"),
                         "object_id": _read_uint(process, obj + 0x3D4, 4)}
        _emit(frame, label, {"fb": _hex(fb), "object": _hex(obj),
                             **state["gate"], "policy": state["policy"]})
        return False

    if not state["gate"]:
        return False
    if label == "TRANSFER_ARM":
        if _reg(frame, "x19") == fb:
            _emit(frame, label, {"fb": _hex(fb), "object": _hex(obj)})
        return False
    if label == "TRANSFER_SCHEDULED":
        if _reg(frame, "x0") != fb or _reg(frame, "x1") != obj:
            return False
        state["scheduled"] = True
        _emit(frame, label, {"verdict": "transfer-scheduled", "fb": _hex(fb),
                             "object": _hex(obj), "gate": state["gate"]})
        ACTIVE.pop(key, None)
        return True
    if label == "CLEANUP_ARM" and _reg(frame, "x19") == fb:
        gate = state["gate"]
        verdict = ("swallow" if gate["w25"] & 1 and gate["w21"] & 1
                   else "unexpected-gate-state")
        _emit(frame, label, {"verdict": verdict, "fb": _hex(fb),
                             "object": _hex(obj), "gate": gate,
                             "policy": state["policy"]})
        ACTIVE.pop(key, None)
        return True
    return False


def install(debugger, slide=0):
    """Arm all sites on a debugger exposing real DCP task-2 execution."""
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    for static, label in SITES:
        address = static + int(slide)
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % address, result)
        if not result.Succeeded() or target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed to arm %s: %s" % (label, result.GetError()))
        bp = target.GetBreakpointAtIndex(before)
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F dcp_a407_w21_gate_callbacks.on_hit %d" % bp.GetID(),
            result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" % (label, result.GetError()))
        LABELS[bp.GetID()] = label
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(), result)
        if "dcp_a407_w21_gate_callbacks.on_hit" not in (result.GetOutput() or ""):
            raise RuntimeError("callback not registered for %s" % label)
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (bp.GetID(), label, address), flush=True)
    print("DCP_A407_GATE_READY sites=%d swap_id=%d fb_filter=%s slide=0x%x" %
          (len(SITES), SWAP_ID, _hex(FB_FILTER), int(slide)), flush=True)

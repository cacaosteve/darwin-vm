"""Read-only, ID-1 keyed AP state at the A407 call and return.

This runs in the matched _kern_SwapEnd thread. It reads the framebuffer's
pending-list links and the same node before/after A407; it never changes guest
memory or emits a D-series completion. Stop the boot on MATCHED_A407_RETURN.
"""

import json
import os
import time

import lldb

import bks_checkin_callbacks as bks
from dm_plugin_lifecycle_callbacks import _read


KERNEL_SLIDE = int(os.environ.get("DVM_KERNEL_SLIDE", "0x20000000"), 0)
SITES = (
    (0x22A391334, "KERN_END_ENTRY", False),
    (0xFFFFfff00A0C36D4, "KERNEL_END_RECORD", True),
    (0xFFFFfff00A0C477C, "MATCHED_A407_CALL", True),
    (0xFFFFfff00A0C4780, "MATCHED_A407_RETURN", True),
)
META = {}
ACTIVE = {}


def _reg(frame, name):
    return bks._reg(frame, name)


def _u(process, address, size):
    if not address:
        return None
    raw = _read(process, address, size)
    return int.from_bytes(raw, "little") if len(raw) == size else None


def _h(value):
    return None if value is None else "0x%x" % value


def _state(process, fb, node, record):
    head = _u(process, fb + 0x4138, 8)
    return {
        "fb": _h(fb), "node": _h(node), "record": _h(record),
        "record_id": _u(process, record + 0x98, 4),
        "head": _h(head), "head_is_node": head == node,
        "tail_link": _h(_u(process, fb + 0x4140, 8)),
        "gate_5870": _h(_u(process, fb + 0x5870, 1)),
        "gate_5df0": _h(_u(process, fb + 0x5df0, 1)),
        "node_id": _u(process, node + 0x3d4, 4),
        "node_3d8": _h(_u(process, node + 0x3d8, 4)),
        "node_3ec": _h(_u(process, node + 0x3ec, 4)),
        "node_next": _h(_u(process, node + 0xd38, 8)),
        "node_prev_link": _h(_u(process, node + 0xd40, 8)),
        "node_62a": _h(_u(process, node + 0x62a, 1)),
        "node_62b": _h(_u(process, node + 0x62b, 1)),
    }


def _emit(frame, label, fields):
    row = {"label": label, "time": time.time(),
           "thread": frame.GetThread().GetThreadID(),
           "tpidr_el1": _h(_reg(frame, "tpidr_el1"))}
    row.update(fields)
    print("DM_A407_POST " + json.dumps(row, sort_keys=True), flush=True)


def on_hit(frame, bp_loc, _context):
    label = META[bp_loc.GetBreakpoint().GetID()]
    owner = _reg(frame, "tpidr_el1")
    process = frame.GetThread().GetProcess()
    if label == "KERN_END_ENTRY":
        obj = _reg(frame, "x0")
        if _u(process, obj + 0xb0, 4) == 1:
            ACTIVE[owner] = {"object": obj}
            _emit(frame, label, {"object": _h(obj), "id": 1})
        return False
    matched = ACTIVE.get(owner)
    if not matched:
        return False
    if label == "KERNEL_END_RECORD":
        record = _reg(frame, "x1")
        if _u(process, record + 0x98, 4) != 1:
            return False
        matched["record"] = record
        _emit(frame, label, {"object": _h(matched["object"]),
                             "record": _h(record), "id": 1})
        return False
    fb, node = _reg(frame, "x21"), _reg(frame, "x23")
    record = matched.get("record")
    if not record or _reg(frame, "x24") != record:
        return False
    fields = _state(process, fb, node, record)
    if fields["record_id"] != 1 or fields["node_id"] != 1:
        return False
    if label == "MATCHED_A407_CALL":
        matched.update(fb=fb, node=node, before=fields)
        _emit(frame, label, fields)
    elif label == "MATCHED_A407_RETURN" and matched.get("fb") == fb and matched.get("node") == node:
        fields.update(status=_h(_reg(frame, "w0")),
                      changed={k: [v, fields[k]] for k, v in matched["before"].items()
                               if fields.get(k) != v})
        _emit(frame, label, fields)
        ACTIVE.pop(owner, None)
    return False


def install(debugger, slide):
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    for static, label, kernel in SITES:
        address = static + (KERNEL_SLIDE if kernel else int(slide))
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % address, result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed to arm %s: %s" % (label, result.GetError()))
        bp = target.GetBreakpointAtIndex(before)
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F dm_a407_postreturn_callbacks.on_hit %d" %
            bp.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" % (label, result.GetError()))
        META[bp.GetID()] = label
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(), result)
        if "dm_a407_postreturn_callbacks.on_hit" not in (result.GetOutput() or result.GetError()):
            raise RuntimeError("missing command proof for %s" % label)
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (bp.GetID(), label, address), flush=True)
    print("DM_A407_POST_READY user_slide=0x%x kernel_slide=0x%x sites=%d" %
          (int(slide), KERNEL_SLIDE, len(SITES)), flush=True)

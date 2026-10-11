"""Read-only SwapEnd link-to-submit bracket for the DataMigration CA stall.

The kernel sites are iOS 27 IOMobileGraphicsFamily-DCP static addresses plus
the kernel slide. Match them to the _kern_SwapEnd caller by tpidr_el1, so
unrelated display traffic cannot classify this pending swap.
"""

import json
import os
import time

import lldb

import bks_checkin_callbacks as bks
from dm_plugin_lifecycle_callbacks import _read


KERNEL_SLIDE = int(os.environ.get("DVM_KERNEL_SLIDE", "0x20000000"), 0)
SITES = (
    (0x22A391334, "KERN_SWAP_END_CALL", False),
    (0x22A391368, "KERN_SWAP_END_RETURN", False),
    (0x22A3912B4, "KERN_SWAP_WAIT_CALL", False),
    (0x22A391300, "KERN_SWAP_WAIT_RETURN", False),
    (0xFFFFfff00A0C3744, "NODE_ID_STORE", True),
    (0xFFFFfff00A0C4518, "NODE_LINKED", True),
    (0xFFFFfff00A0C452C, "POST_LINK_PREP_RETURN", True),
    (0xFFFFfff00A0C4564, "SUBMIT_GATE_1", True),
    (0xFFFFfff00A0C4608, "SUBMIT_GATE_2", True),
    (0xFFFFfff00A0C4664, "A408_BUILD", True),
    (0xFFFFfff00A0C477C, "A407_BUILD", True),
    (0xFFFFfff00A0C4780, "A407_RETURN", True),
    (0xFFFFfff00A0DA3EC, "D582_ENTRY", True),
    (0xFFFFfff00A0C5CAC, "D582_HOL_HELPER", True),
    (0xFFFFfff00A0C5728, "HOL_RETIRE_HELPER", True),
    (0xFFFFfff00A0C5824, "HOL_NODE_UNLINKED", True),
    (0xFFFFfff00A0DB034, "D594_ENTRY", True),
    (0xFFFFfff00A0C58BC, "D594_RETIRE_HELPER", True),
    (0xFFFFfff00A0C5B30, "D594_NODE_UNLINKED", True),
)
META = {}
ACTIVE = {}
COUNTS = {}
WATCHED_FBS = {}
WAIT_CALLS = {}
COMPLETION_SITES = frozenset((
    "D582_ENTRY", "D582_HOL_HELPER", "HOL_RETIRE_HELPER",
    "HOL_NODE_UNLINKED", "D594_ENTRY", "D594_RETIRE_HELPER",
    "D594_NODE_UNLINKED",
))


def _u(process, address, size):
    if not address:
        return None
    raw = _read(process, address, size)
    return int.from_bytes(raw, "little") if len(raw) == size else None


def _h(value):
    return None if value is None else "0x%x" % value


def _record(frame, label, fields):
    row = {"label": label, "time": time.time(),
           "thread": frame.GetThread().GetThreadID(),
           "tpidr_el1": _h(bks._reg(frame, "tpidr_el1"))}
    row.update(fields)
    print("DM_SWAPEND " + json.dumps(row, sort_keys=True), flush=True)


def on_hit(frame, bp_loc, _context):
    process = frame.GetThread().GetProcess()
    label, kernel = META[bp_loc.GetBreakpoint().GetID()]
    owner = bks._reg(frame, "tpidr_el1")
    if not kernel:
        if bks._progname(process) != "backboardd":
            return False
        obj = bks._reg(frame, "x0")
        if label == "KERN_SWAP_END_CALL":
            ACTIVE[owner] = {"object": _h(obj), "connection": _u(process, obj + 0x14, 4),
                             "start": time.time()}
            _record(frame, label, ACTIVE[owner])
        elif label == "KERN_SWAP_END_RETURN":
            call = ACTIVE.pop(owner, None)
            if call:
                _record(frame, label, {"object": call["object"], "connection": call["connection"],
                                       "elapsed": time.time() - call["start"],
                                       "result": _h(bks._reg(frame, "x0"))})
        elif label == "KERN_SWAP_WAIT_CALL":
            fields = {"object": _h(obj), "connection": _u(process, obj + 0x14, 4),
                      "fence": bks._reg(frame, "w1"),
                      "wait_options": bks._reg(frame, "w2")}
            WAIT_CALLS[owner] = dict(fields, start=time.time())
            _record(frame, label, fields)
        elif label == "KERN_SWAP_WAIT_RETURN":
            call = WAIT_CALLS.pop(owner, None)
            if call:
                _record(frame, label, {"object": call["object"], "fence": call["fence"],
                                       "wait_options": call["wait_options"],
                                       "result": _h(bks._reg(frame, "x0")),
                                       "elapsed": time.time() - call["start"]})
        return False

    if label in COMPLETION_SITES:
        if not WATCHED_FBS:
            return False
        if label in ("D582_ENTRY", "D594_ENTRY"):
            # Handler x0 is a dispatcher context, not the framebuffer.  Match
            # the first swap ID in its inbound payload here; the exact
            # framebuffer match must occur in the remover below.
            raw = _read(process, bks._reg(frame, "x2"),
                        12 if label == "D582_ENTRY" else 0x10)
            swap_id = int.from_bytes(raw[:4], "little") if len(raw) >= 4 else None
            if swap_id not in {v["swap_id"] for v in WATCHED_FBS.values()}:
                return False
            fields = {"swap_id": swap_id, "payload": raw.hex()}
        else:
            fb = bks._reg(frame, "x19") if label == "HOL_NODE_UNLINKED" else (
                bks._reg(frame, "x22") if label == "D594_NODE_UNLINKED" else bks._reg(frame, "x0"))
            if fb not in WATCHED_FBS:
                return False
            fields = {"fb": _h(fb), "head": _h(_u(process, fb + 0x4138, 8)),
                      "watched": WATCHED_FBS[fb]}
            if label in ("D582_HOL_HELPER", "HOL_RETIRE_HELPER", "D594_RETIRE_HELPER"):
                fields["callback_swap_id"] = bks._reg(frame, "w1")
            if label in ("HOL_NODE_UNLINKED", "D594_NODE_UNLINKED"):
                fields["node"] = _h(bks._reg(frame, "x20") if label == "HOL_NODE_UNLINKED"
                                    else bks._reg(frame, "x24"))
        count = COUNTS[label] = COUNTS.get(label, 0) + 1
        if count <= 24:
            _record(frame, label, dict(fields, count=count))
        return False

    if owner not in ACTIVE:
        return False
    count = COUNTS[label] = COUNTS.get(label, 0) + 1
    if count > 24:
        return False
    fb = bks._reg(frame, "x21")
    node = bks._reg(frame, "x23")
    fields = {"count": count, "fb": _h(fb), "node": _h(node),
              "x0": _h(bks._reg(frame, "x0")), "x26": _h(bks._reg(frame, "x26"))}
    if label != "NODE_ID_STORE":
        fields.update(head=_h(_u(process, fb + 0x4138, 8)),
                      swap_id=_u(process, node + 0x3d4, 4),
                      word_3d8=_h(_u(process, node + 0x3d8, 4)))
    if label == "SUBMIT_GATE_1":
        fields["gate_byte_870"] = _h(_u(process, bks._reg(frame, "x26") + 0x870, 1))
    elif label == "SUBMIT_GATE_2":
        fields["gate_byte_df0"] = _h(_u(process, bks._reg(frame, "x26") + 0xdf0, 1))
    elif label == "A407_BUILD":
        WATCHED_FBS[fb] = {"node": _h(node), "swap_id": fields["swap_id"]}
    _record(frame, label, fields)
    return False


def install(debugger, slide):
    bks.SLIDE[0] = int(slide)
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
        interpreter.HandleCommand("breakpoint command add -F dm_swapend_submit_callbacks.on_hit %d" % bp.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" % (label, result.GetError()))
        META[bp.GetID()] = (label, kernel)
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(), result)
        if "dm_swapend_submit_callbacks.on_hit" not in (result.GetOutput() or result.GetError()):
            raise RuntimeError("missing command proof for %s" % label)
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" % (bp.GetID(), label, address), flush=True)
    print("DM_SWAPEND_READY user_slide=0x%x kernel_slide=0x%x sites=%d" %
          (slide, KERNEL_SLIDE, len(SITES)), flush=True)

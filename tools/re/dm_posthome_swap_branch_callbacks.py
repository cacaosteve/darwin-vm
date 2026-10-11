"""Read-only handoff-to-SwapEnd/A407/A408 bracket on repaired CLASS16.

The first post-home non-null fb_swap_set_layer handoff selects one
backboardd thread and IOMFB object. Kernel submit sites count only while
that thread is inside a matching _kern_SwapEnd. No state is modified.
"""

import json
import time
from pathlib import Path

import lldb

import bks_checkin_callbacks as bks
import dm_repaired_ls_adfl_home_callbacks as home
from dm_plugin_lifecycle_callbacks import _read


USER_SITES = (
    (0x18446BB14, "FINISH_UPDATE_ENTRY"),
    (0x18446D0C0, "FINISH_SWAP_X22"),
    (0x18446D0CC, "FINISH_SWAP_X22_RETURN"),
    (0x18447238C, "FB_SWAP_SET_LAYER_ENTRY"),
    (0x184472488, "FB_SWAP_HANDOFF"),
    (0x22A391EA0, "SET_LAYER_THUNK"),
    (0x22A3918F4, "KERN_SET_LAYER"),
    (0x22A391750, "END_THUNK"),
    (0x22A396C70, "VIRT_END"),
    (0x22A391334, "KERN_END"),
    (0x22A3912B4, "KERN_WAIT_ENTRY"),
    (0x22A391300, "KERN_WAIT_RETURN"),
)
KERNEL_SITES = (
    (0xFFFFFFF00A0C4518, "NODE_LINKED"),
    (0xFFFFFFF00A0C4564, "SUBMIT_GATE_1"),
    (0xFFFFFFF00A0C4608, "SUBMIT_GATE_2"),
    (0xFFFFFFF00A0C4664, "A408_BUILD"),
    (0xFFFFFFF00A0C477C, "A407_BUILD"),
)
KERNEL_SLIDE = 0x20000000
META = {}
COUNTS = {}
RETURNS = set()
UPDATES = {}
HANDOFF = {}
KERN_ENDS = {}
WAITS = {}


def _u(process, address, size):
    if not address:
        return None
    raw = _read(process, address, size)
    return int.from_bytes(raw, "little") if len(raw) == size else None


def _register(debugger, address, label, thread=0):
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint set --address 0x%x" % address, result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed %s: %s" % (label, result.GetError()))
    bp = target.GetBreakpointAtIndex(before)
    META[bp.GetID()] = label, thread
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand(
        "breakpoint command add -F dm_posthome_swap_branch_callbacks.on_hit %d" %
        bp.GetID(), result)
    if not result.Succeeded():
        raise RuntimeError("callback failure %s: %s" % (label, result.GetError()))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
          (bp.GetID(), label, address), flush=True)
    return bp


def _arm_return(frame, label, thread):
    address = bks._reg(frame, "lr")
    if not address:
        return
    key = label, thread, address
    if key in RETURNS:
        return
    RETURNS.add(key)
    debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
    _register(debugger, address, label, thread)


def _emit(label, thread, fields):
    row = {"label": label, "time": time.time(), "thread": thread}
    row.update(fields)
    print("POSTHOME_SWAP_BRANCH_JSON " + json.dumps(row, sort_keys=True),
          flush=True)


def on_hit(frame, bp_loc, _context):
    bp = bp_loc.GetBreakpoint()
    label, expected_thread = META[bp.GetID()]
    if not home.HOME_RETURNED:
        return False
    process = frame.GetThread().GetProcess()
    thread = bks._reg(frame, "tpidr_el1")
    if expected_thread and thread != expected_thread:
        return False
    if bks._progname(process) != "backboardd":
        return False
    end_object = None

    # Before selection, record only the nearby update and End activity.
    if label == "FINISH_UPDATE_ENTRY":
        if HANDOFF:
            return False
        UPDATES[thread] = {"display": bks._reg(frame, "x0"),
                           "caller": bks._reg(frame, "lr")}
        _arm_return(frame, "FINISH_UPDATE_RETURN", thread)
    elif label == "FB_SWAP_HANDOFF":
        surface = bks._reg(frame, "x2")
        if surface and not HANDOFF:
            HANDOFF.update(thread=thread, object=bks._reg(frame, "x0"),
                           surface=surface, update=UPDATES.get(thread),
                           start=time.time())
            print("POSTHOME_NONNULL_HANDOFF " + json.dumps(HANDOFF, sort_keys=True),
                  flush=True)
    elif label == "KERN_END":
        if not HANDOFF or thread != HANDOFF["thread"]:
            return False
        obj = bks._reg(frame, "x0")
        KERN_ENDS[thread] = obj
        _arm_return(frame, "KERN_END_RETURN", thread)
    elif label in ("NODE_LINKED", "SUBMIT_GATE_1", "SUBMIT_GATE_2",
                   "A407_BUILD", "A408_BUILD"):
        if label == "A408_BUILD" and HANDOFF:
            # A global positive control; only same-thread/object hits are
            # eligible to be paired with the selected handoff.
            pass
        elif not HANDOFF or thread != HANDOFF["thread"] or thread not in KERN_ENDS:
            return False
    elif label in ("END_THUNK", "VIRT_END", "SET_LAYER_THUNK",
                   "KERN_SET_LAYER", "FINISH_SWAP_X22",
                   "FINISH_SWAP_X22_RETURN", "FB_SWAP_SET_LAYER_ENTRY"):
        if HANDOFF and thread != HANDOFF["thread"]:
            return False
        if not HANDOFF and label in ("SET_LAYER_THUNK", "KERN_SET_LAYER",
                                     "FB_SWAP_SET_LAYER_ENTRY"):
            return False
    elif label == "FINISH_UPDATE_RETURN":
        if not HANDOFF or thread != HANDOFF["thread"]:
            return False
    elif label == "KERN_END_RETURN":
        end_object = KERN_ENDS.pop(thread, None)
        if end_object != HANDOFF.get("object"):
            return False
    elif label == "KERN_WAIT_ENTRY":
        obj = bks._reg(frame, "x0")
        fence = bks._reg(frame, "w1")
        if obj != HANDOFF.get("object") or fence != HANDOFF.get("swap_id"):
            return False
        WAITS[thread] = {"object": obj, "fence": fence,
                         "options": bks._reg(frame, "w2"),
                         "start": time.time()}
    elif label == "KERN_WAIT_RETURN":
        wait = WAITS.pop(thread, None)
        if not wait:
            return False

    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count > (64 if label in ("FINISH_UPDATE_ENTRY", "END_THUNK") else 24):
        bp.SetEnabled(False)
        return False
    fields = {"count": count, "pc": bks._reg(frame, "pc"),
              "lr": bks._reg(frame, "lr"), "x0": bks._reg(frame, "x0"),
              "x1": bks._reg(frame, "x1"), "x2": bks._reg(frame, "x2"),
              "x3": bks._reg(frame, "x3"),
              "after_handoff": bool(HANDOFF),
              "handoff_match": bool(HANDOFF and thread == HANDOFF["thread"])}
    if label in ("END_THUNK", "SET_LAYER_THUNK"):
        offset = 0xC28 if label == "END_THUNK" else 0xB38
        fields["dispatch_slot"] = _u(process, fields["x0"] + offset, 8) if fields["x0"] else None
    if label in ("KERN_END", "KERN_END_RETURN", "END_THUNK", "VIRT_END",
                 "SET_LAYER_THUNK", "KERN_SET_LAYER"):
        obj = end_object if label == "KERN_END_RETURN" else fields["x0"]
        fields["object_match"] = bool(HANDOFF and obj == HANDOFF["object"])
        if label == "KERN_END_RETURN":
            fields["end_object"] = end_object
    if label in ("NODE_LINKED", "SUBMIT_GATE_1", "SUBMIT_GATE_2",
                 "A407_BUILD", "A408_BUILD"):
        fb = bks._reg(frame, "x21")
        node = bks._reg(frame, "x23")
        x26 = bks._reg(frame, "x26")
        fields.update(fb=fb, node=node, swap_id=_u(process, node + 0x3D4, 4),
                      word_3d8=_u(process, node + 0x3D8, 4),
                      gate_5870=_u(process, x26 + 0x870, 1),
                      gate_5df0=_u(process, x26 + 0xDF0, 1),
                      end_object_match=bool(HANDOFF and
                                            KERN_ENDS.get(thread) == HANDOFF["object"]))
        if label == "NODE_LINKED" and fields["end_object_match"]:
            HANDOFF["swap_id"] = fields["swap_id"]
    if label == "KERN_WAIT_ENTRY":
        fields.update(object=WAITS[thread]["object"],
                      fence=WAITS[thread]["fence"],
                      options=WAITS[thread]["options"])
    elif label == "KERN_WAIT_RETURN":
        fields.update(object=wait["object"], fence=wait["fence"],
                      options=wait["options"], result=bks._reg(frame, "w0"),
                      elapsed=time.time() - wait["start"])
    _emit(label, thread, fields)
    if label == "FINISH_UPDATE_RETURN":
        bp.SetEnabled(False)
    return False


def install(debugger, slide):
    interpreter = debugger.GetCommandInterpreter()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("command script import %s" %
                              Path(home.__file__).resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("failed home import: %s" % result.GetError())
    home.install(debugger, slide)
    META.clear()
    COUNTS.clear()
    RETURNS.clear()
    UPDATES.clear()
    HANDOFF.clear()
    KERN_ENDS.clear()
    WAITS.clear()
    for static, label in USER_SITES:
        _register(debugger, int(slide) + static, label)
    for static, label in KERNEL_SITES:
        _register(debugger, KERNEL_SLIDE + static, label)
    print("POSTHOME_SWAP_BRANCH_READY sites=%d" %
          debugger.GetSelectedTarget().GetNumBreakpoints(), flush=True)

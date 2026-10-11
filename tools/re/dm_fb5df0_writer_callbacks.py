"""Read-only fb+0x5df0 writer watch for the D572/D582 CLASS16 swap path.

Arm at framebuffer construction in early-kernel mode, or at the first pending
node. The post-home branch probe supplies same-object End and A407/A408 markers.
"""

import json
import os
import time
from pathlib import Path

import lldb

import bks_checkin_callbacks as bks
import dm_posthome_swap_branch_callbacks as branch
from dm_plugin_lifecycle_callbacks import _read


KERNEL_SLIDE = 0x20000000
NODE_LINKED = 0xFFFFFFF00A0C4518
FIELDS = {}
WATCHES = {}
MAX_FB = 3
EARLY_FILE = os.environ.get("DVM_FB5DF0_EARLY_FILE")


def _byte(process, address):
    raw = _read(process, address, 1)
    return raw[0] if raw is not None and len(raw) == 1 else None


def _emit(label, **fields):
    print("FB5DF0_JSON " + json.dumps(dict(label=label, time=time.time(),
                                            **fields), sort_keys=True), flush=True)


def _arm_watch(target, fb, source):
    if not fb or fb in FIELDS or len(FIELDS) >= MAX_FB:
        return
    process = target.GetProcess()
    value = _byte(process, fb + 0x5DF0)
    FIELDS[fb] = value
    error = lldb.SBError()
    watchpoint = target.WatchAddress(fb + 0x5DF0, 1, False, True, error)
    if not error.Success() or not watchpoint or not watchpoint.IsValid():
        _emit("WATCH_FAILED", fb=fb, source=source, error=error.GetCString())
        return
    result = lldb.SBCommandReturnObject()
    target.GetDebugger().GetCommandInterpreter().HandleCommand(
        "watchpoint command add -F dm_fb5df0_writer_callbacks.on_watch %d" %
        watchpoint.GetID(), result)
    if not result.Succeeded():
        target.DeleteWatchpoint(watchpoint.GetID())
        _emit("WATCH_FAILED", fb=fb, source=source, error=result.GetError())
        return
    WATCHES[watchpoint.GetID()] = {"fb": fb, "value": value, "hits": 0}
    _emit("WATCH_ARMED", fb=fb, value=value, source=source,
          watch_id=watchpoint.GetID())


def on_watch(frame, watchpoint, _context):
    state = WATCHES.get(watchpoint.GetID())
    if state is None:
        return False
    process = frame.GetThread().GetProcess()
    value = _byte(process, state["fb"] + 0x5DF0)
    state["hits"] += 1
    _emit("WRITE", fb=state["fb"], old=state["value"], new=value,
          watch_id=watchpoint.GetID(), count=state["hits"],
          pc=bks._reg(frame, "pc"),
          writer_candidate=bks._reg(frame, "pc") - KERNEL_SLIDE - 4,
          thread=bks._reg(frame, "tpidr_el1"),
          stack=[frame.GetThread().GetFrameAtIndex(i).GetPC()
                 for i in range(min(frame.GetThread().GetNumFrames(), 8))])
    state["value"] = value
    if state["hits"] >= 64:
        watchpoint.SetEnabled(False)
        _emit("WATCH_LIMIT", fb=state["fb"], count=state["hits"])
    return False


def on_node(frame, _bp_loc, _context):
    if bks._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    fb = bks._reg(frame, "x21")
    if not fb:
        return False
    process = frame.GetThread().GetProcess()
    value = _byte(process, fb + 0x5DF0)
    _emit("FIRST_NODE", fb=fb, value=value,
          swap_id=int.from_bytes(_read(process, bks._reg(frame, "x23") + 0x3D4,
                                       4), "little"),
          thread=bks._reg(frame, "tpidr_el1"),
          pc=bks._reg(frame, "pc"))
    _arm_watch(process.GetTarget(), fb, "first-node")
    return False


def on_init(frame, _bp_loc, _context):
    fb = bks._reg(frame, "x19")
    if not fb:
        return False
    _emit("CONSTRUCTOR", fb=fb, pc=bks._reg(frame, "pc"),
          thread=bks._reg(frame, "tpidr_el1"))
    if EARLY_FILE:
        path = Path(EARLY_FILE)
        existing = json.loads(path.read_text()) if path.exists() else []
        if fb not in existing:
            existing.append(fb)
            path.write_text(json.dumps(existing))
    _arm_watch(frame.GetThread().GetProcess().GetTarget(), fb, "constructor")
    return False


def _register_init(debugger):
    target = debugger.GetSelectedTarget()
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint set --address 0x%x" %
        (KERNEL_SLIDE + 0xFFFFFFF00A0B52C4), result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("constructor breakpoint failed: %s" % result.GetError())
    bp = target.GetBreakpointAtIndex(before)
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command add -F dm_fb5df0_writer_callbacks.on_init %d" %
        bp.GetID(), result)
    if not result.Succeeded():
        raise RuntimeError("constructor callback failed: %s" % result.GetError())
    print("COMMAND_LIST_PROOF id=%d label=FB5DF0_CONSTRUCTOR address=0x%x" %
          (bp.GetID(), KERNEL_SLIDE + 0xFFFFFFF00A0B52C4), flush=True)


def install_kernel(debugger):
    FIELDS.clear()
    WATCHES.clear()
    _register_init(debugger)
    print("EARLY_KERNEL_TRACE_READY FB5DF0", flush=True)


def install(debugger, slide):
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % Path(branch.__file__).resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("branch import failed: %s" % result.GetError())
    branch.install(debugger, slide)
    FIELDS.clear()
    WATCHES.clear()
    _register_init(debugger)
    if EARLY_FILE and Path(EARLY_FILE).exists():
        for fb in json.loads(Path(EARLY_FILE).read_text()):
            _arm_watch(debugger.GetSelectedTarget(), fb, "early-rearm")
    target = debugger.GetSelectedTarget()
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint set --address 0x%x" % (KERNEL_SLIDE + NODE_LINKED), result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("first-node breakpoint failed: %s" % result.GetError())
    bp = target.GetBreakpointAtIndex(before)
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command add -F dm_fb5df0_writer_callbacks.on_node %d" %
        bp.GetID(), result)
    if not result.Succeeded():
        raise RuntimeError("first-node callback failed: %s" % result.GetError())
    print("COMMAND_LIST_PROOF id=%d label=FB5DF0_FIRST_NODE address=0x%x" %
          (bp.GetID(), KERNEL_SLIDE + NODE_LINKED), flush=True)
    print("FB5DF0_READY", flush=True)

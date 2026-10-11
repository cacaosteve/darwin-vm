"""Read-only CLASS16 ladder from the repaired LS parent through shell check-in.

Reuse the established migration-unwind and FB shell brackets.  The extra
sites only confirm the original swap wait, the DM entry, the LS rebuild
reason, and SpringBoard's applicationDidFinishLaunching boundary.
"""

from pathlib import Path
import json
import time

import lldb

import bks_checkin_callbacks as bks
import dm_install_unwind_callbacks as unwind
import fb_system_shell_service_callbacks as service
import fb_shell_tail_bracket_callbacks as tail


SITES = (
    (0x22A3912B4, "SWAP_WAIT_ENTRY", "backboardd"),
    (0x22A391300, "SWAP_WAIT_RETURN", "backboardd"),
    (0x25E8F02D0, "DM_PERFORM_ENTRY", "backboardd"),
    (0x186EC1E84, "LSD_REBUILD_REASON", "lsd"),
    (0x2244D0BA4, "SB_ADFL_ENTRY", "SpringBoard"),
)
META = {}
WAIT_START = {}


def on_hit(frame, bp_loc, _context):
    label, expected = META[bp_loc.GetBreakpoint().GetID()]
    process = frame.GetThread().GetProcess()
    if bks._progname(process) != expected:
        return False
    thread = bks._reg(frame, "tpidr_el1")
    now = time.time()
    row = {"label": label, "time": now, "thread": thread,
           "pc": bks._reg(frame, "pc"), "x0": bks._reg(frame, "x0")}
    if label == "SWAP_WAIT_ENTRY":
        fence, options = bks._reg(frame, "w1"), bks._reg(frame, "w2")
        if (fence, options) != (1, 1):
            return False
        WAIT_START[thread] = now
        row.update(fence=fence, options=options)
    elif label == "SWAP_WAIT_RETURN":
        started = WAIT_START.pop(thread, None)
        if started is None:
            return False
        row.update(elapsed=now-started, result=bks._reg(frame, "w0"))
    elif label == "LSD_REBUILD_REASON":
        pointer = row["x0"]
        raw = bks._read(process, pointer + 0x28, 2) if pointer else b""
        row["reason_flags"] = int.from_bytes(raw, "little") if len(raw) == 2 else None
    print("DM_REPAIRED_SHELL_JSON " + json.dumps(row, sort_keys=True), flush=True)
    return False


def install(debugger, slide):
    interpreter = debugger.GetCommandInterpreter()
    for module in (unwind, service, tail):
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("command script import %s" %
                                  Path(module.__file__).resolve(), result)
        if not result.Succeeded():
            raise RuntimeError("failed import %s: %s" %
                               (module.__name__, result.GetError()))
    unwind.install(debugger, slide)
    service.install(debugger, slide)
    tail.install(debugger, slide)
    bks.SLIDE[0] = int(slide)
    META.clear()
    WAIT_START.clear()
    target = debugger.GetSelectedTarget()
    for static, label, expected in SITES:
        address = int(slide) + static
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % address,
                                  result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed %s: %s" % (label, result.GetError()))
        bp = target.GetBreakpointAtIndex(before)
        META[bp.GetID()] = label, expected
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F dm_repaired_ls_shell_callbacks.on_hit %d" % bp.GetID(),
            result)
        if not result.Succeeded():
            raise RuntimeError("callback failure %s: %s" % (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (bp.GetID(), label, address), flush=True)
    print("DM_REPAIRED_SHELL_READY sites=%d" % target.GetNumBreakpoints(),
          flush=True)

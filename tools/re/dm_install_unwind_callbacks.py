"""Read-only completion markers for the SystemAppMigrator install wait.

This intentionally omits per-bundle and per-plugin probes so a long TCG
MobileInstallation scan can run with minimal debugger interruption.  Addresses
are from the iOS 27 beta 8 shared cache; setup_gate_probe resolves the slide
on every boot.
"""

import lldb

import bks_checkin_callbacks as bks


SITES = (
    (0x1C22981E8, "IX_CANCEL_RETURN", "com.apple.migrationpluginwrapper"),
    (0x25E8F579C, "DM_XPC_SYNC_RETURN", "backboardd"),
    (0x25E8F044C, "DM_PERFORM_RETURN", "backboardd"),
    (0x18A147774, "BKS_CHECKIN_RETURN", "SpringBoard"),
    (0x1C4A3C32C, "SHELL_PRE_RETURN", "SpringBoard"),
)


def on_hit(frame, bp_loc, context):
    return bks.on_break(frame, bp_loc, context)


def install(debugger, slide):
    bks.SLIDE[0] = int(slide)
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    ids = []
    for static, label, process in SITES:
        address = int(slide) + static
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % address,
                                  result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed breakpoint %s: %s" %
                               (label, result.GetError()))
        bp = target.GetBreakpointAtIndex(before)
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F dm_install_unwind_callbacks.on_hit %d" %
            bp.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" %
                               (label, result.GetError()))
        bks.CONFIG[bp.GetID()] = {
            "address": address, "label": label, "limit": 16,
            "reads": (), "regs": ("pc", "lr", "sp", "tpidr_el1", "x0", "x1"),
            "allowed": (process,),
        }
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(),
                                  result)
        proof = result.GetOutput() or result.GetError()
        if "dm_install_unwind_callbacks.on_hit" not in proof:
            raise RuntimeError("callback proof missing for %s: %s" %
                               (label, proof))
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x\n%s" %
              (bp.GetID(), label, address, proof), flush=True)
        ids.append(bp.GetID())
    print("DM_INSTALL_UNWIND_READY slide=0x%x ids=%s" %
          (slide, ids), flush=True)
    return ids

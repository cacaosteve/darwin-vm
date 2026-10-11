"""Read-only CLASS16 swap-ID lineage and later shell-unwind markers."""

from pathlib import Path

import lldb

import dm_a406_id_lineage_callbacks as swap
import dm_install_unwind_callbacks as unwind


def install(debugger, slide):
    interpreter = debugger.GetCommandInterpreter()
    for module in (swap, unwind):
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("command script import %s" %
                                  Path(module.__file__).resolve(), result)
        if not result.Succeeded():
            raise RuntimeError("failed to import %s: %s" %
                               (module.__name__, result.GetError()))
    swap.install(debugger, slide)
    unwind.install(debugger, slide)
    print("DM_A407_POSTWAIT_READY swap_and_unwind_sites_armed", flush=True)

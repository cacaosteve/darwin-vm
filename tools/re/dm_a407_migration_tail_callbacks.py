"""Read-only swap sanity plus sparse DataMigration and shell markers."""

from pathlib import Path

import lldb

import dm_a406_id_lineage_callbacks as swap
import dm_migration_rolling_callbacks as migration
import dm_plugin_progress_callbacks as plugins
import dm_install_unwind_callbacks as unwind


def install(debugger, slide):
    interpreter = debugger.GetCommandInterpreter()
    for module in (swap, migration, plugins, unwind):
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("command script import %s" %
                                  Path(module.__file__).resolve(), result)
        if not result.Succeeded():
            raise RuntimeError("failed to import %s: %s" %
                               (module.__name__, result.GetError()))
    swap.install(debugger, slide)
    migration.install(debugger, slide)
    plugins.install(debugger, slide)
    unwind.install(debugger, slide)
    print("DM_A407_MIGRATION_TAIL_READY", flush=True)

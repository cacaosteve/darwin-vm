"""Read-only same-boot BKS check-in and DataMigrator reply correlation.

This narrow composition keeps the existing check-in/handoff trace, adds the
DataMigrator msgID-0 server dispatch bracket, and observes DataMigrator's
captured send-barrier semaphore. It does not install poster/render hooks or
change guest state. A barrier is only attributed to the migration request
when its connection/thread/timeline evidence actually joins the captured
server handler; temporal proximity alone is not a causal link.
"""

from pathlib import Path

import lldb

import bks_server_receive_callbacks as bks
import datamigrator_barrier_callbacks as barrier
import dm_migration_server_inner_callbacks as migration


def _import(debugger, module):
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % Path(module.__file__).resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("failed LLDB import %s: %s" %
                           (module.__name__, result.GetError()))


def install(debugger, slide):
    # Scripted breakpoint commands resolve by module name in LLDB, so import
    # the concrete callback modules even though Python imported them above.
    for module in (bks, migration, barrier):
        _import(debugger, module)

    bks.CALLBACK_MODULE = bks.__name__
    bks.ENABLE_DRAIN_PROBES = True
    bks.DRAIN_ARMED = False
    bks.DRAIN_HITS.clear()
    bks.install(debugger, int(slide))

    migration.install(debugger, int(slide))
    barrier.install(debugger, int(slide))
    print("DM_LOCK_CHECKIN_REPLY_READY slide=0x%x components=bks,dm-msgid0,"
          "dm-barrier" % int(slide), flush=True)

"""Narrow SystemAppMigrator/Mach-path capture without BackBoard queue probes.

This composes the msgID-0 migration anchor with the existing plugin lifecycle
and kernel mach_msg2 identity hooks, but deliberately omits BKS queue/drain
breakpoints.  It is useful when the broader check-in callback set stops LLDB
before the target plugin call.  Read-only.
"""

import lldb

import bks_checkin_callbacks as bks
import dm_lock_checkin_plugin_callbacks as full
import dm_migration_server_migrate_callbacks as migration
import dm_perform_roundtrip_callbacks as common
import dm_plugin_lifecycle_callbacks as plugin


SITES = {}
CALL_SITES_ARMED = False


def _arm(debugger, address, label):
    common._install(debugger, address, label, __name__ + ".on_hit")
    bp = debugger.GetSelectedTarget().GetBreakpointAtIndex(
        debugger.GetSelectedTarget().GetNumBreakpoints() - 1)
    SITES[bp.GetID()] = label
    full.SITES[bp.GetID()] = label
    return bp


def _arm_plugin_sites(debugger):
    global CALL_SITES_ARMED
    if CALL_SITES_ARMED:
        return
    base = migration.MAIN_BASE
    if not base:
        raise RuntimeError("DataMigrator main base unavailable at start call")
    _arm(debugger, base + full.CALL, "DM_RUN_PLUGIN_CALL_13768")
    CALL_SITES_ARMED = True
    # Do not arm the global runPluginAtPath return PC here. It fires for every
    # migrator (including unrelated TCC work) and can interrupt a batch LLDB
    # session before the matched wrapper reply path is observed. The scoped
    # CLIENT_SYNC_RETURN remains the completion marker for this investigation.
    print("DM_LOCK_PLUGIN_KERNELPATH_CALLS_READY base=0x%x call=0x%x; "
          "global runPluginAtPath return omitted" %
          (base, base + full.CALL), flush=True)


def on_hit(frame, bp_loc, context):
    bp_id = bp_loc.GetBreakpoint().GetID()
    label = SITES.get(bp_id)
    if label:
        # Reuse the established plugin-argument decoder and per-owner capture.
        result = full.on_hit(frame, bp_loc, context)
        if label == "DM_RUN_PLUGIN_CALL_13768":
            identifier = full.CALLS.get(
                bks._reg(frame, "tpidr_el1"), {}).get("identifier")
            print("DM_LOCK_PLUGIN_KERNELPATH_ENTRY identifier=%s" %
                  (identifier or "<unreadable>"), flush=True)
        return result

    if bp_id in common.SITES:
        migration.on_hit(frame, bp_loc, context)
        if common.SITES.get(bp_id) == "DM_START_MIGRATION_CALL":
            _arm_plugin_sites(frame.GetThread().GetProcess().GetTarget().GetDebugger())
        return False

    # Plugin lifecycle callbacks are attached to their own module callback
    # name. The module function is replaced below so the full transport probe
    # can capture the matched SystemApp call and arm the kernel path.
    if bp_id in plugin.META:
        return full._plugin_on_hit(frame, bp_loc, context)
    return False


def install(debugger, slide):
    global CALL_SITES_ARMED
    slide = int(slide)
    CALL_SITES_ARMED = False
    SITES.clear()
    full.SITES.clear()
    full.CALLS.clear()
    full.RECEIVERS.clear()
    full.SYNC_CALLS.clear()
    full.KERNEL_META.clear()
    full.KERNEL_BREAKPOINTS.clear()
    full.KERNEL_SYNC_OWNER = 0
    full.KERNEL_TARGET = None
    full.KERNEL_PENDING_REPLY_RECEIVE.clear()
    full.CALLBACK_SEND_CONTEXTS.clear()
    full.CALLBACK_SEND_BPS.clear()
    full.CALLBACK_OUTBOUND_CANDIDATES.clear()
    full.CALLBACK_HANDOFF_BPS.clear()
    full.CALLBACK_DM_HANDOFFS.clear()
    full.COMPLETION_INVOKE_BPS.clear()
    full.COMPLETION_INVOKE_META.clear()
    full.COMPLETION_RETURN_META.clear()
    full.SYSTEMAPP_PLUGIN_RETURN_BPS.clear()
    full.SYSTEMAPP_PLUGIN_RETURN_META.clear()
    full.KERNEL_RECEIVE_CONDITIONED = False
    full.KERNEL_PATH_ARMED = False
    full.MACH_TRAPS_ARMED = False
    full.MACH_WINDOW_START = 0.0
    full.MACH_DEST_PORT = 0
    full.MACH_REPLY_PORT = 0

    bks.SLIDE[0] = slide
    common.SITES.clear()
    migration.BPS.clear()
    migration.CLIENT_OWNER = migration.CLIENT_REQUEST = None
    migration.SERVER_OWNER = migration.SERVER_MSG = migration.MAIN_BASE = None
    migration.CALLBACK_MODULE = __name__

    # LLDB resolves Python breakpoint callbacks by module name. Import the
    # transport probe itself into LLDB before any plugin callback can lazily
    # install a breakpoint that names
    # ``dm_lock_checkin_plugin_callbacks.on_hit``. Importing only the plugin
    # lifecycle module leaves that callback unregistered, so the first
    # CLIENT_SYNC_CALL raises while trying to arm mach_msg2_trap.
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % full.__file__, result)
    if not result.Succeeded():
        raise RuntimeError("failed LLDB import %s: %s" %
                           (full.__name__, result.GetError()))

    # Keep the established wrapper logic but route plugin breakpoints through
    # its transport-aware dispatcher. Import into LLDB before monkeypatching;
    # reloading after this point would restore plugin.on_hit and lose the
    # transport-aware wrapper.
    full._import(debugger, plugin)
    full.PLUGIN_ORIGINAL_ON_HIT = plugin.on_hit
    plugin.on_hit = full._plugin_on_hit
    plugin.TARGET_IDS.clear()
    plugin.TARGET_IDS.update({"com.apple.PassbookDataMigrator",
                              "com.apple.sbmigrator"})
    plugin.TARGET_PATHS = ("PassbookDataMigrator.migrator",
                           "SpringBoard.migrator")
    plugin.META.clear()
    plugin.OWNERS.clear()
    plugin.COUNTS.clear()
    plugin.DYNAMIC_ARMED = False

    migration.install(debugger, slide)
    plugin.install(debugger, slide)
    # Arm the small shared wrapper send/reply pair up front.  This avoids the
    # lifecycle wrapper's lazy-arm branch, while retaining its normal ID
    # filtering after the SystemApp call is identified.
    for static, label in plugin.DYNAMIC_SITES:
        plugin._install_one(debugger, bks.SLIDE[0] + static, label)
    plugin.DYNAMIC_ARMED = True
    print("DM_LOCK_PLUGIN_KERNELPATH_READY slide=0x%x; BKS queue probes omitted" %
          slide, flush=True)

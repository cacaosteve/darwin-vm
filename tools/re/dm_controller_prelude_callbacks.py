"""Read-only, entry-gated backboardd migration-controller prelude bracket.

The broad migration ladder showed boot-dependent holds. Arm only the
controller's nearby call/return sites after __DMPerformMigration starts, so a
short local gap is not obscured by unrelated breakpoints.
"""

import bks_checkin_callbacks as bks
import dm_perform_roundtrip_callbacks as common


ARMED = False

SITES = (
    (0x25E8F031C, "DM_ENV_FIRST_CHECK_RETURN"),
    (0x25E8F03CC, "DM_CLASS_CONNECTION_CALL"),
    (0x25E8F03D0, "DM_CLASS_CONNECTION_RETURN"),
    (0x25E8F03F8, "DM_PERFORM_CONTROLLER_CALL"),
    (0x25E8F03FC, "DM_PERFORM_CONTROLLER_RETURN"),
    (0x25E8F2514, "DM_CLIENT_CONTROLLER_ENTRY"),
    (0x25E8F2564, "DM_CLIENT_DATE_CALL"),
    (0x25E8F2568, "DM_CLIENT_DATE_RETURN"),
    (0x25E8F2574, "DM_CLIENT_RETRY_INTERVAL_CALL"),
    (0x25E8F2578, "DM_CLIENT_RETRY_INTERVAL_RETURN"),
    (0x25E8F257C, "DM_CLIENT_RETRY_DATE_CALL"),
    (0x25E8F2580, "DM_CLIENT_RETRY_DATE_RETURN"),
    (0x25E8F2584, "DM_CLIENT_CLAIM_RETURN"),
    (0x25E8F2588, "DM_CLIENT_DATE_RELEASE_CALL"),
    (0x25E8F258C, "DM_CLIENT_DATE_RELEASE_RETURN"),
    (0x25E8F2590, "DM_CLIENT_MAX_ATTEMPTS_CALL"),
    (0x25E8F2594, "DM_CLIENT_MAX_ATTEMPTS_RETURN"),
    (0x25E8F25A4, "DM_CLIENT_LOOP_ENTRY"),
    (0x25E8F25B4, "DM_CLIENT_MIGRATE_CALL"),
    (0x25E8F25B8, "DM_CLIENT_MIGRATE_RETURN"),
    (0x25E8F26EC, "DM_CLIENT_CONTROLLER_RETURN"),
)


def on_stage(frame, bp_loc, context):
    return common.on_stage(frame, bp_loc, context)


def on_entry(frame, bp_loc, context):
    global ARMED
    if bks._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    common.on_stage(frame, bp_loc, context)
    if not ARMED:
        debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
        for static, label in SITES:
            common._install(debugger, bks.SLIDE[0] + static, label,
                            "dm_controller_prelude_callbacks.on_stage")
        ARMED = True
        print("DM_CONTROLLER_PRELUDE_ARMED count=%d thread=%d" %
              (len(SITES), frame.GetThread().GetThreadID()), flush=True)
    return False


def install(debugger, slide):
    global ARMED
    bks.SLIDE[0] = int(slide)
    common.SITES.clear()
    common.ACTIVE.clear()
    common.HITS.clear()
    ARMED = False
    common._install(debugger, bks.SLIDE[0] + 0x25E8F02D0,
                    "DM_PERFORM_ENTRY", "dm_controller_prelude_callbacks.on_entry")
    print("DM_CONTROLLER_PRELUDE_READY slide=0x%x" % bks.SLIDE[0],
          flush=True)

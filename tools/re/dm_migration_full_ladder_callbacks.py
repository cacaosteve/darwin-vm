"""One-boot, read-only migration ladder from backboardd entry to XPC reply.

Install only the __DMPerformMigration entry at boot. Once that exact
backboardd thread enters, arm the nested call/return sites while stopped, so
boot-time instrumentation stays light and variable progression is captured
in one run.
"""

import bks_checkin_callbacks as bks
import dm_perform_roundtrip_callbacks as common


ARMED = False

SITES = (
    (0x25E8F0310, "DM_ENVIRONMENT_SHARED_RETURN"),
    (0x25E8F33F8, "DM_ENV_DEVICE_MODE_ENTRY"),
    (0x25E8F3420, "DM_ENV_MKB_DEVICE_MODE_CALL"),
    (0x25E8F3424, "DM_ENV_MKB_DEVICE_MODE_RETURN"),
    (0x25E8F3494, "DM_ENV_DEVICE_MODE_RETURN"),
    (0x25E8F031C, "DM_ENVIRONMENT_FIRST_CHECK_RETURN"),
    (0x25E8F0334, "DM_ENVIRONMENT_SECOND_CHECK_RETURN"),
    (0x25E8F0358, "DM_ENVIRONMENT_THIRD_CHECK_RETURN"),
    (0x25E8F0378, "DM_ENVIRONMENT_FOURTH_CHECK_RETURN"),
    (0x25E8F0394, "DM_ENVIRONMENT_PREFLIGHT_RETURN"),
    (0x25E8F03C4, "DM_AFTER_PREFLIGHT"),
    (0x25E8F03CC, "DM_CLASS_CONNECTION_CALL"),
    (0x25E8F03D0, "DM_CLASS_CONNECTION_RETURN"),
    (0x25E8F03F8, "DM_PERFORM_CONTROLLER_CALL"),
    (0x25E8F03FC, "DM_PERFORM_CONTROLLER_RETURN"),
    (0x25E8F044C, "DM_PERFORM_RETURN"),
    (0x25E8EF124, "DM_CLASS_CONNECTION_ENTRY"),
    (0x25E8EF130, "DM_CONNECTION_INIT_ENTRY"),
    (0x25E8EF164, "DM_CONNECTION_INIT_SUPER_RETURN"),
    (0x25E8EF180, "DM_CONNECTION_INIT_XPC_SERVICE_CALL"),
    (0x25E8EF184, "DM_CONNECTION_INIT_XPC_SERVICE_RETURN"),
    (0x25E8EF198, "DM_CONNECTION_INIT_FOREGROUND_CALL"),
    (0x25E8EF19C, "DM_CONNECTION_INIT_FOREGROUND_RETURN"),
    (0x25E8EF1A0, "DM_CONNECTION_INIT_RESUME_CALL"),
    (0x25E8EF1A4, "DM_CONNECTION_INIT_RESUME_RETURN"),
    (0x25E8EF1DC, "DM_CONNECTION_INIT_RETURN"),
    (0x25E8EEE54, "DM_XPC_INIT_ENTRY"),
    (0x25E8EEE9C, "DM_XPC_INIT_SUPER_RETURN"),
    (0x25E8EEEF0, "DM_XPC_INIT_QUEUE1_CALL"),
    (0x25E8EEEF4, "DM_XPC_INIT_QUEUE1_RETURN"),
    (0x25E8EEF10, "DM_XPC_INIT_QUEUE2_CALL"),
    (0x25E8EEF14, "DM_XPC_INIT_QUEUE2_RETURN"),
    (0x25E8EEF38, "DM_XPC_INIT_SERVICES_CALL"),
    (0x25E8EEF3C, "DM_XPC_INIT_SERVICES_RETURN"),
    (0x25E8EEF50, "DM_XPC_MACH_SERVICE_CALL"),
    (0x25E8EEF54, "DM_XPC_MACH_SERVICE_RETURN"),
    (0x25E8EEF88, "DM_XPC_TARGET_QUEUE_CALL"),
    (0x25E8EEF8C, "DM_XPC_TARGET_QUEUE_RETURN"),
    (0x25E8EEFE8, "DM_XPC_EVENT_HANDLER_CALL"),
    (0x25E8EEFEC, "DM_XPC_EVENT_HANDLER_RETURN"),
    (0x25E8EF028, "DM_XPC_INIT_RETURN"),
    (0x25E8EF248, "DM_FOREGROUND_ENTRY"),
    (0x25E8EF268, "DM_FOREGROUND_ENABLED_CALL"),
    (0x25E8EF26C, "DM_FOREGROUND_ENABLED_RETURN"),
    (0x25E8EF278, "DM_FOREGROUND_UID_CALL"),
    (0x25E8EF27C, "DM_FOREGROUND_UID_RETURN"),
    (0x25E8EF2CC, "DM_FOREGROUND_SET_UID_CALL"),
    (0x25E8EF2D0, "DM_FOREGROUND_SET_UID_RETURN"),
    (0x25E8EF2E0, "DM_FOREGROUND_RETURN"),
    (0x25E8F2514, "DM_CLIENT_CONTROLLER_ENTRY"),
    (0x25E8F2564, "DM_CLIENT_DATE_CALL"),
    (0x25E8F2568, "DM_CLIENT_DATE_RETURN"),
    (0x25E8F2574, "DM_CLIENT_RETRY_INTERVAL_CALL"),
    (0x25E8F2578, "DM_CLIENT_RETRY_INTERVAL_RETURN"),
    (0x25E8F257C, "DM_CLIENT_RETRY_DATE_CALL"),
    (0x25E8F2580, "DM_CLIENT_RETRY_DATE_RETURN"),
    (0x25E8F2590, "DM_CLIENT_MAX_ATTEMPTS_CALL"),
    (0x25E8F2594, "DM_CLIENT_MAX_ATTEMPTS_RETURN"),
    (0x25E8F25A4, "DM_CLIENT_LOOP_ENTRY"),
    (0x25E8F25B4, "DM_CLIENT_MIGRATE_CALL"),
    (0x25E8F25B8, "DM_CLIENT_MIGRATE_RETURN"),
    (0x25E8F26EC, "DM_CLIENT_CONTROLLER_RETURN"),
    (0x25E8F4AF8, "DM_CONNECTION_ENTRY"),
    (0x25E8F4BA8, "DM_CONNECTION_SEND_CALL"),
    (0x25E8F4BAC, "DM_CONNECTION_SEND_RETURN"),
    (0x25E8F4C48, "DM_CONNECTION_RETURN"),
    (0x25E8F5764, "DM_XPC_SEND_ENTRY"),
    (0x25E8F5798, "DM_XPC_SYNC_CALL"),
    (0x25E8F579C, "DM_XPC_SYNC_RETURN"),
    (0x25E8F57C8, "DM_XPC_SEND_RETURN"),
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
                            "dm_migration_full_ladder_callbacks.on_stage")
        ARMED = True
        print("DM_MIGRATION_FULL_LADDER_ARMED count=%d thread=%d" %
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
                    "DM_PERFORM_ENTRY", "dm_migration_full_ladder_callbacks.on_entry")
    print("DM_MIGRATION_FULL_LADDER_READY slide=0x%x" % bks.SLIDE[0],
          flush=True)

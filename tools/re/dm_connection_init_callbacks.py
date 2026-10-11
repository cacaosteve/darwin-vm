"""Read-only bracket of DMConnection creation during backboardd migration.

Scores only the backboardd thread that entered __DMPerformMigration. The
class method, init, XPC connection creation, foreground-session targeting,
and resume sites are from the iOS 27 beta 8 DataMigration image.
"""

import bks_checkin_callbacks as bks
import dm_perform_roundtrip_callbacks as common


def on_stage(frame, bp_loc, context):
    return common.on_stage(frame, bp_loc, context)


def install(debugger, slide):
    bks.SLIDE[0] = int(slide)
    common.SITES.clear()
    common.ACTIVE.clear()
    common.HITS.clear()
    for static, label in (
        (0x25E8F02D0, "DM_PERFORM_ENTRY"),
        (0x25E8F03C4, "DM_AFTER_PREFLIGHT"),
        (0x25E8F03CC, "DM_CLASS_CONNECTION_CALL"),
        (0x25E8F03D0, "DM_CLASS_CONNECTION_RETURN"),
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
    ):
        common._install(debugger, bks.SLIDE[0] + static, label,
                        "dm_connection_init_callbacks.on_stage")
    print("DM_CONNECTION_INIT_READY count=%d slide=0x%x" %
          (len(common.SITES), bks.SLIDE[0]), flush=True)

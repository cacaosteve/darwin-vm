"""Read-only bracket of foreground-user-session targeting in DMXPCConnection."""

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
        (0x25E8F03CC, "DM_CLASS_CONNECTION_CALL"),
        (0x25E8F03D0, "DM_CLASS_CONNECTION_RETURN"),
        (0x25E8EF184, "DM_CONNECTION_INIT_XPC_SERVICE_RETURN"),
        (0x25E8EF198, "DM_CONNECTION_INIT_FOREGROUND_CALL"),
        (0x25E8EF19C, "DM_CONNECTION_INIT_FOREGROUND_RETURN"),
        (0x25E8EF1A0, "DM_CONNECTION_INIT_RESUME_CALL"),
        (0x25E8EF1A4, "DM_CONNECTION_INIT_RESUME_RETURN"),
        (0x25E8EF248, "DM_FOREGROUND_ENTRY"),
        (0x25E8EF268, "DM_FOREGROUND_ENABLED_CALL"),
        (0x25E8EF26C, "DM_FOREGROUND_ENABLED_RETURN"),
        (0x25E8EF278, "DM_FOREGROUND_UID_CALL"),
        (0x25E8EF27C, "DM_FOREGROUND_UID_RETURN"),
        (0x25E8EF2CC, "DM_FOREGROUND_SET_UID_CALL"),
        (0x25E8EF2D0, "DM_FOREGROUND_SET_UID_RETURN"),
        (0x25E8EF2E0, "DM_FOREGROUND_RETURN"),
    ):
        common._install(debugger, bks.SLIDE[0] + static, label,
                        "dm_foreground_session_callbacks.on_stage")
    print("DM_FOREGROUND_SESSION_READY count=%d slide=0x%x" %
          (len(common.SITES), bks.SLIDE[0]), flush=True)

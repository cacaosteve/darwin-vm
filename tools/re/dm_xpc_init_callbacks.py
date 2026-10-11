"""Read-only nested call/return bracket in DMXPCConnection initialization."""

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
    ):
        common._install(debugger, bks.SLIDE[0] + static, label,
                        "dm_xpc_init_callbacks.on_stage")
    print("DM_XPC_INIT_READY count=%d slide=0x%x" %
          (len(common.SITES), bks.SLIDE[0]), flush=True)

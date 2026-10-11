"""Read-only A/B of stalled and working XPC reply handoffs.

Scores the outer dispatch-Mach work-item unwind, queue hierarchy selection,
and continuation enqueue for each matched BoardServices child handoff.
"""

import bks_server_receive_callbacks as full


on_client = full.on_client
on_bs_dispatch = full.on_bs_dispatch
on_xpc_handoff = full.on_xpc_handoff
on_site_return = full.on_site_return


def install(debugger, slide, callback_module="bks_handoff_queue_callbacks"):
    full.CALLBACK_MODULE = callback_module
    full.PIE_BASE = None
    full.CLIENT_TIME = None
    full.CLIENT_THREAD = None
    full.CHILD_BLOCK = None
    full.HANDOFF_THREAD = None
    full.HANDOFF_TIME = None
    full.HANDOFF_DIH = None
    full.HANDOFF_QUEUE = None
    full.DIH_TO_BLOCK.clear()
    full.SITE_LABELS.clear()
    full.HITS.clear()
    full.RETURNS.clear()
    full.GENERIC_IDS.clear()
    full.base.SLIDE = int(slide)
    for address, label, callback in (
        (0x1C4A3C058, "SB_SHELL_START_SERVICE_CALL", "on_client"),
        (0x18A1471A0, "BKS_START_ENTRY", "on_client"),
        (full.CLIENT_CALL, "CLIENT_CHECKIN_CALL", "on_client"),
        (full.CLIENT_RETURN, "CLIENT_CHECKIN_RETURN", "on_client"),
        (0x18F8BB140, "SERVER_BS_CHILD_ENQUEUE", "on_bs_dispatch"),
        (0x18F8BFC98, "SERVER_BS_ASYNC_DISPATCH_QUEUE", "on_bs_dispatch"),
        (0x18F8B9848, "SERVER_BS_CHILD_BLOCK_INVOKE", "on_bs_dispatch"),
        (full.XPC_DICT_HANDOFF, "XPC_DICT_HANDOFF", "on_xpc_handoff"),
        (full.DISPATCH_MACH_HANDOFF, "DISPATCH_MACH_HANDOFF", "on_xpc_handoff"),
        (0x1AE8D56A8, "DISPATCH_HANDOFF_SET_WLH", "on_xpc_handoff"),
        (0x1AE8D5754, "DISPATCH_HANDOFF_SET_WLH_RETURN", "on_xpc_handoff"),
        (0x1AE8D5CB4, "XPC_OUTER_CALLOUT_RETURN", "on_xpc_handoff"),
        (0x1AE8D5DB4, "XPC_OUTER_HANDOFF_ASYNC_CALL", "on_xpc_handoff"),
        (0x1AE8D5AB8, "XPC_ASYNC_ENTRY", "on_xpc_handoff"),
        (0x1AE8D5B04, "XPC_ASYNC_ENQUEUE_RETURN", "on_xpc_handoff"),
        (0x1AE8D58B4, "XPC_IPC_INVOKE", "on_xpc_handoff"),
    ):
        full._install(debugger, full.base.SLIDE + address, label, callback)

"""Narrow, read-only XPC child-reply handoff probe.

Keeps only the client check-in, child block, libxpc handoff, and dispatch-Mach
call/return sites armed so the first check-in is not delayed by broad tracing.
"""

import bks_server_receive_callbacks as full

# LLDB resolves -F callbacks against the imported script module, not modules
# imported by that script. Keep shared state in `full`, but expose its callbacks
# here for LLDB's command lookup (including dynamically installed returns).
on_client = full.on_client
on_bs_dispatch = full.on_bs_dispatch
on_xpc_handoff = full.on_xpc_handoff
on_site_return = full.on_site_return


def install(debugger, slide):
    full.CALLBACK_MODULE = "bks_handoff_boundary_callbacks"
    full.PIE_BASE = None
    full.CLIENT_TIME = None
    full.CLIENT_THREAD = None
    full.CHILD_BLOCK = None
    full.HANDOFF_THREAD = None
    full.HANDOFF_TIME = None
    full.SITE_LABELS.clear()
    full.HITS.clear()
    full.RETURNS.clear()
    full.GENERIC_IDS.clear()
    full.base.SLIDE = int(slide)
    for address, label, callback in (
        (full.CLIENT_CALL, "CLIENT_CHECKIN_CALL", "on_client"),
        (full.CLIENT_RETURN, "CLIENT_CHECKIN_RETURN", "on_client"),
        (0x18F8BB140, "SERVER_BS_CHILD_ENQUEUE", "on_bs_dispatch"),
        (0x18F8BFC98, "SERVER_BS_ASYNC_DISPATCH_QUEUE", "on_bs_dispatch"),
        (0x18F8B9848, "SERVER_BS_CHILD_BLOCK_INVOKE", "on_bs_dispatch"),
        (full.XPC_DICT_HANDOFF, "XPC_DICT_HANDOFF", "on_xpc_handoff"),
        (full.XPC_MACH_HANDOFF, "XPC_MACH_HANDOFF", "on_xpc_handoff"),
        (full.XPC_MACH_DISPATCH_ARM, "XPC_MACH_DISPATCH_ARM", "on_xpc_handoff"),
        (full.XPC_CHANNEL_DISPATCH_ARM, "XPC_CHANNEL_DISPATCH_ARM", "on_xpc_handoff"),
        (full.DISPATCH_MACH_HANDOFF, "DISPATCH_MACH_HANDOFF", "on_xpc_handoff"),
    ):
        full._install(debugger, full.base.SLIDE + address, label, callback)

"""Capture the pre-existing lane head chain before migration handoff."""

import bks_handoff_queue_callbacks as queue_probe


full = queue_probe.full
on_client = full.on_client
on_bs_dispatch = full.on_bs_dispatch
on_xpc_handoff = full.on_xpc_handoff
on_site_return = full.on_site_return


def install(debugger, slide):
    queue_probe.install(debugger, slide,
                        callback_module="bks_handoff_head_callbacks")
    full.ENABLE_DRAIN_PROBES = False

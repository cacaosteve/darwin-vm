"""Read-only check-in/handoff/migration bracket plus corrected poster hooks.

This composes existing probes for one continuous CLASS16 child.  The pre-home
bracket records the SpringBoard check-in, backboardd receive/handoff, and the
matched DataMigrator ladder.  If migration reaches home, the same LLDB session
already has the corrected poster attachment and display-accept/draw hooks.
No guest state is changed.
"""

from pathlib import Path
import json

import lldb

import bks_server_receive_callbacks as bks
import dm_lock_attach_callbacks as attach
import dm_migration_full_ladder_callbacks as migration
import dm_repaired_ls_adfl_home_callbacks as home


# LLDB's scripted callbacks resolve names against a command-imported module.
# The full BKS probe installs dynamic callbacks through CALLBACK_MODULE.
on_anchor = bks.on_anchor
on_client = bks.on_client
on_site = bks.on_site
on_site_return = bks.on_site_return
on_client_xpc_send = bks.on_client_xpc_send
on_server_xpc_event = bks.on_server_xpc_event
on_server_xpc_handler = bks.on_server_xpc_handler
on_incoming_bs_connection = bks.on_incoming_bs_connection
on_bs_connection_initialized = bks.on_bs_connection_initialized
on_bs_dispatch = bks.on_bs_dispatch
on_generic_server = bks.on_generic_server
on_queue_drain = bks.on_queue_drain


def _import(debugger, module):
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "command script import %s" % Path(module.__file__).resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("failed LLDB import %s: %s" %
                           (module.__name__, result.GetError()))


def _add_bks_extra(debugger, slide, address, label, callback="on_xpc_handoff"):
    bks._install(debugger, int(slide) + address, label, callback)


def _queue_identity(process, queue):
    """Decode only the already-linked lane items and current owner state."""
    chain = bks._queue_chain(process, queue)
    for item in chain:
        context = item.get("context", 0)
        raw = bks.base._read(process, context, 0x40) if context else b""
        invoke = (int.from_bytes(raw[0x10:0x18], "little")
                  if len(raw) >= 0x18 else 0)
        item["block_invoke"] = "0x%x" % invoke if invoke else None
        item["block_invoke_static"] = (
            "0x%x" % (invoke - bks.base.SLIDE)
            if invoke >= bks.base.SLIDE else None)
        item["continuation_flags"] = (
            int.from_bytes(bytes.fromhex(item["bytes"])[0:4], "little")
            if len(item.get("bytes", "")) >= 8 else None)
        item["block_header"] = raw.hex()

    queue_raw = bks.base._read(process, queue, 0x90) if queue else b""
    target = (int.from_bytes(queue_raw[0x18:0x20], "little")
              if len(queue_raw) >= 0x20 else 0)
    target_raw = bks.base._read(process, target, 0x90) if target else b""
    target_state = (int.from_bytes(target_raw[0x38:0x40], "little")
                    if len(target_raw) >= 0x40 else None)
    return {
        "queue": "0x%x" % queue if queue else None,
        "queue_head": "0x%x" % int.from_bytes(queue_raw[0x68:0x70], "little")
        if len(queue_raw) >= 0x70 else None,
        "queue_tail": "0x%x" % int.from_bytes(queue_raw[0x30:0x38], "little")
        if len(queue_raw) >= 0x38 else None,
        "target_queue": "0x%x" % target if target else None,
        "target_dq_state": "0x%x" % target_state
        if target_state is not None else None,
        "target_enqueued": bool(target_state & 1)
        if target_state is not None else None,
        "target_drain_owner_bits": "0x%x" % (target_state & 0xFFFFFFFC)
        if target_state is not None else None,
        "target_dirty": bool(target_state & 0x8000000000)
        if target_state is not None else None,
        "chain": chain,
    }


def on_xpc_handoff(frame, bp_loc, context):
    bks.on_xpc_handoff(frame, bp_loc, context)
    label = bks.SITE_LABELS.get(bp_loc.GetBreakpoint().GetID())
    if label not in ("XPC_DICT_HANDOFF", "XPC_OUTER_CALLOUT_RETURN"):
        return False
    if bks.HANDOFF_QUEUE is None:
        return False
    process = frame.GetThread().GetProcess()
    print("DM_LOCK_CHECKIN_QUEUE_IDENTITY " + json.dumps({
        "label": label,
        "elapsed_from_handoff": (None if bks.HANDOFF_TIME is None else
                                  bks.time.time() - bks.HANDOFF_TIME),
        **_queue_identity(process, bks.HANDOFF_QUEUE),
    }, sort_keys=True), flush=True)
    return False


def _install_wait_controls(debugger, slide):
    target = debugger.GetSelectedTarget()
    for address, label in ((0x22A3912B4, "SWAP_WAIT_ENTRY"),
                           (0x22A391300, "SWAP_WAIT_RETURN")):
        bks._install(debugger, int(slide) + address, label,
                     "on_wait")
    WAIT.clear()


WAIT = {}


def on_wait(frame, bp_loc, _context):
    process = frame.GetThread().GetProcess()
    if bks.base._progname(process) != "backboardd":
        return False
    label = bks.SITE_LABELS[bp_loc.GetBreakpoint().GetID()]
    tid = bks.base._reg(frame, "tpidr_el1")
    if label == "SWAP_WAIT_ENTRY":
        fence, options = bks.base._reg(frame, "w1"), bks.base._reg(frame, "w2")
        if (fence, options) != (1, 1):
            return False
        WAIT[tid] = bks.time.time()
        detail = {"fence": fence, "options": options}
    else:
        started = WAIT.pop(tid, None)
        if started is None:
            return False
        detail = {"elapsed": bks.time.time() - started,
                  "result": bks.base._reg(frame, "w0")}
    print("DM_LOCK_CHECKIN_CONTROL label=%s thread=0x%x %s" %
          (label, tid, detail), flush=True)
    return False


def install(debugger, slide):
    # Import every module referenced by a scripted breakpoint callback, not
    # merely its Python dependencies.
    for module in (bks, migration, home, attach):
        _import(debugger, module)

    bks.CALLBACK_MODULE = __name__
    bks.ENABLE_DRAIN_PROBES = True
    bks.DRAIN_ARMED = False
    bks.DRAIN_HITS.clear()
    bks.install(debugger, slide)

    # These are the existing outer dispatch-Mach/handoff checkpoints used by
    # the queue probes.  Keep them paired to the same BKS child continuation.
    for address, label in (
        (0x1AE8D56A8, "DISPATCH_HANDOFF_SET_WLH"),
        (0x1AE8D5754, "DISPATCH_HANDOFF_SET_WLH_RETURN"),
        (0x1AE8D5CB4, "XPC_OUTER_CALLOUT_RETURN"),
        (0x1AE8D5DB4, "XPC_OUTER_HANDOFF_ASYNC_CALL"),
        (0x1AE8D5AB8, "XPC_ASYNC_ENTRY"),
        (0x1AE8D5B04, "XPC_ASYNC_ENQUEUE_RETURN"),
        (0x1AE8D58B4, "XPC_IPC_INVOKE"),
    ):
        _add_bks_extra(debugger, slide, address, label)

    # One entry arms the existing same-thread DM call/return ladder only if
    # __DMPerformMigration actually starts on this boot.
    migration.install(debugger, slide)

    # Reuse the ADFL/home addresses without installing its heavier historical
    # shell/migration baseline (already covered above).
    home.META.clear()
    home.ARMED.clear()
    home.COUNTS.clear()
    home.ADFL_RETURNED.clear()
    home.HOME_RETURNED.clear()
    for static, label in home.SITES:
        if label == "CA_CONTEXT_SET_LAYER":
            continue  # dm_lock_attach's rich decoder owns this site.
        home._register(debugger, int(slide) + static, label)

    # Existing corrected poster attachment + accepted-context prepare/OGL
    # hooks; this also asserts that hidden-cull sites stay excluded.
    attach.install(debugger, slide)
    _install_wait_controls(debugger, slide)
    print("DM_LOCK_CHECKIN_SURFACE_READY slide=0x%x" % int(slide), flush=True)

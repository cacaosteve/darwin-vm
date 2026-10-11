"""Read-only enqueue/wake trace for BoardServices connection activation blocks."""

import time

import bks_handoff_queue_callbacks as queue_probe


full = queue_probe.full
on_bs_dispatch = full.on_bs_dispatch
on_xpc_handoff = full.on_xpc_handoff
on_site_return = full.on_site_return

ACTIVATION_BLOCKS = {
    0x18F8D818C: "PENDED_EVENTS",
    0x18F8D68DC: "CONNECTION_ACTIVATED",
}
EARLY_ARMED = False
QUEUE_ARMED = False
ACTIVATION_QUEUE = None
ACTIVATION_TARGET = None
ACTIVATION_ROOT = None
PUSH_RETURNS = {}
QUEUE_HITS = {}


def _raw(process, pointer, size):
    return full.base._read(process, pointer, size).hex() if pointer else ""


def _current_thread_port(frame):
    """Read pthread_mach_thread_np's guest pthread+0xf8 field, without a call."""
    tsd_base = full.base._reg(frame, "tpidrro_el0")
    pthread = tsd_base - 0xE0 if tsd_base >= 0xE0 else 0
    raw = full.base._read(frame.GetThread().GetProcess(), pthread + 0xF8, 4)
    return dict(current_tsd_base=tsd_base, current_pthread=pthread,
                current_mach_thread_port=int.from_bytes(raw, "little")
                if len(raw) == 4 else None)


def _queue_state(process):
    queue = ACTIVATION_QUEUE
    target = ACTIVATION_TARGET
    root = ACTIVATION_ROOT
    target_bytes = full.base._read(process, target, 0x90) if target else b""
    target_state = (int.from_bytes(target_bytes[0x38:0x40], "little")
                    if len(target_bytes) >= 0x40 else None)
    return dict(activation_queue=queue, activation_target=target,
                activation_root=root,
                queue_bytes=_raw(process, queue, 0x90),
                target_bytes=target_bytes.hex(),
                target_dq_state=target_state,
                target_enqueued_bit=bool(target_state & 1)
                if target_state is not None else None,
                target_drain_owner_bits=target_state & 0xFFFFFFFC
                if target_state is not None else None,
                target_dirty_bit=bool(target_state & 0x8000000000)
                if target_state is not None else None,
                root_bytes=_raw(process, root, 0x90),
                queue_chain=full._queue_chain(process, queue) if queue else [])


def on_client(frame, bp_loc, context):
    global EARLY_ARMED
    if (full.base._progname(frame.GetThread().GetProcess()) == "SpringBoard" and
            full.SITE_LABELS[bp_loc.GetBreakpoint().GetID()] ==
            "SB_SHELL_START_SERVICE_CALL" and not EARLY_ARMED):
        debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
        full._install(debugger, full.base.SLIDE + 0x1AE8C4BD4,
                      "EARLY_LANE_PUSH", "on_activation_push")
        EARLY_ARMED = True
    return full.on_client(frame, bp_loc, context)


def on_activation_push(frame, bp_loc, context):
    global ACTIVATION_QUEUE, ACTIVATION_TARGET, ACTIVATION_ROOT, QUEUE_ARMED
    if full.base._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    process = frame.GetThread().GetProcess()
    queue = full.base._reg(frame, "x0")
    item = full.base._reg(frame, "x1")
    data = full.base._read(process, item, 0x40)
    if len(data) < 0x30:
        return False
    context = int.from_bytes(data[0x28:0x30], "little")
    block = full.base._read(process, context, 0x40)
    invoke = int.from_bytes(block[0x10:0x18], "little") if len(block) >= 0x18 else 0
    kind = ACTIVATION_BLOCKS.get(invoke - full.base.SLIDE)
    if not kind:
        if ACTIVATION_QUEUE and queue == ACTIVATION_TARGET and item == ACTIVATION_QUEUE:
            full._emit("ACTIVATION_TARGET_LANE_PUSH", frame,
                       **_queue_state(process))
        return False
    if ACTIVATION_QUEUE is None:
        ACTIVATION_QUEUE = queue
        raw = full.base._read(process, queue + 0x18, 8)
        ACTIVATION_TARGET = int.from_bytes(raw, "little") if len(raw) == 8 else None
        raw = (full.base._read(process, ACTIVATION_TARGET + 0x18, 8)
               if ACTIVATION_TARGET else b"")
        ACTIVATION_ROOT = int.from_bytes(raw, "little") if len(raw) == 8 else None
    if queue != ACTIVATION_QUEUE:
        full._emit("ACTIVATION_OTHER_QUEUE", frame, kind=kind,
                   queue=queue, item=item, context=context, invoke=invoke)
        return False
    if not QUEUE_ARMED:
        debugger = process.GetTarget().GetDebugger()
        for address, label in (*full.DRAIN_SITES,
                               (0x1AE8C3D44, "DISPATCH_WORKLOOP_PUSH"),
                               (0x1AE8C6C38, "DISPATCH_ROOT_PUSH"),
                               (0x1AE8C6D08, "DISPATCH_ROOT_PUSH_OVERRIDE_ARM"),
                               (0x1AE8C6B9C, "DISPATCH_ROOT_POKE_AND_WAKEUP"),
                               (0x1AE8C6BFC, "DISPATCH_ROOT_WAKEUP"),
                               (0x1AE8C688C, "DISPATCH_ROOT_POKE"),
                               (0x1AE8C68DC, "DISPATCH_ROOT_POKE_SLOW"),
                               (0x1AE8CB784, "DISPATCH_ROOT_DRAIN")):
            full._install(debugger, full.base.SLIDE + address, label,
                          "on_activation_queue")
        for address, label in (
            (0x1AE8C43B8, "DISPATCH_WAKE_TARGET_ENTRY"),
            (0x1AE8C447C, "DISPATCH_WAKE_PRE_CAS_DECISION"),
            (0x1AE8C44A4, "DISPATCH_WAKE_STATE_DECISION"),
            (0x1AE8C44BC, "DISPATCH_WAKE_BRANCH_DECISION"),
            (0x1AE8C44C0, "DISPATCH_WAKE_EVENT_LOOP_ARM"),
            (0x1AE8C44FC, "DISPATCH_WAKE_RETURN_ARM"),
            (0x1AE8C4538, "DISPATCH_WAKE_STATE_FALLBACK"),
            (0x1AE8C456C, "DISPATCH_WAKE_TARGET_PUSH_ARM"),
            (0x1AE8C4590, "DISPATCH_WAKE_TARGET_PUSH_CALL"),
            (0x1AE8C467C, "DISPATCH_WAKE_RETURN"),
        ):
            full._install(debugger, full.base.SLIDE + address, label,
                          "on_wake_branch")
        QUEUE_ARMED = True
    full._emit("ACTIVATION_PUSH_CALL", frame, kind=kind, item=item,
               context=context, block_bytes=block.hex(), invoke=invoke,
               item_bytes=data.hex(), **_current_thread_port(frame),
               **_queue_state(process))
    address = full.base._reg(frame, "lr") & 0x0000FFFFFFFFFFFF
    breakpoint = full._install(process.GetTarget().GetDebugger(), address,
                               "ACTIVATION_PUSH_RETURN", "on_activation_push_return")
    PUSH_RETURNS[breakpoint.GetID()] = (
        full.base._reg(frame, "tpidr_el1"), queue, item, kind, time.time())
    return False


def on_activation_push_return(frame, bp_loc, context):
    breakpoint = bp_loc.GetBreakpoint()
    saved = PUSH_RETURNS.get(breakpoint.GetID())
    if not saved:
        return False
    thread, queue, item, kind, started = saved
    if (full.base._progname(frame.GetThread().GetProcess()) != "backboardd" or
            full.base._reg(frame, "tpidr_el1") != thread):
        return False
    full._emit("ACTIVATION_PUSH_RETURN", frame, kind=kind, item=item,
               elapsed=time.time() - started,
               **_queue_state(frame.GetThread().GetProcess()))
    breakpoint.SetEnabled(False)
    return False


def on_activation_queue(frame, bp_loc, context):
    if full.base._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    queue_arg = full.base._reg(frame, "x0")
    if queue_arg not in (ACTIVATION_QUEUE, ACTIVATION_TARGET,
                         ACTIVATION_ROOT):
        return False
    label = full.SITE_LABELS[bp_loc.GetBreakpoint().GetID()]
    key = (label, queue_arg)
    QUEUE_HITS[key] = QUEUE_HITS.get(key, 0) + 1
    count = QUEUE_HITS[key]
    if count <= 30 or count % 100 == 0:
        full._emit(label, frame, hit=count, queue_arg=queue_arg,
                   **_queue_state(frame.GetThread().GetProcess()))
    return False


def on_wake_branch(frame, bp_loc, context):
    if full.base._progname(frame.GetThread().GetProcess()) != "backboardd":
        return False
    if full.base._reg(frame, "x8") != ACTIVATION_TARGET:
        return False
    full._emit(full.SITE_LABELS[bp_loc.GetBreakpoint().GetID()], frame,
               decision_state=full.base._reg(frame, "x1"),
               old_state=full.base._reg(frame, "x15"),
               state_delta=full.base._reg(frame, "x9"),
               wake_mask=full.base._reg(frame, "x10"),
               wake_flags=full.base._reg(frame, "x2"),
               candidate_target=full.base._reg(frame, "x3"),
               candidate_push=full.base._reg(frame, "x4"),
               **_queue_state(frame.GetThread().GetProcess()))
    return False


def install(debugger, slide):
    global EARLY_ARMED, QUEUE_ARMED, ACTIVATION_QUEUE, ACTIVATION_TARGET
    global ACTIVATION_ROOT
    queue_probe.install(debugger, slide,
                        callback_module="bks_activation_queue_callbacks")
    EARLY_ARMED = False
    QUEUE_ARMED = False
    ACTIVATION_QUEUE = None
    ACTIVATION_TARGET = None
    ACTIVATION_ROOT = None
    PUSH_RETURNS.clear()
    QUEUE_HITS.clear()
    full.ENABLE_DRAIN_PROBES = False

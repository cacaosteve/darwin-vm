"""Read-only DataMigrator send-barrier and dispatch-Mach drain probe.

Match the executable's semaphore-signal block by its instruction prefix, then
follow its exact connection, channel, send continuation, and target lane.
No guest function is called and no guest memory is written.
"""

import json
import time

import lldb

import bks_checkin_callbacks as bks


BLOCK_INVOKE_PREFIX = bytes.fromhex(
    "7f2303d5ff0301d1f65701a9f44f02a9fd7b03a9fdc30091")
SITES = {}
ARMED = False
NEXT_ID = 0
PENDING = {}
BARRIERS = {}
LAST_BY_SEMAPHORE = {}
HITS = {}


def _reg(frame, name):
    return bks._reg(frame, name)


def _u64(process, address):
    return bks._u64(process, address) if address else None


def _raw(process, address, size):
    return bks._read(process, address, size).hex() if address else ""


def _thread(frame):
    return frame.GetThread()


def _emit(label, frame, **extra):
    thread = _thread(frame)
    process = thread.GetProcess()
    event = dict(label=label, time=time.time(), progname=bks._progname(process),
                 pc=_reg(frame, "pc"), thread=thread.GetThreadID(),
                 lr=_reg(frame, "lr"), x0=_reg(frame, "x0"),
                 x1=_reg(frame, "x1"), x2=_reg(frame, "x2"))
    event.update(extra)
    print("DATAMIGRATOR_BARRIER_JSON " + json.dumps(event, sort_keys=True),
          flush=True)
    bks._write_event("progress.%s.json" % label, event)


def _queue_state(process, record):
    target = record["target"]
    refs = record["refs"]
    return dict(barrier_id=record["id"], connection=record["connection"],
                channel=record["channel"], target_queue=target,
                send_refs=refs, semaphore=record["semaphore"],
                barrier_block=record["block"], barrier_item=record.get("item"),
                channel_bytes=_raw(process, record["channel"], 0x90),
                target_bytes=_raw(process, target, 0x90),
                send_refs_bytes=_raw(process, refs, 0x68),
                send_refs_state=_u64(process, refs + 0x40) if refs else None,
                send_refs_tail=_u64(process, refs + 0x48) if refs else None,
                send_refs_head=_u64(process, refs + 0x50) if refs else None,
                target_head=_u64(process, target + 0x68) if target else None,
                target_tail=_u64(process, target + 0x30) if target else None,
                target_state=_u64(process, target + 0x38) if target else None)


def _install(debugger, address, label, callback):
    target = debugger.GetSelectedTarget()
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint set --address 0x%x" % address, result)
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed to arm %s: %s" % (label, result.GetError()))
    bp = target.GetBreakpointAtIndex(before)
    SITES[bp.GetID()] = label
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command add -F datamigrator_barrier_callbacks.%s %d" %
        (callback, bp.GetID()), result)
    if not result.Succeeded():
        raise RuntimeError("failed to register %s: %s" %
                           (label, result.GetError()))
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
          (bp.GetID(), label, address), flush=True)
    return bp


def on_xpc_barrier(frame, bp_loc, _context):
    global ARMED, NEXT_ID
    process = _thread(frame).GetProcess()
    if not bks._progname(process).startswith("com.apple.datami"):
        return False
    block = _reg(frame, "x1")
    invoke = _u64(process, block + 0x10) if block else None
    invoke = invoke & 0x0000FFFFFFFFFFFF if invoke else None
    if not invoke or bks._read(process, invoke, len(BLOCK_INVOKE_PREFIX)) != BLOCK_INVOKE_PREFIX:
        return False
    connection = _reg(frame, "x0")
    semaphore = _u64(process, block + 0x28)
    channel = _u64(process, connection + 0xA8)
    target = _u64(process, channel + 0x18) if channel else None
    refs = _u64(process, channel + 0x80) if channel else None
    if not all((connection, block, semaphore, channel, target, refs)):
        raise RuntimeError("matched barrier has unreadable connection/queue state")
    NEXT_ID += 1
    record = dict(id=NEXT_ID, connection=connection, block=block,
                  semaphore=semaphore, channel=channel, target=target,
                  refs=refs)
    PENDING[_thread(frame).GetThreadID()] = record
    LAST_BY_SEMAPHORE[semaphore] = record
    if not ARMED:
        debugger = process.GetTarget().GetDebugger()
        for address, label, callback in (
            (0x1AE8D6248, "DISPATCH_MACH_SEND_BARRIER", "on_stage"),
            (0x1AE8D51F8, "DISPATCH_MACH_SEND_PUSH", "on_stage"),
            (0x1AE8D5E14, "MACH_SEND_BARRIER_INVOKE", "on_stage"),
            (0x1AE8BAE94, "SEMAPHORE_WAIT", "on_stage"),
            (0x1AE8BAE78, "SEMAPHORE_SIGNAL", "on_stage"),
            (0x1AE8C2338, "TARGET_LANE_INVOKE", "on_stage"),
        ):
            _install(debugger, bks.SLIDE[0] + address, label, callback)
        _install(debugger, invoke, "SIGNAL_BLOCK_INVOKE", "on_stage")
        ARMED = True
    _emit("DATAMIGRATOR_XPC_SEND_BARRIER", frame, block_invoke=invoke,
          block_bytes=_raw(process, block, 0x40),
          **_queue_state(process, record))
    return False


def on_stage(frame, bp_loc, _context):
    process = _thread(frame).GetProcess()
    if not bks._progname(process).startswith("com.apple.datami"):
        return False
    label = SITES[bp_loc.GetBreakpoint().GetID()]
    arg = _reg(frame, "x0")
    record = None
    if label == "DISPATCH_MACH_SEND_BARRIER":
        record = PENDING.get(_thread(frame).GetThreadID())
        matched = record is not None and arg == record["channel"]
    elif label == "DISPATCH_MACH_SEND_PUSH":
        record = PENDING.get(_thread(frame).GetThreadID())
        item = _reg(frame, "x1")
        vtable = _u64(process, item) if item else None
        matched = (record is not None and arg == record["channel"] and
                   "item" not in record and vtable is not None and
                   vtable & 0x0000FFFFFFFFFFFF ==
                   bks.SLIDE[0] + 0x1EC141250)
        if matched:
            record["item"] = item
            BARRIERS[record["item"]] = record
    elif label == "MACH_SEND_BARRIER_INVOKE":
        record = BARRIERS.get(arg)
        matched = record is not None
    elif label in ("SEMAPHORE_WAIT", "SEMAPHORE_SIGNAL"):
        record = LAST_BY_SEMAPHORE.get(arg)
        matched = record is not None
    elif label == "TARGET_LANE_INVOKE":
        record = next((r for r in reversed(list(LAST_BY_SEMAPHORE.values()))
                       if arg == r["target"]), None)
        matched = record is not None
    else:
        semaphore = _u64(process, arg + 0x28) if arg else None
        record = LAST_BY_SEMAPHORE.get(semaphore)
        matched = label == "SIGNAL_BLOCK_INVOKE" and record is not None
    if not matched:
        return False
    key = (record["id"], label)
    HITS[key] = HITS.get(key, 0) + 1
    if HITS[key] <= 8:
        _emit(label, frame, hit=HITS[key],
              item_bytes=_raw(process, record.get("item"), 0x40),
              **_queue_state(process, record))
    return False


def install(debugger, slide):
    global ARMED, NEXT_ID
    bks.SLIDE[0] = int(slide)
    SITES.clear()
    HITS.clear()
    PENDING.clear()
    BARRIERS.clear()
    LAST_BY_SEMAPHORE.clear()
    ARMED = False
    NEXT_ID = 0
    _install(debugger, bks.SLIDE[0] + 0x1805B268C,
             "XPC_SEND_BARRIER_ENTRY", "on_xpc_barrier")

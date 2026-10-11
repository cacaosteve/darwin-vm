"""Narrow, non-stopping observers for Setup Assistant activation in P150."""

import json
import os
import time

import lldb


SITES = (
    (0x224478298, "SB_RETURN_TO_LOCK_TRANSACTION_ENTRY"),
    (0x22447835C, "SB_FORCE_BUDDY_BLOCK_ENTRY"),
    (0x2243BF920, "SB_SETUP_APPLICATION_LOOKUP_ENTRY"),
    (0x2244783D4, "SB_SETUP_APP_LOOKUP_RETURN"),
    (0x22433FF18, "SB_WORKSPACE_ACTIVATE_APPLICATION_ENTRY"),
    (0x2242E47EC, "SB_TOAPPS_DIDCOMPLETE_ENTRY"),
    (0x2242E48D0, "SB_TOAPPS_UPDATE_RETURN"),
    (0x2242E4A5C, "SB_TOAPPS_SETUPAPP_RETURN"),
    (0x2242E4A64, "SB_TOAPPS_ACTIVATE_CALL"),
    (0x224479448, "SB_HANDLE_SETUP_EXITED"),
)

META = {}
COUNTS = {}
RETURN_BP_BY_ADDRESS = {}
RETURN_CONTEXTS = {}
OUT = "/tmp/dvm/p150-setup-activation.jsonl"
LIMIT = 24
RETURN_LIMIT = 128


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else None


def _emit(row):
    row["time"] = time.time()
    line = json.dumps(row, sort_keys=True)
    with open(OUT, "a") as stream:
        stream.write(line + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    print("P150_SETUP_ACTIVATION " + line, flush=True)


def _read_u64(process, address):
    if not address:
        return None
    error = lldb.SBError()
    data = process.ReadMemory(address, 8, error)
    if not error.Success() or len(data) != 8:
        return None
    return int.from_bytes(data, "little")


def _arm_setup_application_return(frame):
    """Follow setupApplication's tail-call result to its original caller."""
    thread = frame.GetThread()
    process = thread.GetProcess()
    lr = _reg(frame, "lr") or 0
    sp = _reg(frame, "sp") or 0
    tpidr = _reg(frame, "tpidr_el1") or 0
    if not lr:
        _emit({"label": "SB_SETUP_APPLICATION_LOOKUP_RETURN_UNARMED",
               "reason": "null-lr", "pid": process.GetProcessID(),
               "sp": hex(sp)})
        return
    pending = sum(map(len, RETURN_CONTEXTS.values()))
    if pending >= RETURN_LIMIT:
        _emit({"label": "SB_SETUP_APPLICATION_LOOKUP_RETURN_UNARMED",
               "reason": "context-limit", "limit": RETURN_LIMIT,
               "lr": hex(lr)})
        return
    target = process.GetTarget()
    bp_id = RETURN_BP_BY_ADDRESS.get(lr)
    if bp_id is None:
        bp = target.BreakpointCreateByAddress(lr)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            _emit({"label": "SB_SETUP_APPLICATION_LOOKUP_RETURN_UNARMED",
                   "reason": "invalid-return-bp", "lr": hex(lr),
                   "locations": bp.GetNumLocations() if bp.IsValid() else 0})
            return
        bp.SetScriptCallbackFunction(__name__ + ".on_hit")
        bp.SetAutoContinue(True)
        bp_id = bp.GetID()
        RETURN_BP_BY_ADDRESS[lr] = bp_id
        RETURN_CONTEXTS[bp_id] = []
    context = {
        "pid": process.GetProcessID(), "tpidr": tpidr, "sp": sp,
        "entry_lr": lr, "entry_x0": _reg(frame, "x0") or 0,
        "entry_x1": _reg(frame, "x1") or 0,
        "entry_x3": _reg(frame, "x3") or 0,
        "thread_id_at_entry": thread.GetThreadID(), "time": time.time(),
    }
    RETURN_CONTEXTS[bp_id].append(context)
    _emit({"label": "SB_SETUP_APPLICATION_LOOKUP_DYNAMIC_RETURN_ARMED",
           "bp_id": bp_id, "lr": hex(lr), "pid": context["pid"],
           "tpidr_el1": hex(tpidr), "sp": hex(sp),
           "entry_x0": hex(context["entry_x0"]),
           "entry_x1": hex(context["entry_x1"]),
           "entry_x3": hex(context["entry_x3"]),
           "thread_id": context["thread_id_at_entry"],
           "pending_for_return_pc": len(RETURN_CONTEXTS[bp_id])})


def on_hit(frame, bp_loc, _context):
    bp = bp_loc.GetBreakpoint()
    bp_id = bp.GetID()
    thread = frame.GetThread()
    process = thread.GetProcess()
    if process.GetProcessID() != 1:
        return False
    if bp_id in RETURN_CONTEXTS:
        tpidr = _reg(frame, "tpidr_el1") or 0
        sp = _reg(frame, "sp") or 0
        contexts = RETURN_CONTEXTS[bp_id]
        match = next((index for index in range(len(contexts) - 1, -1, -1)
                      if contexts[index]["pid"] == process.GetProcessID() and
                      contexts[index]["tpidr"] == tpidr and
                      contexts[index]["sp"] == sp), None)
        if match is None:
            return False
        expected = contexts.pop(match)
        result = _reg(frame, "x0") or 0
        isa = _read_u64(process, result)
        _emit({"label": "SB_SETUP_APPLICATION_LOOKUP_DYNAMIC_RETURN",
               "pid": process.GetProcessID(),
               "thread_id": thread.GetThreadID(), "tpidr_el1": hex(tpidr),
               "sp": hex(sp), "pc": hex(_reg(frame, "pc") or 0),
               "entry_lr": hex(expected["entry_lr"]),
               "entry_x0": hex(expected["entry_x0"]),
               "entry_x1": hex(expected["entry_x1"]),
               "entry_x3": hex(expected["entry_x3"]),
               "entry_thread_id": expected["thread_id_at_entry"],
               "result_x0": hex(result), "result_is_null": result == 0,
               "result_isa_word": None if isa is None else hex(isa),
               "entry_time": expected["time"]})
        if not contexts:
            bp.SetEnabled(False)
            RETURN_CONTEXTS.pop(bp_id, None)
        return False
    label = META.get(bp_id)
    if not label:
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    row = {"label": label, "count": count, "pid": process.GetProcessID(),
           "thread_id": thread.GetThreadID(),
           "tpidr_el1": hex(_reg(frame, "tpidr_el1") or 0),
           "pc": hex(_reg(frame, "pc") or 0),
           "lr": hex(_reg(frame, "lr") or 0),
           "sp": hex(_reg(frame, "sp") or 0),
           **{"x%d" % i: _reg(frame, "x%d" % i) for i in range(4)}}
    _emit(row)
    if label == "SB_SETUP_APPLICATION_LOOKUP_ENTRY":
        _arm_setup_application_return(frame)
    if count >= LIMIT:
        bp.SetEnabled(False)
    return False


def install(debugger, slide, output_path=None):
    global OUT
    if output_path:
        OUT = output_path
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    target = debugger.GetSelectedTarget()
    META.clear()
    COUNTS.clear()
    RETURN_BP_BY_ADDRESS.clear()
    RETURN_CONTEXTS.clear()
    armed = []
    for offset, label in SITES:
        bp = target.BreakpointCreateByAddress(int(slide) + offset)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("could not arm %s at %#x" %
                               (label, int(slide) + offset))
        META[bp.GetID()] = label
        bp.SetScriptCallbackFunction(__name__ + ".on_hit")
        bp.SetAutoContinue(True)
        armed.append({"label": label, "id": bp.GetID(),
                      "address": hex(int(slide) + offset),
                      "locations": bp.GetNumLocations()})
    _emit({"label": "P150_SETUP_ACTIVATION_READY", "slide": hex(int(slide)),
           "armed": armed, "output": OUT})

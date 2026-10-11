"""Read-only A406 reply -> userland swap object -> A407 identity trace.

Kernel sites use unslid IOMobileGraphicsFamily addresses, and user sites
use unslid shared-cache addresses. This callback only reads registers and
guest memory; it does not call guest code, change the model, or alter the
CLASS16 fixture. Require COMMAND_LIST_PROOF for every site before scoring.
"""

import json
import os
import time

import lldb

import bks_checkin_callbacks as bks
from dm_plugin_lifecycle_callbacks import _read


KERNEL_SLIDE = int(os.environ.get("DVM_KERNEL_SLIDE", "0x20000000"), 0)
SITES = (
    (0xFFFFfff00A0BFBE0, "CREATE1_ALLOC_CALL"),
    (0xFFFFfff00A0BFBE4, "CREATE1_ALLOC_RETURN"),
    (0xFFFFfff00A0BFD48, "CREATE2_ALLOC_CALL"),
    (0xFFFFfff00A0BFD4C, "CREATE2_ALLOC_RETURN"),
    (0xFFFFfff00A0C8C50, "A406_WRAPPER_ENTRY"),
    (0xFFFFfff00A0C8CE8, "A406_REPLY_COPY"),
    (0xFFFFfff00A0C8CF8, "A406_WRAPPER_RETURN"),
    (0xFFFFfff00A0E5CE4, "A406_CALLER_ID_STORE"),
    (0xFFFFfff00A0E5CE8, "A406_CALLER_ID_STORED"),
    (0xFFFFfff00A0C366C, "SWAPEND_THUNK"),
    (0xFFFFfff00A0C36D4, "SWAPEND_RECORD"),
    (0xFFFFfff00A0C3730, "PENDING_ID_LOAD"),
    (0xFFFFfff00A0C3748, "PENDING_ID_STORED"),
    (0xFFFFfff00A0C8D08, "A407_WRAPPER_ENTRY"),
    (0xFFFFfff00A0DA3EC, "D582_ENTRY"),
    (0xFFFFfff00A0DB034, "D594_ENTRY"),
    (0xFFFFfff00A0DB1A8, "D595_ENTRY"),
    (0xFFFFfff00A0C5728, "D582_REMOVER"),
    (0xFFFFfff00A0C58BC, "FULL_REMOVER"),
)
USER_SITES = (
    (0x22A3911CC, "KERN_BEGIN_ENTRY"),
    (0x22A391224, "KERN_BEGIN_RPC_RETURN"),
    (0x22A391258, "KERN_BEGIN_ID_STORE"),
    (0x22A39126C, "KERN_BEGIN_RESULT"),
    (0x22A391334, "KERN_END_ENTRY"),
    (0x22A3912B4, "KERN_WAIT_ENTRY"),
    (0x22A391300, "KERN_WAIT_RETURN"),
    (0x22A396BE0, "VIRT_BEGIN_ENTRY"),
    (0x22A396C70, "VIRT_END_ENTRY"),
)
META = {}
COUNTS = {}
ALLOC_CALLS = {}
RECORDS = {}
THUNKS = {}
CALLER_STORES = {}
BEGIN_CALLS = {}
BEGIN_OBJECTS = {}
WAIT_CALLS = {}


def _u32(process, address):
    raw = _read(process, address, 4) if address else b""
    return int.from_bytes(raw, "little") if len(raw) == 4 else None


def _hex(value):
    return None if value is None else "0x%x" % value


def _emit(frame, label, fields):
    row = {"label": label, "time": time.time(),
           "thread": frame.GetThread().GetThreadID(),
           "tpidr_el1": _hex(bks._reg(frame, "tpidr_el1"))}
    row.update(fields)
    print("DM_A406_ID " + json.dumps(row, sort_keys=True), flush=True)


def on_hit(frame, bp_loc, _context):
    label = META[bp_loc.GetBreakpoint().GetID()]
    count = COUNTS[label] = COUNTS.get(label, 0) + 1
    if count > 24:
        return False
    process = frame.GetThread().GetProcess()
    owner = bks._reg(frame, "tpidr_el1")
    fields = {"count": count}

    if label in ("VIRT_BEGIN_ENTRY", "VIRT_END_ENTRY"):
        obj = bks._reg(frame, "x0")
        fields.update(process=bks._progname(process), object=_hex(obj),
                      record=_hex(obj + 0x18),
                      record_id=_hex(_u32(process, obj + 0xb0)))
    elif label == "KERN_BEGIN_ENTRY":
        obj = bks._reg(frame, "x0")
        BEGIN_CALLS[owner] = obj
        fields.update(process=bks._progname(process), object=_hex(obj),
                      before_id=_hex(_u32(process, obj + 0xb0)),
                      output_pointer=_hex(bks._reg(frame, "x1")))
    elif label == "KERN_BEGIN_RPC_RETURN":
        obj = BEGIN_CALLS.get(owner)
        if obj is None:
            return False
        sp = bks._reg(frame, "sp")
        fields.update(object=_hex(obj), io_result=_hex(bks._reg(frame, "w0")),
                      scalar_output_id=_hex(_u32(process, sp + 8)))
    elif label == "KERN_BEGIN_ID_STORE":
        obj = BEGIN_CALLS.get(owner)
        if obj is None:
            return False
        fields.update(object=_hex(obj), new_id=_hex(bks._reg(frame, "w8")),
                      before_id=_hex(_u32(process, obj + 0xb0)))
    elif label == "KERN_BEGIN_RESULT":
        obj = BEGIN_CALLS.pop(owner, None)
        if obj is None:
            return False
        value = _u32(process, obj + 0xb0)
        result = bks._reg(frame, "w0")
        if result == 0:
            BEGIN_OBJECTS[obj] = value
        fields.update(object=_hex(obj), stored_id=_hex(value),
                      result=_hex(result))
    elif label == "KERN_END_ENTRY":
        obj = bks._reg(frame, "x0")
        fields.update(process=bks._progname(process), object=_hex(obj),
                      record=_hex(obj + 0x18),
                      record_id=_hex(_u32(process, obj + 0xb0)),
                      begin_id=_hex(BEGIN_OBJECTS.get(obj)),
                      same_begin_object=(obj in BEGIN_OBJECTS))
    elif label == "KERN_WAIT_ENTRY":
        obj = bks._reg(frame, "x0")
        WAIT_CALLS[owner] = {"object": obj, "start": time.time(),
                             "fence": bks._reg(frame, "w1"),
                             "options": bks._reg(frame, "w2")}
        fields.update(object=_hex(obj), record_id=_hex(_u32(process, obj + 0xb0)),
                      fence=_hex(bks._reg(frame, "w1")),
                      options=_hex(bks._reg(frame, "w2")),
                      same_begin_object=(obj in BEGIN_OBJECTS))
    elif label == "KERN_WAIT_RETURN":
        call = WAIT_CALLS.pop(owner, None)
        if call is None:
            return False
        fields.update(object=_hex(call["object"]), fence=_hex(call["fence"]),
                      options=_hex(call["options"]),
                      result=_hex(bks._reg(frame, "w0")),
                      elapsed_seconds=time.time() - call["start"])
    elif label.endswith("ALLOC_CALL"):
        record = bks._reg(frame, "x19")
        field = bks._reg(frame, "x1")
        ALLOC_CALLS[owner] = {"record": record, "field": field,
                              "call": label}
        fields.update(record=_hex(record), field=_hex(field),
                      field_matches_record_98=(record is not None and field == record + 0x98),
                      before=_hex(_u32(process, field)),
                      signed_target=_hex(bks._reg(frame, "x9")))
    elif label == "A406_WRAPPER_ENTRY":
        field = bks._reg(frame, "x1")
        call = ALLOC_CALLS.get(owner)
        pcs = [frame.GetThread().GetFrameAtIndex(i).GetPC()
               for i in range(min(6, frame.GetThread().GetNumFrames()))]
        fields.update(process=bks._progname(process),
                      field=_hex(field), before=_hex(_u32(process, field)),
                      lr=_hex(bks._reg(frame, "lr")), stack_pcs=[_hex(pc) for pc in pcs],
                      active_begin_object=_hex(BEGIN_CALLS.get(owner)),
                      matched_call=call["call"] if call and call["field"] == field else None,
                      record=_hex(call["record"]) if call and call["field"] == field else None)
    elif label == "A406_REPLY_COPY":
        sp = bks._reg(frame, "sp")
        field = bks._reg(frame, "x19")
        call = ALLOC_CALLS.get(owner)
        fields.update(field=_hex(field), output_id=_hex(_u32(process, sp + 8)),
                      output_status=_hex(_u32(process, sp + 12)),
                      matched_call=call["call"] if call and call["field"] == field else None)
    elif label == "A406_WRAPPER_RETURN":
        field = bks._reg(frame, "x19")
        fields.update(field=_hex(field), stored_id=_hex(_u32(process, field)),
                      result=_hex(bks._reg(frame, "w0")))
    elif label == "A406_CALLER_ID_STORE":
        descriptor = bks._reg(frame, "x19")
        target = bks._reg(frame, "x9")
        CALLER_STORES[owner] = {"descriptor": descriptor, "target": target,
                                "id": bks._reg(frame, "w8")}
        fields.update(descriptor=_hex(descriptor), target=_hex(target),
                      descriptor_target=_hex(bks._u64(process, descriptor + 0x48)),
                      active_begin_object=_hex(BEGIN_CALLS.get(owner)),
                      id=_hex(bks._reg(frame, "w8")),
                      old_id=_hex(_u32(process, target)))
    elif label == "A406_CALLER_ID_STORED":
        call = CALLER_STORES.get(owner)
        target = call["target"] if call else bks._reg(frame, "x9")
        fields.update(descriptor=_hex(call["descriptor"]) if call else None,
                      target=_hex(target), stored_id=_hex(_u32(process, target)))
    elif label.endswith("ALLOC_RETURN"):
        call = ALLOC_CALLS.pop(owner, None)
        record = call["record"] if call else bks._reg(frame, "x19")
        field = record + 0x98 if record else 0
        if record:
            RECORDS[record] = {"id": _u32(process, field),
                               "call": call["call"] if call else None}
        fields.update(record=_hex(record), field=_hex(field),
                      id=_hex(_u32(process, field)),
                      status=_hex(bks._reg(frame, "w0")),
                      matched_call=call["call"] if call else None)
    elif label == "SWAPEND_THUNK":
        record = bks._reg(frame, "x1")
        THUNKS[owner] = record
        fields.update(record=_hex(record), id=_hex(_u32(process, record + 0x98)),
                      matched_create=RECORDS.get(record))
    elif label == "SWAPEND_RECORD":
        record = bks._reg(frame, "x1")
        fields.update(record=_hex(record), id=_hex(_u32(process, record + 0x98)),
                      matched_thunk=(THUNKS.get(owner) == record),
                      matched_create=RECORDS.get(record))
    elif label == "PENDING_ID_LOAD":
        record = bks._reg(frame, "x24")
        fields.update(record=_hex(record), id=_hex(_u32(process, record + 0x98)),
                      pending_node=_hex(bks._reg(frame, "x23")),
                      matched_create=RECORDS.get(record))
    elif label == "PENDING_ID_STORED":
        record = bks._reg(frame, "x24")
        node = bks._reg(frame, "x23")
        fields.update(record=_hex(record), record_id=_hex(_u32(process, record + 0x98)),
                      pending_node=_hex(node), node_id=_hex(_u32(process, node + 0x3d4)),
                      matched_create=RECORDS.get(record))
    elif label == "A407_WRAPPER_ENTRY":
        record = bks._reg(frame, "x1")
        fields.update(record=_hex(record), id=_hex(_u32(process, record + 0x98)),
                      matched_create=RECORDS.get(record))
    elif label in ("D582_ENTRY", "D594_ENTRY", "D595_ENTRY"):
        payload = bks._reg(frame, "x2")
        fields.update(payload=_hex(payload), id=_hex(_u32(process, payload)),
                      count=_hex(_u32(process, payload + 0x400))
                      if label == "D595_ENTRY" else None,
                      input_size=_hex(bks._reg(frame, "w3")))
    elif label in ("D582_REMOVER", "FULL_REMOVER"):
        fb = bks._reg(frame, "x0")
        head = bks._u64(process, fb + 0x4138) if fb else None
        fields.update(framebuffer=_hex(fb), id=_hex(bks._reg(frame, "w1")),
                      pending_head=_hex(head),
                      pending_head_id=_hex(_u32(process, head + 0x3d4))
                      if head else None)
    _emit(frame, label, fields)
    return False


def install(debugger, _slide):
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    for static, label in SITES + USER_SITES:
        address = static + (int(_slide) if (static, label) in USER_SITES else KERNEL_SLIDE)
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % address, result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed to arm %s: %s" % (label, result.GetError()))
        bp = target.GetBreakpointAtIndex(before)
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F dm_a406_id_lineage_callbacks.on_hit %d" %
            bp.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("failed callback %s: %s" % (label, result.GetError()))
        META[bp.GetID()] = label
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(), result)
        if "dm_a406_id_lineage_callbacks.on_hit" not in (result.GetOutput() or result.GetError()):
            raise RuntimeError("missing command proof for %s" % label)
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" %
              (bp.GetID(), label, address), flush=True)
    print("DM_A406_ID_READY kernel_slide=0x%x user_slide=0x%x sites=%d" %
          (KERNEL_SLIDE, int(_slide), len(SITES) + len(USER_SITES)), flush=True)

"""Read-only native input path markers for the post-shell unlock probe."""

import json
import os
import struct
import time
from pathlib import Path

import lldb

import render_gate_callbacks as render
import welcome_abort_callbacks as names


POINTS = {
    0x2678A7220: "VIRTUAL_HID_DISPATCH_RETURN",
    0x22A352848: "BACKBOARD_SYSTEM_EVENT",
    0x22A351FAC: "BACKBOARD_PRIMARY_EVENT",
    # This is the first processor in the live primary-processor array on P177.
    # The primary event loop calls each processor through the common
    # processEvent:sender:dispatcher: entry; observing this boundary separates
    # BackBoard recognizer handling from later UIKit/SpringBoard delivery.
    0x22A350C3C: "BACKBOARD_RECOGNIZER_ENGINE",
    # BKHIDPrimaryEventProcessor's array dispatch call. At this instruction
    # x0 is the selected processor and x1 the tagged processEvent selector.
    # For direct-touch events x2 is a stack wrapper whose first word is the
    # HIDEvent object; it is not itself an Objective-C event.
    0x22A352074: "BACKBOARD_PROCESSOR_DISPATCH_CALL",
    # The selected direct-touch processor resolves the target service before
    # forwarding the event to BKDirectTouchState.
    0x22A415E40: "BACKBOARD_DIRECT_TOUCH_PROCESS_EVENT",
    0x22A413FE8: "BACKBOARD_DETERMINE_TOUCH_SERVICE",
    0x22A414320: "BACKBOARD_HANDLE_DIRECT_TOUCH_EVENT",
    0x22A414E48: "BACKBOARD_HIT_COORDINATES",
    0x22439E9FC: "SPRINGBOARD_HID_EVENT",
    0x18493425C: "UI_EVENT",
    0x184E40DC0: "UI_WINDOW_HIT_TEST",
}
SLIDE = 0
EVENT_DIR = ""
COUNTS = {}
LIMIT = 256
STOP_LABELS = set()
HIT_TEST_RETURNS = {}
HIT_TEST_RETURN_ARMED = set()
PRIMARY_EVENT_RETURNS = {}
PRIMARY_EVENT_RETURN_BPS = {}
EVENT_OUT = None


def _emit(row):
    line = "UNLOCK_INPUT_JSON " + json.dumps(row, sort_keys=True)
    print(line, flush=True)
    if EVENT_OUT is not None:
        try:
            with EVENT_OUT.open("a") as output:
                output.write(line + "\n")
        except Exception as error:
            print("UNLOCK_INPUT_FILE_ERROR " + repr(error), flush=True)


def _pointer_probe(process, value):
    if not isinstance(value, int) or value < 0x10000:
        return None
    row = {"address": hex(value)}
    try:
        row["class"] = render._objc_class_name(process, value)
    except Exception as error:
        row["class_error"] = repr(error)
    try:
        error = lldb.SBError()
        blob = process.ReadMemory(value, 32, error)
        if error.Success() and blob:
            row["bytes32"] = blob.hex()
            words = [int.from_bytes(blob[i:i + 8], "little")
                     for i in range(0, len(blob) - 7, 8)]
            row["u64_words"] = [hex(word) for word in words]
            referents = {}
            for index, candidate in enumerate(words):
                if candidate < 0x700000000:
                    continue
                ref = {"address": hex(candidate)}
                try:
                    ref["class"] = render._objc_class_name(process, candidate)
                except Exception as class_error:
                    ref["class_error"] = repr(class_error)
                try:
                    ref_error = lldb.SBError()
                    ref_size = (128 if ref.get("class") == "HIDEvent" else
                                48 if ref.get("class") == "__NSArrayM" else 16)
                    ref_blob = process.ReadMemory(candidate, ref_size, ref_error)
                    if not ref_error.Success() or not ref_blob:
                        raise RuntimeError(ref_error.GetCString() or "empty read")
                    ref["bytes"] = ref_blob.hex()
                    if ref.get("class") == "__NSArrayM":
                        array_words = [
                            int.from_bytes(ref_blob[i:i + 8], "little")
                            for i in range(0, len(ref_blob) - 7, 8)
                        ]
                        storage = array_words[2] if len(array_words) > 2 else 0
                        count_candidate = (array_words[3] >> 32
                                           if len(array_words) > 3 else 0)
                        ref["array_count_candidate"] = count_candidate
                        ref["array_storage_candidate"] = hex(storage)
                        if 0x700000000 <= storage < 0x800000000000:
                            array_error = lldb.SBError()
                            array_blob = process.ReadMemory(storage, 64, array_error)
                            if array_error.Success() and array_blob:
                                items = []
                                for idx in range(0, min(len(array_blob), 64), 8):
                                    item = int.from_bytes(array_blob[idx:idx + 8], "little")
                                    if item < 0x700000000:
                                        continue
                                    item_row = {"address": hex(item)}
                                    try:
                                        item_row["class"] = render._objc_class_name(process, item)
                                    except Exception as item_error:
                                        item_row["class_error"] = repr(item_error)
                                    item_error = lldb.SBError()
                                    item_data = process.ReadMemory(item, 32, item_error)
                                    if item_error.Success() and item_data:
                                        item_row["bytes32"] = item_data.hex()
                                    items.append(item_row)
                                ref["array_items_candidate"] = items
                    if ref.get("class") == "HIDEvent":
                        children = {}
                        for offset in (0x40, 0x48, 0x60, 0x78, 0x80):
                            if offset + 8 > len(ref_blob):
                                continue
                            child = int.from_bytes(ref_blob[offset:offset + 8], "little")
                            if child < 0x700000000:
                                continue
                            child_row = {"address": hex(child)}
                            try:
                                child_row["class"] = render._objc_class_name(process, child)
                            except Exception as child_error:
                                child_row["class_error"] = repr(child_error)
                            child_error = lldb.SBError()
                            child_blob = process.ReadMemory(child, 48, child_error)
                            child_words = []
                            if child_error.Success() and child_blob:
                                child_row["bytes48"] = child_blob.hex()
                                child_words = [
                                    int.from_bytes(child_blob[i:i + 8], "little")
                                    for i in range(0, len(child_blob) - 7, 8)
                                ]
                                child_row["u64_words"] = [hex(word) for word in child_words]
                            if child_row.get("class") == "__NSArrayM":
                                # Candidate NSMutableArray storage/count, captured at the
                                # processEvent boundary while its child pointers are live.
                                array_data = child_words[2] if len(child_words) > 2 else 0
                                # The captured NSArrayM layout packs its count in the
                                # high 32 bits of word 3 (word 3 was 0x0000000200000000).
                                array_count = (child_words[3] >> 32) if len(child_words) > 3 else 0
                                child_row["array_count_candidate"] = array_count
                                child_row["array_storage_candidate"] = hex(array_data)
                                if 0x700000000 <= array_data < 0x800000000000:
                                    array_error = lldb.SBError()
                                    array_blob = process.ReadMemory(array_data, min(64, max(16, array_count * 8)), array_error)
                                    if array_error.Success() and array_blob:
                                        refs = []
                                        for idx in range(0, min(len(array_blob), array_count * 8), 8):
                                            item = int.from_bytes(array_blob[idx:idx + 8], "little")
                                            item_row = {"address": hex(item)}
                                            try:
                                                item_row["class"] = render._objc_class_name(process, item)
                                            except Exception as item_error:
                                                item_row["class_error"] = repr(item_error)
                                            item_error = lldb.SBError()
                                            item_data = process.ReadMemory(item, 128, item_error)
                                            if item_error.Success() and item_data:
                                                item_row["bytes"] = item_data.hex()
                                                # Digitizer child events point back to the
                                                # parent and keep their payload at +0x60.
                                                # Capture that payload at the same live
                                                # BackBoard boundary for class/coordinate
                                                # comparison; do not infer from ACKs.
                                                if item_row.get("class") == "HIDEvent" and len(item_data) >= 0x68:
                                                    data_ptr = int.from_bytes(item_data[0x60:0x68], "little")
                                                    item_row["event_data_candidate"] = hex(data_ptr)
                                                    if 0x700000000 <= data_ptr < 0x800000000000:
                                                        data_error = lldb.SBError()
                                                        data_blob = process.ReadMemory(data_ptr, 48, data_error)
                                                        if data_error.Success() and data_blob:
                                                            item_row["event_data_bytes48"] = data_blob.hex()
                                                            item_row["event_data_u64"] = [
                                                                hex(int.from_bytes(data_blob[i:i + 8], "little"))
                                                                for i in range(0, 48, 8)
                                                            ]
                                                            item_row["event_data_f64"] = [
                                                                struct.unpack_from("<d", data_blob, i)[0]
                                                                for i in (16, 24, 32, 40)
                                                            ]
                                            refs.append(item_row)
                                        child_row["array_items"] = refs
                            children[hex(offset)] = child_row
                        if children:
                            ref["children"] = children
                except Exception as ref_error:
                    ref["read_error"] = repr(ref_error)
                referents[str(index)] = ref
            if referents:
                row["qword_referents"] = referents
    except Exception as error:
        row["read_error"] = repr(error)
    return row


def _reg(frame, name):
    register = frame.FindRegister(name)
    return register.GetValueAsUnsigned() if register.IsValid() else None


def _register(debugger, address, label):
    target = debugger.GetSelectedTarget()
    before = target.GetNumBreakpoints()
    breakpoint = target.BreakpointCreateByAddress(address)
    if not breakpoint.IsValid() or target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed to create %s at %#x" % (label, address))
    breakpoint.SetAutoContinue(label not in STOP_LABELS)
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command add -F dm_unlock_input_callbacks.on_hit %d" %
        breakpoint.GetID(), result)
    if not result.Succeeded() or breakpoint.GetNumLocations() < 1:
        breakpoint.SetEnabled(False)
        raise RuntimeError("failed to bind %s: %s" %
                           (label, result.GetError()))
    print("UNLOCK_INPUT_ARMED label=%s address=0x%x id=%d" %
          (label, address, breakpoint.GetID()), flush=True)


def _arm_hit_test_return(frame, thread_pointer):
    """Log the UIView returned by the hit-test for the current touch."""
    address = _reg(frame, "lr")
    if not address:
        return
    key = (address, thread_pointer)
    if key in HIT_TEST_RETURN_ARMED:
        return
    HIT_TEST_RETURN_ARMED.add(key)
    debugger = frame.GetThread().GetProcess().GetTarget().GetDebugger()
    target = debugger.GetSelectedTarget()
    before = target.GetNumBreakpoints()
    breakpoint = target.BreakpointCreateByAddress(address)
    if (not breakpoint.IsValid() or
            target.GetNumBreakpoints() != before + 1):
        print("UNLOCK_INPUT_HIT_TEST_RETURN_UNARMED address=0x%x" % address,
              flush=True)
        return
    breakpoint.SetAutoContinue(True)
    HIT_TEST_RETURNS[breakpoint.GetID()] = thread_pointer
    result = lldb.SBCommandReturnObject()
    debugger.GetCommandInterpreter().HandleCommand(
        "breakpoint command add -F dm_unlock_input_callbacks.on_hit_test_return %d" %
        breakpoint.GetID(), result)
    if not result.Succeeded() or breakpoint.GetNumLocations() < 1:
        breakpoint.SetEnabled(False)
        HIT_TEST_RETURNS.pop(breakpoint.GetID(), None)
        print("UNLOCK_INPUT_HIT_TEST_RETURN_UNARMED address=0x%x error=%s" %
              (address, result.GetError()), flush=True)
        return
    print("UNLOCK_INPUT_HIT_TEST_RETURN_ARMED address=0x%x id=%d thread=0x%x" %
          (address, breakpoint.GetID(), thread_pointer), flush=True)


def _arm_primary_event_return(frame, label):
    """Pair the BackBoard dispatch entry with its return on the same stack."""
    thread = frame.GetThread()
    process = thread.GetProcess()
    lr = _reg(frame, "lr") or 0
    sp = _reg(frame, "sp") or 0
    tpidr = _reg(frame, "tpidr_el1") or 0
    pid = process.GetProcessID()
    if not lr or not sp or not tpidr:
        return
    key = (lr, pid, tpidr)
    debugger = process.GetTarget().GetDebugger()
    target = debugger.GetSelectedTarget()
    bp_id = PRIMARY_EVENT_RETURN_BPS.get(key)
    if bp_id is None:
        before = target.GetNumBreakpoints()
        bp = target.BreakpointCreateByAddress(lr)
        if (not bp.IsValid() or target.GetNumBreakpoints() != before + 1):
            print("UNLOCK_INPUT_PRIMARY_RETURN_UNARMED " + json.dumps({
                "label": label, "lr": hex(lr), "pid": pid,
                "tpidr_el1": hex(tpidr), "sp": hex(sp)}, sort_keys=True),
                flush=True)
            return
        bp.SetAutoContinue(True)
        result = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command add -F dm_unlock_input_callbacks.on_primary_event_return %d" %
            bp.GetID(), result)
        if not result.Succeeded() or bp.GetNumLocations() < 1:
            bp.SetEnabled(False)
            print("UNLOCK_INPUT_PRIMARY_RETURN_UNARMED " + json.dumps({
                "label": label, "lr": hex(lr), "pid": pid,
                "error": result.GetError()}, sort_keys=True), flush=True)
            return
        bp_id = bp.GetID()
        PRIMARY_EVENT_RETURN_BPS[key] = bp_id
        print("UNLOCK_INPUT_PRIMARY_RETURN_BP_ARMED " + json.dumps({
            "label": label, "lr": hex(lr), "pid": pid,
            "tpidr_el1": hex(tpidr), "id": bp_id}, sort_keys=True),
            flush=True)
    PRIMARY_EVENT_RETURNS.setdefault(bp_id, []).append({
        "label": label, "lr": lr, "pid": pid, "tpidr_el1": tpidr,
        "sp": sp, "entry_time": time.time(),
    })


def on_primary_event_return(frame, location, _context):
    bp = location.GetBreakpoint()
    thread = frame.GetThread()
    process = thread.GetProcess()
    current = {
        "pid": process.GetProcessID(),
        "tpidr_el1": _reg(frame, "tpidr_el1") or 0,
        "sp": _reg(frame, "sp") or 0,
    }
    pending = PRIMARY_EVENT_RETURNS.get(bp.GetID(), [])
    match = next((row for row in pending
                  if row["pid"] == current["pid"] and
                  row["tpidr_el1"] == current["tpidr_el1"] and
                  row["sp"] == current["sp"]), None)
    if match is None:
        return False
    pending.remove(match)
    row = {
        "label": match["label"] + "_RETURN",
        "time": time.time(), "entry_time": match["entry_time"],
        "process": names._progname(process), "pid": current["pid"],
        "tpidr_el1": hex(current["tpidr_el1"]), "sp": hex(current["sp"]),
        "pc": hex(_reg(frame, "pc") or 0), "lr": hex(_reg(frame, "lr") or 0),
        **{"x%d" % i: _reg(frame, "x%d" % i) for i in range(4)},
    }
    if match["label"] == "BACKBOARD_DETERMINE_TOUCH_SERVICE":
        service = row.get("x0") or 0
        row["service"] = hex(service) if service else None
        if service:
            row["service_class"] = render._objc_class_name(process, service)
    _emit(row)
    return False


def on_hit_test_return(frame, location, _context):
    breakpoint = location.GetBreakpoint()
    expected_thread = HIT_TEST_RETURNS.get(breakpoint.GetID())
    thread = frame.GetThread()
    process = thread.GetProcess()
    thread_pointer = _reg(frame, "tpidr_el1")
    if (thread_pointer != expected_thread or
            names._progname(process) != "SpringBoard"):
        return False
    result_view = _reg(frame, "x0")
    row = {
        "label": "UI_WINDOW_HIT_TEST_RETURN",
        "time": time.time(),
        "process": "SpringBoard",
        "tpidr_el1": thread_pointer,
        "pc": _reg(frame, "pc"),
        "lr": _reg(frame, "lr"),
        "result_view": result_view,
        "result_view_class": (render._objc_class_name(process, result_view)
                               if result_view else None),
    }
    _emit(row)
    breakpoint.SetEnabled(False)
    HIT_TEST_RETURNS.pop(breakpoint.GetID(), None)
    return False


def on_hit(frame, location, _context):
    process = frame.GetThread().GetProcess()
    address = location.GetAddress().GetLoadAddress(
        process.GetTarget()) - SLIDE
    label = POINTS.get(address, hex(address))
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count > LIMIT:
        location.GetBreakpoint().SetEnabled(False)
        return False
    row = {
        "label": label,
        "count": count,
        "time": time.time(),
        "process": names._progname(frame.GetThread().GetProcess()),
        "tpidr_el1": _reg(frame, "tpidr_el1"),
        "pc": _reg(frame, "pc"),
        "lr": _reg(frame, "lr"),
        **{"x%d" % i: _reg(frame, "x%d" % i) for i in range(8)},
        "d0": frame.FindRegister("d0").GetValue()
        if frame.FindRegister("d0").IsValid() else None,
        "d1": frame.FindRegister("d1").GetValue()
        if frame.FindRegister("d1").IsValid() else None,
    }
    if label == "UI_WINDOW_HIT_TEST":
        row["x0_class"] = render._objc_class_name(process, row["x0"])
        row["x2_class"] = render._objc_class_name(process, row["x2"])
        _arm_hit_test_return(frame, row["tpidr_el1"])
    if label in ("BACKBOARD_SYSTEM_EVENT", "BACKBOARD_PRIMARY_EVENT",
                 "BACKBOARD_RECOGNIZER_ENGINE",
                 "BACKBOARD_DIRECT_TOUCH_PROCESS_EVENT",
                 "BACKBOARD_DETERMINE_TOUCH_SERVICE",
                 "BACKBOARD_HANDLE_DIRECT_TOUCH_EVENT"):
        _arm_primary_event_return(frame, label)
    if label == "UI_WINDOW_HIT_TEST_RETURN":
        row["result_view_class"] = render._objc_class_name(
            process, row["x0"]) if row["x0"] else None
    if label in ("BACKBOARD_SYSTEM_EVENT", "BACKBOARD_PRIMARY_EVENT",
                 "BACKBOARD_RECOGNIZER_ENGINE") and count == 1:
        row["pointer_probes"] = {
            "x%d" % index: _pointer_probe(process, row.get("x%d" % index))
            for index in (0, 1, 2, 3, 4, 7)
        }
    if label == "BACKBOARD_PROCESSOR_DISPATCH_CALL":
        row["processor_class"] = render._objc_class_name(process, row["x0"])
        selector = row["x1"]
        if isinstance(selector, int) and selector > 0x10000:
            selector &= 0x0000ffffffffffff
            row["selector_pointer"] = hex(selector)
            row["selector_bytes"] = render._cstring(process, selector, 96)
        event_wrapper = row.get("x2") or 0
        if count <= 4 and 0x700000000 <= event_wrapper < 0x800000000000:
            try:
                event = render._u64(process, event_wrapper)
                row["event_object_candidate"] = hex(event) if event else None
                row["event_class"] = (render._objc_class_name(process, event)
                                       if event else None)
            except Exception as error:
                row["event_object_read_error"] = repr(error)
    if label == "BACKBOARD_DIRECT_TOUCH_PROCESS_EVENT":
        wrapper = row.get("x2") or 0
        if count <= 4 and 0x700000000 <= wrapper < 0x800000000000:
            try:
                event = render._u64(process, wrapper)
                row["event_object"] = hex(event) if event else None
                row["event_class"] = (render._objc_class_name(process, event)
                                       if event else None)
            except Exception as error:
                row["event_object_read_error"] = repr(error)
        if count <= 4:
            row["sender_class"] = render._objc_class_name(process, row["x3"])
            row["dispatcher_class"] = render._objc_class_name(process, row["x4"])
    elif label == "BACKBOARD_DETERMINE_TOUCH_SERVICE" and count <= 4:
        row["event_class"] = render._objc_class_name(process, row["x2"])
        row["sender_class"] = render._objc_class_name(process, row["x3"])
        flag_pointer = row.get("x4") or 0
        if 0x700000000 <= flag_pointer < 0x800000000000:
            try:
                error = lldb.SBError()
                raw = process.ReadMemory(flag_pointer, 1, error)
                if error.Success() and raw:
                    row["from_touch_pad_out"] = raw[0]
            except Exception as error:
                row["from_touch_pad_read_error"] = repr(error)
    elif label == "BACKBOARD_HANDLE_DIRECT_TOUCH_EVENT" and count <= 4:
        wrapper = row.get("x2") or 0
        if 0x700000000 <= wrapper < 0x800000000000:
            try:
                event = render._u64(process, wrapper)
                row["event_object"] = hex(event) if event else None
                row["event_class"] = (render._objc_class_name(process, event)
                                       if event else None)
            except Exception as error:
                row["event_object_read_error"] = repr(error)
        row["service"] = hex(row["x3"]) if row.get("x3") else None
        row["service_class"] = render._objc_class_name(process, row["x3"])
    _emit(row)
    if label in STOP_LABELS:
        thread = frame.GetThread()
        row_frames = []
        for index in range(min(thread.GetNumFrames(), 20)):
            current = thread.GetFrameAtIndex(index)
            row_frames.append({
                "index": index,
                "pc": current.GetPC() & 0x0000ffffffffffff,
                "lr": _reg(current, "lr"),
                "sp": current.GetSP(),
            })
        _emit("UNLOCK_INPUT_STACK " + json.dumps({
            "label": label, "process": row["process"],
            "frames": row_frames,
        }, sort_keys=True))
        return True
    return False


def install(debugger, slide):
    global SLIDE, EVENT_OUT
    SLIDE = int(slide)
    event_dir = Path(os.environ.get(
        "DVM_PROBE_EVENT_DIR", "/tmp/dvm/post-shell-home-input-events"))
    event_dir.mkdir(parents=True, exist_ok=True)
    EVENT_OUT = event_dir / "unlock-input.jsonl"
    COUNTS.clear()
    PRIMARY_EVENT_RETURNS.clear()
    PRIMARY_EVENT_RETURN_BPS.clear()
    names.SLIDE[0] = SLIDE
    for address, label in POINTS.items():
        _register(debugger, SLIDE + address, label)
    print(json.dumps({"label": "UNLOCK_INPUT_READY", "slide": hex(SLIDE),
                      "sites": len(POINTS), "output": str(EVENT_OUT)},
          sort_keys=True), flush=True)

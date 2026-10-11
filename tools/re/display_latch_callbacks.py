"""Read-only AppleInternalDisplay latch provenance probe.

Use with CLASS16_COHERENT1 fixtures and DARWIN_DCP_IOMFB_ECHO_MODE=1.
The first AppleDisplay constructed is the internal display on the measured
boot; log a mismatch if surface allocation later uses another instance.
"""

import lldb


SLIDE = 0
TARGET_DISPLAY = None
CTOR_COUNT = 0
WATCH = {}
LAST_ALLOC = None
BP_LABEL = {}
ORDINARY_HITS = {}
UPDATE_FLAGS_BY_PTR = {}
PREPARE_BY_UPDATE = {}
PREPARE_ACTIVE_BY_THREAD = {}


def _reg(frame, name):
    reg = frame.FindRegister(name)
    return reg.GetValueAsUnsigned() if reg.IsValid() else None


def _read(process, addr, size):
    if not addr:
        return None
    error = lldb.SBError()
    data = process.ReadMemory(addr, size, error)
    return bytes(data) if error.Success() and data and len(data) == size else None


def _hex(value):
    return hex(value) if value is not None else None


def _f32(process, address):
    import struct
    raw = _read(process, address, 4)
    return struct.unpack("<f", raw)[0] if raw else None


def _stack(thread):
    rows = []
    for index in range(min(thread.GetNumFrames(), 10)):
        frame = thread.GetFrameAtIndex(index)
        pc = frame.GetPC()
        rows.append("%s:%s:%s" % (
            index, _hex(pc - SLIDE), frame.GetFunctionName() or "?"))
    return " | ".join(rows)


def _watch(frame, display, offset, size, label, initial):
    target = frame.GetThread().GetProcess().GetTarget()
    error = lldb.SBError()
    point = target.WatchAddress(display + offset, size, False, True, error)
    if not error.Success() or not point or not point.IsValid():
        print("DISPLAY_LATCH_WATCH_FAILED label=%s address=%s error=%s" % (
            label, _hex(display + offset), error.GetCString()), flush=True)
        return
    result = lldb.SBCommandReturnObject()
    lldb.debugger.GetCommandInterpreter().HandleCommand(
        "watchpoint command add -F display_latch_callbacks.on_watch %d" %
        point.GetID(), result)
    if not result.Succeeded():
        target.DeleteWatchpoint(point.GetID())
        print("DISPLAY_LATCH_WATCH_FAILED label=%s command=%s" % (
            label, result.GetError()), flush=True)
        return
    WATCH[point.GetID()] = {"label": label, "display": display,
                            "offset": offset, "size": size, "old": initial,
                            "hits": 0}
    print("DISPLAY_LATCH_WATCH_ARMED label=%s id=%d display=%s addr=%s "
          "size=%d initial=%s" % (
              label, point.GetID(), _hex(display), _hex(display + offset),
              size, initial.hex() if initial else None), flush=True)


def on_watch(frame, watchpoint, internal_dict=None):
    global LAST_ALLOC
    state = WATCH.get(watchpoint.GetID())
    if not state:
        return False
    thread = frame.GetThread()
    process = thread.GetProcess()
    display = state["display"]
    offset = state["offset"]
    old = state["old"]
    new = _read(process, display + offset, state["size"])
    state["hits"] += 1
    pc = frame.GetPC()
    word = _read(process, display + 0x755c, 4)
    print("DISPLAY_LATCH_WRITE label=%s hit=%d display=%s pc_static=%s "
          "writer_candidate_static=%s old=%s new=%s word755c=%s "
          "mode2b8=%s clear7545=%s power_bytes=%s size_b8=%s "
          "last_alloc=%s regs_x19_x20_x21_x22=%s stack=%s" % (
              state["label"], state["hits"], _hex(display),
              _hex(pc - SLIDE), _hex(pc - SLIDE - 4),
              old.hex() if old else None, new.hex() if new else None,
              word.hex() if word else None,
              _hex(int.from_bytes(_read(process, display + 0x2b8, 8), "little"))
              if _read(process, display + 0x2b8, 8) else None,
              _read(process, display + 0x7545, 1).hex()
              if _read(process, display + 0x7545, 1) else None,
              _read(process, display + 0x7540, 0x28).hex()
              if _read(process, display + 0x7540, 0x28) else None,
              _read(process, display + 0xb8, 8).hex()
              if _read(process, display + 0xb8, 8) else None,
              _hex(LAST_ALLOC),
              [_hex(_reg(frame, name)) for name in ("x19", "x20", "x21", "x22")],
              _stack(thread)), flush=True)
    if (state["label"] == "755e" and old and new and
            old[2] == 0 and new[2] == 1):
        print("DISPLAY_LATCH_755E_FIRST_0_TO_1 display=%s pc_static=%s "
              "word_old=%s word_new=%s" % (
                  _hex(display), _hex(pc - SLIDE), old.hex(), new.hex()),
              flush=True)
    if (state["label"] == "7545" and old and new and
            (old[0] & 1) == 0 and (new[0] & 1) == 1):
        print("DISPLAY_LATCH_7545_FIRST_0_TO_1 display=%s pc_static=%s "
              "old=%s new=%s" % (
                  _hex(display), _hex(pc - SLIDE), old.hex(), new.hex()),
              flush=True)
    if (state["label"] == "7545" and old and new and
            (old[0] & 1) == 1 and (new[0] & 1) == 0):
        print("DISPLAY_LATCH_7545_FIRST_1_TO_0 display=%s pc_static=%s "
              "old=%s new=%s" % (
                  _hex(display), _hex(pc - SLIDE), old.hex(), new.hex()),
              flush=True)
    state["old"] = new
    if state["hits"] >= 64:
        watchpoint.SetEnabled(False)
        print("DISPLAY_LATCH_WATCH_DISABLED label=%s hits=%d" % (
            state["label"], state["hits"]), flush=True)
    return False


def on_break(frame, bp_loc, internal_dict=None):
    global TARGET_DISPLAY, CTOR_COUNT, LAST_ALLOC
    label = BP_LABEL.get(bp_loc.GetBreakpoint().GetID())
    process = frame.GetThread().GetProcess()
    display = _reg(frame, "x19")
    thread_key = _reg(frame, "tpidr_el1") or frame.GetThread().GetThreadID()
    if label == "PREPARE_LAYER_CALL":
        update = _reg(frame, "x21")
        entry = {"update": update, "node": _reg(frame, "x2"),
                 "out_ref": _reg(frame, "x4"), "classify": [],
                 "bit22_gate": [], "mask": []}
        PREPARE_ACTIVE_BY_THREAD[thread_key] = entry
    elif label in ("PREPARE_CLASSIFY_BEFORE", "PREPARE_CLASSIFY_AFTER",
                   "PREPARE_ALPHA_ACCUM", "PREPARE_VCALL_ACCUM"):
        entry = PREPARE_ACTIVE_BY_THREAD.get(thread_key)
        if entry and len(entry["classify"]) < 20:
            color = _reg(frame, "x1")
            entry["classify"].append({
                "site": label, "color_obj": _hex(color),
                "rgba": tuple(_f32(process, color + off) for off in
                              (0x10, 0x14, 0x18, 0x1c)) if color else None,
                "w9": _reg(frame, "w9"), "w25": _reg(frame, "w25")})
    elif label == "PREPARE_BIT22_GATE":
        entry = PREPARE_ACTIVE_BY_THREAD.get(thread_key)
        if entry and len(entry["bit22_gate"]) < 20:
            entry["bit22_gate"].append({"w25": _reg(frame, "w25"),
                                        "context5d0": _hex(int.from_bytes(
                                            _read(process, (_reg(frame, "x19") or 0) + 0x5d0, 8),
                                            "little")) if _reg(frame, "x19") and
                                            _read(process, _reg(frame, "x19") + 0x5d0, 8)
                                            else None})
    elif label == "PREPARE_FLAGS_MASK":
        entry = PREPARE_ACTIVE_BY_THREAD.get(thread_key)
        if entry and len(entry["mask"]) < 20:
            entry["mask"].append({"raw": _hex(_reg(frame, "x8")),
                                  "mask": _hex(_reg(frame, "x10"))})
    elif label == "PREPARE_LAYER_RETURN":
        entry = PREPARE_ACTIVE_BY_THREAD.pop(thread_key, None)
        if entry and entry["update"]:
            sp = _reg(frame, "sp")
            prepared_raw = _read(process, sp + 0xf0, 8) if sp else None
            entry["prepared_low"] = _hex(int.from_bytes(
                prepared_raw, "little")) if prepared_raw else None
            rows = PREPARE_BY_UPDATE.setdefault(entry["update"], [])
            if len(rows) < 64:
                rows.append(entry)
                print("DISPLAY_PREPARE_SUMMARY update=%s index=%d node=%s "
                      "rgba=%s w25=%s bit22_gate=%s prepared=%s" % (
                          _hex(entry["update"]), len(rows) - 1,
                          _hex(entry["node"]),
                          [sample["rgba"] for sample in entry["classify"]
                           if sample["site"] == "PREPARE_CLASSIFY_AFTER"],
                          [sample["w25"] for sample in entry["classify"]
                           if sample["site"] == "PREPARE_CLASSIFY_AFTER"],
                          entry["bit22_gate"], entry["prepared_low"]),
                      flush=True)
    elif label == "UPDATE_ADD_CONTEXT_FLAGS":
        update = _reg(frame, "x21")
        if update:
            source = UPDATE_FLAGS_BY_PTR.setdefault(update, {"samples": [], "set8": 0})
            if len(source["samples"]) < 64:
                raw = _reg(frame, "x8")
                source["samples"].append({
                    "sp_f0": _hex(raw),
                    "bit22": bool(raw & (1 << 22)) if raw is not None else None,
                    "mask28000": _hex(_reg(frame, "x9")),
                    "update5da": (_read(process, update + 0x5da, 1) or b"").hex(),
                })
                print("DISPLAY_ADD_CONTEXT_SUMMARY update=%s index=%d "
                      "prepared=%s raw=%s bit22=%s mask28000=%s" % (
                          _hex(update), len(source["samples"]) - 1,
                          len(PREPARE_BY_UPDATE.get(update, [])),
                          _hex(raw), bool(raw & (1 << 22)) if raw is not None
                          else None, _hex(_reg(frame, "x9"))), flush=True)
    elif label == "UPDATE_ADD_CONTEXT_SET8":
        update = _reg(frame, "x21")
        if update:
            UPDATE_FLAGS_BY_PTR.setdefault(update, {"samples": [], "set8": 0})["set8"] += 1
    elif label == "CTOR_BEFORE_7545":
        CTOR_COUNT += 1
        print("DISPLAY_LATCH_CTOR_BEFORE index=%d display=%s x26=%s w23=%s "
              "old7545=%s word755c=%s stack=%s" % (
                  CTOR_COUNT, _hex(display), _hex(_reg(frame, "x26")),
                  _reg(frame, "w23"),
                  (_read(process, display + 0x7545, 1) or b"").hex() if display else None,
                  (_read(process, display + 0x755c, 4) or b"").hex() if display else None,
                  _stack(frame.GetThread())), flush=True)
        if TARGET_DISPLAY is None and display:
            TARGET_DISPLAY = display
            _watch(frame, display, 0x7545, 1, "7545",
                   _read(process, display + 0x7545, 1))
    elif label == "CTOR_AFTER_755C":
        if display == TARGET_DISPLAY:
            word = _read(process, display + 0x755c, 4)
            print("DISPLAY_LATCH_CTOR_AFTER display=%s word755c=%s "
                  "clear7545=%s" % (
                      _hex(display), word.hex() if word else None,
                      (_read(process, display + 0x7545, 1) or b"").hex()), flush=True)
            _watch(frame, display, 0x755c, 4, "755e", word)
    elif label == "ALLOC_RETURN":
        result = _reg(frame, "x0")
        if result:
            LAST_ALLOC = result
            print("DISPLAY_LATCH_ALLOC_RETURN surface=%s target_display=%s" % (
                _hex(result), _hex(TARGET_DISPLAY)), flush=True)
    elif label == "FINISH_ELIGIBILITY":
        active = _reg(frame, "x20")
        print("DISPLAY_LATCH_FINISH active_display=%s target_display=%s "
              "same=%s mode=%s word755c=%s clear7545=%s return=%s "
              "surface=%s" % (
                  _hex(active), _hex(TARGET_DISPLAY), active == TARGET_DISPLAY,
                  (_read(process, active + 0x2b8, 8) or b"").hex() if active else None,
                  (_read(process, active + 0x755c, 4) or b"").hex() if active else None,
                  (_read(process, active + 0x7545, 1) or b"").hex() if active else None,
                  _hex(_reg(frame, "x0")), _hex(LAST_ALLOC)), flush=True)
        print("DISPLAY_LATCH_FINISH active=%s target=%s same=%s" % (
            _hex(active), _hex(TARGET_DISPLAY), active == TARGET_DISPLAY),
            flush=True)
    elif label == "LAYER_DECISION" and LAST_ALLOC:
        active = _reg(frame, "x20")
        render = _reg(frame, "x19")
        render_flags = _read(process, render + 0x12c, 1) if render else None
        print("DISPLAY_LAYER_DECISION display=%s target=%s same=%s "
              "surface=%s w25=%s w24=%s render=%s provider66e8=%s "
              "render_flags12c=%s real_surface18=%s clear7545=%s mode2b8=%s "
              "stack=%s" % (
                  _hex(active), _hex(TARGET_DISPLAY), active == TARGET_DISPLAY,
                  _hex(LAST_ALLOC), _hex(_reg(frame, "w25")),
                  _hex(_reg(frame, "w24")), _hex(render),
                  (_read(process, active + 0x66e8, 8) or b"").hex() if active else None,
                  render_flags.hex() if render_flags else None,
                  (_read(process, render + 0x18, 8) or b"").hex() if render else None,
                  (_read(process, active + 0x7545, 1) or b"").hex() if active else None,
                  (_read(process, active + 0x2b8, 8) or b"").hex() if active else None,
                  _stack(frame.GetThread())), flush=True)
    elif label.startswith("ORDINARY_") and LAST_ALLOC:
        active = _reg(frame, "x20")
        if active == TARGET_DISPLAY:
            count = ORDINARY_HITS.get(label, 0) + 1
            ORDINARY_HITS[label] = count
            if count <= 12:
                x22 = _reg(frame, "x22")
                x27 = _reg(frame, "x27")
                x26 = _reg(frame, "x26")
                print("DISPLAY_ORDINARY label=%s hit=%d display=%s "
                      "surface_return=%s x22=%s x27=%s x26=%s "
                      "w24=%s w25=%s w26=%s w8=%s "
                      "page22=%s page27=%s frame20=%s frame28=%s "
                      "last_alloc=%s" % (
                          label, count, _hex(active), _hex(_reg(frame, "x0")),
                          _hex(x22), _hex(x27), _hex(x26),
                          _hex(_reg(frame, "w24")), _hex(_reg(frame, "w25")),
                          _hex(_reg(frame, "w26")), _hex(_reg(frame, "w8")),
                          (_read(process, x22 + 0x28, 8) or b"").hex()
                          if x22 else None,
                          (_read(process, x27 + 0x28, 8) or b"").hex()
                          if x27 else None,
                          (_read(process, x26 + 0x20, 16) or b"").hex()
                          if x26 else None,
                          (_read(process, x28 + 0x5d8, 8) or b"").hex()
                          if (x28 := _reg(frame, "x28")) else None,
                          _hex(LAST_ALLOC)), flush=True)
                if label == "ORDINARY_CLEAR_CONTINUE":
                    update = _reg(frame, "x28")
                    print("DISPLAY_UPDATE_SOURCE_CORRELATED update=%s "
                          "flags140=%s add_context=%s prepared=%s" % (
                              _hex(update),
                              (_read(process, update + 0x140, 8) or b"").hex()
                              if update else None,
                              UPDATE_FLAGS_BY_PTR.get(update),
                              PREPARE_BY_UPDATE.get(update)), flush=True)
                if label in ("ORDINARY_BIT2_PRECHECK", "ORDINARY_BIT2_STORE",
                             "ORDINARY_BIT2_DECISION"):
                    update = _reg(frame, "x28")
                    flags = _read(process, x26 + 0x44, 1) if x26 else None
                    update_tag = _read(process, update + 0x140, 8) if update else None
                    print("DISPLAY_ORDINARY_CAUSE label=%s display=%s "
                          "frame=%s surface=%s flags44=%s w8=%s "
                          "update=%s update140=%s update140_bit3=%s "
                          "detached=%s" % (
                              label, _hex(active), _hex(x26),
                              (_read(process, x26 + 0x20, 8) or b"").hex()
                              if x26 else None,
                              flags.hex() if flags else None,
                              _hex(_reg(frame, "w8")), _hex(update),
                              update_tag.hex() if update_tag else None,
                              (update_tag[0] >> 3) & 1 if update_tag else None,
                              _hex(_reg(frame, "w25"))), flush=True)
    elif label == "CLONE_UPDATE_ENTRY" and _reg(frame, "x0") == TARGET_DISPLAY:
        print("DISPLAY_LAYER_PRODUCER label=%s display=%s update=%s "
              "provider66e8=%s stack=%s" % (
                  label, _hex(TARGET_DISPLAY), _hex(_reg(frame, "x3")),
                  (_read(process, TARGET_DISPLAY + 0x66e8, 8) or b"").hex(),
                  _stack(frame.GetThread())), flush=True)
    elif label == "CLONE_UPDATE_INSTALL" and _reg(frame, "x19") == TARGET_DISPLAY:
        print("DISPLAY_LAYER_PRODUCER label=%s display=%s object=%s "
              "flags12c=%s stack=%s" % (
                  label, _hex(TARGET_DISPLAY), _hex(_reg(frame, "x0")),
                  (_read(process, _reg(frame, "x0") + 0x12c, 1) or b"").hex()
                  if _reg(frame, "x0") else None,
                  _stack(frame.GetThread())), flush=True)
    elif label == "DETACH_LAYER_INSTALL" and _reg(frame, "x20") == TARGET_DISPLAY:
        print("DISPLAY_LAYER_PRODUCER label=%s display=%s object=%s "
              "flags12c=%s stack=%s" % (
                  label, _hex(TARGET_DISPLAY), _hex(_reg(frame, "x28")),
                  (_read(process, _reg(frame, "x28") + 0x12c, 1) or b"").hex()
                  if _reg(frame, "x28") else None,
                  _stack(frame.GetThread())), flush=True)
    elif label == "DELETE_DETACHED_CLEAR" and _reg(frame, "x19") == TARGET_DISPLAY:
        print("DISPLAY_LAYER_PRODUCER label=%s display=%s old66e8=%s "
              "stack=%s" % (
                  label, _hex(TARGET_DISPLAY),
                  (_read(process, TARGET_DISPLAY + 0x66e8, 8) or b"").hex(),
                  _stack(frame.GetThread())), flush=True)
    elif label.startswith("LIMITS_") and display == TARGET_DISPLAY:
        x27 = _reg(frame, "x27")
        x22 = _reg(frame, "x22")
        x25 = _reg(frame, "x25")
        virtualized_once = _read(process, 0x1e7058000 + SLIDE + 0x600, 8)
        is_virtualized = _read(process, 0x1e7053000 + SLIDE + 0xbf9, 1)
        print("DISPLAY_LATCH_LIMITS label=%s display=%s pc=%s w8=%s "
              "byte7537=%s provider640=%s provider648=%s virtualized_once=%s "
              "is_virtualized=%s word755c=%s" % (
                  label, _hex(display), _hex(frame.GetPC() - SLIDE),
                  _hex(_reg(frame, "w8")),
                  _read(process, x27, 1).hex() if x27 else None,
                  _read(process, x22 + 0x640, 8).hex() if x22 else None,
                  _read(process, x25 + 0x648, 1).hex() if x25 else None,
                  virtualized_once.hex() if virtualized_once else None,
                  is_virtualized.hex() if is_virtualized else None,
                  _read(process, display + 0x755c, 4).hex()), flush=True)
    return False


def install(debugger, slide):
    global SLIDE, TARGET_DISPLAY, CTOR_COUNT, LAST_ALLOC
    SLIDE = int(slide)
    TARGET_DISPLAY = None
    CTOR_COUNT = 0
    LAST_ALLOC = None
    WATCH.clear()
    BP_LABEL.clear()
    ORDINARY_HITS.clear()
    UPDATE_FLAGS_BY_PTR.clear()
    PREPARE_BY_UPDATE.clear()
    PREPARE_ACTIVE_BY_THREAD.clear()
    target = debugger.GetSelectedTarget()
    sites = ((0x1845f3b88, "CTOR_BEFORE_7545"),
             (0x1845f3be4, "CTOR_AFTER_755C"),
             (0x1845f6e20, "LIMITS_TEST_7537"),
             (0x1845f6e34, "LIMITS_TEST_PROVIDER648"),
             (0x1845f6e40, "LIMITS_GLOBAL_PATH"),
             (0x1845f6e54, "LIMITS_READ_GLOBAL_BF9"),
             (0x1845f6e5c, "LIMITS_STORE_755E"),
             (0x1847ae314, "ALLOC_RETURN"),
             (0x18446bd80, "FINISH_ELIGIBILITY"),
             (0x18446c6c8, "LAYER_DECISION"),
             (0x184440e60, "UPDATE_ADD_CONTEXT_FLAGS"),
             (0x184440e70, "UPDATE_ADD_CONTEXT_SET8"),
             (0x184440b0c, "PREPARE_LAYER_CALL"),
             (0x184440b10, "PREPARE_LAYER_RETURN"),
             (0x184433594, "PREPARE_CLASSIFY_BEFORE"),
             (0x184433598, "PREPARE_CLASSIFY_AFTER"),
             (0x18443370c, "PREPARE_ALPHA_ACCUM"),
             (0x18443374c, "PREPARE_VCALL_ACCUM"),
             (0x184433a74, "PREPARE_BIT22_GATE"),
             (0x18443b0a4, "PREPARE_FLAGS_MASK"),
             (0x18446cb50, "ORDINARY_CLEAR_CONTINUE"),
             (0x18446c960, "ORDINARY_BIT2_PRECHECK"),
             (0x18446c974, "ORDINARY_BIT2_STORE"),
             (0x18446cb60, "ORDINARY_BIT2_DECISION"),
             (0x18446cd78, "ORDINARY_PAGE_A_RETURN"),
             (0x18446cecc, "ORDINARY_PAGE_B_RETURN"),
             (0x18446d05c, "ORDINARY_X22_GATE"),
             (0x18446d0c8, "ORDINARY_X22_CALL"),
             (0x18446e280, "ORDINARY_X27_CALL"),
             (0x1847aaa3c, "CLONE_UPDATE_ENTRY"),
             (0x1847ab994, "CLONE_UPDATE_INSTALL"),
             (0x18454a748, "DETACH_LAYER_INSTALL"),
             (0x18457f7ec, "DELETE_DETACHED_CLEAR"))
    for address, label in sites:
        point = target.BreakpointCreateByAddress(address + SLIDE)
        if not point.IsValid():
            raise RuntimeError("breakpoint failed: %s" % label)
        BP_LABEL[point.GetID()] = label
        result = lldb.SBCommandReturnObject()
        debugger.GetCommandInterpreter().HandleCommand(
            "breakpoint command add -F display_latch_callbacks.on_break %d" %
            point.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("breakpoint command failed %s: %s" %
                               (label, result.GetError()))
        print("DISPLAY_LATCH_BP label=%s static=%s runtime=%s" % (
            label, _hex(address), _hex(address + SLIDE)), flush=True)
    print("DISPLAY_LATCH_READY slide=%s" % _hex(SLIDE), flush=True)

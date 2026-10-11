"""Read-only dart-disp0 state and matched post-home SwapEnd map bracket.

Pairs the first post-home non-null handoff with its kernel SwapEnd using
dm_posthome_swap_branch_callbacks. DART state transitions are recorded before
the handoff; map sites count only on the selected End's thread.
"""

import json
import time
from pathlib import Path

import lldb

import bks_checkin_callbacks as bks
import dm_posthome_swap_branch_callbacks as branch
from dm_plugin_lifecycle_callbacks import _read


KERNEL_SLIDE = 0x20000000
SITES = (
    (0xFFFFFFF00A0D9AA4, "CALLER_D572_ENTRY"),
    (0xFFFFFFF009F1B560, "CALLER_PLATFORM_ENTRY"),
    (0xFFFFFFF009F1B944, "CALLER_PLATFORM_AVAIL_BRANCH"),
    (0xFFFFFFF009F1B988, "CALLER_PLATFORM_AVAIL_CALL"),
    (0xFFFFFFF00A0D667C, "CALLER_POWERUP_MODE_CALL"),
    (0xFFFFFFF00A0D9B74, "CALLER_POWERUP_CALLBACK_CALL"),
    (0xFFFFFFF00A0BA4B0, "CALLER_POWERUP_ENTRY"),
    (0xFFFFFFF00A0BA658, "CALLER_POWERUP_VTABLE"),
    (0xFFFFFFF00A0BA678, "CALLER_POWERUP_IMPL"),
    (0xFFFFFFF00A0BA78C, "CALLER_MAPPER_POWER_CALL1"),
    (0xFFFFFFF00A0BA7D0, "CALLER_MAPPER_POWER_CALL2"),
    (0xFFFFFFF009F26474, "CALLER_MAPPER_PLATFORM_ENTRY"),
    (0xFFFFFFF009F265B0, "CALLER_MAPPER_STATE_CALL"),
    (0xFFFFFFF009707C3C, "PROVIDER_POWER_ENTRY"),
    (0xFFFFFFF009707CE4, "PROVIDER_POWER_ACTIVE_WRITE"),
    (0xFFFFFFF009707CE8, "PROVIDER_POWER_ACTIVE_AFTER"),
    (0xFFFFFFF009708A98, "PROVIDER_AVAIL_ENTRY"),
    (0xFFFFFFF009708AD8, "PROVIDER_AVAIL_ACTIVE_WRITE"),
    (0xFFFFFFF009708ADC, "PROVIDER_AVAIL_ACTIVE_AFTER"),
    (0xFFFFFFF009F1EE98, "STATE_ENTRY"),
    (0xFFFFFFF009F1EEF4, "DART_QUERY_CALL"),
    (0xFFFFFFF009F1EEF8, "DART_QUERY_RETURN"),
    (0xFFFFFFF009F1EF08, "STATE_PREREQ_RETURN"),
    (0xFFFFFFF009F1EFB8, "STATE_CAPABILITY_GATE"),
    (0xFFFFFFF009F1EFFC, "STATE_CONFIG_BIT31_GATE"),
    (0xFFFFFFF009F1F000, "STATE_REQUEST_QUERY_COMPARE"),
    (0xFFFFFFF009F1F008, "STATE_SKIP_POWER_JOIN"),
    (0xFFFFFFF009F1F110, "STATE_PREREQ_FAIL"),
    (0xFFFFFFF009F1F134, "STATE_UNSUPPORTED"),
    (0xFFFFFFF009F1F074, "DART_POWER_CALL"),
    (0xFFFFFFF009F1F078, "DART_POWER_RETURN"),
    (0xFFFFFFF009F1F07C, "STATE_FLAG_WRITE"),
    (0xFFFFFFF009F1F0A8, "STATE_JOIN"),
    (0xFFFFFFF009F1F0CC, "STATE_FAILURE"),
    (0xFFFFFFF009F22254, "MAP_ENTRY"),
    (0xFFFFFFF009F22324, "MAP_DART_570_RETURN"),
    (0xFFFFFFF009F22354, "MAP_DART_608_RETURN"),
    (0xFFFFFFF009F22358, "MAP_ACTIVE_CALL"),
    (0xFFFFFFF009F228D8, "ALLOC_ACTIVE_CALL"),
    (0xFFFFFFF009F228DC, "ALLOC_ACTIVE_RETURN"),
    (0xFFFFFFF009F22F38, "ALLOC2_ACTIVE_CALL"),
    (0xFFFFFFF009F22F3C, "ALLOC2_ACTIVE_RETURN"),
    (0xFFFFFFF009F23168, "DART_MAP_CALL"),
    (0xFFFFFFF009F2316C, "DART_MAP_RETURN"),
    (0xFFFFFFF009F227C0, "MAP_STATUS"),
    (0xFFFFFFF00B257E54, "IODMA_MAP_CALL"),
    (0xFFFFFFF00B257E58, "IODMA_MAP_RETURN"),
    (0xFFFFFFF00A0B9600, "SURFACE_MAP_ENTRY"),
)
LABELS = {}
COUNTS = {}
MATCHED_MAP = {}


def _u(process, address, size):
    if not address:
        return None
    raw = _read(process, address, size)
    return int.from_bytes(raw, "little") if len(raw) == size else None


def _name(process, dart):
    ptr = _u(process, dart + 0xC30, 8) if dart else None
    raw = _read(process, ptr, 64) if ptr else b""
    return raw.split(b"\0", 1)[0].decode("ascii", "replace") if raw else None


def _dart_fields(process, mapper, dart):
    return {
        "mapper": mapper,
        "mapper_state_1e0": _u(process, mapper + 0x1E0, 1) if mapper else None,
        "mapper_state_1e4": _u(process, mapper + 0x1E4, 4) if mapper else None,
        "mapper_sid_110": _u(process, mapper + 0x110, 4) if mapper else None,
        "mapper_cfg_120": _u(process, mapper + 0x120, 8) if mapper else None,
        "mapper_workloop_6e8": _u(process, mapper + 0x6E8, 8) if mapper else None,
        "dart": dart,
        "dart_name": _name(process, dart),
        "dart_flags_988": _u(process, dart + 0x988, 4) if dart else None,
        "dart_state_99a": _u(process, dart + 0x99A, 1) if dart else None,
    }


def _emit(label, thread, fields):
    row = {"label": label, "time": time.time(), "thread": thread}
    row.update(fields)
    print("POSTHOME_DART_JSON " + json.dumps(row, sort_keys=True), flush=True)


def on_hit(frame, bp_loc, _context):
    label = LABELS[bp_loc.GetBreakpoint().GetID()]
    process = frame.GetThread().GetProcess()
    thread = bks._reg(frame, "tpidr_el1")
    x0 = bks._reg(frame, "x0")
    x19 = bks._reg(frame, "x19")

    if label == "CALLER_D572_ENTRY":
        count = COUNTS.get(label, 0) + 1
        COUNTS[label] = count
        if count <= 64:
            payload = bks._reg(frame, "x2")
            _emit(label, thread, dict(count=count, pc=bks._reg(frame, "pc"),
                                      lr=bks._reg(frame, "lr"),
                                      x0=x0, x1=bks._reg(frame, "x1"),
                                      x2=payload, x3=bks._reg(frame, "x3"),
                                      x4=bks._reg(frame, "x4"),
                                      x5=bks._reg(frame, "x5"),
                                      payload_word_0=_u(process, payload, 4)))
        return False

    if label.startswith("CALLER_"):
        mapper = None
        iomfb = None
        if label.startswith("CALLER_PLATFORM_"):
            dart = x0 if label == "CALLER_PLATFORM_ENTRY" else x19
        elif label == "CALLER_MAPPER_PLATFORM_ENTRY":
            mapper = x0
            dart = _u(process, mapper + 0xE8, 8)
        elif label == "CALLER_MAPPER_STATE_CALL":
            mapper = bks._reg(frame, "x20")
            dart = _u(process, mapper + 0xE8, 8)
        elif label.startswith("CALLER_MAPPER_POWER_CALL"):
            mapper = bks._reg(frame, "x22")
            dart = _u(process, mapper + 0xE8, 8)
            iomfb = bks._reg(frame, "x21")
        else:
            iomfb = x0
            mapper = (bks._reg(frame, "x2") if label == "CALLER_POWERUP_IMPL"
                      else _u(process, iomfb + 0x138, 8))
            dart = _u(process, mapper + 0xE8, 8) if mapper else None
        fields = _dart_fields(process, mapper, dart)
        count = COUNTS.get(label, 0) + 1
        COUNTS[label] = count
        if count > 64:
            return False
        fields.update(count=count, iomfb=iomfb,
                      pc=bks._reg(frame, "pc"), lr=bks._reg(frame, "lr"),
                      **{name: bks._reg(frame, name) for name in
                         ("x0", "x1", "x2", "x3", "x4", "x5", "x6",
                          "x19", "x20", "x21", "x22", "x23", "x24", "x25")})
        if label == "CALLER_PLATFORM_AVAIL_BRANCH":
            fields["availability_input_word"] = _u(process, fields["x21"], 4)
        _emit(label, thread, fields)
        return False

    if label.startswith("PROVIDER_"):
        dart = x0 if label.endswith("_ENTRY") else x19
        fields = _dart_fields(process, 0, dart)
        if fields["dart_name"] != "dart-disp0":
            return False
        count = COUNTS.get(label, 0) + 1
        COUNTS[label] = count
        fields.update(x0=x0, x1=bks._reg(frame, "x1"),
                      x2=bks._reg(frame, "x2"), x3=bks._reg(frame, "x3"),
                      x4=bks._reg(frame, "x4"), x19=x19,
                      x20=bks._reg(frame, "x20"), x21=bks._reg(frame, "x21"),
                      lr=bks._reg(frame, "lr"), pc=bks._reg(frame, "pc"),
                      count=count)
        _emit(label, thread, fields)
        return False

    if label.startswith("STATE_") or label.startswith("DART_POWER") or label.startswith("DART_QUERY"):
        mapper = x0 if label == "STATE_ENTRY" else x19
        dart = x0 if label == "DART_QUERY_CALL" else _u(process, mapper + 0xE8, 8)
        fields = _dart_fields(process, mapper, dart)
        if fields["dart_name"] != "dart-disp0":
            return False
        count = COUNTS.get(label, 0) + 1
        COUNTS[label] = count
        if count > 32:
            return False
        fields.update(x0=x0, x1=bks._reg(frame, "x1"), x19=x19,
                      x20=bks._reg(frame, "x20"),
                      x21=bks._reg(frame, "x21"),
                      x23=bks._reg(frame, "x23"),
                      x25=bks._reg(frame, "x25"),
                      x27=bks._reg(frame, "x27"),
                      w0=x0 & 0xFFFFFFFF, count=count,
                      pc=bks._reg(frame, "pc"))
        cfg = fields["mapper_cfg_120"]
        fields["mapper_cfg_word_8"] = _u(process, cfg + 8, 4) if cfg else None
        _emit(label, thread, fields)
        return False

    if (not branch.HANDOFF or thread != branch.HANDOFF.get("thread")
            or branch.KERN_ENDS.get(thread) != branch.HANDOFF.get("object")
            or bks._progname(process) != "backboardd"):
        return False
    count = COUNTS.get(label, 0) + 1
    COUNTS[label] = count
    if count > 32:
        return False
    # The IOMMU +0x550 entry takes the mapper object in x0. Later callee
    # breadcrumbs retain it in x19. Both are sampled, never overwritten.
    mapper = x0 if label == "MAP_ENTRY" else x19
    dart = _u(process, mapper + 0xE8, 8) if mapper else None
    fields = _dart_fields(process, mapper, dart)
    fields.update(count=count, pc=bks._reg(frame, "pc"), x0=x0,
                  x1=bks._reg(frame, "x1"), x2=bks._reg(frame, "x2"),
                  x3=bks._reg(frame, "x3"), x5=bks._reg(frame, "x5"),
                  x6=bks._reg(frame, "x6"), x19=x19,
                  w0=x0 & 0xFFFFFFFF,
                  handoff_object=branch.HANDOFF["object"],
                  handoff_surface=branch.HANDOFF["surface"])
    if label == "MAP_ENTRY" and fields["dart_name"] == "dart-disp0":
        MATCHED_MAP.update(thread=thread, mapper=mapper, dart=dart,
                           handoff=branch.HANDOFF["object"])
    if MATCHED_MAP.get("thread") != thread:
        return False
    fields["map_mapper_match"] = mapper == MATCHED_MAP.get("mapper")
    fields["map_dart_match"] = dart == MATCHED_MAP.get("dart")
    _emit(label, thread, fields)
    return False


def install(debugger, slide):
    interpreter = debugger.GetCommandInterpreter()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("command script import %s" %
                              Path(branch.__file__).resolve(), result)
    if not result.Succeeded():
        raise RuntimeError("branch import: %s" % result.GetError())
    branch.install(debugger, slide)
    LABELS.clear()
    COUNTS.clear()
    MATCHED_MAP.clear()
    target = debugger.GetSelectedTarget()
    for address, label in SITES:
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" %
                                  (address + KERNEL_SLIDE), result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("failed %s: %s" % (label, result.GetError()))
        bp = target.GetBreakpointAtIndex(before)
        LABELS[bp.GetID()] = label
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F dm_posthome_dart_active_callbacks.on_hit %d" %
            bp.GetID(), result)
        if not result.Succeeded():
            raise RuntimeError("callback %s: %s" % (label, result.GetError()))
        print("COMMAND_LIST_PROOF id=%d label=DART_%s address=0x%x" %
              (bp.GetID(), label, address + KERNEL_SLIDE), flush=True)
    print("POSTHOME_DART_READY sites=%d" % len(SITES), flush=True)

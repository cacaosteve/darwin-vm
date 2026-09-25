"""Trace the first real QuartzCore IOSurface through IOMFB in one boot.

The addresses and ordering are derived in docs/re/windowserver-first-surface.md.
The decisive userspace witness is a non-null IOSurface at
IOMFBDisplay::fb_swap_set_layer.  The later kernel breakpoints distinguish a
userspace-only render from a submission which reaches H17P and its DCP mapping
path.  Hits are bounded and written both to LLDB's log and as JSON events so a
condition watcher can freeze the same guest for memory inspection.
"""
import json
import hashlib
import os
import time

import lldb


SLIDE = [0]
KERNEL_SLIDE = int(os.environ.get("DVM_KERNEL_SLIDE", "0x20000000"), 0)
EVENT_DIR = os.environ.get("DVM_PROBE_EVENT_DIR", "")
SUCCESS_LABELS = set(filter(None, os.environ.get(
    "DVM_PROBE_SUCCESS_LABELS", "").split(",")))
STOP_ON_SUCCESS = os.environ.get(
    "DVM_FIRST_SURFACE_STOP_ON_SUCCESS", "0") != "0"
PROGNAME_PTR = 0x1e6ef1590
CONFIG = {}
HITS = {}
CAPTURED = set()
CAPTURE_SEQUENCE = [0]
IOSURFACE_IVAR_OFFSET_GLOBAL = 0x1e5c16b44
PIXEL_CHANNELS = {
    0x42475241: (2, 1, 0, 3),  # 'BGRA' bytes -> R, G, B, A
    0x41524742: (1, 2, 3, 0),  # 'ARGB'
    0x52474241: (0, 1, 2, 3),  # 'RGBA'
    0x41424752: (3, 2, 1, 0),  # 'ABGR'
}


def _reg(frame, name):
    value = frame.FindRegister(name)
    return value.GetValueAsUnsigned() if value.IsValid() else None


def _read(process, address, size):
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    return data if error.Success() else b""


def _u64(process, address):
    data = _read(process, address, 8)
    return int.from_bytes(data, "little") if len(data) == 8 else None


def _u32(process, address):
    data = _read(process, address, 4)
    return int.from_bytes(data, "little") if len(data) == 4 else None


def _cstring(process, address, limit=96):
    if not address:
        return "<null>"
    data = _read(process, address, limit)
    return (data.split(b"\0", 1)[0].decode("ascii", "replace")
            if data else "<read-error>")


def _progname(process):
    pointer = _u64(process, SLIDE[0] + PROGNAME_PTR)
    string = _u64(process, pointer) if pointer else None
    return _cstring(process, string)


def _write_event(name, payload):
    if not EVENT_DIR:
        return
    os.makedirs(EVENT_DIR, exist_ok=True)
    path = os.path.join(EVENT_DIR, name)
    temporary = "%s.%d.tmp" % (path, os.getpid())
    with open(temporary, "w") as stream:
        json.dump(payload, stream, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _surface_metadata(process, surface):
    """Decode the public IOSurface accessors without executing guest code.

    The field offsets are the direct loads in IOSurfaceGetBaseAddress,
    IOSurfaceGetAllocSize, IOSurfaceGetWidth/Height/BytesPerRow, and
    IOSurfaceGetPixelFormat in this build's IOSurface framework.  Preserve the
    raw object pointers in the record so a bad candidate is obvious rather
    than silently treated as pixels.
    """
    raw_offset = _read(process, SLIDE[0] + IOSURFACE_IVAR_OFFSET_GLOBAL, 4)
    if len(raw_offset) != 4:
        return {"metadata_error": "ivar-offset-unreadable"}
    ivar_offset = int.from_bytes(raw_offset, "little", signed=True)
    internal = _u64(process, surface + ivar_offset)
    if not internal:
        return {"ivar_offset": ivar_offset,
                "metadata_error": "internal-object-unreadable"}
    plane_count = _u32(process, internal + 0xc8)
    base = None
    if plane_count == 0:
        base_component = _u64(process, internal + 0xb0)
        base_offset = _u64(process, internal + 0x70)
        if base_component is not None and base_offset is not None:
            base = base_component + base_offset
    return {
        "ivar_offset": ivar_offset,
        "internal": internal,
        "alloc_size": _u64(process, internal + 0x90),
        "width": _u64(process, internal + 0x98),
        "height": _u64(process, internal + 0xa0),
        "bytes_per_row": _u64(process, internal + 0xa8),
        "base_address": base,
        "pixel_format": _u32(process, internal + 0xb8),
        "plane_count": plane_count,
    }


def _capture_surface(process, surface, metadata, hit):
    base = metadata.get("base_address")
    row = metadata.get("bytes_per_row")
    height = metadata.get("height")
    alloc_size = metadata.get("alloc_size")
    if not all(isinstance(value, int) and value > 0
               for value in (base, row, height)):
        return {"capture_error": "missing-base-or-geometry"}
    expected = row * height
    if expected > 64 * 1024 * 1024 or row > 1024 * 1024 or height > 16384:
        return {"capture_error": "implausible-geometry", "expected": expected}
    size = min(expected, alloc_size) if isinstance(alloc_size, int) and alloc_size else expected
    pieces = []
    offset = 0
    while offset < size:
        piece = _read(process, base + offset, min(1024 * 1024, size - offset))
        if not piece:
            return {"capture_error": "guest-memory-read-failed",
                    "capture_bytes": offset}
        pieces.append(piece)
        offset += len(piece)
    data = b"".join(pieces)
    digest = hashlib.sha256(data).hexdigest()
    key = (surface, digest)
    duplicate = key in CAPTURED
    CAPTURED.add(key)
    pixel_stats = {"pixel_stats_error": "unsupported-pixel-format"}
    channels = PIXEL_CHANNELS.get(metadata.get("pixel_format"))
    width = metadata.get("width")
    if channels and isinstance(width, int) and width > 0 and width * 4 <= row:
        red, green, blue, alpha = channels
        nonblack_pixels = 0
        opaque_pixels = 0
        color_bytes = 0
        peak_rgb = 0
        pixels_over_32 = 0
        for y in range(height):
            scanline = data[y * row:y * row + width * 4]
            for x in range(0, len(scanline), 4):
                r = scanline[x + red]
                g = scanline[x + green]
                b = scanline[x + blue]
                brightness = max(r, g, b)
                peak_rgb = max(peak_rgb, brightness)
                pixels_over_32 += int(brightness > 32)
                color_bytes += int(r != 0) + int(g != 0) + int(b != 0)
                nonblack_pixels += int(r != 0 or g != 0 or b != 0)
                opaque_pixels += int(scanline[x + alpha] != 0)
        pixel_stats = {
            "color_nonzero_bytes": color_bytes,
            "nonblack_pixels": nonblack_pixels,
            "opaque_pixels": opaque_pixels,
            "total_pixels": width * height,
            "peak_rgb": peak_rgb,
            "pixels_over_32": pixels_over_32,
        }
    CAPTURE_SEQUENCE[0] += 1
    directory = EVENT_DIR or "/tmp/dvm"
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(
        directory, "iosurface-%03d-0x%x-hit%d.raw" %
        (CAPTURE_SEQUENCE[0], surface, hit))
    if not duplicate:
        with open(path, "wb") as stream:
            stream.write(data)
    result = {"capture_bytes": len(data), "capture_sha256": digest,
              "capture_nonzero_bytes": sum(byte != 0 for byte in data),
              "capture_duplicate": duplicate, "pixel_stats": pixel_stats}
    if not duplicate:
        result["raw_path"] = path
    return result


def _emit(label, frame, config, hit):
    thread = frame.GetThread()
    process = thread.GetProcess()
    values = {name: _reg(frame, name) for name in config["regs"]}
    payload = {
        "label": label,
        "time": time.time(),
        "hit": hit,
        "thread": thread.GetThreadID(),
        "pc": _reg(frame, "pc"),
        "lr": _reg(frame, "lr"),
        "progname": _progname(process),
        "registers": values,
    }
    print("=== %s hit=%d/%d t=%.3f ===" %
          (label, hit, config["limit"], payload["time"]))
    print("TRACE_JSON " + json.dumps(payload, sort_keys=True))
    return payload


def on_break(frame, bp_loc, _dict):
    breakpoint = bp_loc.GetBreakpoint()
    config = CONFIG[breakpoint.GetID()]
    process = frame.GetThread().GetProcess()
    name = _progname(process)
    if config.get("allowed") and name not in config["allowed"]:
        return False
    # Startup builds several display layers with no IOSurface.  They are
    # controls, not capture candidates, and must not exhaust the bounded
    # decisive breakpoint before SpringBoard/Setup submits real pixels.
    if (config["decisive"] and config["surface_reg"] and
            not _reg(frame, config["surface_reg"])):
        return False
    hit = HITS.get(breakpoint.GetID(), 0) + 1
    HITS[breakpoint.GetID()] = hit
    payload = _emit(config["label"], frame, config, hit)

    surface = None
    if config["surface_reg"]:
        surface = _reg(frame, config["surface_reg"])
        payload["surface"] = surface
        print("surface=%s" % ("<invalid>" if surface is None else "0x%x" % surface))
    if config["label"].startswith("IOSURFACE_ID_RETURN"):
        payload["surface_id"] = _reg(frame, "w0")
        payload["surface"] = _reg(frame, "x20")
        print("surface=0x%x surface_id=0x%x" %
              (payload["surface"] or 0, payload["surface_id"] or 0))

    if surface and config["decisive"]:
        metadata = _surface_metadata(process, surface)
        payload["surface_metadata"] = metadata
        payload["surface_capture"] = _capture_surface(
            process, surface, metadata, hit)
        print("SURFACE_JSON " + json.dumps({
            "surface": surface,
            "metadata": metadata,
            "capture": payload["surface_capture"],
        }, sort_keys=True))

    success = config["label"] in SUCCESS_LABELS
    if surface and config["decisive"]:
        payload["label"] = "NONNULL_IOSURFACE"
        _write_event("surface.%s.%d.json" % (config["label"], hit), payload)
        stats = payload["surface_capture"].get("pixel_stats", {})
        if stats.get("nonblack_pixels", 0) > 0:
            payload["label"] = "NONBLACK_IOSURFACE"
        # A fade-in at RGB 1-2 is mathematically nonblack but visibly blank.
        # This opt-in stop is a capture threshold, not recognition of a UI.
        if ("VISIBLE_IOSURFACE" in SUCCESS_LABELS and
                stats.get("pixels_over_32", 0) >= 128):
            payload["label"] = "VISIBLE_IOSURFACE"
        success = payload["label"] in SUCCESS_LABELS
    if success:
        label = payload["label"]
        _write_event("success.%s.json" % label, payload)
    _write_event("last-hit.json", payload)

    if hit >= config["limit"]:
        breakpoint.SetEnabled(False)
        print("bounded-disabled breakpoint=%d after=%d" %
              (breakpoint.GetID(), hit))
    return success and STOP_ON_SUCCESS


def _install(target, interpreter, address, label, regs, limit,
             surface_reg=None, decisive=False, allowed=None):
    before = target.GetNumBreakpoints()
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint set --address 0x%x" % address,
                              result)
    print(result.GetOutput() or result.GetError())
    if target.GetNumBreakpoints() != before + 1:
        raise RuntimeError("failed breakpoint %s at 0x%x" % (label, address))
    breakpoint = target.GetBreakpointAtIndex(before)
    CONFIG[breakpoint.GetID()] = {
        "label": label,
        "regs": regs.split(),
        "limit": limit,
        "surface_reg": surface_reg,
        "decisive": decisive,
        "allowed": set(allowed or ()),
    }
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand(
        "breakpoint command add -F first_surface_callbacks.on_break %d" %
        breakpoint.GetID(), result)
    if not result.Succeeded():
        raise RuntimeError("callback attach failed for %d: %s" %
                           (breakpoint.GetID(), result.GetError()))
    result = lldb.SBCommandReturnObject()
    interpreter.HandleCommand("breakpoint command list %d" %
                              breakpoint.GetID(), result)
    print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x\n%s" %
          (breakpoint.GetID(), label, address,
           result.GetOutput() or result.GetError()))


# (static VA, label, extra registers, hit limit, IOSurface register, decisive?)
# Upstream of create_surface (docs/re/windowserver-first-surface.md,
# docs/re/surface-cache-and-completion.md): UI_FS_SKS3 reached CADisplay with
# a non-null main display but never entered create_surface/allocate_iosurface
# before the A385 poll.  These two edges name whether render ran at all.
USER_ENTRIES = [
    (0x18490d11c, "UIAPPLICATION_MAIN", "x0 x1 x2 x3", 8, None, False),
    (0x2244d0ba4, "SB_ADFL_ENTRY", "x0 x2", 8, None, False),
    (0x1843e2e58, "CADISPLAY_MAIN_ENTRY", "x0", 16, None, False),
    (0x1843e2e84, "CADISPLAY_MAIN_RETURN", "x0", 16, None, False),
    (0x184776174, "SERVER_RENDER_UPDATE_ENTRY", "x0 x1 x2 x3", 32, None, False),
    (0x1846820e4, "DISPLAY_ALLOCATE_SURFACE_ENTRY", "x0 x1 x2 x3", 16, None, False),
    (0x18445b330, "IOMFB_UPDATE_SURFACE_ENTRY", "x0 x1 x2 x3", 16, None, False),
    (0x1847ae2cc, "CREATE_SURFACE_ENTRY", "x0 x1 x2 x3", 16, None, False),
    (0x18471bf38, "ALLOCATE_IOSURFACE_ENTRY", "x0 x1 x2 x3", 16, None, False),
    (0x1847ae314, "ALLOCATE_IOSURFACE_RETURN", "x0", 16, "x0", False),
    (0x18446bb14, "FINISH_UPDATE_ENTRY", "x0 x1 x2 x3", 32, None, False),
    (0x184478b48, "SWAP_SET_LAYER_ENTRY", "x0 x1 x2 x3", 32, "x3", False),
    (0x18447238c, "FB_SWAP_SET_LAYER_ENTRY", "x0 x1 x2 x3 x4 x5 x6 x7",
     32, "x3", True),
    (0x1844723dc, "IOSURFACE_ID_RETURN_1", "x0 x20", 32, None, False),
    (0x184472400, "IOSURFACE_ID_RETURN_2", "x0 x20", 32, None, False),
    (0x184472488, "FB_SWAP_HANDOFF", "x0 x1 x2 x3 x8", 32, "x2", True),
]

KERNEL_ENTRIES = [
    (0xfffffff00918e3c4, "H17P_SWAP_SUBMIT", "x0 x1 x2 x3 x4 x5", 16),
    (0xfffffff00a0c366c, "IOMFB_GENERIC_MAP", "x0 x1 x2 x3 x4", 16),
    (0xfffffff00a0c40d4, "IOMFB_PRIMARY_MAP_CALL", "x0 x1 x2 x3", 16),
    (0xfffffff00a0c41cc, "IOMFB_SECONDARY_MAP_CALL", "x0 x1 x2 x3", 16),
    (0xfffffff00a0c4664, "IOMFB_A408_BUILD", "x0 x1 x2 x3", 16),
    (0xfffffff00a0c477c, "IOMFB_A407_BUILD", "x0 x1 x2 x3", 16),
]


def install(debugger, slide):
    SLIDE[0] = slide
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    base_regs = "pc lr sp tpidr_el1 "
    for static, label, regs, limit, surface_reg, decisive in USER_ENTRIES:
        _install(target, interpreter, static + slide, label,
                 base_regs + regs, limit, surface_reg, decisive,
                 ("SpringBoard", "Setup", "backboardd"))
    for static, label, regs, limit in KERNEL_ENTRIES:
        _install(target, interpreter, static + KERNEL_SLIDE, label,
                 base_regs + regs, limit)
    _write_event("ready", {"time": time.time(), "slide": slide,
                           "kernel_slide": KERNEL_SLIDE,
                           "breakpoints": len(CONFIG)})
    print("FIRST_SURFACE_TRACE_READY user_slide=0x%x kernel_slide=0x%x "
          "breakpoints=%d stop_on_success=%d" %
          (slide, KERNEL_SLIDE, len(CONFIG), int(STOP_ON_SUCCESS)))

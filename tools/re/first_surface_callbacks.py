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
    # IODARTMapper::setActive is also exercised by the unrelated DCP mapper
    # during backboardd startup. Those calls used to consume every bounded
    # state breakpoint before mapper-disp0 reached the IOSurface map. Keep
    # only state transitions whose owning DART service is the display DART.
    if config["label"].startswith("IOMMU_STATE_"):
        label = config["label"]
        if label == "IOMMU_STATE_DART_QUERY_CALL":
            dart = _reg(frame, "x0")
        else:
            mapper = (_reg(frame, "x0")
                      if label == "IOMMU_STATE_TRANSITION_ENTRY"
                      else _reg(frame, "x19"))
            dart = _u64(process, mapper + 0xe8) if mapper else None
        service_name_ptr = _u64(process, dart + 0xc30) if dart else None
        service_name = _cstring(process, service_name_ptr) if service_name_ptr else None
        if service_name != "dart-disp0":
            return False
    # Mapper/IODMA breadcrumbs: ignore non-backboardd before counting or
    # logging (GATE138–142 burned budget / drowned the surface frame).
    if config["label"] in (
            "IOMFB_MAPPER_MEMDESC_PSEG", "IOMFB_MAPPER_MEMDESC_PSEG_RET",
            "IOMFB_MAPPER_BUSY_RET", "IOMFB_MAPPER_VT_A0_CALL",
            "IOMFB_MAPPER_VT_A0_RET", "IOMFB_IODMA_AFTER_HELPER",
            "IOMFB_IODMA_PSEG5_RET", "IOMFB_IODMA_KALLOC",
            "IOMFB_IODMA_KALLOC_RET", "IOMFB_IODMA_NOMEM_DEMOTE",
            "IOMFB_MEMDESC_DMA90_OP4", "IOMFB_MEMDESC_DMA90_NULL90",
            "IOMFB_MEMDESC_DMA90_SETUP", "IOMFB_MEMDESC_DMA90_SETUP_RET",
            "IOMFB_MEMDESC_DMA90_NOMEM", "IOMFB_IODMA_MAP_HELPER",
            "IOMFB_IODMA_MAP_HELPER_RET", "IOMFB_IODMA_IOMMU_MAP",
            "IOMFB_IODMA_IOMMU_MAP_RET", "IOMMU_VT550_METHOD_ENTRY",
            "IOMMU_VT550_AFTER_570", "IOMMU_VT550_AFTER_608",
            "IOMMU_VT550_AFTER_STATE_CHECK", "IOMMU_VT550_AFTER_PHYSSEG",
            "IOMMU_VT550_AFTER_MAP_ALLOC", "IOMMU_VT550_MAP_RESULT",
            "IOMMU_VT550_NOMEM_CLASSIFY", "IOMMU_VT550_STATUS"):
        if name != "backboardd":
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
    if config["label"] == "IOMFB_GENERIC_MAP":
        # Swap descriptor in x1: layer mask +0x14c, surface slots used by the
        # later map loop (producer at 0xa0c36ac). Virt SetLayer leaves these
        # empty → A407; kern SetLayer fills them → 0xa0b910c.
        swap = _reg(frame, "x1")
        if swap:
            mask = _u32(process, swap + 0x14c)
            slots = [_u64(process, swap + 0x78 + i * 8) for i in range(4)]
            print("generic_map_swap x1=%s mask+0x14c=%s slots+0x78=%s" % (
                hex(swap),
                hex(mask) if mask is not None else None,
                [hex(s) if s else "0" for s in slots],
            ))
    if config["label"] == "IOMFB_CACHE_MAP_ENTRY":
        # 0xa0b910c: cache-mode gate at +0x84 cmp #0x700 → BadMedia 0xe00002d1;
        # x1 is the IOSurface* being mapped (surface-cache-and-completion.md).
        iosurf = _reg(frame, "x1")
        cache = _u32(process, iosurf + 0x9c) if iosurf else None
        print("cache_map_entry x1=%s cache+0x9c=%s" % (
            hex(iosurf) if iosurf else None,
            hex(cache) if cache is not None else None,
        ))
    if config["label"] == "IOMFB_CACHE_MAP_AFTER_PREP":
        # GATE133: w0 from bl 0xa0dcd30; *out still in [x22].
        w0 = _reg(frame, "x0")
        if w0 is not None:
            w0 = w0 & 0xffffffff
        outp = _reg(frame, "x22")
        outv = _u64(process, outp) if outp else None
        print("cache_map_after_prep w0=%s out=%s take_cache_path=%s" % (
            hex(w0) if w0 is not None else None,
            hex(outv) if outv else "0",
            (w0 == 0) if w0 is not None else None,
        ))
    if config["label"] in (
            "IOMFB_CACHE_MAP_CACHE_PATH", "IOMFB_CACHE_MAP_AFTER_WIRE",
            "IOMFB_CACHE_MAP_BADMEDIA", "IOMFB_CACHE_MAP_NORESOURCES"):
        print("cache_map_breadcrumb label=%s w0=%s x20=%s x23=%s" % (
            config["label"],
            hex((_reg(frame, "x0") or 0) & 0xffffffff),
            hex(_reg(frame, "x20") or 0),
            hex(_reg(frame, "x23") or 0),
        ))
    if config["label"] == "IOMFB_IOSURFACE_WIRE_ENTRY":
        surf = _reg(frame, "x0")
        mem = _u64(process, surf + 0x30) if surf else None
        flags = _u32(process, surf + 0x3d0) if surf else None
        b3d1 = _u32(process, surf + 0x3d1) if surf else None
        print("iosurface_wire_entry x0=%s +0x30=%s +0x3d0=%s +0x3d1=%s" % (
            hex(surf) if surf else None,
            hex(mem) if mem else "0",
            hex(flags) if flags is not None else None,
            hex(b3d1) if b3d1 is not None else None,
        ))
    if config["label"] in (
            "IOMFB_IOSURFACE_WIRE_MEMDESC", "IOMFB_IOSURFACE_WIRE_AFTER_LOCK",
            "IOMFB_IOSURFACE_WIRE_RET0"):
        print("iosurface_wire_breadcrumb label=%s x0=%s x8=%s x19=%s "
              "x20=%s x21=%s" % (
                  config["label"],
                  hex(_reg(frame, "x0") or 0),
                  hex(_reg(frame, "x8") or 0),
                  hex(_reg(frame, "x19") or 0),
                  hex(_reg(frame, "x20") or 0),
                  hex(_reg(frame, "x21") or 0),
              ))
    if config["label"] == "IOMFB_IOSURFACE_FALLBACK_ENTRY":
        surf = _reg(frame, "x0")
        mem = _u64(process, surf + 0x30) if surf else None
        print("iosurface_fallback_entry x0=%s will_return_+0x30=%s" % (
            hex(surf) if surf else None,
            hex(mem) if mem else "0",
        ))
    if config["label"] == "IOMFB_MAPPER_MEMDESC_PSEG":
        # GATE138/139: nested memdesc vtable +0x90 inside mapper +0x88
        # (0xb24983c). w1 is options 0x2000000/1; w3=0x20; x2=out buf.
        mem = _reg(frame, "x0")
        w1 = _reg(frame, "x1")
        if w1 is not None:
            w1 = w1 & 0xffffffff
        outp = _reg(frame, "x2")
        w3 = _reg(frame, "x3")
        if w3 is not None:
            w3 = w3 & 0xffffffff
        vt = _u64(process, mem) if mem else None
        slot = _u64(process, vt + 0x90) if vt else None
        print("memdesc_pseg mem=%s w1=%s outp=%s w3=%s vt=%s slot+0x90=%s" % (
            hex(mem) if mem else None,
            hex(w1) if w1 is not None else None,
            hex(outp) if outp else None,
            hex(w3) if w3 is not None else None,
            hex(vt) if vt else None,
            hex(slot) if slot else None,
        ))
    if config["label"] == "IOMFB_MAPPER_MEMDESC_PSEG_RET":
        print("memdesc_pseg_ret w0=%s" % (
            hex((_reg(frame, "x0") or 0) & 0xffffffff),
        ))
    if config["label"] == "IOMFB_MAPPER_BUSY_RET":
        print("mapper_busy_ret w0=%s" % (
            hex((_reg(frame, "x0") or 0) & 0xffffffff),
        ))
    if config["label"] == "IOMFB_MAPPER_VT_A0_CALL":
        # GATE140/141: IODMACommand-style mapper method (kstrs →
        # IODMACommand.cpp). x1/x2 often 0 → length from [+0x70]+0x40.
        obj = _reg(frame, "x0")
        o30 = _u64(process, obj + 0x30) if obj else None
        o40 = _u64(process, obj + 0x40) if obj else None
        o48 = _u64(process, obj + 0x48) if obj else None
        o50 = _u64(process, obj + 0x50) if obj else None
        o58 = _u32(process, obj + 0x58) if obj else None
        o64 = _u32(process, obj + 0x64) if obj else None
        o68 = _u32(process, obj + 0x68) if obj else None
        o70 = _u64(process, obj + 0x70) if obj else None
        s40 = _u64(process, o70 + 0x40) if o70 else None
        s68 = _u64(process, o70 + 0x68) if o70 else None
        s75 = _u32(process, o70 + 0x75) if o70 else None
        if s75 is not None:
            s75 = s75 & 0xff
        sb0 = _u64(process, o70 + 0xb0) if o70 else None
        print("mapper_vt_a0_call x0=%s x1=%s x2=%s x3=%s x4=%s "
              "+0x30=%s +0x40=%s +0x48=%s +0x50=%s +0x58=%s +0x64=%s "
              "+0x68=%s +0x70=%s seg+0x40=%s seg+0x68=%s seg+0x75=%s "
              "seg+0xb0=%s" % (
                  hex(obj) if obj else None,
                  hex(_reg(frame, "x1") or 0),
                  hex(_reg(frame, "x2") or 0),
                  hex(_reg(frame, "x3") or 0),
                  hex(_reg(frame, "x4") or 0),
                  hex(o30) if o30 else "0",
                  hex(o40) if o40 else "0",
                  hex(o48) if o48 else "0",
                  hex(o50) if o50 else "0",
                  hex(o58) if o58 is not None else None,
                  hex(o64) if o64 is not None else None,
                  hex(o68) if o68 is not None else None,
                  hex(o70) if o70 else "0",
                  hex(s40) if s40 else "0",
                  hex(s68) if s68 else "0",
                  hex(s75) if s75 is not None else None,
                  hex(sb0) if sb0 else "0",
              ))
    if config["label"] == "IOMFB_MAPPER_VT_A0_RET":
        print("mapper_vt_a0_ret w0=%s x20=%s" % (
            hex((_reg(frame, "x0") or 0) & 0xffffffff),
            hex(_reg(frame, "x20") or 0),
        ))
    if config["label"] == "IOMFB_IODMA_AFTER_HELPER":
        # GATE141: after bl 0xb24856c inside +0xa0 (w0 = helper status).
        print("iodma_after_helper w0=%s x20=%s x3=%s" % (
            hex((_reg(frame, "x0") or 0) & 0xffffffff),
            hex(_reg(frame, "x20") or 0),
            hex(_reg(frame, "x3") or 0),
        ))
    if config["label"] == "IOMFB_IODMA_PSEG5_RET":
        # After memdesc +0x90 with options 0x5000000 (prepare/gen).
        print("iodma_pseg5_ret w0=%s sp78=%s" % (
            hex((_reg(frame, "x0") or 0) & 0xffffffff),
            hex(_u64(process, (_reg(frame, "sp") or 0) + 0x78) or 0),
        ))
    if config["label"] == "IOMFB_MEMDESC_DMA90_OP4":
        # GATE142: 0xb2571f8 — op 0x5000000 checks [memdesc+0x90].
        md = _reg(frame, "x0")
        prep = _reg(frame, "x2")
        inner = _u64(process, md + 0x90) if md else None
        prep0 = _u64(process, prep) if prep else None
        prep8 = _u64(process, prep + 8) if prep else None
        prep10 = _u64(process, prep + 0x10) if prep else None
        prep38 = _u64(process, prep + 0x38) if prep else None
        prep40 = _u64(process, prep + 0x40) if prep else None
        print("memdesc_dma90_op4 md=%s +0x90=%s prep0=%s prep8=%s "
              "prep+0x10=%s prep+0x38=%s prep+0x40=%s w3=%s" % (
                  hex(md) if md else None,
                  hex(inner) if inner else "0",
                  hex(prep0) if prep0 else "0",
                  hex(prep8) if prep8 else "0",
                  hex(prep10) if prep10 else "0",
                  hex(prep38) if prep38 else "0",
                  hex(prep40) if prep40 else "0",
                  hex((_reg(frame, "x3") or 0) & 0xffffffff),
              ))
    if config["label"] == "IOMFB_MEMDESC_DMA90_NULL90":
        print("memdesc_dma90_null90 md=%s (will bl setup 0xb257a80)" % (
            hex(_reg(frame, "x0") or 0),
        ))
    if config["label"] == "IOMFB_MEMDESC_DMA90_SETUP":
        print("memdesc_dma90_setup md=%s size=%s flag=%s +0x90=%s" % (
            hex(_reg(frame, "x0") or 0),
            hex(_reg(frame, "x1") or 0),
            hex((_reg(frame, "x2") or 0) & 0xffffffff),
            hex(_u64(process, (_reg(frame, "x0") or 0) + 0x90) or 0),
        ))
    if config["label"] == "IOMFB_MEMDESC_DMA90_SETUP_RET":
        print("memdesc_dma90_setup_ret w0=%s" % (
            hex((_reg(frame, "x0") or 0) & 0xffffffff),
        ))
    if config["label"] == "IOMFB_MEMDESC_DMA90_NOMEM":
        # 0xb25767c: CannotWire (0xe00002e7) - 0x2a → NoMemory.
        print("memdesc_dma90_nomem w20=%s" % (
            hex((_reg(frame, "x20") or 0) & 0xffffffff),
        ))
    if config["label"] == "IOMFB_IODMA_MAP_HELPER":
        # GATE143: 0xb257c54 — may call IOMMU vt+0x550.
        md = _reg(frame, "x0")
        iommu = _reg(frame, "x1")
        flags = _u32(process, md + 0x20) if md else None
        m50 = _u64(process, md + 0x50) if md else None
        print("iodma_map_helper md=%s iommu=%s mapper=%s x5=%s x6=%s "
              "md+0x20=%s md+0x50=%s" % (
                  hex(md) if md else None,
                  hex(iommu) if iommu else None,
                  hex(_reg(frame, "x3") or 0),
                  hex(_reg(frame, "x5") or 0),
                  hex(_reg(frame, "x6") or 0),
                  hex(flags) if flags is not None else None,
                  hex(m50) if m50 else "0",
              ))
    if config["label"] == "IOMFB_IODMA_MAP_HELPER_RET":
        print("iodma_map_helper_ret w0=%s" % (
            hex((_reg(frame, "x0") or 0) & 0xffffffff),
        ))
    if config["label"] == "IOMFB_IODMA_IOMMU_MAP":
        # At 0xb257d7c / 0xb257e54: IOMMU vtable +0x550.
        iommu = _reg(frame, "x0")
        vt = _u64(process, iommu) if iommu else None
        slot = _u64(process, vt + 0x550) if vt else None
        print("iodma_iommu_map iommu=%s md=%s x2=%s x3=%s x4=%s "
              "x5=%s x6=%s vt=%s slot+0x550=%s" % (
                  hex(iommu) if iommu else None,
                  hex(_reg(frame, "x1") or 0),
                  hex(_reg(frame, "x2") or 0),
                  hex(_reg(frame, "x3") or 0),
                  hex(_reg(frame, "x4") or 0),
                  hex(_reg(frame, "x5") or 0),
                  hex(_reg(frame, "x6") or 0),
                  hex(vt) if vt else None,
                  hex(slot) if slot else None,
              ))
    if config["label"] == "IOMFB_IODMA_IOMMU_MAP_RET":
        print("iodma_iommu_map_ret w0=%s" % (
            hex((_reg(frame, "x0") or 0) & 0xffffffff),
        ))
    if config["label"] == "IOMMU_VT550_METHOD_ENTRY":
        # GATE144B resolved the live IOMMU vtable +0x550 target to static
        # 0x9f22254. Capture register and stack arguments before its prologue.
        obj = _reg(frame, "x0")
        sp = _reg(frame, "sp")
        fields = {}
        if obj:
            for offset in (0xc9, 0xca, 0xcd, 0xce, 0xe1, 0xe8,
                           0x108, 0x110, 0x114, 0x120):
                if offset in (0xc9, 0xca, 0xcd, 0xce, 0xe1):
                    raw = _read(process, obj + offset, 1)
                    fields["+0x%x" % offset] = raw[0] if raw else None
                elif offset in (0x110, 0x114):
                    fields["+0x%x" % offset] = _u32(process, obj + offset)
                else:
                    fields["+0x%x" % offset] = _u64(process, obj + offset)
        stack0 = _u64(process, sp) if sp else None
        stack8 = _u64(process, sp + 8) if sp else None
        print("iommu_vt550_entry x0-x7=%s stack0=%s stack8=%s fields=%s" % (
            [hex(_reg(frame, "x%d" % i) or 0) for i in range(8)],
            hex(stack0) if stack0 else None,
            hex(stack8) if stack8 else None,
            fields,
        ))
    if config["label"].startswith("IOMMU_VT550_") and config["label"] != \
            "IOMMU_VT550_METHOD_ENTRY":
        obj = _reg(frame, "x19")
        sp = _reg(frame, "sp")
        raw_c9 = _read(process, obj + 0xc9, 1) if obj else b""
        state_c9 = raw_c9[0] if raw_c9 else None
        sp68 = _u32(process, sp + 0x68) if sp else None
        sp70 = _u64(process, sp + 0x70) if sp else None
        print("iommu_vt550_detail label=%s w0=%s w20=%s x0=%s x8=%s "
              "x9=%s x19=%s x20=%s x21=%s x22=%s x23=%s x24=%s "
              "x26=%s x27=%s x28=%s state_c9=%s sp+68=%s sp+70=%s" % (
                  config["label"],
                  hex((_reg(frame, "x0") or 0) & 0xffffffff),
                  hex((_reg(frame, "x20") or 0) & 0xffffffff),
                  hex(_reg(frame, "x0") or 0),
                  hex(_reg(frame, "x8") or 0),
                  hex(_reg(frame, "x9") or 0),
                  hex(obj or 0),
                  hex(_reg(frame, "x20") or 0),
                  hex(_reg(frame, "x21") or 0),
                  hex(_reg(frame, "x22") or 0),
                  hex(_reg(frame, "x23") or 0),
                  hex(_reg(frame, "x24") or 0),
                  hex(_reg(frame, "x26") or 0),
                  hex(_reg(frame, "x27") or 0),
                  hex(_reg(frame, "x28") or 0),
                  hex(state_c9) if state_c9 is not None else None,
                  hex(sp68) if sp68 is not None else None,
                  hex(sp70) if sp70 else "0",
              ))
    if config["label"] in (
            "IOMMU_VT550_ACTIVE_CALL",
            "IOMMU_ALLOC_ACTIVE_CALL",
            "IOMMU_ALLOC_ACTIVE_RETURN",
            "IOMMU_ALLOC_DIAGNOSTIC_CALL",
            "IOMMU_ALLOC_DIAGNOSTIC_RETURN"):
        obj = _reg(frame, "x19")
        fields = {}
        if obj:
            for offset in (0xc9, 0xca, 0xcd, 0xce, 0xe1, 0x1e0):
                raw = _read(process, obj + offset, 1)
                fields["+0x%x" % offset] = raw[0] if raw else None
        print("iommu_vt550_gate label=%s x0=%s x1=%s x2=%s x3=%s "
              "x19=%s fields=%s" % (
                  config["label"],
                  hex(_reg(frame, "x0") or 0),
                  hex(_reg(frame, "x1") or 0),
                  hex(_reg(frame, "x2") or 0),
                  hex(_reg(frame, "x3") or 0),
                  hex(obj or 0), fields,
              ))
    if config["label"] in ("IOMMU_VT550_DART_MAP_CALL",
                            "IOMMU_VT550_DART_MAP_RETURN"):
        dart = _reg(frame, "x0")
        vt = _u64(process, dart) if dart else None
        slot = _u64(process, vt + 0x80) if vt else None
        dart_fields = {}
        if dart:
            for offset in (0x40, 0x48, 0x50, 0x58, 0x60, 0x68,
                           0x70, 0x78, 0x80, 0x88, 0x90, 0x98,
                           0xa0, 0xa8, 0xb0, 0xb8, 0xc0):
                dart_fields["+0x%x" % offset] = _u64(process, dart + offset)
        print("iommu_dart_map label=%s x0=%s x1=%s x2=%s x3=%s x4=%s "
              "x5=%s x6=%s x7=%s x8=%s vt=%s slot+0x80=%s fields=%s" % (
                  config["label"],
                  *[hex(_reg(frame, "x%d" % i) or 0) for i in range(8)],
                  hex(_reg(frame, "x8") or 0),
                  hex(vt) if vt else None,
                  hex(slot) if slot else None,
                  dart_fields,
              ))
    if config["label"] in ("IOMMU_STATE_DART_POWER_CALL",
                            "IOMMU_STATE_DART_POWER_RETURN"):
        dart = _reg(frame, "x0")
        vt = _u64(process, dart) if dart else None
        slot = _u64(process, vt + 0x580) if vt else None
        print("iommu_state_dart_power label=%s x0=%s x1=%s x2=%s x3=%s "
              "x4=%s x8=%s x16=%s vt=%s slot+0x580=%s" % (
                  config["label"],
                  *[hex(_reg(frame, "x%d" % i) or 0) for i in range(5)],
                  hex(_reg(frame, "x8") or 0),
                  hex(_reg(frame, "x16") or 0),
                  hex(vt) if vt else None,
                  hex(slot) if slot else None,
              ))
    if config["label"].startswith("IOMMU_STATE_"):
        obj = (_reg(frame, "x0")
               if config["label"] == "IOMMU_STATE_TRANSITION_ENTRY"
               else _reg(frame, "x19"))
        state = {}
        if obj:
            for offset in (0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xe1,
                           0x1e0, 0x1e4):
                if offset == 0x1e4:
                    state["+0x1e4"] = _u32(process, obj + offset)
                else:
                    raw = _read(process, obj + offset, 1)
                    state["+0x%x" % offset] = raw[0] if raw else None
        cfg = _u64(process, obj + 0x120) if obj else None
        cfg_word = _u32(process, cfg + 8) if cfg else None
        dart = _u64(process, obj + 0xe8) if obj else None
        workloop = _u64(process, obj + 0x6e8) if obj else None
        dart_vt = _u64(process, dart) if dart else None
        dart_power_slot = _u64(process, dart_vt + 0x598) if dart_vt else None
        print("iommu_state_transition label=%s x0=%s x1=%s x2=%s x3=%s "
              "x19=%s state=%s cfg=%s cfg+8=%s dart=%s workloop=%s "
              "dart_vt=%s dart+598=%s" % (
                  config["label"],
                  hex(_reg(frame, "x0") or 0),
                  hex(_reg(frame, "x1") or 0),
                  hex(_reg(frame, "x2") or 0),
                  hex(_reg(frame, "x3") or 0),
                  hex(obj or 0), state,
                  hex(cfg) if cfg else None,
                  hex(cfg_word) if cfg_word is not None else None,
                  hex(dart) if dart else None,
                  hex(workloop) if workloop else None,
                  hex(dart_vt) if dart_vt else None,
                  hex(dart_power_slot) if dart_power_slot else None,
              ))
    if config["label"] in ("IOMMU_STATE_DART_QUERY_CALL",
                            "IOMMU_STATE_DART_QUERY_RETURN"):
        obj = _reg(frame, "x19")
        dart = (_reg(frame, "x0")
                if config["label"] == "IOMMU_STATE_DART_QUERY_CALL"
                else (_u64(process, obj + 0xe8) if obj else None))
        vt = _u64(process, dart) if dart else None
        slot = _u64(process, vt + 0x598) if vt else None
        service_flags = _u32(process, dart + 0x988) if dart else None
        service_state = _read(process, dart + 0x99a, 1) if dart else b""
        service_name_ptr = _u64(process, dart + 0xc30) if dart else None
        print("iommu_state_dart_query label=%s x0=%s x1=%s x2=%s "
              "x8=%s x16=%s x19=%s dart=%s vt=%s slot+0x580=%s "
              "slot+0x598=%s flags+0x988=%s manual_flag=%s state+0x99a=%s "
              "name=%s" % (
                  config["label"],
                  hex(_reg(frame, "x0") or 0),
                  hex(_reg(frame, "x1") or 0),
                  hex(_reg(frame, "x2") or 0),
                  hex(_reg(frame, "x8") or 0),
                  hex(_reg(frame, "x16") or 0),
                  hex(obj or 0),
                  hex(dart) if dart else None,
                  hex(vt) if vt else None,
                  hex(_u64(process, vt + 0x580) or 0) if vt else None,
                  hex(slot) if slot else None,
                  hex(service_flags) if service_flags is not None else None,
                  bool(service_flags is not None and service_flags & 0x80000000),
                  service_state[0] if service_state else None,
                  _cstring(process, service_name_ptr) if service_name_ptr else None,
              ))
    if config["label"] == "IOMFB_IODMA_KALLOC":
        # GATE141: segment-array alloc; null → NotReady-0x1b = NoMemory.
        print("iodma_kalloc size=%s w2=%s" % (
            hex(_reg(frame, "x1") or 0),
            hex((_reg(frame, "x2") or 0) & 0xffffffff),
        ))
    if config["label"] == "IOMFB_IODMA_KALLOC_RET":
        print("iodma_kalloc_ret ptr=%s" % (
            hex(_reg(frame, "x0") or 0),
        ))
    if config["label"] == "IOMFB_IODMA_NOMEM_DEMOTE":
        # 0xb249508: sub w20, w24, #0x1b with w24=NotReady → NoMemory.
        print("iodma_nomem_demote w24=%s" % (
            hex((_reg(frame, "x24") or 0) & 0xffffffff),
        ))
    if config["label"] == "IOMFB_CACHE_MAP_VT88_CALL":
        # GATE137/140: method +0x88 on *[x22] with x1=memdesc, w2=1.
        obj = _reg(frame, "x0")
        mem = _reg(frame, "x1")
        w2 = _reg(frame, "x2")
        if w2 is not None:
            w2 = w2 & 0xffffffff
        vt = _u64(process, obj) if obj else None
        slot = _u64(process, vt + 0x88) if vt else None
        slot_a0 = _u64(process, vt + 0xa0) if vt else None
        o48 = _u64(process, obj + 0x48) if obj else None
        o64 = _u32(process, obj + 0x64) if obj else None
        o68 = _u32(process, obj + 0x68) if obj else None
        o70 = _u64(process, obj + 0x70) if obj else None
        print("vt88_call obj=%s memdesc=%s w2=%s vt=%s slot+0x88=%s "
              "slot+0xa0=%s +0x48=%s +0x64=%s +0x68=%s +0x70=%s" % (
                  hex(obj) if obj else None,
                  hex(mem) if mem else None,
                  hex(w2) if w2 is not None else None,
                  hex(vt) if vt else None,
                  hex(slot) if slot else None,
                  hex(slot_a0) if slot_a0 else None,
                  hex(o48) if o48 else "0",
                  hex(o64) if o64 is not None else None,
                  hex(o68) if o68 is not None else None,
                  hex(o70) if o70 else "0",
              ))
    if config["label"] in (
            "IOMFB_CACHE_MAP_AFTER_FALLBACK", "IOMFB_CACHE_MAP_AFTER_MEMDESC_VT",
            "IOMFB_CACHE_MAP_NOMEMORY_DEMOTE", "IOMFB_CACHE_MAP_937C_D5CD8",
            "IOMFB_CACHE_MAP_AFTER_D5CD8", "IOMFB_CACHE_MAP_AFTER_DC540",
            "IOMFB_CACHE_MAP_AFTER_VT88", "IOMFB_CACHE_MAP_MAPFAIL_JOIN"):
        print("cache_map_fallback_breadcrumb label=%s x0=%s x23=%s x24=%s "
              "x25=%s x26=%s x27=%s" % (
                  config["label"],
                  hex(_reg(frame, "x0") or 0),
                  hex(_reg(frame, "x23") or 0),
                  hex(_reg(frame, "x24") or 0),
                  hex(_reg(frame, "x25") or 0),
                  hex(_reg(frame, "x26") or 0),
                  hex(_reg(frame, "x27") or 0),
              ))
    if config["label"] in (
            "IOMFB_GENERIC_MAP", "IOMFB_PRIMARY_MAP_CALL",
            "IOMFB_CACHE_MAP_ENTRY", "IOMFB_SECONDARY_MAP_CALL",
            "IOMFB_SURFACE_MAP_ENTRY"):
        # GATE127/132: arm one-shot return to see map IOReturn.
        lr = _reg(frame, "lr")
        if lr and hit <= 16:
            target = process.GetTarget()
            interpreter = target.GetDebugger().GetCommandInterpreter()
            before = target.GetNumBreakpoints()
            result = lldb.SBCommandReturnObject()
            interpreter.HandleCommand(
                "breakpoint set --address 0x%x" % lr, result)
            if target.GetNumBreakpoints() == before + 1:
                rbp = target.GetBreakpointAtIndex(before)
                CONFIG[rbp.GetID()] = {
                    "label": config["label"] + "_RETURN",
                    "regs": ["x0", "x1", "x2", "x3"],
                    "limit": 1,
                    "surface_reg": None,
                    "decisive": False,
                    "allowed": set(),
                }
                HITS[rbp.GetID()] = 0
                interpreter.HandleCommand(
                    "breakpoint command add -F first_surface_callbacks.on_break %d" %
                    rbp.GetID(), result)
                print("armed %s_RETURN at lr=%s" % (config["label"], hex(lr)))
    if config["label"].endswith("_MAP_RETURN") or config["label"].endswith(
            "SURFACE_MAP_RETURN") or config["label"].endswith(
            "CACHE_MAP_ENTRY_RETURN"):
        print("map_return label=%s w0=%s" % (
            config["label"],
            hex(_reg(frame, "x0") or 0),
        ))

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
    (0xfffffff00a0c366c, "IOMFB_GENERIC_MAP", "x0 x1 x2 x3 x4", 32),
    # Producer call sites (kdis of 0xa0c36ac): 0xa0c40b0 → cache/primary
    # mapper 0xa0b910c; only on success 0xa0c40d4 → surface_map_dcp 0xa0b9600.
    # Old PRIMARY label at 40d4 was the surface_map call, so NoMemory returns
    # from 0xa0b910c were invisible (GATE127-129).
    (0xfffffff00a0c40b0, "IOMFB_PRIMARY_MAP_CALL", "x0 x1 x2 x3 x4 x5 x6", 32),
    (0xfffffff00a0b910c, "IOMFB_CACHE_MAP_ENTRY", "x0 x1 x2 x3 x4 x5 x6", 32),
    # GATE133: after bl 0xa0dcd30 (GOT stub); w0==0 → cache path at 9184,
    # else return that IOReturn (GATE132's NoMemory) via 92cc.
    (0xfffffff00a0b916c, "IOMFB_CACHE_MAP_AFTER_PREP", "x0 x1 x2 x3 x20 x22 x23", 16),
    (0xfffffff00a0b9184, "IOMFB_CACHE_MAP_CACHE_PATH", "x0 x1 x20", 16),
    (0xfffffff00a0b91a0, "IOMFB_CACHE_MAP_AFTER_WIRE", "x0 x20 x23", 16),
    (0xfffffff00a0b91ac, "IOMFB_CACHE_MAP_BADMEDIA", "x0 x20", 8),
    (0xfffffff00a0b9310, "IOMFB_CACHE_MAP_NORESOURCES", "x0 x20 x23", 8),
    # GATE134: IOSurface wire helper (GOT 0xa0dcdb0 → 0xa29590c).
    (0xfffffff00a29590c, "IOMFB_IOSURFACE_WIRE_ENTRY", "x0 x1", 16),
    (0xfffffff00a295938, "IOMFB_IOSURFACE_WIRE_MEMDESC", "x0 x8", 16),
    (0xfffffff00a295950, "IOMFB_IOSURFACE_WIRE_AFTER_LOCK", "x0 x19", 16),
    (0xfffffff00a295b00, "IOMFB_IOSURFACE_WIRE_RET0", "x0 x19 x20 x21", 16),
    # GATE135: fallback is ldr [x0+0x30] (0xa2943dc); then vtable +0xd8.
    (0xfffffff00a2943dc, "IOMFB_IOSURFACE_FALLBACK_ENTRY", "x0", 16),
    (0xfffffff00a0b9324, "IOMFB_CACHE_MAP_AFTER_FALLBACK", "x0 x20 x23 x24", 16),
    (0xfffffff00a0b934c, "IOMFB_CACHE_MAP_AFTER_MEMDESC_VT", "x0 x23 x24", 16),
    (0xfffffff00a0b9374, "IOMFB_CACHE_MAP_NOMEMORY_DEMOTE", "x0 x23", 8),
    (0xfffffff00a0b938c, "IOMFB_CACHE_MAP_937C_D5CD8", "x0 x1 x2 x23", 16),
    (0xfffffff00a0b9390, "IOMFB_CACHE_MAP_AFTER_D5CD8", "x0 x23", 16),
    (0xfffffff00a0b941c, "IOMFB_CACHE_MAP_AFTER_DC540", "x0 x22 x23", 16),
    (0xfffffff00a0b9460, "IOMFB_CACHE_MAP_VT88_CALL", "x0 x1 x2 x22 x24", 16),
    (0xfffffff00a0b9464, "IOMFB_CACHE_MAP_AFTER_VT88", "x0 x23 x26", 16),
    (0xfffffff00a0b95dc, "IOMFB_CACHE_MAP_MAPFAIL_JOIN", "x0 x23 x25 x26 x27", 16),
    (0xfffffff00b249778, "IOMFB_MAPPER_BUSY_RET", "x0 x19", 32),
    (0xfffffff00b24983c, "IOMFB_MAPPER_MEMDESC_PSEG", "x0 x1 x2 x3 x19 x20", 4096),
    (0xfffffff00b249840, "IOMFB_MAPPER_MEMDESC_PSEG_RET", "x0 x19 x20", 4096),
    (0xfffffff00b249964, "IOMFB_MAPPER_VT_A0_CALL", "x0 x1 x2 x3 x4 x19", 32),
    (0xfffffff00b249968, "IOMFB_MAPPER_VT_A0_RET", "x0 x19 x20", 32),
    # GATE141: IODMACommand.cpp path inside mapper +0xa0 (0xb248f94).
    (0xfffffff00b24912c, "IOMFB_IODMA_AFTER_HELPER", "x0 x3 x20 x21 x22", 32),
    (0xfffffff00b249270, "IOMFB_IODMA_PSEG5_RET", "x0 x19 x20 x22", 32),
    (0xfffffff00b2493b0, "IOMFB_IODMA_KALLOC", "x0 x1 x2 x3 x21 x22 x26", 32),
    (0xfffffff00b2493b4, "IOMFB_IODMA_KALLOC_RET", "x0 x21 x22 x24", 32),
    (0xfffffff00b249508, "IOMFB_IODMA_NOMEM_DEMOTE", "x20 x22 x24", 16),
    # GATE142: IOMemoryDescriptor +0x90 op 0x5000000 (w9=4) path.
    (0xfffffff00b2571f8, "IOMFB_MEMDESC_DMA90_OP4", "x0 x1 x2 x3", 32),
    (0xfffffff00b257440, "IOMFB_MEMDESC_DMA90_NULL90", "x0 x2", 16),
    (0xfffffff00b257a80, "IOMFB_MEMDESC_DMA90_SETUP", "x0 x1 x2", 32),
    (0xfffffff00b257454, "IOMFB_MEMDESC_DMA90_SETUP_RET", "x0 x21", 32),
    # SETUP returns via multiple epilogues; also catch the NoMemory demote.
    (0xfffffff00b25767c, "IOMFB_MEMDESC_DMA90_NOMEM", "x0 x20 x21", 16),
    # GATE143: map helper + IOMMU vt+0x550.
    (0xfffffff00b257c54, "IOMFB_IODMA_MAP_HELPER", "x0 x1 x2 x3 x4 x5 x6 x7", 32),
    (0xfffffff00b257d9c, "IOMFB_IODMA_MAP_HELPER_RET", "x0", 32),
    (0xfffffff00b257d08, "IOMFB_IODMA_MAP_HELPER_RET", "x0", 8),
    (0xfffffff00b257d10, "IOMFB_IODMA_MAP_HELPER_RET", "x0", 8),
    (0xfffffff00b257d7c, "IOMFB_IODMA_IOMMU_MAP", "x0 x1 x2 x3 x4 x5 x6", 32),
    (0xfffffff00b257d80, "IOMFB_IODMA_IOMMU_MAP_RET", "x0", 32),
    # GATE144: fast-path IOMMU +0x550 (taken when md+0x50 == length).
    (0xfffffff00b257e54, "IOMFB_IODMA_IOMMU_MAP", "x0 x1 x2 x3 x4 x5 x6", 32),
    (0xfffffff00b257e58, "IOMFB_IODMA_IOMMU_MAP_RET", "x0", 32),
    # GATE145: resolve the callee of that dynamic vtable +0x550 call.
    (0xfffffff009f22254, "IOMMU_VT550_METHOD_ENTRY",
     "x0 x1 x2 x3 x4 x5 x6 x7 sp", 8),
    (0xfffffff009f22324, "IOMMU_VT550_AFTER_570", "x0 x19 x21 x22", 8),
    (0xfffffff009f22354, "IOMMU_VT550_AFTER_608", "x0 x19 x21 x22", 8),
    (0xfffffff009f2235c, "IOMMU_VT550_AFTER_STATE_CHECK",
     "x0 x19 x20 x21 x22 x23 x24 x26 x27 x28", 8),
    (0xfffffff009f22450, "IOMMU_VT550_AFTER_PHYSSEG",
     "x0 x8 x9 x19 x20 x21 x22 x23 x24 x26 x27 x28", 16),
    (0xfffffff009f2253c, "IOMMU_VT550_AFTER_MAP_ALLOC",
     "x0 x19 x20 x21 x22 x23 x24 x26 x27 x28", 8),
    (0xfffffff009f225a0, "IOMMU_VT550_MAP_RESULT",
     "x0 x8 x9 x11 x19 x20 x21 x22 x23 x24 x26 x27 x28 sp", 8),
    (0xfffffff009f22680, "IOMMU_VT550_NOMEM_CLASSIFY",
     "x0 x8 x9 x19 x20 x21 x22 x23 x24 x26 x27 x28 sp", 8),
    (0xfffffff009f227c0, "IOMMU_VT550_STATUS",
     "x0 x19 x20 x21 x22 x23 x24 x26 x27 x28", 8),
    # GATE146: nested mapper state gates and Apple DART vtable mapping result.
    (0xfffffff009f22358, "IOMMU_VT550_ACTIVE_CALL",
     "x0 x1 x2 x3 x19 x21 x22 x27", 8),
    (0xfffffff009f228d8, "IOMMU_ALLOC_ACTIVE_CALL",
     "x0 x19 x21 x22 x24 x25 x26 x27 x28", 8),
    (0xfffffff009f228dc, "IOMMU_ALLOC_ACTIVE_RETURN",
     "x0 x19 x21 x22 x24 x25 x26 x27 x28", 8),
    (0xfffffff009f22f38, "IOMMU_ALLOC_ACTIVE_CALL",
     "x0 x19 x20 x21 x22 x23 x24 x25", 8),
    (0xfffffff009f22f3c, "IOMMU_ALLOC_ACTIVE_RETURN",
     "x0 x19 x20 x21 x22 x23 x24 x25", 8),
    (0xfffffff009f23168, "IOMMU_VT550_DART_MAP_CALL",
     "x0 x1 x2 x3 x4 x5 x6 x7 x8 x16 x19 x20 x21 x22 x23 x24", 8),
    (0xfffffff009f2316c, "IOMMU_VT550_DART_MAP_RETURN",
     "x0 x19 x20 x21 x22 x23 x24 x26 x27 x28", 8),
    (0xfffffff009f2321c, "IOMMU_ALLOC_DIAGNOSTIC_CALL",
     "x0 x19 x20 x21 x22 x23 x24 x25 x26 x27", 8),
    (0xfffffff009f23220, "IOMMU_ALLOC_DIAGNOSTIC_RETURN",
     "x0 x8 x19 x20 x21 x22 x23 x24 x25 x26 x27", 8),
    # GATE147: trace the internal active-state request and downstream DART
    # vtable +0x580 method that commits the mapper's +0x1e0 state bit.
    (0xfffffff009f23064, "IOMMU_STATE_REQUEST_CALL",
     "x0 x1 x2 x3 x19 x20 x21 x22", 8),
    (0xfffffff009f23068, "IOMMU_STATE_REQUEST_RETURN",
     "x0 x8 x19 x20 x21 x22 x23 x24 x25", 8),
    (0xfffffff009f1ee98, "IOMMU_STATE_TRANSITION_ENTRY",
     "x0 x1 x2 x3 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1eef4, "IOMMU_STATE_DART_QUERY_CALL",
     "x0 x1 x2 x8 x16 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1eef8, "IOMMU_STATE_DART_QUERY_RETURN",
     "x0 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1ef00, "IOMMU_STATE_WORKLOOP_CHECK",
     "x0 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1ef08, "IOMMU_STATE_PREREQ_RETURN",
     "x0 x8 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1effc, "IOMMU_STATE_CONFIG_BIT31_CHECK",
     "x0 x8 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1f000, "IOMMU_STATE_COMPARE_DART_REQUEST",
     "x0 x8 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1f110, "IOMMU_STATE_PREREQ_FAIL",
     "x0 x8 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1f074, "IOMMU_STATE_DART_POWER_CALL",
     "x0 x1 x2 x3 x4 x8 x16 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1efb8, "IOMMU_STATE_CAPABILITY_BRANCH",
     "x0 x8 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1f134, "IOMMU_STATE_UNSUPPORTED_BRANCH",
     "x0 x8 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1f078, "IOMMU_STATE_DART_POWER_RETURN",
     "x0 x8 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1f07c, "IOMMU_STATE_FLAG_WRITE",
     "x0 x8 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1f0a8, "IOMMU_STATE_TRANSITION_JOIN",
     "x0 x8 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff009f1f0cc, "IOMMU_STATE_TRANSITION_FAILURE",
     "x0 x8 x19 x20 x21 x22 x23 x24 x25 x26", 8),
    (0xfffffff00a0c41cc, "IOMFB_SECONDARY_MAP_CALL", "x0 x1 x2 x3", 32),
    # docs/re: surface_map_dcp entry/return (iomfb_calltrace.py).
    (0xfffffff00a0b9600, "IOMFB_SURFACE_MAP_ENTRY", "x0 x1 x2 x3 x4", 32),
    (0xfffffff00a0b97a0, "IOMFB_SURFACE_MAP_RETURN", "x0 x1 x2 x3", 32),
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
    kernel_entries = KERNEL_ENTRIES
    kernel_mode = "full"
    if os.environ.get("DVM_FIRST_SURFACE_IOMMU_SLIM") == "1":
        # GATE144 showed that the PSEG/DMA setup sites fire repeatedly during
        # ordinary backboardd startup, before the surface under investigation
        # reaches the map helper. Keep the render/handoff witnesses above and
        # only the map chain needed to resolve the vtable +0x550 call.
        iommu_labels = {
            "H17P_SWAP_SUBMIT",
            "IOMFB_GENERIC_MAP",
            "IOMFB_PRIMARY_MAP_CALL",
            "IOMFB_CACHE_MAP_ENTRY",
            "IOMFB_CACHE_MAP_AFTER_VT88",
            "IOMFB_IODMA_MAP_HELPER",
            "IOMFB_IODMA_MAP_HELPER_RET",
            "IOMFB_IODMA_IOMMU_MAP",
            "IOMFB_IODMA_IOMMU_MAP_RET",
            "IOMMU_VT550_METHOD_ENTRY",
            "IOMMU_VT550_AFTER_570",
            "IOMMU_VT550_AFTER_608",
            "IOMMU_VT550_AFTER_STATE_CHECK",
            "IOMMU_VT550_AFTER_PHYSSEG",
            "IOMMU_VT550_AFTER_MAP_ALLOC",
            "IOMMU_VT550_MAP_RESULT",
            "IOMMU_VT550_NOMEM_CLASSIFY",
            "IOMMU_VT550_STATUS",
            "IOMMU_VT550_ACTIVE_CALL",
            "IOMMU_ALLOC_ACTIVE_CALL",
            "IOMMU_ALLOC_ACTIVE_RETURN",
            "IOMMU_VT550_DART_MAP_CALL",
            "IOMMU_VT550_DART_MAP_RETURN",
            "IOMMU_ALLOC_DIAGNOSTIC_CALL",
            "IOMMU_ALLOC_DIAGNOSTIC_RETURN",
            "IOMMU_STATE_REQUEST_CALL",
            "IOMMU_STATE_REQUEST_RETURN",
            "IOMMU_STATE_TRANSITION_ENTRY",
            "IOMMU_STATE_DART_QUERY_CALL",
            "IOMMU_STATE_DART_QUERY_RETURN",
            "IOMMU_STATE_WORKLOOP_CHECK",
            "IOMMU_STATE_PREREQ_RETURN",
            "IOMMU_STATE_CONFIG_BIT31_CHECK",
            "IOMMU_STATE_COMPARE_DART_REQUEST",
            "IOMMU_STATE_PREREQ_FAIL",
            "IOMMU_STATE_CAPABILITY_BRANCH",
            "IOMMU_STATE_UNSUPPORTED_BRANCH",
            "IOMMU_STATE_DART_POWER_CALL",
            "IOMMU_STATE_DART_POWER_RETURN",
            "IOMMU_STATE_FLAG_WRITE",
            "IOMMU_STATE_TRANSITION_JOIN",
            "IOMMU_STATE_TRANSITION_FAILURE",
            "IOMFB_SURFACE_MAP_ENTRY",
            "IOMFB_SURFACE_MAP_RETURN",
            "IOMFB_A408_BUILD",
            "IOMFB_A407_BUILD",
        }
        kernel_entries = [entry for entry in KERNEL_ENTRIES
                          if entry[1] in iommu_labels]
        kernel_mode = "iommu-slim"
    for static, label, regs, limit in kernel_entries:
        _install(target, interpreter, static + KERNEL_SLIDE, label,
                 base_regs + regs, limit)
    _write_event("ready", {"time": time.time(), "slide": slide,
                           "kernel_slide": KERNEL_SLIDE,
                           "breakpoints": len(CONFIG),
                           "kernel_mode": kernel_mode})
    print("FIRST_SURFACE_TRACE_READY user_slide=0x%x kernel_slide=0x%x "
          "breakpoints=%d kernel_mode=%s stop_on_success=%d" %
          (slide, KERNEL_SLIDE, len(CONFIG), kernel_mode,
           int(STOP_ON_SUCCESS)))

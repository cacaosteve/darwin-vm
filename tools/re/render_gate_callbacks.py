"""Post-WS-setup render/digital-out witnesses (docs/re/cadisplay-discovery-gates.md).

    script render_gate_callbacks.install(lldb.debugger, <dsc_slide>)
"""
import lldb

CONFIG = {}
HITS = {}
KERNEL_SLIDE = 0x20000000
DSC_SLIDE = 0
DISPLAY_WH_OBJ = None  # AppleDisplay with staged A453 WxH at +0xb8
NAT8000_STATIC = 0x1844881f4
# Last mutator sample for same-thread correlation with CA send/recv hooks.
LAST_MUT = None


def _xpac(val):
    """Strip PAC high bits — low 48 are the code pointer on arm64e DSC."""
    return val & 0x0000ffffffffffff if val is not None else None


def _scan_vtable_for_nat8000(process, obj, tag):
    """Walk vtable of obj looking for a slot whose bare target is NAT8000 fn."""
    if not obj:
        return
    vt = _u64(process, obj)
    if not vt:
        print("%s vtable_scan obj=%s vt=None" % (tag, hex(obj)), flush=True)
        return
    want = _xpac(NAT8000_STATIC + DSC_SLIDE)
    hits = []
    for off in range(0, 0x400, 8):
        slot = _u64(process, vt + off)
        if slot is None:
            break
        bare = _xpac(slot)
        if bare == want:
            hits.append(off)
    print("%s vtable_scan obj=%s vt=%s want=%s hits=%s" % (
        tag, hex(obj), hex(vt), hex(want) if want else None,
        [hex(h) for h in hits] if hits else "NONE"), flush=True)

# DSC-relative unless noted. Runtime = static + dsc_slide (user) or + KERNEL_SLIDE.
USER = (
    # Construction: was AccelServer even built?
    (0x1845f77f8, "APPLEDISPLAY_NEW_SERVER_ENTRY", 8),
    (0x184629180, "ACCEL_SERVER_CTOR_ENTRY", 8),
    # Display-like ctor after strb wzr,[x0,#0x2b8] — does NOT clear +0x2bb.
    (0x18466c25c, "DISPLAY_CTOR_AFTER_2B8", 16),
    # AppleDisplay base ctor intentionally stores -1 at +0x2b8/+0x2c0 (mode sentinel).
    (0x184680dc0, "DISPLAY_SET_2B8_SENTINEL", 8),
    # Replaces sentinel with packed mode (x20/x22) — naturalistic clear of bit4.
    (0x184681b04, "DISPLAY_MODE_SET_ENTRY", 16),
    (0x184681c38, "DISPLAY_MODE_SET_STORE", 16),
    # AppleDisplay ctor: skips mode-set when [x26]!=1.
    (0x1845f3e14, "APPLEDISPLAY_MODE_BRANCH", 8),
    (0x1845f3ea4, "APPLEDISPLAY_MODE_SKIP", 8),
    # Full qword nulls that clear +0x2bb as byte3 of +0x2b8.
    (0x1844306f4, "STR_XZR_2B8_A", 8),
    (0x184440014, "STR_XZR_2B8_B", 8),
    (0x1844e5460, "STR_XZR_2B8_C", 8),
    # Virtual schedulers that call OUTER_A_FN -> SERVER_UPDATE_OUTER
    (0x1843eb030, "SCHED_UPDATE_A_ENTRY", 16),
    (0x18477e2d8, "SCHED_UPDATE_B_ENTRY", 16),
    # CA::WindowServer::Server::render_for_time — entry dumps +0x390 / ctx+0x3b9.
    (0x18440fc74, "OUTER_A_FN_ENTRY", 32),
    # Path markers inside render_for_time (GATE12: MAIN always; CALL_OUTER never).
    (0x18440fda0, "RFT_MAIN_AFTER_390", 16),   # past server+0x390==1 early exit
    (0x18441007c, "RFT_PAST_398", 16),         # after +0x398 one-shot clear
    (0x1844100e8, "RFT_TIME_DECISION", 32),    # b.ne FULL / fall-through SHORT
    # GATE75: display-list bit17 eligibility — walk reads [entry+0xc] bit17 into x26.
    (0x18440ff88, "BIT17_LIST_WALK", 64),      # ldr w12,[x12,#0xc] before ubfx #0x11
    (0x18441ceb0, "BIT17_CLEAR_STORE", 32),    # str after and #0xfffdffff (clears bit17)
    (0x18441cfe4, "BIT17_ORR_LSL8_STORE", 32), # str after orr w6,lsl#8 (can set bit17)
    # GATE76: earlier +0xc flag builders toward observed 0x2420a.
    (0x18441ca74, "FLAGS_INIT_3E", 16),         # mov w10,#0x3e; str [x0,#0xc]
    (0x18441cb2c, "FLAGS_ORR_FINISH", 16),      # orr parent bits into new obj +0xc
    (0x18441dd1c, "FLAGS_ORR_BIT13_A", 32),     # orr #0x2000 → [x8,#0xc]
    (0x18441ef04, "FLAGS_ORR_BIT13_B", 32),     # same pattern, second site
    (0x184424860, "FLAGS_BIT9_INJECT", 32),     # bit9 from parent>>0x14 into +0xc
    # GATE77: display-list entry ctor (vt 0x1e904cab0). Starts +0xc=0xa; then
    # orr w20<<8 installs bits 9/14/17 — observed 0x2420a when w20==0x242.
    (0x184518a1c, "DISP_CTOR_FLAGS_0A", 16),    # mov w8,#0xa; str [x0,#0xc]
    (0x184518dd0, "DISP_CTOR_ORR_BIT14", 16),   # conditional orr #0x4000
    (0x184518ed0, "DISP_CTOR_ORR_LSL8", 32),    # orr w8,w8,w20,lsl#8 → +0xc
    (0x1845023ec, "BIT17_ORR_DIRECT", 16),      # orr #0x20000; str [x19,#0xc]
    (0x184773b44, "DISP_CTOR_CALL_240", 8),     # bl ctor with w3=#0x240
    (0x18451ae64, "DISP_CTOR_CALL_COMPUTED", 8), # bl ctor with computed w3
    (0x18441015c, "RFT_FULL_PATH", 32),        # continues toward CALL_OUTER
    (0x184410158, "RFT_SHORT_SKIP", 16),       # b to 0x18441469c — skips CALL_OUTER
    # Inside FULL: first failure is vtable+0x178 nonzero -> 0x184411b4c (skips 0x11000).
    (0x184410280, "RFT_FULL_AFTER_V178", 32),  # cbnz w0, skip; dump w0
    (0x184410284, "RFT_FULL_V178_OK", 16),     # fallthrough when w0==0
    (0x184411b4c, "RFT_FULL_SKIP_V178", 32),   # taken when v178 returned nonzero
    (0x184410350, "RFT_FULL_CHECK_548", 16),   # cbz [x19+0x548]
    (0x184411cc4, "RFT_FULL_SKIP_NULL548", 16),
    (0x1844108cc, "RFT_FULL_BR_108CC", 32),    # once-init cmn; either way -> 0x108d0
    (0x1844108d0, "RFT_FULL_108CC_FALL", 32),  # post-init continue
    (0x18441090c, "RFT_FULL_BEFORE_V248", 32), # blraa vtable+0x248
    (0x184410910, "RFT_FULL_AFTER_V248", 32),  # dump w0
    (0x1844109cc, "RFT_FULL_TBNZ_109CC", 16),
    (0x184410c2c, "RFT_FULL_BR_10C2C", 16),
    (0x184410df4, "RFT_FULL_BR_10DF4", 16),
    (0x184411000, "RFT_WP_11000", 16),
    (0x184411048, "RFT_WP_11048", 32),         # merge point used by several paths
    (0x1844114d0, "RFT_FULL_BR_114D0", 32),   # tst flags+0x688; b.ne skip-v218
    (0x18441155c, "RFT_FULL_BEFORE_V218", 32), # blraa vtable+0x218
    (0x184411560, "RFT_FULL_AFTER_V218", 32),  # eor w23,w0,#1 — dump w0
    (0x18441157c, "RFT_FULL_SKIP_1157C", 32),  # b epilog — update aborted
    (0x184411580, "RFT_FULL_SKIP_V218_PATH", 32),  # from 114d0 / other skips
    (0x184411594, "RFT_FULL_CONT_11594", 32),  # continues toward CALL_OUTER
    (0x1844115b0, "RFT_FULL_BEFORE_VF8", 16),  # blraa vtable+0xf8
    (0x1844115b4, "RFT_FULL_AFTER_VF8", 16),   # cbz w0 -> 0x11c48
    (0x1844115b8, "RFT_FULL_VF8_OK", 16),      # fallthrough when w0!=0
    (0x1844115e0, "RFT_FULL_BEFORE_V170", 32), # blraa vtable+0x170
    (0x1844115e4, "RFT_FULL_AFTER_V170", 32),  # tbnz w0,#0 -> 0x11c48
    (0x1844115e8, "RFT_FULL_V170_OK", 16),     # fallthrough when bit0 clear
    (0x184411c48, "RFT_FULL_SKIP_VF8", 32),    # shared skip (vf8 fail OR v170 bit0)
    (0x184414d54, "RFT_FULL_EXIT_14D54", 16),
    (0x184414da8, "RFT_FULL_EPILOG_14DA8", 16),
    (0x184411c10, "RFT_FULL_TO_1469C", 16),
    (0x184412000, "RFT_WP_12000", 16),
    (0x184413000, "RFT_WP_13000", 8),
    (0x1844134cc, "CALL_OUTER_A", 8),          # sole bl SERVER_UPDATE_OUTER in RFT
    # Upstream of AccelServer::render_update (vtable slot 0x1f8 / Server PAC).
    (0x18444b954, "SERVER_UPDATE_OUTER_ENTRY", 16),
    (0x18444bee0, "CALL_RENDER_UPDATE_LOOP", 16),  # bl 0x18447b9ac
    (0x18447b9ac, "RENDER_UPDATE_LOOP_ENTRY", 16),  # virtual render_update w1=0
    # Other BL sites into SERVER_UPDATE_OUTER
    (0x1844514bc, "CALL_OUTER_FROM_RENDER_DISPLAY", 8),  # inside Display::render_display
    (0x184560d00, "CALL_OUTER_C", 8),
    (0x1845ac984, "CALL_OUTER_D", 8),
    (0x184620e84, "CALL_OUTER_E", 8),
    (0x184620eb4, "CALL_OUTER_F", 8),
    (0x1847708a8, "CALL_OUTER_SERVER", 8),
    (0x184451598, "ACCEL_RENDER_UPDATE_ENTRY", 16),  # AccelServer::render_update
    (0x184776174, "SERVER_RENDER_UPDATE_ENTRY", 16),
    (0x1844510f0, "DISPLAY_RENDER_DISPLAY_ENTRY", 16),
    (0x1845bd8f0, "HOTPLUG_SETUP_FN_ENTRY", 8),  # calls get_hotplug; not update_framebuffer
    (0x1847ae5a4, "UPDATE_FB_GET_HOTPLUG", 16),  # before get_hotplug_state call
    (0x1847ae5bc, "UPDATE_FB_HOTPLUG_OR", 16),     # OR ioreturn|state; nonzero = not active
    (0x1847b4370, "GET_HOTPLUG_STATE_ENTRY", 16),
    (0x1847ae2cc, "CREATE_SURFACE_ENTRY", 8),
    (0x18445b330, "UPDATE_SURFACE_ENTRY", 8),
    (0x1846820e4, "DISPLAY_ALLOCATE_SURFACE_ENTRY", 8),
    (0x18471bf38, "ALLOCATE_IOSURFACE_ENTRY", 8),
    (0x184440e1c, "UPDATE_FLAGS_MASK_CHECK", 32),  # ldr [sp,#0x170]; ands #0x28000 → orr #2
    (0x184440e34, "UPDATE_FLAGS_ORR_2", 16),
    (0x184440e74, "UPDATE_FLAGS_ORR_8", 16),
    # 0x18441ae24: layer walk that fills caller [sp,#0x170] via [x28,#0x60].
    # Filter at 0x184421554-574: if (x8&0x28000) then x8|=0x20000; keep bits in
    # 0x14000406020. UPDATE_FLAGS orr#2 needs sp170&0x28000 (0x184440e20-e2c);
    # orr#8 also accepts bit22 of [sp,#0xf0]. Type 0x1a ORs 0x80|+hi into +0x360
    # — that is NOT the 0x28000 path (prior GATE34 misread).
    (0x18441afc8, "LAYER_WALK_X27_INIT", 32),   # csel x27 — dump x9; optional force obj+0x70
    (0x18441c44c, "LAYER_WALK_TYPE_BYTE", 64),  # after ldrb; cmp #0x19 / #0x1a
    (0x18441c468, "LAYER_WALK_ORR_80", 16),     # type-0x1a: orr 0x100000000080 → +0x360
    (0x184421560, "LAYER_WALK_TST_28000", 32),  # tst x8,#0x28000 before csel 0x20000
    (0x184421588, "LAYER_WALK_MASK_STORE", 32), # stp filtered mask to [x28,#0x60]
    # Natural 0x20000 into update ctx+0x60 (same slot as MASK_STORE): 0x184442eb4.
    # Gated by [ctx+0x5db] bit4, [node+0xd] bit2, [x23+0xb8]==1, then time compare.
    (0x18441ae24, "LAYER_WALK_ENTRY", 16),     # dump x4/x5 (orr'd into x27 mask)
    # NAT20000 fn @0x184442668: PAC/virtual; no direct BL in QC text. Writes
    # 0x20000→ctx+0x60 when [ctx+0x5db]bit4, [node+0xd]bit2, [x23+0xb8]==1,
    # and time[x23+0x70] > ctx+0x50. Early [node+0x88]==0 forces x23=0 → skip.
    (0x184442668, "NAT20000_FN_ENTRY", 32),
    (0x1844426d0, "NAT20000_X23_NULL", 16),     # [x0+0x88]==0 path
    (0x184442e04, "NAT20000_BEFORE_GATE", 32),  # cbnz x23 → gate; dump x23
    (0x184442e44, "NAT20000_GATE", 32),         # after refcount; dump gate bytes
    (0x184442e74, "NAT20000_ORR_68", 8),        # orr #0x20000 → [x20,#0x68]
    (0x184442eb4, "NAT20000_ORR_60", 8),        # orr #0x20000 → [x20,#0x60] = sp170
    # Mutator 0x1844329a4 merges local+0x5d0 into *(&sp0f0). Bit22 (→ orr#8) comes
    # from orr 0x20400000 when w25 bit0 is clear at 0x184433a74; when set, only
    # 0x800000 (bit23) is applied — matches observed natural sp0f0=0x10000800008.
    (0x184433a6c, "MUT_ORR_BIT23", 16),       # orr #0x800000 → +0x5d0
    (0x184433a74, "MUT_W25_BIT22_GATE", 32),  # tbz w25,#0 → orr 0x20400000
    (0x184433a80, "MUT_ORR_BIT22", 8),        # mov 0x20400000 path (bit22)
    (0x18443353c, "MUT_W25_SET1", 16),        # mov w25,#1 — empty-bounds path
    (0x18443351c, "MUT_BOUNDS_CHECK", 16),    # after float ANDs; dump before w25 decision
    (0x184433740, "MUT_VIRT_A8_RET", 16),     # after blraa +0xa8; ands into w25
    (0x18443b0a8, "MUT_MERGE_SP0F0", 16),     # final orr into *(&sp0f0)
    # +0x88 fill: requires [obj+0x78] and [[+0x78]+0x28]; then bl 0x1844282d4.
    (0x18441e8b4, "FILL88_GATE", 32),         # before cbz [x25,#0x78]
    (0x18441e8d4, "FILL88_STORE", 8),          # str x0, [x25, #0x88]
    # +0x78 is written by filter stp at 0x1844214f8 (flags→+0x70, x20→+0x78),
    # but only when ctx+0x168 nonzero. +0x168 itself is set at 0x18441afbc from
    # ctx+0x1a0 iff [layer+0x18] bit26 (0x4000000) is clear; else xzr.
    (0x18441afbc, "SET168_STORE", 32),        # str x10 → [x19,#0x168]
    (0x1844214f4, "SET78_FILTER_GATE", 32),   # cbz x13 → skip stp +0x70/+0x78
    (0x1844214f8, "SET78_FILTER_STORE", 16),  # stp x27,x20 → [x22,#0x70]
    # Large-object +0x78 alloc (may be timestamp/generation, not FILL88 pointer).
    (0x18441d00c, "SET78_ALLOC_CALL", 16),    # bl 0x184449018
    (0x18444920c, "SET78_ALLOC_STORE", 16),   # str x0 → [x20,#0x78]
    # Geometry into +0x10 (mutator empty-bounds reads s2/s3/s1/s0 here).
    (0x18441b184, "BOUNDS_STR_X28_10", 16),  # str d0,[x28,#0x10] from [x23,#0x50]
    (0x184432234, "BOUNDS_COPY_Q", 16),       # stp q1,q2 → [x0,#0x10]
    # AppleDisplay size→rect: ldp w25,w26,[obj+0xb8] then scvtf; skipped if
    # [[x21+0x60]+0x90] bit4 set (tbnz → 0x1845f1640).
    (0x1845f11d8, "AD_SIZE_FN_ENTRY", 8),
    (0x1845f14a8, "AD_SIZE_BIT4_GATE", 16),  # tbnz bit4 → skip sizes
    (0x1845f14b0, "AD_SIZE_LOAD_WH", 16),    # ldp w25,w26 from [x22+0x60]+0xb8
    (0x1845f1584, "AD_SIZE_SCVTF", 8),       # scvtf path when w9==2
    (0x1845f0ee0, "AD_SIZE_PARENT", 8),      # enclosing configurator (PAC/virtual only)
    (0x1845f10f4, "AD_SIZE_SKIP_GATE", 16),  # cbz x4/x8 → skip AD_SIZE_FN
    # Shared early gate of AD_SIZE_PARENT / siblings; siblings have no BL either
    # except into this helper — if these never fire, size→rect is never scheduled.
    (0x1845a5c0c, "CONFIG_APPLY_GATE", 32),
    (0x1845a5b50, "CONFIG_SIBLING_A", 8),
    (0x1845d0e08, "CONFIG_SIBLING_B", 8),

    # Natural 0x8000 into persistent mask [obj+0x70] (feeds X27 via ldp +0x70):
    # after virtual returns nonzero at 0x18448b1bc.
    (0x18448b1bc, "NAT8000_AFTER_VIRT", 32),   # cbz w0 — dump whether virt succeeded
    (0x18448b1cc, "NAT8000_ORR_70", 16),        # orr #0x8000 → [x9,#0x70]
    # Enclosing commit fn (PAC/virtual); early-exit epilogue at 0x184488560, body continues via b #0x594.
    (0x1844881f4, "NAT8000_FN_ENTRY", 16),
    (0x184488270, "NAT8000_FN_MASK_AND", 16),   # ands [x1+0x20], #0x200005020
    # Null-surface layer updates fire early; only log non-null x3 as decisive.
    (0x18447238c, "FB_SWAP_SET_LAYER", 64),
)
KERNEL = (
    # IOConnectCallMethod / user client — optional; keep light
)


def _reg(frame, name):
    v = frame.FindRegister(name)
    return v.GetValueAsUnsigned() if v.IsValid() else None


def _wh_scan(process, base, span=0x800):
    """Return up to 8 (off, h) where u32@off==1179 (A453 width)."""
    import struct
    if not base:
        return []
    err = lldb.SBError()
    blob = process.ReadMemory(base, span, err)
    hits = []
    if not err.Success() or not blob:
        return hits
    for off in range(0, len(blob) - 7, 4):
        w = struct.unpack_from("<I", blob, off)[0]
        if w != 1179:
            continue
        h = struct.unpack_from("<I", blob, off + 4)[0]
        hits.append((off, h))
        if len(hits) >= 8:
            break
    return hits


def _dump_size_slots(process, obj, tag):
    """Dump +0xb8/+0xbc and [obj+0x60]+0xb8/+0xbc (AD_SIZE_LOAD_WH source)."""
    if not obj:
        return
    w = _u32(process, obj + 0xb8)
    h = _u32(process, obj + 0xbc)
    p60 = _u64(process, obj + 0x60)
    w60 = _u32(process, p60 + 0xb8) if p60 else None
    h60 = _u32(process, p60 + 0xbc) if p60 else None
    print("%s obj=%s +b8=(%s,%s) +60=%s +60.b8=(%s,%s) wh1179=%s" % (
        tag, hex(obj), w, h,
        hex(p60) if p60 else None, w60, h60,
        _wh_scan(process, obj),
    ), flush=True)


def _u32(process, addr):
    err = lldb.SBError()
    data = process.ReadMemory(addr, 4, err)
    return int.from_bytes(data, "little") if err.Success() and data else None


def _u8(process, addr):
    err = lldb.SBError()
    data = process.ReadMemory(addr, 1, err)
    return data[0] if err.Success() and data else None


def _u64(process, addr):
    err = lldb.SBError()
    data = process.ReadMemory(addr, 8, err)
    return int.from_bytes(data, "little") if err.Success() and data else None


def on_break(frame, bp_loc, _dict):
    global DISPLAY_WH_OBJ, LAST_MUT
    thread = frame.GetThread()
    process = thread.GetProcess()
    target = process.GetTarget()
    bp_id = bp_loc.GetBreakpoint().GetID()
    cfg = CONFIG[bp_id]
    label = cfg["label"]
    HITS[label] = HITS.get(label, 0) + 1
    hit = HITS[label]
    x0, x1, x2, x3, w0, w8, lr = (_reg(frame, n) for n in (
        "x0", "x1", "x2", "x3", "w0", "w8", "lr"))
    # Null-surface fb_swap fires constantly during setup; keep quiet unless nonempty.
    if label == "FB_SWAP_SET_LAYER" and not x3:
        if hit >= cfg["limit"]:
            bp_loc.GetBreakpoint().SetEnabled(False)
        return False
    # Layer-tree type walk is hot; only emit for 0x19/0x1a or the first few samples.
    if label == "LAYER_WALK_TYPE_BYTE":
        x28 = _reg(frame, "x28")
        t = _u8(process, x28 + 0xc) if x28 else None
        if t not in (0x19, 0x1a) and hit > 8:
            if hit >= cfg["limit"]:
                bp_loc.GetBreakpoint().SetEnabled(False)
            return False
        print("=== %s hit=%d/%d ===" % (label, hit, cfg["limit"]), flush=True)
        print("layer_walk_type node=%s type=%s" % (
            hex(x28) if x28 else None, t), flush=True)
        if hit >= cfg["limit"]:
            bp_loc.GetBreakpoint().SetEnabled(False)
        return False
    print("=== %s hit=%d/%d ===" % (label, hit, cfg["limit"]), flush=True)
    print("registers x0=%s x1=%s x2=%s x3=%s w0=%s w8=%s lr=%s" % (
        hex(x0) if x0 is not None else None,
        hex(x1) if x1 is not None else None,
        hex(x2) if x2 is not None else None,
        hex(x3) if x3 is not None else None,
        hex(w0) if w0 is not None else None,
        hex(w8) if w8 is not None else None,
        hex(lr) if lr is not None else None,
    ), flush=True)
    # render_for_time early-exit / short-path witnesses.
    if label == "OUTER_A_FN_ENTRY" and x0:
        flag390 = _u8(process, x0 + 0x390)
        flag398 = _u8(process, x0 + 0x398)
        ctx = _u64(process, x0 + 0x60)
        flag3b9 = _u8(process, ctx + 0x3b9) if ctx else None
        print("rft_flags server=%s +0x390=%s +0x398=%s ctx=%s +0x3b9=%s bit3=%s" % (
            hex(x0),
            flag390,
            flag398,
            hex(ctx) if ctx is not None else None,
            flag3b9,
            (flag3b9 >> 3) & 1 if flag3b9 is not None else None,
        ), flush=True)
    if label == "RFT_TIME_DECISION":
        # At 0x1844100e8: cmp [x19+0x1c8], x26; ccmp x28,#0; b.ne FULL else SHORT.
        # FULL needs count!=bit17_count AND x28 (temp list) non-null.
        import os
        x19 = _reg(frame, "x19")
        x25 = _reg(frame, "x25")  # server
        x26 = _reg(frame, "x26")  # bit17_count
        x28 = _reg(frame, "x28")  # temp entry list
        slot = _u64(process, x19 + 0x1c8) if x19 is not None else None
        list_ptr = _u64(process, x25 + 0x70) if x25 else None
        list_count = _u64(process, x25 + 0x80) if x25 else None
        # GATE82: arm hotplug latch bytes before v178 so we learn the next code.
        # GATE83: also poke +0x2b8 non-null (v178 only cbz-checks it).
        poked = None
        if (os.environ.get("RFT_POKE_HOTPLUG_LATCH") == "1" and x25):
            ctx = _u64(process, x25 + 0x60)
            st = _u64(process, ctx + 0x330) if ctx else None
            if st:
                err = lldb.SBError()
                process.WriteMemory(st, bytes([1]), err)       # state[0]=1
                process.WriteMemory(st + 5, bytes([1]), err)   # state[5]=1
                poked = (hex(ctx), hex(st), err.Success())
            if (os.environ.get("RFT_POKE_2B8") == "1" and ctx and
                    (_u64(process, ctx + 0x2b8) in (None, 0))):
                err = lldb.SBError()
                # Non-null passes v178 cbz. Prefer 1 over -1: -1 sets +0x2bb=0xff
                # (bit4) which makes v170 return 1 and skip CALL_OUTER (GATE83).
                val = 1 if os.environ.get("RFT_POKE_2B8_M1") != "1" else 0xffffffffffffffff
                process.WriteMemory(
                    ctx + 0x2b8,
                    int(val).to_bytes(8, "little"),
                    err,
                )
                poked = (poked, "2b8", hex(val), err.Success()) if poked else ("2b8", hex(val), err.Success())
        print("rft_time_decision x19=%s slot_1c8=%s x26=%s x28=%s "
              "server=%s list+70=%s count+80=%s ne=%s temp_nonnull=%s "
              "latch_poke=%s" % (
            hex(x19) if x19 is not None else None,
            hex(slot) if slot is not None else None,
            hex(x26) if x26 is not None else None,
            hex(x28) if x28 is not None else None,
            hex(x25) if x25 else None,
            hex(list_ptr) if list_ptr else None,
            hex(list_count) if list_count is not None else None,
            (slot != x26) if (slot is not None and x26 is not None) else None,
            bool(x28) if x28 is not None else None,
            poked,
        ), flush=True)
        # Dump first few 16-byte list slots: qword0=entry ptr, then entry+0xc flags.
        if list_ptr and list_count:
            n = min(int(list_count), 4)
            for i in range(n):
                ent = _u64(process, list_ptr + i * 16)
                flags = _u32(process, ent + 0xc) if ent else None
                print("rft_list_entry i=%d ent=%s +0xc=%s bit17=%s" % (
                    i,
                    hex(ent) if ent else None,
                    hex(flags) if flags is not None else None,
                    ((flags >> 17) & 1) if flags is not None else None,
                ), flush=True)
    if label == "BIT17_LIST_WALK":
        # At 0x18440ff88: x12 = entry object; next insn ubfx bit17 from [entry+0xc].
        x8 = _reg(frame, "x8")    # index
        x12 = _reg(frame, "x12")  # entry
        x25 = _reg(frame, "x25")
        x26 = _reg(frame, "x26")
        flags = _u32(process, x12 + 0xc) if x12 else None
        vt = _u64(process, x12) if x12 else None
        print("bit17_list_walk idx=%s entry=%s vt=%s +0xc=%s bit17=%s "
              "bits(1,3,9,13,17)=%s server=%s bit17_so_far=%s" % (
            hex(x8) if x8 is not None else None,
            hex(x12) if x12 else None,
            hex(vt) if vt else None,
            hex(flags) if flags is not None else None,
            ((flags >> 17) & 1) if flags is not None else None,
            (
                ((flags >> 1) & 1, (flags >> 3) & 1, (flags >> 9) & 1,
                 (flags >> 13) & 1, (flags >> 17) & 1)
                if flags is not None else None
            ),
            hex(x25) if x25 else None,
            hex(x26) if x26 is not None else None,
        ), flush=True)
    if label == "DISP_CTOR_FLAGS_0A":
        x0 = _reg(frame, "x0")
        x1 = _reg(frame, "x1")
        x2 = _reg(frame, "x2")
        x3 = _reg(frame, "x3")  # later lsl#8 source (bits→9/14/17)
        x4 = _reg(frame, "x4")
        print("disp_ctor_flags_0a obj=%s x1=%s x2=%s x3=%s x4=%s "
              "x3_bits(1,6,9)=%s lr=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(x4) if x4 is not None else None,
            (
                ((x3 >> 1) & 1, (x3 >> 6) & 1, (x3 >> 9) & 1)
                if x3 is not None else None
            ),
            hex(lr) if lr else None,
        ), flush=True)
    if label == "DISP_CTOR_ORR_BIT14":
        x19 = _reg(frame, "x19")
        w8 = _reg(frame, "w8")
        before = _u32(process, x19 + 0xc) if x19 else None
        print("disp_ctor_orr_bit14 obj=%s before+0xc=%s w8=%s lr=%s" % (
            hex(x19) if x19 else None,
            hex(before) if before is not None else None,
            hex(w8) if w8 is not None else None,
            hex(lr) if lr else None,
        ), flush=True)
    if label == "DISP_CTOR_ORR_LSL8":
        # About to: orr w8, w8, w20, lsl #8; str [x19,#0xc]
        import os
        x19 = _reg(frame, "x19")
        w8 = _reg(frame, "w8")
        w20 = _reg(frame, "w20")
        before = _u32(process, x19 + 0xc) if x19 else None
        cleared = False
        if (os.environ.get("RFT_CLEAR_CTOR_W3_BIT9") == "1" and
                w20 is not None and (w20 & 0x200)):
            new20 = w20 & ~0x200
            reg = frame.FindRegister("x20")
            ok = reg.SetValueFromCString(hex(new20)) if reg.IsValid() else False
            if ok:
                w20 = new20
                cleared = True
        result = ((before or 0) | (((w20 or 0) & 0xffffffff) << 8)) & 0xffffffff
        print("disp_ctor_orr_lsl8 obj=%s before+0xc=%s w20=%s "
              "w20_bits(1,6,9)=%s result+0xc=%s result_bits(1,3,9,14,17)=%s "
              "matches_2420a=%s cleared_w20_bit9=%s lr=%s" % (
            hex(x19) if x19 else None,
            hex(before) if before is not None else None,
            hex(w20) if w20 is not None else None,
            (
                ((w20 >> 1) & 1, (w20 >> 6) & 1, (w20 >> 9) & 1)
                if w20 is not None else None
            ),
            hex(result),
            (
                (result >> 1) & 1, (result >> 3) & 1, (result >> 9) & 1,
                (result >> 14) & 1, (result >> 17) & 1,
            ),
            result == 0x2420a,
            cleared,
            hex(lr) if lr else None,
        ), flush=True)
    if label == "BIT17_ORR_DIRECT":
        x19 = _reg(frame, "x19")
        w8 = _reg(frame, "w8")
        w9 = _reg(frame, "w9")
        before = _u32(process, x19 + 0xc) if x19 else None
        vt = _u64(process, x19) if x19 else None
        print("bit17_orr_direct obj=%s vt=%s before+0xc=%s w8=%s w9=%s "
              "low_byte=%s lr=%s" % (
            hex(x19) if x19 else None,
            hex(vt) if vt else None,
            hex(before) if before is not None else None,
            hex(w8) if w8 is not None else None,
            hex(w9) if w9 is not None else None,
            hex(before & 0xff) if before is not None else None,
            hex(lr) if lr else None,
        ), flush=True)
    if label in ("DISP_CTOR_CALL_240", "DISP_CTOR_CALL_COMPUTED"):
        import os
        x0 = _reg(frame, "x0")
        x1 = _reg(frame, "x1")
        w2 = _reg(frame, "w2")
        w3 = _reg(frame, "w3")
        w4 = _reg(frame, "w4")
        cleared = False
        # GATE78: w3 bit9 <<8 becomes +0xc bit17 (short-skip). Clear it to test
        # whether bit17-clear alone unlocks RFT_FULL / CREATE_SURFACE.
        if (os.environ.get("RFT_CLEAR_CTOR_W3_BIT9") == "1" and
                w3 is not None and (w3 & 0x200)):
            new3 = w3 & ~0x200
            reg = frame.FindRegister("x3")
            ok = reg.SetValueFromCString(hex(new3)) if reg.IsValid() else False
            cleared = bool(ok)
            w3 = new3 if ok else w3
        print("%s obj=%s x1=%s w2=%s w3=%s w4=%s w3_bits(1,6,9)=%s "
              "cleared_bit9=%s lr=%s" % (
            label.lower(),
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            hex(w2) if w2 is not None else None,
            hex(w3) if w3 is not None else None,
            hex(w4) if w4 is not None else None,
            (
                ((w3 >> 1) & 1, (w3 >> 6) & 1, (w3 >> 9) & 1)
                if w3 is not None else None
            ),
            cleared,
            hex(lr) if lr else None,
        ), flush=True)
    if label == "FLAGS_INIT_3E":
        x0 = _reg(frame, "x0")
        x21 = _reg(frame, "x21")
        parent_c = _u32(process, x21 + 0xc) if x21 else None
        print("flags_init_3e new=%s parent=%s parent+0xc=%s lr=%s" % (
            hex(x0) if x0 else None,
            hex(x21) if x21 else None,
            hex(parent_c) if parent_c is not None else None,
            hex(lr) if lr else None,
        ), flush=True)
    if label == "FLAGS_ORR_FINISH":
        x0 = _reg(frame, "x0")
        w8 = _reg(frame, "w8")
        before = _u32(process, x0 + 0xc) if x0 else None
        print("flags_orr_finish obj=%s before+0xc=%s orr_w8=%s result=%s "
              "bit17=%s lr=%s" % (
            hex(x0) if x0 else None,
            hex(before) if before is not None else None,
            hex(w8) if w8 is not None else None,
            hex((before or 0) | (w8 or 0)) if before is not None else None,
            ((((before or 0) | (w8 or 0)) >> 17) & 1) if before is not None else None,
            hex(lr) if lr else None,
        ), flush=True)
    if label in ("FLAGS_ORR_BIT13_A", "FLAGS_ORR_BIT13_B"):
        x8 = _reg(frame, "x8")
        w9 = _reg(frame, "w9")
        before = _u32(process, x8 + 0xc) if x8 else None
        print("%s obj=%s before+0xc=%s w9=%s bit13=%s bit17=%s lr=%s" % (
            label.lower(),
            hex(x8) if x8 else None,
            hex(before) if before is not None else None,
            hex(w9) if w9 is not None else None,
            ((w9 >> 13) & 1) if w9 is not None else None,
            ((w9 >> 17) & 1) if w9 is not None else None,
            hex(lr) if lr else None,
        ), flush=True)
    if label == "FLAGS_BIT9_INJECT":
        x22 = _reg(frame, "x22")
        w8 = _reg(frame, "w8")
        before = _u32(process, x22 + 0xc) if x22 else None
        print("flags_bit9_inject obj=%s before+0xc=%s w8=%s bit9=%s bit17=%s lr=%s" % (
            hex(x22) if x22 else None,
            hex(before) if before is not None else None,
            hex(w8) if w8 is not None else None,
            ((w8 >> 9) & 1) if w8 is not None else None,
            ((w8 >> 17) & 1) if w8 is not None else None,
            hex(lr) if lr else None,
        ), flush=True)
    if label == "BIT17_CLEAR_STORE":
        # About to str w8 → [x28,#0xc] after conditional clear of bit17.
        x28 = _reg(frame, "x28")
        w8 = _reg(frame, "w8")
        x27 = _reg(frame, "x27")
        before = _u32(process, x28 + 0xc) if x28 else None
        print("bit17_clear_store obj=%s before+0xc=%s before_bit17=%s w8=%s "
              "w8_bit17=%s x27=%s x27_bit54=%s lr=%s" % (
            hex(x28) if x28 else None,
            hex(before) if before is not None else None,
            ((before >> 17) & 1) if before is not None else None,
            hex(w8) if w8 is not None else None,
            ((w8 >> 17) & 1) if w8 is not None else None,
            hex(x27) if x27 is not None else None,
            ((x27 >> 54) & 1) if x27 is not None else None,
            hex(lr) if lr else None,
        ), flush=True)
    if label == "BIT17_ORR_LSL8_STORE":
        # About to str w8 → [x28,#0xc] after orr with w6<<8 (bit9 of w6 → bit17).
        x28 = _reg(frame, "x28")
        w8 = _reg(frame, "w8")
        w6 = _reg(frame, "w6")
        before = _u32(process, x28 + 0xc) if x28 else None
        print("bit17_orr_lsl8_store obj=%s before+0xc=%s before_bit17=%s "
              "w6=%s w6_bit9=%s w8=%s w8_bit17=%s lr=%s" % (
            hex(x28) if x28 else None,
            hex(before) if before is not None else None,
            ((before >> 17) & 1) if before is not None else None,
            hex(w6) if w6 is not None else None,
            ((w6 >> 9) & 1) if w6 is not None else None,
            hex(w8) if w8 is not None else None,
            ((w8 >> 17) & 1) if w8 is not None else None,
            hex(lr) if lr else None,
        ), flush=True)
    if label == "RFT_FULL_AFTER_V178":
        # Receiver is x20 = [server+0x60] (display/ctx); method was vt+0x178.
        # Method 0x18442f208: returns 0xff unless [self+0x330] hotplug-state
        # bytes pass (byte0==1 || byte1==1), then further checks → 0 on success.
        # GATE79/80: always w0=0xff (early exit). Dump the state blob.
        x20 = _reg(frame, "x20")
        x25 = _reg(frame, "x25")
        vt = _u64(process, x20) if x20 else None
        slot = _u64(process, vt + 0x178) if vt else None
        st = _u64(process, x20 + 0x330) if x20 else None
        b0 = b1 = b2 = b3 = b4 = b5 = None
        if st:
            b0 = _u8(process, st)
            b1 = _u8(process, st + 1)
            b2 = _u8(process, st + 2)
            b3 = _u8(process, st + 3)
            b4 = _u8(process, st + 4)
            b5 = _u8(process, st + 5)
        ptr2b8 = _u64(process, x20 + 0x2b8) if x20 else None
        print("rft_v178_ret w0=%s x1=%s recv=%s vt=%s slot178=%s "
              "state330=%s bytes=%s +2b8=%s server=%s" % (
            hex(w0) if w0 is not None else None,
            hex(x1) if x1 is not None else None,
            hex(x20) if x20 else None,
            hex(vt) if vt else None,
            hex(slot) if slot else None,
            hex(st) if st else None,
            (b0, b1, b2, b3, b4, b5),
            hex(ptr2b8) if ptr2b8 else None,
            hex(x25) if x25 else None,
        ), flush=True)
    if label == "RFT_FULL_BR_108CC":
        x8 = _reg(frame, "x8")
        print("rft_108cc sentinel=%s is_m1=%s" % (
            hex(x8) if x8 is not None else None,
            (x8 == 0xffffffffffffffff) if x8 is not None else None,
        ), flush=True)
    if label == "RFT_FULL_AFTER_V248":
        print("rft_v248_ret w0=%s" % (hex(w0) if w0 is not None else None), flush=True)
    if label == "RFT_FULL_AFTER_V218":
        x19 = _reg(frame, "x19")
        x20 = _reg(frame, "x20")
        flag68a = _u8(process, x19 + 0x68a) if x19 is not None else None
        print("rft_v218_ret w0=%s w20=%s flag68a=%s" % (
            hex(w0) if w0 is not None else None,
            hex(x20) if x20 is not None else None,
            flag68a,
        ), flush=True)
    if label == "RFT_FULL_AFTER_VF8":
        print("rft_vf8_ret w0=%s" % (hex(w0) if w0 is not None else None), flush=True)
    if label == "UPDATE_FLAGS_MASK_CHECK":
        sp = _reg(frame, "sp")
        x21 = _reg(frame, "x21")
        m170 = _u64(process, sp + 0x170) if sp is not None else None
        m0f0 = _u64(process, sp + 0xf0) if sp is not None else None
        f140 = _u64(process, x21 + 0x140) if x21 else None
        print("update_flags_mask sp170=%s sp0f0=%s has_28000=%s bit22_0f0=%s flags140=%s" % (
            hex(m170) if m170 is not None else None,
            hex(m0f0) if m0f0 is not None else None,
            bool(m170 & 0x28000) if m170 is not None else None,
            ((m0f0 >> 22) & 1) if m0f0 is not None else None,
            hex(f140) if f140 is not None else None,
        ), flush=True)
        import os
        if os.environ.get("RFT_FORCE_SP0F0_BIT22") == "1" and sp is not None and m0f0 is not None:
            # orr#8 via tbnz w8,#0x16 on [sp,#0xf0] — alone satisfies flags&0xa.
            err = lldb.SBError()
            process.WriteMemory(sp + 0xf0, int(m0f0 | (1 << 22)).to_bytes(8, "little"), err)
            print("rft_force_sp0f0_bit22 ok=%s sp0f0=%s" % (
                err.Success(), hex(m0f0 | (1 << 22))), flush=True)
        if os.environ.get("RFT_FORCE_UPDATE_MASK_0A") == "1" and sp is not None and m170 is not None:
            # Real ands mask is 0x28000 (0x8000|0x20000); also set bit22 of sp0f0 for orr#8.
            err = lldb.SBError()
            process.WriteMemory(sp + 0x170, int(m170 | 0x28000).to_bytes(8, "little"), err)
            if m0f0 is not None:
                process.WriteMemory(sp + 0xf0, int(m0f0 | (1 << 22)).to_bytes(8, "little"), err)
            print("rft_force_update_mask_0a ok=%s" % err.Success(), flush=True)
    if label == "UPDATE_FLAGS_ORR_2":
        print("update_flags_orr2 hit", flush=True)
    if label == "UPDATE_FLAGS_ORR_8":
        print("update_flags_orr8 hit", flush=True)
    if label == "LAYER_WALK_ENTRY":
        import os
        x4 = _reg(frame, "x4")
        x5 = _reg(frame, "x5")
        if os.environ.get("RFT_FORCE_WALK_X4_20000") == "1" and x4 is not None:
            new = x4 | 0x20000
            reg = frame.FindRegister("x4")
            ok = reg.SetValueFromCString(hex(new)) if reg.IsValid() else False
            x4 = new if ok else x4
            print("rft_force_walk_x4_20000 ok=%s x4=%s" % (ok, hex(x4)), flush=True)
        print("layer_walk_entry x0=%s x1=%s x2=%s x3=%s x4=%s x5=%s has_28000_x4=%s" % (
            hex(x0) if x0 is not None else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(x4) if x4 is not None else None,
            hex(x5) if x5 is not None else None,
            bool((x4 or 0) & 0x28000),
        ), flush=True)
        if HITS.get("LAYER_WALK_ENTRY", 0) <= 4:
            import struct as _st
            def _f4(base):
                if not base:
                    return None
                err = lldb.SBError()
                b = process.ReadMemory(base + 0x10, 16, err)
                return _st.unpack('<ffff', b) if err.Success() and b else None
            f1, f2 = _f4(x1), _f4(x2)
            print("layer_walk_geom x1+10=%s x2+10=%s x1_big=%s x2_big=%s" % (
                f1, f2,
                any(v is not None and abs(v) > 2.0 for v in (f1 or ())),
                any(v is not None and abs(v) > 2.0 for v in (f2 or ())),
            ), flush=True)
    if label == "NAT20000_FN_ENTRY":
        import os
        lr = _reg(frame, "lr")
        lr_bare = _xpac(lr)
        lr_static = (lr_bare - DSC_SLIDE) if lr_bare is not None else None
        node88 = _u64(process, x0 + 0x88) if x0 else None
        b5db = _u8(process, x1 + 0x5db) if x1 else None
        bn0d = _u8(process, x0 + 0xd) if x0 else None
        print("nat20000_fn_entry x0=%s x1=%s x2=%s node88=%s +5db=%s bit4=%s +0xd=%s bit2=%s "
              "lr_static=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            hex(x2) if x2 else None,
            hex(node88) if node88 else None,
            b5db,
            ((b5db >> 4) & 1) if b5db is not None else None,
            bn0d,
            ((bn0d >> 2) & 1) if bn0d is not None else None,
            hex(lr_static) if lr_static is not None else None,
        ), flush=True)
        if os.environ.get("RFT_FORCE_NAT20000_GATES") == "1" and x0 and x1:
            err = lldb.SBError()
            # Satisfy gate bytes; time poke happens at BEFORE_GATE when x23 known.
            process.WriteMemory(x1 + 0x5db, bytes([(_u8(process, x1 + 0x5db) or 0) | 0x10]), err)
            process.WriteMemory(x0 + 0xd, bytes([(_u8(process, x0 + 0xd) or 0) | 0x4]), err)
            print("rft_force_nat20000_gates_entry ok=%s" % err.Success(), flush=True)
    if label == "NAT20000_X23_NULL":
        print("nat20000_x23_null (node+0x88 was 0)", flush=True)
    if label == "NAT20000_BEFORE_GATE":
        import os
        x20 = _reg(frame, "x20")
        x23 = _reg(frame, "x23")
        t50 = _u64(process, x20 + 0x50) if x20 else None
        t70 = _u64(process, x23 + 0x70) if x23 else None
        b0b8 = _u8(process, x23 + 0xb8) if x23 else None
        print("nat20000_before_gate x23=%s +0xb8=%s ctx50=%s obj70=%s" % (
            hex(x23) if x23 else None,
            b0b8,
            hex(t50) if t50 is not None else None,
            hex(t70) if t70 is not None else None,
        ), flush=True)
        if os.environ.get("RFT_FORCE_NAT20000_GATES") == "1" and x23 and x20:
            err = lldb.SBError()
            process.WriteMemory(x23 + 0xb8, bytes([1]), err)
            # Make fcmp d1=[ctx+0x50] < d0=[obj+0x70]: zero ctx time, obj time = 1.0
            process.WriteMemory(x20 + 0x50, (0).to_bytes(8, "little"), err)
            process.WriteMemory(x23 + 0x70,
                                (0x3ff0000000000000).to_bytes(8, "little"), err)
            print("rft_force_nat20000_gates_time ok=%s" % err.Success(), flush=True)
    if label == "NAT20000_GATE":
        import os
        x20 = _reg(frame, "x20")
        x23 = _reg(frame, "x23")
        x28 = _reg(frame, "x28")
        b5db = _u8(process, x20 + 0x5db) if x20 else None
        bn0d = _u8(process, x28 + 0xd) if x28 else None
        b0b8 = _u8(process, x23 + 0xb8) if x23 else None
        m60 = _u64(process, x20 + 0x60) if x20 else None
        print("nat20000_gate x20=%s x23=%s x28=%s +5db=%s bit4=%s +0xd=%s bit2=%s +0xb8=%s m60=%s" % (
            hex(x20) if x20 else None,
            hex(x23) if x23 else None,
            hex(x28) if x28 else None,
            b5db,
            ((b5db >> 4) & 1) if b5db is not None else None,
            bn0d,
            ((bn0d >> 2) & 1) if bn0d is not None else None,
            b0b8,
            hex(m60) if m60 is not None else None,
        ), flush=True)
    if label in ("NAT20000_ORR_60", "NAT20000_ORR_68"):
        x20 = _reg(frame, "x20")
        print("nat20000_orr hit label=%s x20=%s" % (
            label, hex(x20) if x20 else None), flush=True)
    if label == "FILL88_GATE":
        x25 = _reg(frame, "x25")
        x19 = _reg(frame, "x19")
        p78 = _u64(process, x25 + 0x78) if x25 else None
        p78_28 = _u64(process, p78 + 0x28) if p78 else None
        p88 = _u64(process, x25 + 0x88) if x25 else None
        p18 = _u64(process, x25 + 0x18) if x25 else None
        p28 = _u64(process, x25 + 0x28) if x25 else None
        type_b = _u8(process, x25 + 0xc) if x25 else None
        c168 = _u64(process, x19 + 0x168) if x19 else None
        c1a0 = _u64(process, x19 + 0x1a0) if x19 else None
        c358 = _u64(process, x19 + 0x358) if x19 else None
        print("fill88_gate x25=%s type_c=%s +18=%s bit26=%s +28=%s +78=%s +78_28=%s "
              "+88=%s ctx168=%s ctx1a0=%s ctx358=%s will_alloc=%s" % (
            hex(x25) if x25 else None, type_b,
            hex(p18) if p18 is not None else None,
            bool(p18 & 0x4000000) if p18 is not None else None,
            hex(p28) if p28 else None,
            hex(p78) if p78 else None,
            hex(p78_28) if p78_28 else None,
            hex(p88) if p88 else None,
            hex(c168) if c168 else None,
            hex(c1a0) if c1a0 else None,
            hex(c358) if c358 else None,
            bool(p78 and p78_28 and not p88),
        ), flush=True)
        import os, struct
        if os.environ.get("RFT_FORCE_FILL78") == "1" and x25 and not p78 and p28:
            # Natural objects already have +0x28 → layer. FILL88 wants
            # [[+0x78]+0x28]; pointing +0x78 at self reuses that +0x28.
            err = lldb.SBError()
            process.WriteMemory(x25 + 0x78, struct.pack('<Q', x25), err)
            print("rft_force_fill78 self_ptr ok=%s +28=%s" % (
                err.Success(), hex(p28)), flush=True)
    if label == "SET78_ALLOC_CALL":
        x0 = _reg(frame, "x0")
        x25 = _reg(frame, "x25")
        x28 = _reg(frame, "x28")
        print("set78_alloc_call x0=%s x25=%s x28=%s same_as_25=%s" % (
            hex(x0) if x0 else None,
            hex(x25) if x25 else None,
            hex(x28) if x28 else None,
            x0 == x25 if x0 and x25 else None,
        ), flush=True)
    if label == "SET78_ALLOC_STORE":
        x0 = _reg(frame, "x0")
        x20 = _reg(frame, "x20")
        print("set78_alloc_store obj=%s val=%s" % (
            hex(x20) if x20 else None, hex(x0) if x0 else None), flush=True)
    if label == "BOUNDS_STR_X28_10":
        x28 = _reg(frame, "x28")
        x23 = _reg(frame, "x23")
        import struct
        err = lldb.SBError()
        raw = process.ReadMemory(x28 + 0x10, 16, err) if x28 else None
        floats = struct.unpack('<ffff', raw) if err.Success() and raw else None
        src = _u64(process, x23 + 0x50) if x23 else None
        print("bounds_str_x28_10 x28=%s src_x23_50=%s floats_after_will_be_from_d0 src50=%s" % (
            hex(x28) if x28 else None,
            hex(src) if src is not None else None,
            floats,
        ), flush=True)
    if label == "BOUNDS_COPY_Q":
        x0 = _reg(frame, "x0")
        x1 = _reg(frame, "x1")
        import struct
        err = lldb.SBError()
        raw = process.ReadMemory(x1, 0x30, err) if x1 else None
        if err.Success() and raw:
            # dst +0x10 gets q1 (x1+0x10) and q2 (x1+0x20)
            src10 = struct.unpack('<ffff', raw[0x10:0x20])
            src20 = struct.unpack('<ffff', raw[0x20:0x30])
            print("bounds_copy_q dst=%s src=%s src+10=%s src+20=%s nonempty=%s" % (
                hex(x0) if x0 else None, hex(x1) if x1 else None,
                src10, src20,
                any(abs(v) > 1e-6 for v in src10 + src20),
            ), flush=True)
        else:
            print("bounds_copy_q dst=%s src=%s read_fail" % (
                hex(x0) if x0 else None, hex(x1) if x1 else None), flush=True)
    if label == "AD_SIZE_FN_ENTRY":
        print("ad_size_fn_entry x0=%s x1=%s" % (
            hex(x0) if x0 else None, hex(x1) if x1 else None), flush=True)
    if label == "AD_SIZE_BIT4_GATE":
        x21 = _reg(frame, "x21")
        p60 = _u64(process, x21 + 0x60) if x21 else None
        b90 = _u8(process, p60 + 0x90) if p60 else None
        print("ad_size_bit4_gate x21=%s +60=%s +90=%s bit4=%s will_skip=%s" % (
            hex(x21) if x21 else None,
            hex(p60) if p60 else None,
            hex(b90) if b90 is not None else None,
            bool(b90 & 0x10) if b90 is not None else None,
            bool(b90 & 0x10) if b90 is not None else None,
        ), flush=True)
    if label == "AD_SIZE_LOAD_WH":
        x0 = _reg(frame, "x0")
        w = _u32(process, x0 + 0xb8) if x0 else None
        h = _u32(process, x0 + 0xbc) if x0 else None
        print("ad_size_load_wh obj=%s w=%s h=%s is_display=%s" % (
            hex(x0) if x0 else None, w, h,
            (w, h) == (1179, 2556) if w is not None else None,
        ), flush=True)
    if label == "AD_SIZE_SCVTF":
        w25 = _reg(frame, "w25")
        w26 = _reg(frame, "w26")
        if w25 is None:
            x25 = _reg(frame, "x25")
            w25 = x25 & 0xffffffff if x25 is not None else None
        if w26 is None:
            x26 = _reg(frame, "x26")
            w26 = x26 & 0xffffffff if x26 is not None else None
        print("ad_size_scvtf w25=%s w26=%s" % (w25, w26), flush=True)
    if label == "HOTPLUG_SETUP_FN_ENTRY":
        if hit <= 5 and x0:
            _dump_size_slots(process, x0, "hotplug_setup")
            # Also scan a few deep slots that often hold the IOMFB display nub.
            for off in (0x60, 0x330, 0x6490, 0x64d0, 0x6700):
                p = _u64(process, x0 + off)
                if p and p > 0x100000000:
                    _dump_size_slots(process, p, "hotplug_slot_%x" % off)
    if label == "CONFIG_APPLY_GATE":
        lr = _reg(frame, "lr")
        w2 = _reg(frame, "w2")
        if w2 is None:
            x2 = _reg(frame, "x2")
            w2 = x2 & 0xffffffff if x2 is not None else None
        print("config_apply_gate x0=%s x1=%s w2=%s lr=%s" % (
            hex(x0) if x0 else None, hex(x1) if x1 else None,
            w2, hex(lr) if lr else None), flush=True)
    if label in ("CONFIG_SIBLING_A", "CONFIG_SIBLING_B"):
        lr = _reg(frame, "lr")
        x2 = _reg(frame, "x2")
        print("%s x0=%s x1=%s x2=%s lr=%s" % (
            label.lower(), hex(x0) if x0 else None,
            hex(x1) if x1 else None, hex(x2) if x2 else None,
            hex(lr) if lr else None), flush=True)
        if x2:
            b70 = _u8(process, x2 + 0x70)
            b68 = _u8(process, x2 + 0x68)
            print("%s_txn +68=%s +70=%s bit4_70=%s" % (
                label.lower(), b68, b70,
                bool(b70 & 0x10) if b70 is not None else None), flush=True)
    if label == "AD_SIZE_PARENT":
        lr = _reg(frame, "lr")
        x2 = _reg(frame, "x2")
        print("ad_size_parent x0=%s x1=%s x2=%s lr=%s" % (
            hex(x0) if x0 else None, hex(x1) if x1 else None,
            hex(x2) if x2 else None, hex(lr) if lr else None), flush=True)
        if x1:
            _dump_size_slots(process, x1, "ad_size_parent_x1")
        if x2:
            p10 = _u64(process, x2 + 0x10)
            b70 = _u8(process, x2 + 0x70)
            print("ad_size_parent_txn +10=%s +70=%s bit4=%s" % (
                hex(p10) if p10 else None, b70,
                bool(b70 & 0x10) if b70 is not None else None), flush=True)
    if label == "AD_SIZE_SKIP_GATE":
        x4 = _reg(frame, "x4")
        x8 = _reg(frame, "x8")
        x23 = _reg(frame, "x23")
        p18 = _u64(process, x23 + 0x18) if x23 else None
        p20 = _u64(process, x23 + 0x20) if x23 else None
        print("ad_size_skip_gate x4=%s x8=%s x23+18=%s x23+20=%s will_call=%s" % (
            hex(x4) if x4 else None, hex(x8) if x8 else None,
            hex(p18) if p18 else None, hex(p20) if p20 else None,
            bool(x4 and x8),
        ), flush=True)
    if label == "SET168_STORE":
        x19 = _reg(frame, "x19")
        x25 = _reg(frame, "x25")
        x10 = _reg(frame, "x10")
        x22 = _reg(frame, "x22")  # [x25+0x18] flags just loaded
        c1a0 = _u64(process, x19 + 0x1a0) if x19 else None
        print("set168 x10=%s x22_flags=%s bit26=%s ctx1a0=%s layer=%s nonempty=%s" % (
            hex(x10) if x10 else None,
            hex(x22) if x22 is not None else None,
            bool(x22 & 0x4000000) if x22 is not None else None,
            hex(c1a0) if c1a0 else None,
            hex(x25) if x25 else None,
            bool(x10),
        ), flush=True)
    if label == "SET78_FILTER_GATE":
        x13 = _reg(frame, "x13")
        x19 = _reg(frame, "x19")
        x20 = _reg(frame, "x20")
        x22 = _reg(frame, "x22")
        x27 = _reg(frame, "x27")
        c168 = _u64(process, x19 + 0x168) if x19 else None
        c358 = _u64(process, x19 + 0x358) if x19 else None
        print("set78_gate x13=%s will_store=%s x22=%s x27=%s x20=%s "
              "ctx168=%s ctx358=%s" % (
            hex(x13) if x13 else None, bool(x13),
            hex(x22) if x22 else None,
            hex(x27) if x27 is not None else None,
            hex(x20) if x20 else None,
            hex(c168) if c168 else None,
            hex(c358) if c358 else None,
        ), flush=True)
    if label == "SET78_FILTER_STORE":
        x20 = _reg(frame, "x20")
        x22 = _reg(frame, "x22")
        x27 = _reg(frame, "x27")
        p28 = _u64(process, x20 + 0x28) if x20 else None
        print("set78_store layer=%s +70=%s +78=%s +78_28=%s" % (
            hex(x22) if x22 else None,
            hex(x27) if x27 is not None else None,
            hex(x20) if x20 else None,
            hex(p28) if p28 else None,
        ), flush=True)
    if label == "FILL88_STORE":
        x25 = _reg(frame, "x25")
        x0 = _reg(frame, "x0")
        print("fill88_store x25=%s new88=%s" % (
            hex(x25) if x25 else None, hex(x0) if x0 else None), flush=True)
    if label == "MUT_ORR_BIT23":
        x9 = _reg(frame, "x9")
        print("mut_orr_bit23 x9=%s" % (hex(x9) if x9 is not None else None), flush=True)
    if label == "MUT_BOUNDS_CHECK":
        x1 = _reg(frame, "x1")
        # s2,s3 at +0x10; s1 at +0x18; s0 at +0x1c; x8=[x1+0x88]; x10=[x8+0x50]
        def _f32(addr):
            err = lldb.SBError()
            b = process.ReadMemory(addr, 4, err)
            if not err.Success() or not b:
                return None
            import struct
            return struct.unpack('<f', b)[0]
        s2 = _f32(x1 + 0x10) if x1 else None
        s3 = _f32(x1 + 0x14) if x1 else None
        s1 = _f32(x1 + 0x18) if x1 else None
        s0 = _f32(x1 + 0x1c) if x1 else None
        x8 = _u64(process, x1 + 0x88) if x1 else None
        x10 = _u64(process, x8 + 0x50) if x8 else None
        tpidr = _reg(frame, "tpidr_el1") or 0
        all0 = ((s2 == 0 and s3 == 0 and s1 == 0 and s0 == 0 and not x10)
                if None not in (s2, s3, s1, s0) else None)
        LAST_MUT = {
            "label": "MUT_BOUNDS_CHECK",
            "tpidr_el1": tpidr,
            "thread": thread.GetThreadID(),
            "x1": x1,
            "bounds": [s2, s3, s1, s0],
            "all0": all0,
            "type_c": _u8(process, x1 + 0xc) if x1 else None,
            "time": __import__("time").time(),
        }
        print("mut_context label=MUT_BOUNDS_CHECK thread=0x%x sp=0x%x x1=%s x2=%s x3=%s lr=%s" % (
            tpidr, _reg(frame, "sp") or 0,
            hex(x1) if x1 else None,
            hex(_reg(frame, "x2")) if _reg(frame, "x2") else None,
            hex(_reg(frame, "x3")) if _reg(frame, "x3") else None,
            hex(_reg(frame, "lr")) if _reg(frame, "lr") else None), flush=True)
        print("mut_bounds x1=%s s2=%s s3=%s s1=%s s0=%s x88=%s x88_50=%s all0=%s" % (
            hex(x1) if x1 else None, s2, s3, s1, s0,
            hex(x8) if x8 else None, hex(x10) if x10 else None, all0,
        ), flush=True)
        # Partner geometry: +0x70 often points at a sibling/layer slot.
        if x1 and HITS.get("MUT_BOUNDS_CHECK", 0) <= 4:
            p70 = _u64(process, x1 + 0x70)
            if p70:
                def _f32p(addr):
                    err = lldb.SBError()
                    b = process.ReadMemory(addr, 4, err)
                    if not err.Success() or not b:
                        return None
                    import struct as _st
                    return _st.unpack('<f', b)[0]
                ps = tuple(_f32p(p70 + o) for o in (0x10, 0x14, 0x18, 0x1c, 0x20, 0x24, 0x28, 0x2c))
                print("mut_partner70 p70=%s floats+10..2c=%s any_big=%s" % (
                    hex(p70), ps,
                    any(v is not None and abs(v) > 2.0 for v in ps),
                ), flush=True)
        # One-shot identity dump for naturalization.
        if x1 and HITS.get("MUT_BOUNDS_CHECK", 0) <= 2:
            vt = _u64(process, x1)
            type_b = _u8(process, x1 + 0xc)
            # Common nearby slots
            p20 = _u64(process, x1 + 0x20)
            p28 = _u64(process, x1 + 0x28)
            p18 = _u64(process, x1 + 0x18)
            p68 = _u64(process, x1 + 0x68)
            p70 = _u64(process, x1 + 0x70)
            p78 = _u64(process, x1 + 0x78)
            p88 = _u64(process, x1 + 0x88)
            p78_28 = _u64(process, p78 + 0x28) if p78 else None
            print("mut_bounds_id vt=%s type_c=%s +18=%s bit26=%s +20=%s +28=%s "
                  "+68=%s +70=%s +78=%s +78_28=%s +88=%s" % (
                hex(vt) if vt else None, type_b,
                hex(p18) if p18 is not None else None,
                bool(p18 & 0x4000000) if p18 is not None else None,
                hex(p20) if p20 else None, hex(p28) if p28 else None,
                hex(p68) if p68 else None, hex(p70) if p70 else None,
                hex(p78) if p78 else None,
                hex(p78_28) if p78_28 else None,
                hex(p88) if p88 else None,
            ), flush=True)
            _dump_size_slots(process, x1, "mut_bounds_self")
            if p70:
                _dump_size_slots(process, p70, "mut_bounds_p70")
            if p28 and p28 > 0x100000000:
                _dump_size_slots(process, p28, "mut_bounds_p28")
        import os, struct
        if os.environ.get("RFT_FORCE_BOUNDS_NONEMPTY") == "1" and x1:
            # Breakpoint is ON the tbz w9,#0 — clear w9 so we skip the empty path.
            # Also patch +0x10 for any later re-reads.
            err = lldb.SBError()
            process.WriteMemory(x1 + 0x10, struct.pack('<f', 1179.0), err)
            reg = frame.FindRegister("x9")
            ok = reg.SetValueFromCString("0") if reg.IsValid() else False
            print("rft_force_bounds_nonempty mem_ok=%s w9_ok=%s" % (
                err.Success(), ok), flush=True)
        # Soft naturalization: copy staged AppleDisplay WxH into empty mutator
        # bounds (source is the same A453 geometry already on +0xb8).
        if (os.environ.get("RFT_BOUNDS_FROM_DISPLAY") == "1" and x1 and
                DISPLAY_WH_OBJ):
            if s2 is None or abs(s2) < 1e-6:
                dw = _u32(process, DISPLAY_WH_OBJ + 0xb8)
                dh = _u32(process, DISPLAY_WH_OBJ + 0xbc)
                if dw and dh:
                    err = lldb.SBError()
                    process.WriteMemory(
                        x1 + 0x10,
                        struct.pack('<ffff', 0.0, 0.0, float(dw), float(dh)),
                        err)
                    reg = frame.FindRegister("x9")
                    ok = reg.SetValueFromCString("0") if reg.IsValid() else False
                    print("rft_bounds_from_display src=%s wh=(%s,%s) mem_ok=%s w9_ok=%s" % (
                        hex(DISPLAY_WH_OBJ), dw, dh, err.Success(), ok),
                        flush=True)
    if label == "MUT_W25_SET1":
        print("mut_context label=MUT_W25_SET1 thread=0x%x sp=0x%x x0=%s x1=%s x2=%s x3=%s w25=%s lr=%s" % (
            _reg(frame, "tpidr_el1") or 0, _reg(frame, "sp") or 0,
            hex(_reg(frame, "x0")) if _reg(frame, "x0") else None,
            hex(_reg(frame, "x1")) if _reg(frame, "x1") else None,
            hex(_reg(frame, "x2")) if _reg(frame, "x2") else None,
            hex(_reg(frame, "x3")) if _reg(frame, "x3") else None,
            hex(_reg(frame, "w25")) if _reg(frame, "w25") is not None else None,
            hex(_reg(frame, "lr")) if _reg(frame, "lr") else None), flush=True)
    if label == "MUT_VIRT_A8_RET":
        w0 = _reg(frame, "w0")
        if w0 is None:
            w0 = _reg(frame, "x0")
            if w0 is not None:
                w0 = w0 & 0xffffffff
        w25 = _reg(frame, "w25")
        if w25 is None:
            w25 = _reg(frame, "x25")
            if w25 is not None:
                w25 = w25 & 0xffffffff
        print("mut_virt_a8_ret w0=%s w25_before_and=%s clears_w25=%s" % (
            hex(w0) if w0 is not None else None,
            hex(w25) if w25 is not None else None,
            (w0 == 1) if w0 is not None else None,
        ), flush=True)
    if label == "MUT_W25_BIT22_GATE":
        import os
        w25 = _reg(frame, "w25")
        if w25 is None:
            w25 = _reg(frame, "x25")
            if w25 is not None:
                w25 = w25 & 0xffffffff
        will_orr = (w25 & 1) == 0 if w25 is not None else None
        tpidr = _reg(frame, "tpidr_el1") or 0
        print("mut_w25_bit22_gate w25=%s bit0=%s will_orr_bit22=%s" % (
            hex(w25) if w25 is not None else None,
            (w25 & 1) if w25 is not None else None,
            will_orr,
        ), flush=True)
        print("mut_context label=MUT_W25_BIT22_GATE thread=0x%x sp=0x%x x0=%s x1=%s x2=%s x3=%s lr=%s" % (
            tpidr, _reg(frame, "sp") or 0,
            hex(_reg(frame, "x0")) if _reg(frame, "x0") else None,
            hex(_reg(frame, "x1")) if _reg(frame, "x1") else None,
            hex(_reg(frame, "x2")) if _reg(frame, "x2") else None,
            hex(_reg(frame, "x3")) if _reg(frame, "x3") else None,
            hex(_reg(frame, "lr")) if _reg(frame, "lr") else None), flush=True)
        prev = LAST_MUT if isinstance(LAST_MUT, dict) else {}
        LAST_MUT = dict(prev)
        LAST_MUT.update({
            "label": "MUT_W25_BIT22_GATE",
            "tpidr_el1": tpidr,
            "thread": thread.GetThreadID(),
            "w25": w25,
            "will_orr_bit22": will_orr,
            "same_thread_as_bounds": (
                prev.get("tpidr_el1") == tpidr if prev else None),
            "prior_bounds_all0": prev.get("all0"),
            "prior_bounds": prev.get("bounds"),
            "time": __import__("time").time(),
        })
        print("mut_w25_corr same_thread=%s prior_all0=%s prior_bounds=%s" % (
            LAST_MUT["same_thread_as_bounds"],
            LAST_MUT["prior_bounds_all0"],
            LAST_MUT["prior_bounds"]), flush=True)
        if os.environ.get("RFT_FORCE_W25_CLEAR") == "1" and w25 is not None:
            reg = frame.FindRegister("x25")
            ok = False
            if reg.IsValid():
                # Clear only bit0; keep the rest of x25.
                x25 = _reg(frame, "x25") or 0
                ok = reg.SetValueFromCString(hex(x25 & ~1))
            print("rft_force_w25_clear ok=%s" % ok, flush=True)
    if label == "MUT_ORR_BIT22":
        print("mut_orr_bit22 path taken", flush=True)
    if label == "MUT_MERGE_SP0F0":
        x8 = _reg(frame, "x8")
        x9 = _reg(frame, "x9")
        x11 = _reg(frame, "x11")
        cur = _u64(process, x11) if x11 else None
        print("mut_merge_sp0f0 x8=%s x9=%s cur=%s has_bit22_x8=%s" % (
            hex(x8) if x8 is not None else None,
            hex(x9) if x9 is not None else None,
            hex(cur) if cur is not None else None,
            bool((x8 or 0) & 0x400000),
        ), flush=True)
    if label == "NAT8000_AFTER_VIRT":
        x19 = _reg(frame, "x19")
        obj = _u64(process, x19 + 0x40) if x19 else None
        m70 = _u64(process, obj + 0x70) if obj else None
        print("nat8000_after_virt w0=%s obj40=%s m70=%s will_orr=%s" % (
            hex(w0) if w0 is not None else None,
            hex(obj) if obj else None,
            hex(m70) if m70 is not None else None,
            bool(w0),
        ), flush=True)
    if label == "NAT8000_ORR_70":
        x9 = _reg(frame, "x9")
        x8 = _reg(frame, "x8")
        print("nat8000_orr70 x9=%s x8=%s" % (
            hex(x9) if x9 else None,
            hex(x8) if x8 is not None else None,
        ), flush=True)
    if label == "NAT8000_FN_ENTRY":
        x1 = _reg(frame, "x1")
        lr = _reg(frame, "lr")
        m20 = _u64(process, x1 + 0x20) if x1 else None
        # PAC'd LR → static DSC offset for xref hunting.
        lr_bare = _xpac(lr)
        lr_static = (lr_bare - DSC_SLIDE) if lr_bare is not None else None
        # Parent frame PC if LLDB unwound one.
        parent_pc = None
        parent_static = None
        try:
            thr = frame.GetThread()
            if thr.GetNumFrames() > 1:
                parent_pc = thr.GetFrameAtIndex(1).GetPC()
                parent_bare = _xpac(parent_pc)
                parent_static = (parent_bare - DSC_SLIDE) if parent_bare else None
        except Exception:
            pass
        print("nat8000_fn_entry x0=%s x1=%s m20=%s and_200005020=%s "
              "lr=%s lr_static=%s parent_pc=%s parent_static=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            hex(m20) if m20 is not None else None,
            hex(m20 & 0x200005020) if m20 is not None else None,
            hex(lr) if lr else None,
            hex(lr_static) if lr_static is not None else None,
            hex(parent_pc) if parent_pc else None,
            hex(parent_static) if parent_static is not None else None,
        ), flush=True)
        # Also scan this/arg0 vtable for the NAT8000 slot (caller may be blraa).
        _scan_vtable_for_nat8000(process, x0, "nat8000_self")
        if x1 and x1 != x0:
            _scan_vtable_for_nat8000(process, x1, "nat8000_arg1")
    if label == "NAT8000_FN_MASK_AND":
        x8 = _reg(frame, "x8")
        x28 = _reg(frame, "x28")
        print("nat8000_fn_mask_and x8=%s x28=%s eq_clear=%s" % (
            hex(x8) if x8 is not None else None,
            hex(x28) if x28 is not None else None,
            (x8 & 0x200005020) == 0 if x8 is not None else None,
        ), flush=True)
    if label == "LAYER_WALK_X27_INIT":
        import os
        # Breakpoint is ON the csel — x27 not written yet. x9 is incoming mask.
        x4 = _reg(frame, "x4")
        x5 = _reg(frame, "x5")
        x9 = _reg(frame, "x9")
        x10 = _reg(frame, "x10")
        x21 = _reg(frame, "x21")
        obj98 = _u64(process, x21 + 0x98) if x21 else None
        m70 = _u64(process, obj98 + 0x70) if obj98 else None
        if os.environ.get("RFT_FORCE_OBJ70_8000") == "1" and obj98 and m70 is not None:
            err = lldb.SBError()
            process.WriteMemory(obj98 + 0x70, int(m70 | 0x8000).to_bytes(8, "little"), err)
            # Also refresh x10 if it was loaded from that slot (ldp x10,x9,[obj,#0x70]).
            if x10 is not None:
                reg = frame.FindRegister("x10")
                if reg.IsValid():
                    reg.SetValueFromCString(hex(x10 | 0x8000))
                    x10 = x10 | 0x8000
            if x9 is not None:
                # x9 may be the second half; the mask orr uses x10|x4 → put bit on x9 path too
                reg = frame.FindRegister("x9")
                # At csel, x9 is already (x10|x4) candidate from prior orr at af90.
                # Force bit into x9 so csel picks it up.
                new9 = x9 | 0x8000
                if reg.IsValid():
                    reg.SetValueFromCString(hex(new9))
                    x9 = new9
            print("rft_force_obj70_8000 ok=%s obj=%s m70=%s x9=%s" % (
                err.Success(), hex(obj98), hex(m70 | 0x8000), hex(x9)), flush=True)
        print("layer_walk_x27_init x4=%s x5=%s x9=%s x10=%s obj98=%s m70=%s has_28000_x9=%s" % (
            hex(x4) if x4 is not None else None,
            hex(x5) if x5 is not None else None,
            hex(x9) if x9 is not None else None,
            hex(x10) if x10 is not None else None,
            hex(obj98) if obj98 else None,
            hex(m70) if m70 is not None else None,
            bool((x9 or 0) & 0x28000),
        ), flush=True)
    if label == "LAYER_WALK_ORR_80":
        x19 = _reg(frame, "x19")
        m360 = _u64(process, x19 + 0x360) if x19 else None
        print("layer_walk_orr80 +0x360=%s" % (
            hex(m360) if m360 is not None else None), flush=True)
    if label == "LAYER_WALK_TST_28000":
        x8 = _reg(frame, "x8")
        x27 = _reg(frame, "x27")
        print("layer_walk_tst28000 x8=%s x27=%s has_28000=%s" % (
            hex(x8) if x8 is not None else None,
            hex(x27) if x27 is not None else None,
            bool((x8 or 0) & 0x28000),
        ), flush=True)
    if label == "LAYER_WALK_MASK_STORE":
        import os
        x9 = _reg(frame, "x9")
        x8 = _reg(frame, "x8")
        x27 = _reg(frame, "x27")
        x28 = _reg(frame, "x28")
        if os.environ.get("RFT_FORCE_MASK_STORE_20080") == "1" and x9 is not None:
            # On the stp — inject 0x20000 (survives filter; satisfies ands #0x28000).
            new = x9 | 0x20000
            reg = frame.FindRegister("x9")
            ok = reg.SetValueFromCString(hex(new)) if reg.IsValid() else False
            x9 = new if ok else x9
            print("rft_force_mask_store_20080 ok=%s x9=%s" % (ok, hex(x9)), flush=True)
        print("layer_walk_mask_store x9=%s x8=%s x27=%s x28=%s has_28000_x9=%s has_28000_x8=%s" % (
            hex(x9) if x9 is not None else None,
            hex(x8) if x8 is not None else None,
            hex(x27) if x27 is not None else None,
            hex(x28) if x28 else None,
            bool((x9 or 0) & 0x28000),
            bool((x8 or 0) & 0x28000),
        ), flush=True)
    if label == "DISPLAY_SET_2B8_SENTINEL":
        x19 = _reg(frame, "x19")
        print("display_set_sentinel obj=%s" % (hex(x19) if x19 else None), flush=True)
    if label == "DISPLAY_MODE_SET_ENTRY":
        print("display_mode_set_entry obj=%s x1=%s x2=%s x3=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            hex(x2) if x2 else None,
            hex(x3) if x3 else None,
        ), flush=True)
    if label == "DISPLAY_MODE_SET_STORE":
        x19 = _reg(frame, "x19")
        x20 = _reg(frame, "x20")
        x22 = _reg(frame, "x22")
        print("display_mode_set_store obj=%s mode0=%s mode1=%s" % (
            hex(x19) if x19 else None,
            hex(x20) if x20 is not None else None,
            hex(x22) if x22 is not None else None,
        ), flush=True)
    if label == "APPLEDISPLAY_MODE_BRANCH":
        # cmp just done; w8 was [x26]. Dump x26 byte and whether we will take ne→skip.
        x26 = _reg(frame, "x26")
        b = _u8(process, x26) if x26 else None
        print("appledisplay_mode_branch x26=%s byte=%s" % (
            hex(x26) if x26 else None, b), flush=True)
    if label == "APPLEDISPLAY_MODE_SKIP":
        x19 = _reg(frame, "x19") or x0
        q = _u64(process, x19 + 0x2b8) if x19 else None
        print("appledisplay_mode_skip obj=%s +0x2b8=%s" % (
            hex(x19) if x19 else None,
            hex(q) if q is not None else None,
        ), flush=True)
    if label == "APPLEDISPLAY_NEW_SERVER_ENTRY" and x0:
        q = _u64(process, x0 + 0x2b8)
        print("new_server_display obj=%s +0x2b8=%s +0x2bb=%s bit4=%s" % (
            hex(x0),
            hex(q) if q is not None else None,
            (q >> 24) & 0xff if q is not None else None,
            ((q >> 28) & 1) if q is not None else None,
        ), flush=True)
        _dump_size_slots(process, x0, "new_server")
        w = _u32(process, x0 + 0xb8)
        h = _u32(process, x0 + 0xbc)
        if (w, h) == (1179, 2556):
            DISPLAY_WH_OBJ = x0
            print("display_wh_obj_capture obj=%s" % hex(x0), flush=True)
        _scan_vtable_for_nat8000(process, x0, "new_server")
        # Also scan server pointer if present at a common slot.
        for off in (0x60, 0x68, 0x70, 0x80, 0x88, 0x90):
            p = _u64(process, x0 + off)
            if p and p > 0x100000000:
                _scan_vtable_for_nat8000(process, p, "new_server_slot_%x" % off)
                _dump_size_slots(process, p, "new_server_slot_%x" % off)
        import os
        if os.environ.get("RFT_CLEAR_2B8_AT_NEWSERVER") == "1" and q == 0xffffffffffffffff:
            err = lldb.SBError()
            process.WriteMemory(x0 + 0x2bb, bytes([0xef]), err)
            print("rft_clear_2bb_bit4_at_newserver ok=%s" % err.Success(), flush=True)
        if os.environ.get("RFT_SET_7537_AT_NEWSERVER") == "1":
            err = lldb.SBError()
            process.WriteMemory(x0 + 0x7537, bytes([1]), err)
            print("rft_set_7537_at_newserver ok=%s" % err.Success(), flush=True)
        # Soft naturalization seed: write A453 geometry into +0xb8 if empty.
        if os.environ.get("RFT_SEED_DISPLAY_WH") == "1":
            w = _u32(process, x0 + 0xb8)
            h = _u32(process, x0 + 0xbc)
            if w in (None, 0) or h in (None, 0):
                import struct
                err = lldb.SBError()
                process.WriteMemory(x0 + 0xb8, struct.pack("<II", 1179, 2556), err)
                print("rft_seed_display_wh ok=%s was=(%s,%s)" % (
                    err.Success(), w, h), flush=True)
    if label == "DISPLAY_CTOR_AFTER_2B8" and x0:
        # Right after strb wzr,[x0,#0x2b8]; qword may still be garbage in bytes 1-7.
        q = _u64(process, x0 + 0x2b8)
        print("display_ctor_2b8 obj=%s qword=%s +0x2bb=%s" % (
            hex(x0),
            hex(q) if q is not None else None,
            (q >> 24) & 0xff if q is not None else None,
        ), flush=True)
    if label in ("STR_XZR_2B8_A", "STR_XZR_2B8_B", "STR_XZR_2B8_C"):
        # Before the store: which object is being nulled?
        if label == "STR_XZR_2B8_B":
            base = x1
        else:
            base = _reg(frame, "x19")
        q = _u64(process, base + 0x2b8) if base else None
        print("str_xzr_2b8 label=%s obj=%s before=%s" % (
            label, hex(base) if base else None,
            hex(q) if q is not None else None,
        ), flush=True)
    if label == "ACCEL_SERVER_CTOR_ENTRY":
        # x0=AccelServer self; x1 is typically the Display being wrapped.
        for tag, obj in (("self", x0), ("x1", x1)):
            if not obj:
                continue
            q = _u64(process, obj + 0x2b8)
            print("accel_ctor_%s obj=%s +0x2b8=%s +0x2bb=%s bit4=%s" % (
                tag, hex(obj),
                hex(q) if q is not None else None,
                (q >> 24) & 0xff if q is not None else None,
                ((q >> 28) & 1) if q is not None else None,
            ), flush=True)
            _scan_vtable_for_nat8000(process, obj, "accel_%s" % tag)
            _dump_size_slots(process, obj, "accel_%s" % tag)
    if label in ("OUTER_A_FN_ENTRY", "RFT_FULL_PATH"):
        # OUTER/FULL: x0 is server; context at server+0x60.
        if x0 and HITS.get(label, 0) <= 2:
            _scan_vtable_for_nat8000(process, x0, "outer_server")
        if x0:
            ctx = _u64(process, x0 + 0x60)
            q = _u64(process, ctx + 0x2b8) if ctx else None
            print("rft_2bb_trace label=%s server=%s ctx=%s +0x2b8=%s +0x2bb=%s bit4=%s" % (
                label,
                hex(x0),
                hex(ctx) if ctx else None,
                hex(q) if q is not None else None,
                (q >> 24) & 0xff if q is not None else None,
                ((q >> 28) & 1) if q is not None else None,
            ), flush=True)
    if label == "RFT_FULL_BEFORE_V170":
        # x0 is display/context; dump vtable slot +0x170 target and +0x2b8 qword.
        x0v = x0
        vt = _u64(process, x0v) if x0v else None
        slot = _u64(process, vt + 0x170) if vt else None
        bare = (slot & 0x0000ffffffffffff) if slot is not None else None
        q = _u64(process, x0v + 0x2b8) if x0v else None
        flag2bb = (q >> 24) & 0xff if q is not None else None
        x1_140 = _u64(process, x1 + 0x140) if x1 else None
        print("rft_v170_target obj=%s vt=%s bare=%s +0x2b8=%s +0x2bb=%s bit4=%s x1_140=%s" % (
            hex(x0v) if x0v else None,
            hex(vt) if vt else None,
            hex(bare) if bare is not None else None,
            hex(q) if q is not None else None,
            flag2bb,
            ((flag2bb >> 4) & 1) if flag2bb is not None else None,
            hex(x1_140) if x1_140 is not None else None,
        ), flush=True)
        # Experimental: clear bit4 so method returns 0 (pass). Opt-in via env.
        import os
        if os.environ.get("RFT_CLEAR_2BB_BIT4") == "1" and x0v and flag2bb is not None:
            newv = flag2bb & ~0x10
            err = lldb.SBError()
            process.WriteMemory(x0v + 0x2bb, bytes([newv]), err)
            print("rft_clear_2bb_bit4 old=%s new=%s ok=%s" % (
                flag2bb, newv, err.Success()), flush=True)
        # Alternate pass for type-0 sentinel (-1): set update flags 0x2|0x8.
        if os.environ.get("RFT_SET_UPDATE_FLAGS_0A") == "1" and x1 and x1_140 is not None:
            if (x1_140 & 0xa) == 0:
                err = lldb.SBError()
                newf = x1_140 | 0xa
                process.WriteMemory(x1 + 0x140, int(newf).to_bytes(8, "little"), err)
                print("rft_set_update_flags_0a old=%s new=%s ok=%s" % (
                    hex(x1_140), hex(newf), err.Success()), flush=True)
    if label == "RFT_FULL_AFTER_V170":
        print("rft_v170_ret w0=%s bit0=%s" % (
            hex(w0) if w0 is not None else None,
            (w0 & 1) if w0 is not None else None,
        ), flush=True)
    if label == "FB_SWAP_SET_LAYER" and x3:
        print("FB_SWAP_NONEMPTY x3=%s layer=%s" % (
            hex(x3), hex(x2) if x2 is not None else None), flush=True)
    # One-shot return probe for digital-out: dump IOReturn (x0) and *out
    if label == "GET_HOTPLUG_STATE_ENTRY" and lr and x1 and hit <= 4:
        out_ptr = x1
        rbp = target.BreakpointCreateByAddress(lr)
        CONFIG[rbp.GetID()] = {
            "label": "GET_HOTPLUG_STATE_RETURN", "limit": 1, "out_ptr": out_ptr,
        }
        HITS["GET_HOTPLUG_STATE_RETURN"] = 0
        interpreter = target.GetDebugger().GetCommandInterpreter()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F render_gate_callbacks.on_break %d" %
            rbp.GetID(), result)
        print("armed return bp at lr=%s out=%s" % (hex(lr), hex(out_ptr)), flush=True)
    if label == "GET_HOTPLUG_STATE_RETURN":
        out_ptr = cfg.get("out_ptr")
        state = _u32(process, out_ptr) if out_ptr else None
        print("hotplug_return ior=%s state=%s out=%s" % (
            hex(x0) if x0 is not None else None,
            state,
            hex(out_ptr) if out_ptr else None,
        ), flush=True)
    if hit >= cfg["limit"]:
        bp_loc.GetBreakpoint().SetEnabled(False)
    return False


def install(debugger, slide):
    import os
    global DSC_SLIDE, DISPLAY_WH_OBJ
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    slide = int(slide)
    DSC_SLIDE = slide
    DISPLAY_WH_OBJ = None
    slim = os.environ.get("RFT_SLIM", "")
    if slim == "1":
        # Store-poke / flags path only — late attach without 80 hot breakpoints.
        want = {
            "LAYER_WALK_ENTRY", "LAYER_WALK_X27_INIT",
            "LAYER_WALK_TST_28000", "LAYER_WALK_MASK_STORE",
            "NAT20000_FN_ENTRY", "NAT20000_X23_NULL", "NAT20000_BEFORE_GATE",
            "NAT20000_GATE", "NAT20000_ORR_60", "NAT20000_ORR_68",
            "MUT_ORR_BIT23", "MUT_W25_BIT22_GATE", "MUT_ORR_BIT22", "MUT_MERGE_SP0F0",
            "MUT_BOUNDS_CHECK", "MUT_W25_SET1", "MUT_VIRT_A8_RET",
            "FILL88_GATE", "FILL88_STORE",
            "SET168_STORE", "SET78_FILTER_GATE", "SET78_FILTER_STORE",
            "SET78_ALLOC_CALL", "SET78_ALLOC_STORE",
            "BOUNDS_STR_X28_10", "BOUNDS_COPY_Q",
            "AD_SIZE_FN_ENTRY", "AD_SIZE_BIT4_GATE", "AD_SIZE_LOAD_WH", "AD_SIZE_SCVTF",
            "AD_SIZE_PARENT", "AD_SIZE_SKIP_GATE",
            "CONFIG_APPLY_GATE", "CONFIG_SIBLING_A", "CONFIG_SIBLING_B",
            "HOTPLUG_SETUP_FN_ENTRY", "UPDATE_FB_GET_HOTPLUG", "UPDATE_FB_HOTPLUG_OR",
            "GET_HOTPLUG_STATE_ENTRY", "GET_HOTPLUG_STATE_RETURN",
            "NAT8000_FN_ENTRY", "NAT8000_FN_MASK_AND",
            "NAT8000_AFTER_VIRT", "NAT8000_ORR_70",
            "UPDATE_FLAGS_MASK_CHECK", "UPDATE_FLAGS_ORR_2", "UPDATE_FLAGS_ORR_8",
            # GATE79: early FULL-path abort sites (GATE78 reached FULL, never V170).
            "RFT_FULL_AFTER_V178", "RFT_FULL_V178_OK", "RFT_FULL_SKIP_V178",
            "RFT_FULL_CHECK_548", "RFT_FULL_SKIP_NULL548",
            "RFT_FULL_BR_108CC", "RFT_FULL_108CC_FALL",
            "RFT_FULL_BEFORE_V248", "RFT_FULL_AFTER_V248",
            "RFT_FULL_TBNZ_109CC", "RFT_FULL_BR_10C2C", "RFT_FULL_BR_10DF4",
            "RFT_FULL_BR_114D0", "RFT_FULL_BEFORE_V218", "RFT_FULL_AFTER_V218",
            "RFT_FULL_SKIP_1157C", "RFT_FULL_SKIP_V218_PATH", "RFT_FULL_CONT_11594",
            "RFT_FULL_BEFORE_VF8", "RFT_FULL_AFTER_VF8", "RFT_FULL_VF8_OK",
            "RFT_FULL_SKIP_VF8", "RFT_FULL_TO_1469C",
            "RFT_FULL_BEFORE_V170", "RFT_FULL_AFTER_V170", "RFT_FULL_V170_OK",
            "CALL_OUTER_A", "OUTER_A_FN_ENTRY",
            "RFT_TIME_DECISION", "RFT_SHORT_SKIP", "RFT_FULL_PATH",
            "BIT17_LIST_WALK", "BIT17_CLEAR_STORE", "BIT17_ORR_LSL8_STORE",
            "FLAGS_INIT_3E", "FLAGS_ORR_FINISH", "FLAGS_ORR_BIT13_A",
            "FLAGS_ORR_BIT13_B", "FLAGS_BIT9_INJECT",
            "DISP_CTOR_FLAGS_0A", "DISP_CTOR_ORR_BIT14", "DISP_CTOR_ORR_LSL8",
            "BIT17_ORR_DIRECT", "DISP_CTOR_CALL_240", "DISP_CTOR_CALL_COMPUTED",
            "APPLEDISPLAY_NEW_SERVER_ENTRY", "ACCEL_SERVER_CTOR_ENTRY",
        }
        sites = [(a, l, lim) for a, l, lim in USER if l in want]
    else:
        sites = list(USER)
    print("RENDER_GATE_READY slide=0x%x slim=%s sites=%d" % (
        slide, slim or "0", len(sites)), flush=True)
    for addr, label, limit in sites:
        runtime = addr + slide
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % runtime, result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("bp failed %s: %s" % (label, result.GetError()))
        bp = target.GetBreakpointAtIndex(before)
        CONFIG[bp.GetID()] = {"label": label, "limit": limit}
        HITS[label] = 0
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F render_gate_callbacks.on_break %d" %
            bp.GetID(), result)
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" % (
            bp.GetID(), label, runtime), flush=True)
    for addr, label, limit in KERNEL:
        runtime = addr + KERNEL_SLIDE
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % runtime, result)
        bp = target.GetBreakpointAtIndex(before)
        CONFIG[bp.GetID()] = {"label": label, "limit": limit}
        HITS[label] = 0
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F render_gate_callbacks.on_break %d" %
            bp.GetID(), result)
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" % (
            bp.GetID(), label, runtime), flush=True)

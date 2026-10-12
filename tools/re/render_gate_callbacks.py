"""Post-WS-setup render/digital-out witnesses (docs/re/cadisplay-discovery-gates.md).

    script render_gate_callbacks.install(lldb.debugger, <dsc_slide>)
"""
import json
import lldb

CONFIG = {}
ORPHAN_BREAKPOINT_IDS = set()
HITS = {}
KERNEL_SLIDE = 0x20000000
# The true video proxy vtable was measured at runtime in GATE202. Keep this
# seed so the post-slide LLDB session can filter the same object class; the
# early-start callback refreshes it from the live object on every boot.
VIDEO_INTERFACE_VTABLE_STATIC_LOW48 = 0xfff0081d1990
DSC_SLIDE = 0
DISPLAY_WH_OBJ = None  # AppleDisplay with staged A453 WxH at +0xb8
NAT8000_STATIC = 0x1844881f4
# Last mutator sample for same-thread correlation with CA send/recv hooks.
LAST_MUT = None
# GATE97: last nonzero IOSurface from ALLOCATE_IOSURFACE_RETURN.
LAST_ALLOC_SURFACE = None
# GATE105: IOMFB object from begin/handoff for forced SwapEnd after x22.
LAST_IOMFB = None
# GATE113: last non-zero io_connect_t from IOMobileFramebufferOpen.
LAST_GOOD_CONN = None
# GATE124: connect from a successful _IOMobileFramebufferOpen only
# (not arbitrary backboardd IOServiceOpen).
LAST_IOMFB_CONN = None
# GATE125/131: (obj, conn) from backboardd _IOMobileFramebufferOpen only.
# Virt swap objects need a sibling/recent type-0 connect; foreign-process
# Opens (thermalmonitord, MobileGestaltHelper) must not enter this list.
IOMFB_OPEN_PAIRS = []
LAST_BB_IOMFB_CONN = None  # most recent backboardd kern-slotted connect
LAST_BB_IOMFB = None
# GATE115: candidate connects from GetMatchingService→Open in-process.
LAST_CONNS = []
# GATE119: last backboardd io_service_t from GetMatchingService.
LAST_MATCHED_SVC = None
LAST_TASK_PORT = 0x203  # mach_task_self-style seen on live opens
OPEN_INJECT_STATE = None  # None | "pending" | "done"
RUNTIME_ADDR = {}  # label -> runtime address
AFK_PUBLISH_THREAD_CONTEXT = {}  # LLDB thread id -> (iface id, service name)
AFK_PUBLISH_IFACE_BY_NAME = {}
AFK_PUBLISH_NUB_CONTEXT = {}  # nub address -> (iface id, service name)
LAYER_STATE70_WATCH = None  # (address, watchpoint id) for GATE241
LAYER_STATE70_WATCH_HITS = 0
MODE_2B8_WATCH = None  # (display, selected mode, watchpoint id)
MODE_2B8_WATCH_HITS = 0
MODE_6B00_WATCH = None  # (display, watchpoint id)
MODE_6B00_WATCH_HITS = 0
# GATE245: correlate CALayer objects copied into render layers with contents
# assignments seen later in the same client process.
COPY_CALAYER_KEYS = set()
MIPI_AP_PROBE_MATCHES = 0


def _xpac(val):
    """Strip PAC high bits — low 48 are the code pointer on arm64e DSC."""
    return val & 0x0000ffffffffffff if val is not None else None


def _canonical_kernel_address(val):
    """Return a PAC-stripped, sign-extended 48-bit kernel VA for LLDB."""
    bare = _xpac(val)
    if bare is not None and bare & (1 << 47):
        bare |= 0xffff000000000000
    return bare


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
    (0x184681c40, "DISPLAY_MODE_POST_STORE", 16),
    # GATE169: IOMFBDisplay::update_framebuffer_locked fallback Mode(-1) path.
    (0x1847af5e0, "DISPLAY_MODE_FALLBACK_MINUS_ONE", 16),
    (0x1847af5f0, "DISPLAY_MODE_ADD_MODE", 32),
    (0x1847af650, "DISPLAY_SET_ALL_MODES_CALL", 16),
    (0x1847af678, "DISPLAY_POST_SET_V308_RETURN", 16),
    # GATE170: why update_framebuffer_locked has no concrete display mode.
    (0x1847ae4c4, "DISPLAY_UPDATE_FRAMEBUFFER_ENTRY", 16),
    (0x1847af1b4, "DISPLAY_MODE_UPDATE_FLAGS", 16),
    (0x1847af1f4, "DISPLAY_MODE_SIGNAL_TYPE2_BRANCH", 8),
    (0x1847af1fc, "DISPLAY_MODE_SIGNAL_TYPE1_BRANCH", 8),
    (0x1847af218, "DISPLAY_MODE_ARRAY_RETURN", 16),
    # GATE252: inspect the actual objects returned by the IOMFB mode query
    # before QuartzCore asks each array for its count.
    (0x1847af238, "DISPLAY_MODE_COLOR_ARRAY_COUNT_CALL", 16),
    (0x1847af2d0, "DISPLAY_MODE_TIMING_ARRAY_COUNT_CALL", 16),
    (0x1847af2d8, "DISPLAY_MODE_TIMING_ARRAY_COUNT_BRANCH", 16),
    (0x1847af2f8, "DISPLAY_MODE_PREFERRED_MODE_GATE", 16),
    (0x1847af31c, "DISPLAY_MODE_SINGLE_MODE_GATE", 16),
    # GATE178: type-2 branch's direct supported-mode query returns at +0x270.
    (0x1847af270, "DISPLAY_MODE_ARRAY_RETURN_TYPE2", 16),
    (0x1847af240, "DISPLAY_MODE_ARRAY_COUNT_BRANCH", 16),
    (0x1847af254, "DISPLAY_MODE_SIGNAL_TYPE2_STATE", 8),
    (0x1847af298, "DISPLAY_UPDATE_DIGITAL_MODES_RETURN_A", 16),
    (0x1847af338, "DISPLAY_UPDATE_DIGITAL_MODES_RETURN_B", 16),
    (0x1847af340, "DISPLAY_FETCH_CURRENT_MODE_RETURN", 16),
    (0x1847b1930, "DISPLAY_UPDATE_DIGITAL_MODES_ENTRY", 16),
    (0x1847b1d6c, "DISPLAY_UPDATE_DIGITAL_MODES_ATTRIBUTES_RESULT", 8),
    (0x1847b1e9c, "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_COUNT_GATE", 8),
    (0x1847b1eec, "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_LOOP", 8),
    (0x1847b1ef8, "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_ELEMENT", 8),
    (0x1847b1f20, "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_ATTRIBUTES", 8),
    (0x1847b1fa4, "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_FILTER", 8),
    (0x1847b2034, "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_2034", 8),
    (0x1847b2090, "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_2090", 8),
    (0x1847b20a4, "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_20A4", 8),
    (0x1847b214c, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_214C", 8),
    (0x1847b2150, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2150", 8),
    (0x1847b219c, "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_219C", 8),
    (0x1847b23b4, "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_23B4", 8),
    # Nested Color candidate: classify its key values and first reject route.
    (0x1847b2548, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2548", 8),
    (0x1847b25f8, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_25F8", 8),
    (0x1847b27bc, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_27BC", 8),
    (0x1847b28c4, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_28C4", 8),
    (0x1847b29d8, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_29D8", 8),
    (0x1847b2d14, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2D14", 8),
    (0x1847b2d88, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2D88", 8),
    (0x1847b2dd0, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2DD0", 8),
    (0x1847b2e24, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2E24", 8),
    (0x1847b2e28, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2E28", 8),
    (0x1847b2e2c, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2E2C", 8),
    (0x1847b2e40, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2E40", 8),
    (0x1847b2e50, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2E50", 8),
    (0x1847b2e54, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2E54", 8),
    (0x1847b3564, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_3564", 8),
    (0x1847b3628, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_3628", 8),
    (0x1847b3680, "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_3680", 8),
    (0x1847b3690, "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_3690", 8),
    (0x1847b37a4, "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_37A4", 8),
    (0x1847b2094, "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_NEXT", 8),
    # GATE237: trace the natural producer of Display state bytes [4] and [5].
    (0x1845bd954, "DISPLAY_POWER_TARGET_STATE", 32),
    (0x1845bdaa4, "DISPLAY_POWER_STORE_STATE5", 32),
    # The power-change path can replace the pending mode before the caller
    # copies it into current +0x2b8. Keep these stops on the unmodified path.
    (0x1845bdd30, "DISPLAY_POWER_MODE_LOOKUP_RETURN", 8),
    (0x1845bdf28, "DISPLAY_POWER_MODE_NORMALIZE", 8),
    (0x1845bdd38, "DISPLAY_POWER_MODE_COPY_SOURCE", 8),
    (0x1845be218, "DISPLAY_HOTPLUG_MODE_KEYS", 8),
    (0x1845be23c, "DISPLAY_HOTPLUG_MODE_FIRST_MATCH", 8),
    (0x1845be25c, "DISPLAY_HOTPLUG_MODE_SECOND_MATCH", 8),
    (0x1845be26c, "DISPLAY_HOTPLUG_MODE_LIST", 8),
    (0x1845be298, "DISPLAY_HOTPLUG_MODE_MISS", 8),
    (0x1845be2c8, "DISPLAY_HOTPLUG_MODE_LIST_MATCH", 8),
    (0x1846128f0, "DISPLAY_SET_POWER_STATE_LOCKED", 32),
    # GATE175: identify and sample the implementation behind
    # IOMobileFramebufferGetSupportedDigitalOutModes.
    (0x22a3c3628, "IOMFB_MODE_KERN_ENTRY", 8),
    (0x22a3c3570, "IOMFB_SET_DIGITAL_OUT_MODE_KERN", 8),
    (0x22a3c35e8, "IOMFB_SET_DIGITAL_OUT_MODE_IOCONNECT_RETURN", 8),
    # 0x360c moves the saved IOConnect return from x20 into x0; stop after it.
    (0x22a3c3610, "IOMFB_SET_DIGITAL_OUT_MODE_KERN_EXIT", 8),
    (0x22a3c73dc, "IOMFB_SET_DIGITAL_OUT_MODE_VIRT", 8),
    # AP A413 output becomes an IOReturn; this QuartzCore caller branches on it.
    (0x1847afe0c, "DISPLAY_DIGITAL_OUT_CALLER_STATUS", 8),
    (0x1847afe10, "DISPLAY_DIGITAL_OUT_CALLER_CONTINUE", 8),
    (0x1847b0f54, "DISPLAY_DIGITAL_OUT_CALLER_FAILURE", 8),
    # GATE185: inspect the two IORegistry properties consumed by the
    # kernel-backed digital-mode provider and their CFArray type checks.
    (0x22a3c36c0, "IOMFB_MODE_COLOR_PROPERTY_RESULT", 16),
    (0x22a3c36dc, "IOMFB_MODE_COLOR_TYPE_COMPARE", 16),
    (0x22a3c3718, "IOMFB_MODE_TIMING_PROPERTY_RESULT", 16),
    (0x22a3c3738, "IOMFB_MODE_TIMING_TYPE_COMPARE", 16),
    (0x22a3c3798, "IOMFB_MODE_KERN_RETURN", 16),
    (0x22a3c73cc, "IOMFB_MODE_VIRT_DISPATCH", 8),
    (0x22a3c7448, "IOMFB_MODE_VIRT_RETURN", 16),
    # _iomfb_match_callback's non-type-2 descriptor path computes the type in w8.
    (0x22a3957a8, "IOMFB_NAME_MATCH_TYPE_STORE", 32),
    (0x22a3957c4, "IOMFB_NAME_MATCH_APPEND_COUNT", 32),
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
    # GATE86: post-V170 breadcrumbs — GATE85 never reached WP_12000 after V170_OK.
    (0x1844115ec, "RFT_POST_V170_TIME", 16),   # after bl 0x1843f6ddc
    (0x184411630, "RFT_POST_V170_V1B8", 16),   # after blraa vt+0x1b8
    (0x18441168c, "RFT_POST_V170_CMP648", 16), # cmp +0x648/+0x40
    (0x184411694, "RFT_POST_V170_BEQ648", 16), # b.eq → 0x11d00 (skips CALL_OUTER)
    (0x184411698, "RFT_POST_V170_NEQ648", 16), # fallthrough toward CALL_OUTER
    (0x184411d00, "RFT_POST_V170_ALT_11D00", 16),  # eq-path entry
    (0x184411710, "RFT_POST_V170_GFLAG", 16),  # tbnz global → 0x14dbc
    (0x1844117bc, "RFT_POST_V170_20C", 16),    # ldrb [x19,#0x20c]; tbnz #3
    (0x184413810, "RFT_ALT_MERGE_13810", 16),  # alt path lands here past CALL_OUTER
    (0x184411c48, "RFT_FULL_SKIP_VF8", 32),    # shared skip (vf8 fail OR v170 bit0)
    (0x184414d54, "RFT_FULL_EXIT_14D54", 16),
    (0x184414da8, "RFT_FULL_EPILOG_14DA8", 16),
    (0x184411c10, "RFT_FULL_TO_1469C", 16),
    (0x184412000, "RFT_WP_12000", 16),
    (0x184413000, "RFT_WP_13000", 8),
    (0x1844134cc, "CALL_OUTER_A", 8),          # sole bl SERVER_UPDATE_OUTER in RFT
    # GATE92: IOMFB update_surface loads WxH via ldp w1,w2,[x24,#8] then
    # bl DISPLAY_ALLOCATE (0x1846820e4). GATE91 CREATE still saw x1=x2=0.
    (0x18445b684, "ALLOC_LDP_WH", 16),
    (0x18445b6ac, "ALLOC_BL_DISPLAY", 8),
    # GATE97: SWAP_SET_LAYER always saw x2=0; poke last alloc surface to test FB path.
    (0x184478b48, "SWAP_SET_LAYER_ENTRY", 64),
    # GATE99: FB_SWAP_HANDOFF lands in stub→0x22a391ea0; null [x0+0xb38]
    # returns 0xe00002c2 and never reaches H17P swap_submit.
    (0x22a391ea0, "HANDOFF_SINK_ENTRY", 32),
    (0x22a391eb0, "HANDOFF_SINK_FAIL", 16),
    # GATE100/101: finish_update real-surface SWAP vs clear-path.
    # MODE_ECHO: the virtual +0x6c0 return and packed current mode jointly
    # decide whether finish_update may enter its non-null surface path.
    (0x18446bd80, "FINISH_MODE_ELIGIBILITY_RETURN", 64),
    (0x18446bdb0, "FINISH_MODE_ELIGIBILITY_JOIN", 64),
    # The explicit display+0x7545=1 writer used by the clear-layer test.
    (0x1847adff8, "SWAP_CLEAR_FLAG_SET", 32),
    (0x18446c6f4, "FINISH_SWAP_CLEAR_GATE", 16),
    # GATE102: after clear, ca14 must keep w24 bit0 to reach update_surface→x22.
    (0x18446ca14, "FINISH_POST_CLEAR_CA14", 16),
    (0x18446ca68, "FINISH_UPDATE_SURFACE_BL", 8),
    (0x18446d014, "FINISH_SWAP_X22_GATE", 16),  # decision before mov x2,x22
    (0x18446d0c0, "FINISH_SWAP_X22", 16),  # mov x2,x22 before bl SWAP
    (0x18446d0cc, "FINISH_SWAP_X22_RET", 8),  # after bl SWAP — force End
    (0x18445bd00, "SWAP_TRY_BEGIN_ASYNC", 16),
    (0x184464f60, "SWAP_ASYNC_CB", 16),
    (0x184464ff4, "SWAP_ASYNC_WORK", 16),
    # GATE101/102: public IOMFB swap trampolines → per-conn fps.
    # Slots (from trampoline ldr): Begin +0xc20, End +0xc28, Wait +0xc60,
    # SetLayer +0xb38. iOS 27 fills these with _virt_*; A408 needs _kern_*.
    (0x22a3911b0, "KERN_SWAP_BEGIN_THUNK", 16),  # _IOMobileFramebufferSwapBegin
    (0x22a3911cc, "KERN_SWAP_BEGIN", 16),         # _kern_SwapBegin
    (0x22a391750, "KERN_SWAP_END_THUNK", 16),     # _IOMobileFramebufferSwapEnd (+0xc28)
    (0x22a391298, "KERN_SWAP_WAIT_THUNK", 16),    # _IOMobileFramebufferSwapWait (+0xc60)
    (0x22a396be0, "VIRT_SWAP_BEGIN", 8),
    (0x22a396bcc, "VIRT_SWAP_WAIT", 8),           # was mislabeled KERN_SWAP_END_LOCAL
    (0x22a396c70, "VIRT_SWAP_END", 8),
    (0x22a3918f4, "KERN_SWAP_SET_LAYER", 64),
    (0x22a391334, "KERN_SWAP_END", 16),           # _kern_SwapEnd
    # GATE113/114: connect open path — IOServiceOpen writes into obj+0x14.
    (0x22a392954, "IOMFB_OPEN", 16),              # _IOMobileFramebufferOpen
    (0x22a395f2c, "IOMFB_GET_MAIN_DISPLAY", 16),
    (0x22a39233c, "IOMFB_OPEN_BY_NAME", 8),
    # OpenByName after ldr w8,[x26,#8]; force w8≠2 → _iomfb_get_display.
    (0x22a392428, "IOMFB_OPEN_BY_NAME_TYPE", 16),
    (0x22a392160, "IOMFB_GET_DISPLAY", 16),       # calls Open with service at info+0x14
    (0x1845f3d6c, "APPLEDISPLAY_POST_OPEN", 16),  # after ldr x23,[sp,#0x28]
    (0x18efe5450, "IOSERVICE_OPEN", 32),          # _IOServiceOpen
    (0x18efe4f48, "IOSERVICE_GET_MATCHING", 32),  # _IOServiceGetMatchingService
    # GATE111: IOConnect stubs SwapBegin/End call (sel 4 scalar / sel 5 struct).
    (0x2300d77d0, "IOCONNECT_SCALAR_STUB", 32),
    (0x2300dcca0, "IOCONNECT_STRUCT_STUB", 32),
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
    (0x1847ae314, "ALLOCATE_IOSURFACE_RETURN", 16),
    (0x184440e1c, "UPDATE_FLAGS_MASK_CHECK", 32),  # ldr [sp,#0x170]; ands #0x28000 → orr #2
    (0x184440e34, "UPDATE_FLAGS_ORR_2", 16),
    (0x184440e74, "UPDATE_FLAGS_ORR_8", 16),
    # GATE239: add_context passes this output slot through prepare_layer, then
    # consumes it at UPDATE_FLAGS_MASK_CHECK. Capture the natural before/after.
    (0x184440b0c, "PREPARE_LAYER_CONTEXT_FLAGS_CALL", 48),
    (0x184440b10, "PREPARE_LAYER_CONTEXT_FLAGS_RETURN", 48),
    # GATE240: expose the inputs and final merge that produce the flags above.
    (0x184432a70, "PREPARE_LAYER_MASK_INPUTS", 48),
    (0x18443b0a4, "PREPARE_LAYER_FLAGS_MASK", 48),
    (0x18443b0b8, "PREPARE_LAYER_FLAGS_WRITE", 48),
    # GATE241: inspect the pair merged into the local state's +0x70 record.
    (0x18443b068, "PREPARE_LAYER_STATE_FLAGS_MERGE_IN", 16),
    (0x18443b074, "PREPARE_LAYER_STATE_FLAGS_MERGE_OUT", 16),
    # GATE242: identify the later state reset that cleared a watched +0x70 word.
    (0x18444979c, "LAYER_STATE_RESET_ENTRY", 32),
    (0x184449824, "LAYER_STATE_RESET_CLEAR", 32),
    # GATE243: follow every direct update-context +0x5d0 write through the
    # render-layer update routine; the early mask at 0x18443501c removes bits
    # 22-24 and later branches may reintroduce them.
    (0x184433880, "UPDATE_FLAGS_STORE_433880", 8),
    (0x184433a70, "UPDATE_FLAGS_STORE_433A70", 8),
    (0x184433a8c, "UPDATE_FLAGS_STORE_433A8C", 8),
    (0x1844343e8, "UPDATE_FLAGS_STORE_4343E8", 8),
    (0x184434538, "UPDATE_FLAGS_STORE_434538", 8),
    (0x184434794, "UPDATE_FLAGS_STORE_434794", 8),
    (0x1844347c0, "UPDATE_FLAGS_STORE_4347C0", 8),
    (0x1844348d8, "UPDATE_FLAGS_STORE_4348D8", 8),
    (0x184434904, "UPDATE_FLAGS_STORE_434904", 8),
    (0x184434958, "UPDATE_FLAGS_STORE_434958", 8),
    (0x184434ad8, "UPDATE_FLAGS_STORE_434AD8", 8),
    (0x184434be0, "UPDATE_FLAGS_STORE_434BE0", 8),
    (0x184435020, "UPDATE_FLAGS_STORE_435020", 8),
    (0x1844350dc, "UPDATE_FLAGS_STORE_4350DC", 8),
    (0x18443537c, "UPDATE_FLAGS_STORE_43537C", 8),
    (0x1844355cc, "UPDATE_FLAGS_STORE_4355CC", 8),
    (0x1844355e8, "UPDATE_FLAGS_STORE_4355E8", 8),
    (0x1844357c0, "UPDATE_FLAGS_STORE_4357C0", 8),
    (0x184435e6c, "UPDATE_FLAGS_STORE_435E6C", 8),
    (0x184435e80, "UPDATE_FLAGS_STORE_435E80", 8),
    (0x184435eac, "UPDATE_FLAGS_STORE_435EAC", 8),
    (0x184435f00, "UPDATE_FLAGS_STORE_435F00", 8),
    # 0x18441ae24: layer walk that fills caller [sp,#0x170] via [x28,#0x60].
    # Filter at 0x184421554-574: if (x8&0x28000) then x8|=0x20000; keep bits in
    # 0x140004066020. UPDATE_FLAGS orr#2 needs sp170&0x28000 (0x184440e20-e2c);
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
    (0x18443353c, "MUT_W25_SET1", 16),        # mov w25,#1 — color-classification path
    # GATE244: capture the other live producers of w25 before it gates bit 22.
    (0x184433598, "MUT_W25_ALPHA_CLASSIFY_RESULT", 16),
    (0x18443370c, "MUT_W25_ALPHA_ACCUM_RESULT", 16),
    (0x18443374c, "MUT_W25_VCALL_ACCUM_RESULT", 16),
    (0x18443351c, "MUT_BOUNDS_CHECK", 16),    # updater tests type-31 backgroundColor RGBA here
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
    # Geometry setup: prepare_layer0 writes updater LocalState0, not Layer+0x10.
    (0x18441ae24, "PREPARE_LAYER0_ENTRY", 32), # CA::Render::Updater::prepare_layer0
    (0x18441b15c, "LAYER_GEOMETRY_FLAG22_GATE", 64), # layer flags+0x30 bit22 skips local frame copy
    (0x18441b184, "BOUNDS_STR_X28_10", 16),  # str d0,[LocalState0+0x10] from [x23,#0x50]
    (0x184432234, "BOUNDS_COPY_Q", 16),       # stp q1,q2 → [x0,#0x10]
    # GATE165: commit_transaction computes d13 then appends a timing-only
    # type-0x1f tail after the geometry records have already been queued.
    (0x184400fb8, "ENCODER_D13_SOURCE_SUB", 16),
    (0x184401e20, "ENCODER_TYPE31_STORE", 16),
    (0x184401e34, "ENCODER_D13_STORE", 16),
    # GATE168: CALayer render-copy path: entry, contents getter return, and
    # type-filter decision immediately before the direct Layer+0x68 store.
    (0x1844c0268, "COPY_RENDER_LAYER_ENTRY", 32),
    (0x1844c061c, "COPY_RENDER_CONTENTS_SOURCE_RETURN", 64),
    (0x1844c0868, "COPY_RENDER_CONTENTS_DECISION", 64),
    (0x1844c086c, "COPY_RENDER_CONTENTS_STORE", 64),
    # GATE245: direct CALayer property setter; correlate with copied sources.
    (0x1843f58d8, "CALAYER_SET_CONTENTS_ENTRY", 128),
    # GATE166: inspect the object returned to the layer commit callback and
    # the type-31 object's virtual encoder. The first site is the return from
    # 0x188018940; the second is after loading vtable+0x20 in encode_set_object.
    (0x1844c011c, "LAYER_OBJECT_FETCH_RETURN", 1024),
    (0x1844becd4, "ENCODER_OBJECT_VCALL", 1024),
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
    # P199: AppleMobileDispH17P-DCP / AppleCLCD2 mode-table property path.
    # The function at static 0x918d3f8 queries the primary object and its
    # attached +0x61b8 object through vtable +0xb38. The two return sites are
    # immediately after those indirect calls; inspect only the caller's
    # bounded stack buffers and counts. These are observations, not a mode or
    # ColorElements synthesis path.
    (0xfffffff00918d3f8, "CLCD2_MODE_TABLE_ENTRY", 16),
    (0xfffffff00918d540, "CLCD2_PRIMARY_PROPERTY_RETURN", 16),
    (0xfffffff00918d5e8, "CLCD2_ATTACHED_PROPERTY_RETURN", 16),
    (0xfffffff00918d61c, "CLCD2_MODE_TABLE_COUNTS", 16),
    # P196: AppleSynopsysMIPIDSIController::start reads the internal-panel
    # configuration from its IOService provider via vtable slot +0x118.
    # Trace only when explicitly requested. Property hooks are at the
    # instruction immediately after each indirect call so x0 is the result.
    # P222: this class inherits IOService::probe at vtable +0x2a8.  The base
    # implementation is the two-instruction BTI/RET stub at this bootkc PC;
    # use it only as a dynamic-class discriminator, since the stub itself
    # does not implement matching or prove that start will follow.
    (0xfffffff00b1fcf9c, "MIPI_AP_BASE_PROBE_ENTRY", 1000000),
    (0xfffffff0096f0e68, "MIPI_AP_START_ENTRY", 8),
    (0xfffffff0096f1bec, "MIPI_AP_GET_DISPLAY_TIMING_RETURN", 8),
    (0xfffffff0096f1c48, "MIPI_AP_GET_DISPLAY_TIMING_LPM_RETURN", 8),
    (0xfffffff0096f1ca4, "MIPI_AP_GET_CONFIG_LPM_RETURN", 8),
    (0xfffffff0096f1d20, "MIPI_AP_CONFIG_INPUTS", 8),
    (0xfffffff0096f1d24, "MIPI_AP_CONFIG_RETURN", 8),
    (0xfffffff0096f1e60, "MIPI_AP_GET_SAC_ENABLE_RETURN", 8),
    # GATE187: follow the real internal framebuffer provider's start path and
    # its DeviceTree width/height reads. These hooks only observe arguments
    # and return values; enable them explicitly for the focused mode probe.
    (0xfffffff00a0b5604, "IOMFB_AP_START_ENTRY", 16),
    (0xfffffff00a0b4de4, "IOMFB_AP_EDT_READ_ENTRY", 64),
    # GATE190: fn_0x8c8 is hotPlug_notify_gated, not a display-mode
    # configurator. +0x430 is external-display state. Keep these hooks named
    # for the hot-plug transition so natural internal-panel boots are not
    # misread as missing configuration.
    (0xfffffff00a0bcad8, "IOMFB_HOTPLUG_NOTIFY_ENTRY", 48),
    (0xfffffff00a0bcb90, "IOMFB_HOTPLUG_LATCH_SET", 32),
    (0xfffffff00a0bcbb4, "IOMFB_HOTPLUG_LATCH_CLEAR_LOOP", 32),
    (0xfffffff00a0bcc3c, "IOMFB_HOTPLUG_LATCH_CLEAR_MODE0", 32),
    # Trace hotPlug_notify_gated's two callers: pending-event replay from the
    # event-registration path and the D575 input callback. Neither is the
    # ColorElements/mode-publication path.
    (0xfffffff00a0b9e88, "IOMFB_AP_FN698_ENTRY", 32),
    (0xfffffff00a0ba0a4, "IOMFB_PENDING_HOTPLUG_REPLAY", 32),
    (0xfffffff00a0d9e4c, "IOMFB_D575_HANDLER", 32),
    (0xfffffff00a0d9f7c, "IOMFB_D575_HOTPLUG_DISPATCH", 32),
    # GATE194: the UserClient externalMethod dispatcher routes notification
    # registrations into AppleCLCD2; the selector-indexed 0xb0 rows are
    # client/event-registration records, not the D575 0x58-byte payload.
    (0xfffffff00a0e50b4, "IOMFB_USERCLIENT_EXTERNAL_METHOD", 32),
    (0xfffffff00a0e5164, "IOMFB_AP_EVENT_METHOD_CALL", 32),
    # SEL17_SHIM1: distinguish the successful IOConnect selector return from
    # actual execution of the user-client forwarding thunk and H17P shim.
    (0xfffffff00a0e4e10, "IOMFB_DIGITAL_OUT_THUNK_ENTRY", 4),
    (0xfffffff00a0e5820, "IOMFB_DIGITAL_OUT_USERCLIENT_DISPATCH", 16),
    (0xfffffff00b347aa0, "IOMFB_DIGITAL_OUT_GENERIC_DISPATCH", 16),
    (0xfffffff00a0e6c80, "IOMFB_DIGITAL_OUT_SCALAR23_STUB", 4),
    (0xfffffff00a0c94bc, "IOMFB_DIGITAL_OUT_AP_ENTRY", 4),
    (0xfffffff00a0c9510, "IOMFB_DIGITAL_OUT_AP_RPC_CALL", 4),
    (0xfffffff00a0c9514, "IOMFB_DIGITAL_OUT_AP_RPC_REPLY", 4),
    (0xfffffff00a0c951c, "IOMFB_DIGITAL_OUT_AP_RETURN", 4),
    (0xfffffff00919114c, "IOMFB_DIGITAL_OUT_SHIM_ENTRY", 4),
    (0xfffffff009191494, "IOMFB_DIGITAL_OUT_SHIM_EARLY_FAIL", 4),
    (0xfffffff009191a8c, "IOMFB_DIGITAL_OUT_SHIM_LATE_FAIL", 4),
    (0xfffffff009191e68, "IOMFB_DIGITAL_OUT_SHIM_POWER_PATH", 4),
    (0xfffffff009191e84, "IOMFB_DIGITAL_OUT_SHIM_FINALIZE", 4),
    (0xfffffff009191e98, "IOMFB_DIGITAL_OUT_SHIM_CLEANUP", 4),
    (0xfffffff009191ebc, "IOMFB_DIGITAL_OUT_SHIM_RETURN", 4),
    # GATE197: candidate constructor variants for a DCPAV proxy object. Their
    # no-hits are inconclusive; the direct start hooks below are authoritative.
    (0xfffffff009afc928, "DCPAV_PROXY_CTOR_VARIANT1_ENTRY", 4),
    (0xfffffff009afc9b8, "DCPAV_PROXY_CTOR_VARIANT1_RETURN", 4),
    (0xfffffff009afc9bc, "DCPAV_PROXY_CTOR_VARIANT2_ENTRY", 4),
    (0xfffffff009afca4c, "DCPAV_PROXY_CTOR_VARIANT2_RETURN", 4),
    # GATE198: test the shared DCPAVProxy::start path directly. The location
    # result is the OSObject returned by provider->getProperty("EPICLocation").
    (0xfffffff009b03e40, "DCPAV_PROXY_BASE_START_ENTRY", 32),
    (0xfffffff009b03e60, "DCPAV_PROXY_BASE_START_BODY_ENTRY", 32),
    (0xfffffff009b040ac, "DCPAV_PROXY_BASE_LOCATION_RESULT", 32),
    (0xfffffff009b0421c, "DCPAV_PROXY_BASE_START_RETURN", 32),
    # GATE199/GATE201 correction: 0x9b091c0 is the seven-handler
    # DCPAVPowerControllerProxy (SetRemoteDevicePower), not the video proxy.
    (0xfffffff009b091c0, "DCPAV_POWER_CONTROLLER_PROXY_START_ENTRY", 8),
    (0xfffffff009b09218, "DCPAV_POWER_CONTROLLER_PROXY_AFTER_REGISTER_CALL", 8),
    (0xfffffff009b0921c, "DCPAV_POWER_CONTROLLER_PROXY_START_RETURN", 8),
    # GATE203: the actual five-handler DCPAVVideoInterfaceProxy start path.
    (0xfffffff009b07570, "DCPAV_VIDEO_INTERFACE_PROXY_START_ENTRY", 8),
    (0xfffffff009b07624, "DCPAV_VIDEO_INTERFACE_BASE_START_CALL", 8),
    (0xfffffff009b07628, "DCPAV_VIDEO_INTERFACE_BASE_START_RETURN", 8),
    (0xfffffff009b076c8, "DCPAV_VIDEO_INTERFACE_REGISTER_SERVICE_CALL", 8),
    (0xfffffff009b076d0, "DCPAV_VIDEO_INTERFACE_PROXY_START_RETURN", 8),
    # Follow common AP requests and DCP command dispatch. By default the
    # callback filters to the captured video-interface vtable; set
    # RFT_TRACE_DCPAV_ALL_MESSAGES=1 to log all DCPAV proxy objects.
    (0xfffffff009af37e0, "DCPAV_COMMON_SEND_MESSAGE_ENTRY", 256),
    (0xfffffff009af39fc, "DCPAV_COMMON_SEND_MESSAGE_RETURN", 256),
    (0xfffffff009af5250, "DCPAV_COMMON_HANDLE_COMMAND_ENTRY", 256),
    # GATE205: sendMessage selects the 0x9af3a14 trampoline when message
    # flags bit 1 is set, and 0x9af3a64 when it is clear. GATE206 follows
    # the clear-bit helper through its endpoint call and response result.
    (0xfffffff009af3a14, "DCPAV_COMMON_SEND_ACTION_BIT1_SET_ENTRY", 32),
    (0xfffffff009af3a64, "DCPAV_COMMON_SEND_ACTION_BIT1_CLEAR_ENTRY", 32),
    (0xfffffff009b03928, "DCPAV_COMMON_CLEAR_ACTION_HELPER_ENTRY", 32),
    (0xfffffff009b039a0, "DCPAV_COMMON_CLEAR_ACTION_ENDPOINT_CALL", 32),
    (0xfffffff009b039a4, "DCPAV_COMMON_CLEAR_ACTION_ENDPOINT_RETURN", 32),
    (0xfffffff009b039d0, "DCPAV_COMMON_CLEAR_ACTION_RESPONSE_CALL", 32),
    (0xfffffff009b039d4, "DCPAV_COMMON_CLEAR_ACTION_RESPONSE_RETURN", 32),
    (0xfffffff009b03a54, "DCPAV_COMMON_CLEAR_ACTION_HELPER_RETURN", 32),
    # AFKEndpointInterface::fn_0x548 forwards through concrete endpoint slot
    # +0x578; trace its entry and normal return.
    (0xfffffff008b79e34, "DCPAV_COMMON_AFK_QUEUED_COMMAND_ENTRY", 32),
    (0xfffffff008b7a270, "DCPAV_COMMON_AFK_QUEUED_COMMAND_RETURN", 32),
    # Response parsers immediately after the controller and DP-device sends.
    (0xfffffff009b05428, "DCPAV_COMMON_CONTROLLER_RESPONSE_SITE", 32),
    (0xfffffff009b0d45c, "DCPAV_COMMON_DP_DEVICE_RESPONSE_SITE", 32),
    # GATE211: DCPMIPIControllerProxy::start in IOMIPIFamily. The superclass
    # result controls whether this proxy continues into property setup and
    # registerService. Capture that result, the registration return, and the
    # final start result. GATE212 adds its class-specific allocator and
    # constructors to distinguish no instance creation from start failure.
    (0xfffffff009b0dba0, "DCPMIPI_PROXY_ALLOC_ENTRY", 8),
    (0xfffffff009b0dbe0, "DCPMIPI_PROXY_ALLOC_RETURN", 8),
    (0xfffffff009b0dbf0, "DCPMIPI_PROXY_CTOR_A_ENTRY", 8),
    (0xfffffff009b0dc5c, "DCPMIPI_PROXY_CTOR_A_RETURN", 8),
    (0xfffffff009b0dc64, "DCPMIPI_PROXY_CTOR_B_ENTRY", 8),
    (0xfffffff009b0dcd0, "DCPMIPI_PROXY_CTOR_B_RETURN", 8),
    (0xfffffff009b0edd8, "DCPMIPI_PROXY_START_ENTRY", 8),
    (0xfffffff009b0ee0c, "DCPMIPI_PROXY_SUPER_START_RETURN", 8),
    (0xfffffff009b0efc4, "DCPMIPI_PROXY_REGISTER_SERVICE_RETURN", 8),
    (0xfffffff009b0efc8, "DCPMIPI_PROXY_START_RETURN", 8),
    # GATE213: follow the AP-side PUBLISH path from report dispatch through
    # OSSerialize decode, AFKEndpointInterface nub creation, and handoff.
    (0xfffffff008b7bc58, "AFK_PUBLISH_REPORT_TYPE", 512),
    (0xfffffff008b7bcd4, "AFK_PUBLISH_PARSE_ENTRY", 128),
    (0xfffffff008b7bf90, "AFK_PUBLISH_DECODE_RETURN", 128),
    (0xfffffff008b7bf54, "AFK_PUBLISH_NUB_READY", 128),
    (0xfffffff008b7ee34, "AFK_PUBLISH_HANDOFF_CALL", 128),
    (0xfffffff008b7ee38, "AFK_PUBLISH_HANDOFF_RETURN", 128),
    # The handoff helper's last indirect AFK nub method is where interface 13
    # stopped progressing in GATE214/215. Trace its stages to identify it.
    (0xfffffff008ba3ec0, "AFK_PUBLISH_HELPER_ENTRY", 128),
    (0xfffffff008ba3f08, "AFK_PUBLISH_HELPER_STAGE1_RETURN", 128),
    (0xfffffff008ba3f2c, "AFK_PUBLISH_HELPER_STAGE2_CALL", 128),
    (0xfffffff008ba3f30, "AFK_PUBLISH_HELPER_STAGE2_RETURN", 128),
    (0xfffffff008ba3f70, "AFK_PUBLISH_HELPER_FINAL_DISPATCH", 128),
    # GATE231: class-specific OSMetaClass::alloc implementations, resolved
    # from the live +0x68 metaclass slots in GATE230. Keep these as static
    # text breakpoints so they can be installed before IOAVFamily data is
    # readable; the early-session metaclass-slot reads were unavailable.
    (0xfffffff009d8d58c, "IOAV_META_ALLOC_ENTRY_IOAVAbstractVideoInterface", 32),
    (0xfffffff009d8bd78, "IOAV_META_ALLOC_ENTRY_IOAVSimpleVideoInterface", 32),
    (0xfffffff009dd4c44, "IOAV_META_ALLOC_ENTRY_IOAVSliceVideoInterface", 32),
    (0xfffffff009dad238, "IOAV_META_ALLOC_ENTRY_IOAVSplitDisplayVideoInterface", 32),
    (0xfffffff009d97f84, "IOAV_META_ALLOC_ENTRY_IOAVVideoInterface", 32),
    # GATE186: IOAVAbstractVideoInterface's property-publication path. The
    # shared fn_0xa8 parser is inherited by the simple, slice, split, and base
    # video interfaces. Stop only at the existing active type-1 mode result;
    # these kernel callbacks collect a bounded trace without changing values.
    # They are opt-in because kernel breakpoints should not affect other runs.
    (0xfffffff009d8d66c, "IOAV_VIDEO_ELEMENTS_ENTRY", 32),
    (0xfffffff009d8d6f8, "IOAV_VIDEO_ELEMENTS_PARSE_RESULT", 32),
    (0xfffffff009d8d720, "IOAV_COLOR_ELEMENTS_SET_CALL", 16),
    (0xfffffff009d8d72c, "IOAV_COLOR_ELEMENTS_SET_RETURN", 16),
    (0xfffffff009d8d81c, "IOAV_VIDEO_ELEMENTS_FAILURE", 32),
    # The literal key has one additional exact reference in IOAVFamily:
    # distinguish it from a coincidental #0xc9d immediate on a different page.
    (0xfffffff009df09d8, "IOAV_ABSTRACT_VIDEO_WITH_PROPERTIES_ENTRY", 8),
    (0xfffffff009df0aa8, "IOAV_ABSTRACT_VIDEO_WITH_PROPERTIES_RETURN", 8),
    (0xfffffff009dfd910, "IOAV_COLOR_ELEMENTS_PATH_C_ENTRY", 8),
    (0xfffffff009dfde44, "IOAV_COLOR_ELEMENTS_PATH_C_SET_CALL", 8),
    (0xfffffff009dfde48, "IOAV_COLOR_ELEMENTS_PATH_C_SET_RETURN", 8),
    # GATE223: trace IOAVVideoInterface's only direct call site for
    # IOAVAbstractVideoInterface::withProperties. It is conditional on the
    # intermediate video object at this+0x2f0 being absent; record the method
    # entry and the returned child pointer so a quiet withProperties hook can
    # be distinguished from the alternate, child-present path.
    (0xfffffff009da87cc, "IOAV_VIDEO_CHILD_SETUP_ENTRY", 8),
    (0xfffffff009da8914, "IOAV_VIDEO_CHILD_SETUP_RESULT", 8),
    (0xfffffff009da8a48, "IOAV_VIDEO_WITH_PROPERTIES_CALLSITE", 8),
    (0xfffffff009da8104, "IOAV_VIDEO_INTERFACE_TRACK_ENTRY", 16),
    (0xfffffff009da84a4, "IOAV_VIDEO_INTERFACE_TRACK_COMPARE", 16),
    (0xfffffff009da84b0, "IOAV_VIDEO_INTERFACE_TRACK_CHILD_CALL", 8),
    # GATE230: IOAVInterface::start is the inherited +0x2b0 IOService hook
    # used by the video-interface concrete classes. The class allocator entry
    # hooks are resolved from each runtime OSMetaClass vtable in install_kernel.
    (0xfffffff009de961c, "IOAV_INTERFACE_START_ENTRY", 32),
    # GATE227: IOMobileGraphicsFamily's IOAVHandlerInternalDisplay consumes
    # color/timing elements from an IOAVVideoInterface during plugEvent. The
    # cstring references identify this method and its three failure branches.
    (0xfffffff00a0dea80, "MGF_INTERNAL_DISPLAY_PLUG_ENTRY", 8),
    (0xfffffff00a0deb30, "MGF_INTERNAL_DISPLAY_COLOR_SELECT_RESULT", 8),
    (0xfffffff00a0dee60, "MGF_INTERNAL_DISPLAY_NO_COLOR", 8),
    (0xfffffff00a0dee7c, "MGF_INTERNAL_DISPLAY_NO_TIMING", 8),
    (0xfffffff00a0deeb0, "MGF_INTERNAL_DISPLAY_OPEN_FAILED", 8),
    # GATE229: distinguish an absent internal-display handler from a handler
    # that exists but never registers/receives IOAV service-interest events.
    # de720 allocates the 0x50-byte derived handler object; ea794 constructs
    # the base; ea860 is IOAVHandler's concrete vtable +0x78 method; eb150 is
    # the helper that looks up the IOAVVideoInterface service and installs the
    # event callback. Keep names neutral where the full C++ method is unknown.
    # GATE232: this named factory allocates and initializes the derived handler
    # directly, so it bypasses the metaclass +0x68 allocator watched above.
    (0xfffffff00a0de8ac, "MGF_INTERNAL_DISPLAY_FACTORY_ENTRY", 16),
    (0xfffffff00a0de720, "MGF_INTERNAL_DISPLAY_ALLOC_ENTRY", 16),
    (0xfffffff00a0ea794, "MGF_IOAV_HANDLER_CTOR_ENTRY", 16),
    (0xfffffff00a0ea860, "MGF_IOAV_HANDLER_BASE_METHOD78_ENTRY", 32),
    (0xfffffff00a0eb150, "MGF_IOAV_HANDLER_EVENT_SETUP_ENTRY", 32),
    # GATE228: base IOAVHandler::IOAVEvent casts the incoming IOService to
    # IOAVVideoInterface, then dispatches event 0 to plugEvent (+0x80) or
    # event 1 to unplugEvent (+0x88) on IOAVHandlerInternalDisplay.
    (0xfffffff00a0eb1d4, "MGF_IOAV_HANDLER_EVENT_ENTRY", 32),
    (0xfffffff00a0eb204, "MGF_IOAV_HANDLER_EVENT_CAST_RESULT", 32),
    (0xfffffff00a0eb234, "MGF_IOAV_HANDLER_PLUG_CALLSITE", 32),
    (0xfffffff00a0eb25c, "MGF_IOAV_HANDLER_UNPLUG_CALLSITE", 32),
    (0xfffffff00a0eb264, "MGF_IOAV_HANDLER_EVENT_DISPATCH_RETURN", 32),
    # GATE235: determine whether the generic IOAV controller ever enters its
    # provider-open/device-add path on the natural internal-display boot.
    (0xfffffff009d924d8, "IOAV_CONTROLLER_START_ENTRY", 8),
    (0xfffffff009d93800, "IOAV_CONTROLLER_OPEN_AUXILIARY_PROVIDERS", 16),
    (0xfffffff009d93b00, "IOAV_CONTROLLER_OPEN_MATCHING_PROVIDER", 32),
    (0xfffffff009d93dc0, "IOAV_CONTROLLER_OPEN_MATCHING", 32),
    (0xfffffff009d94818, "IOAV_CONTROLLER_ADD_DEVICE", 32),
    (0xfffffff009de57c4, "IOAV_CONTROLLER_ADD_DEVICE_INTERNAL", 32),
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


def _u16(process, addr):
    err = lldb.SBError()
    data = process.ReadMemory(addr, 2, err)
    return int.from_bytes(data, "little") if err.Success() and data else None


def _u8(process, addr):
    err = lldb.SBError()
    data = process.ReadMemory(addr, 1, err)
    return data[0] if err.Success() and data else None


def _u64(process, addr):
    err = lldb.SBError()
    data = process.ReadMemory(addr, 8, err)
    return int.from_bytes(data, "little") if err.Success() and data else None


def _blob_hex(process, addr, size):
    if not addr or size <= 0:
        return None
    err = lldb.SBError()
    data = process.ReadMemory(addr, size, err)
    return data.hex() if err.Success() and data else None


def _read_blob(process, addr, size):
    if not addr or size <= 0:
        return None
    err = lldb.SBError()
    data = process.ReadMemory(addr, size, err)
    return bytes(data) if err.Success() and data else None


def _parse_epic_props(data):
    """Decode the simple binary OSSerialize dictionaries sent by QEMU EPIC."""
    if not data or len(data) < 8:
        return {"error": "short"}
    if int.from_bytes(data[:4], "little") != 0xd3:
        return {"error": "signature", "prefix": data[:8].hex()}
    root = int.from_bytes(data[4:8], "little")
    if root & 0x7f000000 != 0x01000000:
        return {"error": "root-tag", "tag": hex(root)}
    count = root & 0xffffff
    if count > 256:
        return {"error": "pair-count", "count": count}
    offset = 8
    result = {}

    def take(size):
        nonlocal offset
        if size < 0 or offset + size > len(data):
            raise ValueError("truncated at 0x%x size 0x%x" % (offset, size))
        value = data[offset:offset + size]
        offset = (offset + size + 3) & ~3
        return value

    try:
        for _ in range(count):
            key_tag = int.from_bytes(data[offset:offset + 4], "little")
            offset += 4
            key_kind, key_len = key_tag & 0x7f000000, key_tag & 0xffffff
            if key_kind != 0x08000000 or not key_len or key_len > 4096:
                return {"error": "key-tag", "tag": hex(key_tag), "offset": offset - 4}
            key_bytes = take(key_len).split(b"\0", 1)[0]
            value_tag = int.from_bytes(data[offset:offset + 4], "little")
            offset += 4
            value_kind, value_len = value_tag & 0x7f000000, value_tag & 0xffffff
            if value_kind == 0x09000000:
                value = take(value_len).decode("utf-8", "replace")
            elif value_kind == 0x04000000 and value_len in (8, 16, 32, 64):
                width = value_len // 8
                value = int.from_bytes(data[offset:offset + width], "little")
                offset = (offset + width + 3) & ~3
            else:
                return {"error": "value-tag", "tag": hex(value_tag),
                        "key": key_bytes.decode("ascii", "replace")}
            result[key_bytes.decode("ascii", "replace")] = value
    except (IndexError, ValueError) as exc:
        return {"error": str(exc), "offset": offset, "length": len(data)}
    return result


def _ioav_video_class(process, obj):
    """Best-effort class label for the IOAV video-interface this pointer."""
    if not obj:
        return None, None
    vtable = _u64(process, obj)
    if not vtable:
        return vtable, None
    static_vtable = _xpac(vtable) - KERNEL_SLIDE
    classes = {
        _xpac(0xfffffff0081fb190): "IOAVAbstractVideoInterface",
        _xpac(0xfffffff0081f9850): "IOAVSimpleVideoInterface",
        _xpac(0xfffffff0081fd6b8): "IOAVVideoInterface",
        _xpac(0xfffffff008207de0): "IOAVSliceVideoInterface",
        _xpac(0xfffffff008202840): "IOAVSplitDisplayVideoInterface",
    }
    return vtable, classes.get(static_vtable, "unknown:%s" % hex(static_vtable))


def _ioav_handler_class(process, obj):
    """Best-effort class label for IOMobileGraphicsFamily's IOAV handler."""
    if not obj:
        return None, None
    vtable = _u64(process, obj)
    if not vtable:
        return vtable, None
    static_vtable = _xpac(vtable) - KERNEL_SLIDE
    classes = {
        _xpac(0xfffffff00829d098): "IOAVHandler",
        _xpac(0xfffffff008298dd0): "IOAVHandlerInternalDisplay",
    }
    return vtable, classes.get(static_vtable, "unknown:%s" % hex(static_vtable))


def _iomfb_ap_class(process, obj):
    """Best-effort class label for the internal framebuffer provider."""
    if not obj:
        return None, None
    vtable = _u64(process, obj)
    if not vtable:
        return vtable, None
    static_vtable = _xpac(vtable) - KERNEL_SLIDE
    classes = {_xpac(0xfffffff00808c778): "AppleCLCD2"}
    return vtable, classes.get(static_vtable, "unknown:%s" % hex(static_vtable))


def _mode_set_snapshot(process, mode_set, limit=8):
    """Read the begin/end pair and packed 64-bit modes from a ModeSet vector."""
    if not mode_set:
        return None
    begin = _u64(process, mode_set)
    end = _u64(process, mode_set + 8)
    if begin is None or end is None or end < begin or (end - begin) % 8:
        return {"begin": begin, "end": end, "modes": None}
    count = (end - begin) // 8
    if count > 0x1000:
        return {"begin": begin, "end": end, "count": count, "modes": "oversize"}
    return {
        "begin": begin,
        "end": end,
        "count": count,
        "modes": [_u64(process, begin + i * 8) for i in range(min(count, limit))]
        if begin else [],
    }


def _cstring(process, addr, limit=96):
    if not addr:
        return "<null>"
    err = lldb.SBError()
    data = process.ReadMemory(addr, limit, err)
    if not err.Success() or not data:
        return "<read-error>"
    return data.split(b"\0", 1)[0].decode("ascii", "replace")


def _objc_class_name(process, obj):
    """Best-effort class name for a live Objective-C object's isa pointer."""
    try:
        isa = _u64(process, obj)
        if not isa:
            return None
        mask = 0x0000fffffffffff8
        cls = isa & mask
        data = _u64(process, cls + 0x20) & mask
        if not data:
            return None
        flags = _u32(process, data)
        ro = _u64(process, data + 8) if flags & 0x80000000 else data
        if ro & 1:
            ro = _u64(process, ro & ~7)
        ro &= mask
        name = _u64(process, ro + 0x18)
        return _cstring(process, name, 128) if name else None
    except Exception:
        return None


def _progname(process):
    # libsystem_c ___progname pointer — same site as first_surface_callbacks.
    pointer = _u64(process, DSC_SLIDE + 0x1e6ef1590)
    string = _u64(process, pointer) if pointer else None
    return _cstring(process, string)


def _iomfb_name_registry(process):
    """Snapshot the 16-entry OpenByName descriptor table (0xa0-byte stride)."""
    base = 0x26fdcc6b0 + DSC_SLIDE
    entries = []
    for index in range(16):
        entry = base + index * 0xa0
        name = _u64(process, entry)
        display_type = _u32(process, entry + 0x8)
        service = _u32(process, entry + 0x14)
        if name not in (None, 0) or display_type not in (None, 0) or service not in (None, 0):
            entries.append({
                "index": index,
                "entry": hex(entry),
                "name_ref": hex(name) if name not in (None, 0) else name,
                "type": display_type,
                "service": hex(service) if service not in (None, 0) else service,
            })
    return base, entries


def _iomfb_name_registry_index(entry):
    base = 0x26fdcc6b0 + DSC_SLIDE
    if entry is None:
        return None
    delta = entry - base
    if 0 <= delta < 16 * 0xa0 and delta % 0xa0 == 0:
        return delta // 0xa0
    return None


def on_iomfb_name_count_watch(frame, watchpoint):
    """Report a newly appended _s_display_infos entry after its count store."""
    process = frame.GetThread().GetProcess()
    base = 0x26fdcc6b0 + DSC_SLIDE
    # _s_display_infos_len is the word immediately preceding the array.
    count_addr = 0x26fdcc6a8 + DSC_SLIDE
    count = _u32(process, count_addr)
    index = count - 1 if count else None
    entry = base + index * 0xa0 if index is not None and 0 <= index < 16 else None
    name = _u64(process, entry) if entry else None
    display_type = _u32(process, entry + 0x8) if entry else None
    aux = _u32(process, entry + 0x10) if entry else None
    service = _u32(process, entry + 0x14) if entry else None
    flags = (_u8(process, entry + 0x98), _u8(process, entry + 0x99)) if entry else (None, None)
    pc = frame.GetPC()
    lr = _reg(frame, "lr")
    print("iomfb_name_registry_count_write progname=%s watchpoint=%s count=%s "
          "index=%s entry=%s name_ref=%s type=%s aux=%s service=%s "
          "flags98_99=%s pc=%s pc_static=%s lr_static=%s" % (
        _progname(process),
        watchpoint.GetID(),
        count,
        index,
        hex(entry) if entry else None,
        hex(name) if name else None,
        display_type,
        hex(aux) if aux is not None else None,
        hex(service) if service is not None else None,
        flags,
        hex(pc) if pc else None,
        hex(_xpac(pc) - DSC_SLIDE) if pc else None,
        hex(_xpac(lr) - DSC_SLIDE) if lr else None,
    ), flush=True)
    return False


def on_layer_state70_watch(frame, watchpoint, internal_dict=None):
    """Report writes to the recurring layer state's low flag word."""
    global LAYER_STATE70_WATCH_HITS
    process = frame.GetThread().GetProcess()
    addr = watchpoint.GetWatchAddress()
    value = _u64(process, addr)
    pc = frame.GetPC()
    lr = _reg(frame, "lr")
    print("layer_state70_write watchpoint=%s addr=%s value=%s pc=%s "
          "pc_static=%s lr_static=%s hit=%s" % (
        watchpoint.GetID(),
        hex(addr) if addr else None,
        hex(value) if value is not None else None,
        hex(pc) if pc else None,
        hex(_xpac(pc) - DSC_SLIDE) if pc else None,
        hex(_xpac(lr) - DSC_SLIDE) if lr else None,
        LAYER_STATE70_WATCH_HITS + 1,
    ), flush=True)
    LAYER_STATE70_WATCH_HITS += 1
    if LAYER_STATE70_WATCH_HITS >= 24:
        watchpoint.SetEnabled(False)
        print("LAYER_STATE70_WATCH_DISABLED hits=%d" %
              LAYER_STATE70_WATCH_HITS, flush=True)
    return False


def on_mode_2b8_watch(frame, watchpoint, internal_dict=None):
    """Identify the first writes after the selected digital mode is stored."""
    global MODE_2B8_WATCH_HITS
    process = frame.GetThread().GetProcess()
    addr = watchpoint.GetWatchAddress()
    pc = frame.GetPC()
    sp = _reg(frame, "sp")
    # sub_184681c7c saves its authenticated caller LR at sp+0x18.
    saved_lr = _u64(process, sp + 0x18) if sp else None
    display = MODE_2B8_WATCH[0] if MODE_2B8_WATCH else None
    print("display_mode_2b8_write display=%s selected=%s current2b8=%s "
          "current2c0=%s pending6b00=%s pc=%s pc_static=%s caller_static=%s "
          "x19=%s x20=%s x22=%s x24=%s hit=%d" % (
        hex(display) if display else None,
        hex(MODE_2B8_WATCH[1]) if MODE_2B8_WATCH else None,
        hex(_u64(process, addr)) if _u64(process, addr) is not None else None,
        hex(_u64(process, display + 0x2c0)) if display and _u64(process, display + 0x2c0) is not None else None,
        hex(_u64(process, display + 0x6b00)) if display and _u64(process, display + 0x6b00) is not None else None,
        hex(pc) if pc else None,
        hex(_xpac(pc) - DSC_SLIDE) if pc else None,
        hex(_xpac(saved_lr) - DSC_SLIDE) if saved_lr else None,
        hex(_reg(frame, "x19")) if _reg(frame, "x19") is not None else None,
        hex(_reg(frame, "x20")) if _reg(frame, "x20") is not None else None,
        hex(_reg(frame, "x22")) if _reg(frame, "x22") is not None else None,
        hex(_reg(frame, "x24")) if _reg(frame, "x24") is not None else None,
        MODE_2B8_WATCH_HITS + 1,
    ), flush=True)
    MODE_2B8_WATCH_HITS += 1
    if MODE_2B8_WATCH_HITS >= 8:
        watchpoint.SetEnabled(False)
        print("MODE_2B8_WATCH_DISABLED hits=%d" % MODE_2B8_WATCH_HITS, flush=True)
    return False


def on_mode_6b00_watch(frame, watchpoint, internal_dict=None):
    """Track updates to the mode value later copied into current +0x2b8."""
    global MODE_6B00_WATCH_HITS
    process = frame.GetThread().GetProcess()
    display = MODE_6B00_WATCH[0] if MODE_6B00_WATCH else None
    pc = frame.GetPC()
    print("display_mode_6b00_write display=%s pending6b00=%s current2b8=%s "
          "target2c0=%s pc_static=%s hit=%d" % (
        hex(display) if display else None,
        hex(_u64(process, display + 0x6b00)) if display and _u64(process, display + 0x6b00) is not None else None,
        hex(_u64(process, display + 0x2b8)) if display and _u64(process, display + 0x2b8) is not None else None,
        hex(_u64(process, display + 0x2c0)) if display and _u64(process, display + 0x2c0) is not None else None,
        hex(_xpac(pc) - DSC_SLIDE) if pc else None,
        MODE_6B00_WATCH_HITS + 1,
    ), flush=True)
    MODE_6B00_WATCH_HITS += 1
    if MODE_6B00_WATCH_HITS >= 12:
        watchpoint.SetEnabled(False)
        print("MODE_6B00_WATCH_DISABLED hits=%d" % MODE_6B00_WATCH_HITS, flush=True)
    return False


def _record_bb_iomfb_open(process, obj, conn, c20):
    """Keep only backboardd Opens whose +0xc20 looks like kern SwapBegin."""
    global LAST_BB_IOMFB_CONN, LAST_BB_IOMFB, IOMFB_OPEN_PAIRS
    if not obj or not conn:
        return False
    prog = _progname(process)
    if prog != "backboardd":
        return False
    begin_rt = RUNTIME_ADDR.get("KERN_SWAP_BEGIN")
    # Accept if slot already points at kern Begin, or if FORCE will rewrite it.
    kernish = True
    if begin_rt and c20:
        bare = (c20 & 0x0000ffffffffffff)
        brt = (begin_rt & 0x0000ffffffffffff)
        # Virt Begin is a different symbol; require kern or unknown.
        virt_rt = RUNTIME_ADDR.get("VIRT_SWAP_BEGIN")
        if virt_rt and bare == (virt_rt & 0x0000ffffffffffff):
            kernish = False
        elif bare == brt:
            kernish = True
    if not kernish:
        return False
    LAST_BB_IOMFB_CONN = conn
    LAST_BB_IOMFB = obj
    IOMFB_OPEN_PAIRS.append((obj, conn))
    if len(IOMFB_OPEN_PAIRS) > 32:
        del IOMFB_OPEN_PAIRS[:-32]
    return True


def on_break(frame, bp_loc, _dict):
    global DISPLAY_WH_OBJ, LAST_MUT, LAST_ALLOC_SURFACE, LAST_IOMFB
    global MIPI_AP_PROBE_MATCHES
    global AFK_PUBLISH_THREAD_CONTEXT
    global AFK_PUBLISH_IFACE_BY_NAME, AFK_PUBLISH_NUB_CONTEXT
    global VIDEO_INTERFACE_VTABLE_STATIC_LOW48
    global LAST_GOOD_CONN, LAST_IOMFB_CONN, LAST_CONNS, LAST_MATCHED_SVC
    global LAST_TASK_PORT, OPEN_INJECT_STATE, IOMFB_OPEN_PAIRS
    global LAST_BB_IOMFB_CONN, LAST_BB_IOMFB
    thread = frame.GetThread()
    process = thread.GetProcess()
    target = process.GetTarget()
    bp_id = bp_loc.GetBreakpoint().GetID()
    cfg = CONFIG.get(bp_id)
    if cfg is None:
        # Deferred/narrow installers and composed probes can leave LLDB
        # breakpoint IDs without metadata in this module (for example after
        # another callback module has dynamically armed a site).  An unknown
        # ID must not raise out of an LLDB Python command: that tears down the
        # debugger session while leaving the guest at the breakpoint.  Log
        # the first occurrence and auto-continue; this site is unclassified,
        # so it contributes no semantic trace row.
        if bp_id not in ORPHAN_BREAKPOINT_IDS:
            ORPHAN_BREAKPOINT_IDS.add(bp_id)
            print("RENDER_GATE_ORPHAN_BREAKPOINT " + json.dumps({
                "breakpoint_id": bp_id,
                "pc": "0x%x" % frame.GetPC(),
                "reason": "no CONFIG metadata",
                "read_only": True,
            }, sort_keys=True), flush=True)
        return False
    label = cfg["label"]
    HITS[label] = HITS.get(label, 0) + 1
    hit = HITS[label]
    x0, x1, x2, x3, w0, w8, lr = (_reg(frame, n) for n in (
        "x0", "x1", "x2", "x3", "w0", "w8", "lr"))
    x19 = _reg(frame, "x19")
    x20 = _reg(frame, "x20")
    x23 = _reg(frame, "x23")
    x24 = _reg(frame, "x24")
    if label == "MIPI_AP_BASE_PROBE_ENTRY":
        # This is the generic inherited IOService::probe target, so it can be
        # reached for unrelated services.  Read the receiver's dynamic vtable
        # and print detailed rows only for AppleSynopsysMIPIDSIController.
        # The function is a BTI/RET stub; a match proves a probe dispatch to
        # this class, not successful matching or a later start call.
        receiver = x0
        vtable = _u64(process, receiver) if receiver else None
        vtable_static = (_canonical_kernel_address(vtable) - KERNEL_SLIDE
                         if vtable else None)
        expected_vtable = 0xfffffff008155c48
        if vtable_static == expected_vtable:
            MIPI_AP_PROBE_MATCHES += 1
            print("mipi_ap_base_probe_match receiver=%s provider=%s score_ptr=%s "
                  "vtable=%s vtable_static=%s expected_vtable_static=%s "
                  "lr_static=%s tpidr_el1=%s thread_id=%s count=%d" % (
                hex(receiver) if receiver else None,
                hex(x1) if x1 else None,
                hex(x2) if x2 else None,
                hex(vtable) if vtable else None,
                hex(vtable_static) if vtable_static else None,
                hex(expected_vtable),
                hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
                hex(_reg(frame, "tpidr_el1"))
                if _reg(frame, "tpidr_el1") else None,
                thread.GetThreadID(), MIPI_AP_PROBE_MATCHES,
            ), flush=True)
        elif hit <= 10 or hit % 10000 == 0:
            print("mipi_ap_base_probe_sample calls=%d matches=%d "
                  "receiver=%s vtable_static=%s" % (
                hit, MIPI_AP_PROBE_MATCHES,
                hex(receiver) if receiver else None,
                hex(vtable_static) if vtable_static else None,
            ), flush=True)
        return False
    if label in ("IOMFB_DIGITAL_OUT_USERCLIENT_DISPATCH",
                 "IOMFB_DIGITAL_OUT_GENERIC_DISPATCH") and _reg(frame, "w1") != 0x17:
        HITS[label] -= 1
        return False
    if label == "MIPI_AP_START_ENTRY":
        controller = x0
        provider = x1
        provider_vtable = _u64(process, provider) if provider else None
        print("mipi_ap_start controller=%s controller_vtable=%s provider=%s "
              "provider_vtable=%s provider_vtable_static=%s provider_head=%s "
              "lr_static=%s" % (
            hex(controller) if controller else None,
            hex(_u64(process, controller))
            if controller and _u64(process, controller) else None,
            hex(provider) if provider else None,
            hex(provider_vtable) if provider_vtable else None,
            hex(_xpac(provider_vtable) - KERNEL_SLIDE)
            if provider_vtable else None,
            _blob_hex(process, provider, 0x30) if provider else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "CLCD2_MODE_TABLE_ENTRY":
        obj = x0
        attached = _u64(process, obj + 0x61b8) if obj else None
        vtable = _u64(process, obj) if obj else None
        attached_vtable = _u64(process, attached) if attached else None
        print("clcd2_mode_table_entry object=%s vtable=%s attached=%s "
              "attached_vtable=%s lr_static=%s" % (
            hex(obj) if obj else None,
            hex(vtable) if vtable else None,
            hex(attached) if attached else None,
            hex(attached_vtable) if attached_vtable else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label in ("CLCD2_PRIMARY_PROPERTY_RETURN",
                 "CLCD2_ATTACHED_PROPERTY_RETURN",
                 "CLCD2_MODE_TABLE_COUNTS"):
        sp = _reg(frame, "sp")
        base = (0x6a0 if label == "CLCD2_PRIMARY_PROPERTY_RETURN" else
                0xd0 if label == "CLCD2_ATTACHED_PROPERTY_RETURN" else None)
        count_off = (0xc8 if label == "CLCD2_PRIMARY_PROPERTY_RETURN" else
                     0xc0 if label == "CLCD2_ATTACHED_PROPERTY_RETURN" else None)
        if label == "CLCD2_MODE_TABLE_COUNTS":
            counts = {
                "primary_color": _u32(process, sp + 0xc8) if sp else None,
                "primary_timing": _u32(process, sp + 0xcc) if sp else None,
                "attached_color": _u32(process, sp + 0xc0) if sp else None,
                "attached_timing": _u32(process, sp + 0xc4) if sp else None,
            }
            print("clcd2_mode_table_counts sp=%s counts=%s result=%s "
                  "attached=%s" % (
                hex(sp) if sp else None, json.dumps(counts, sort_keys=True),
                hex(x0) if x0 is not None else None,
                hex(_u64(process, _reg(frame, "x27") + 0x61b8))
                if _reg(frame, "x27") and _u64(process, _reg(frame, "x27") + 0x61b8)
                else None,
            ), flush=True)
        else:
            obj = _reg(frame, "x27")
            if label == "CLCD2_ATTACHED_PROPERTY_RETURN":
                obj = _u64(process, obj + 0x61b8) if obj else None
            vtable = _u64(process, obj) if obj else None
            count_a = _u32(process, sp + count_off) if sp else None
            count_b_off = count_off + 4 if count_off is not None else None
            count_b = _u32(process, sp + count_b_off) if sp else None
            data = _read_blob(process, sp + base, 0x120) if sp and base is not None else None
            keybuf_off = 0x888 if label == "CLCD2_PRIMARY_PROPERTY_RETURN" else 0x2b8
            keybuf = _read_blob(process, sp + keybuf_off, 0x100) if sp else None
            print("clcd2_property_return label=%s object=%s vtable=%s "
                  "result=%s count_a=%s count_b=%s keybuf_hex=%s "
                  "records_hex=%s lr_static=%s" % (
                label, hex(obj) if obj else None,
                hex(vtable) if vtable else None,
                hex(x0) if x0 is not None else None,
                count_a, count_b, keybuf.hex() if keybuf else None,
                data.hex() if data else None,
                hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
            ), flush=True)
    if label in (
            "MIPI_AP_GET_DISPLAY_TIMING_RETURN",
            "MIPI_AP_GET_DISPLAY_TIMING_LPM_RETURN",
            "MIPI_AP_GET_CONFIG_LPM_RETURN",
            "MIPI_AP_GET_SAC_ENABLE_RETURN"):
        keys = {
            "MIPI_AP_GET_DISPLAY_TIMING_RETURN": "display-timing",
            "MIPI_AP_GET_DISPLAY_TIMING_LPM_RETURN": "display-timing-lpm",
            "MIPI_AP_GET_CONFIG_LPM_RETURN": "mipi-config-lpm",
            "MIPI_AP_GET_SAC_ENABLE_RETURN": "sac-enable",
        }
        controller = x19
        provider = x21
        value_vtable = _u64(process, x0) if x0 else None
        print("mipi_ap_property_result key=%s controller=%s provider=%s "
              "value=%s value_vtable=%s value_vtable_static=%s value_head=%s "
              "controller_slot_4b0=%s controller_slot_4b8=%s "
              "controller_slot_4c0=%s lr_static=%s" % (
            keys[label],
            hex(controller) if controller else None,
            hex(provider) if provider else None,
            hex(x0) if x0 else None,
            hex(value_vtable) if value_vtable else None,
            hex(_xpac(value_vtable) - KERNEL_SLIDE)
            if value_vtable else None,
            _blob_hex(process, x0, 0x30) if x0 else None,
            hex(_u64(process, controller + 0x4b0))
            if controller and _u64(process, controller + 0x4b0) else None,
            hex(_u64(process, controller + 0x4b8))
            if controller and _u64(process, controller + 0x4b8) else None,
            hex(_u64(process, controller + 0x4c0))
            if controller and _u64(process, controller + 0x4c0) else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "MIPI_AP_CONFIG_INPUTS":
        local_timing = x1
        config_object = x3
        lpm_object = x4
        print("mipi_ap_config_inputs controller=%s local_timing=%s "
              "local_timing_90=%s mode_output=%s config=%s config_head=%s "
              "lpm_config=%s lpm_config_head=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            hex(local_timing) if local_timing else None,
            _blob_hex(process, local_timing, 0x90) if local_timing else None,
            hex(x2) if x2 else None,
            hex(config_object) if config_object else None,
            _blob_hex(process, config_object, 0x30) if config_object else None,
            hex(lpm_object) if lpm_object else None,
            _blob_hex(process, lpm_object, 0x30) if lpm_object else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "MIPI_AP_CONFIG_RETURN":
        print("mipi_ap_config_return result=%s result_w0=%s "
              "controller=%s lr_static=%s" % (
            hex(x0) if x0 is not None else None,
            hex(w0) if w0 is not None else None,
            hex(x19) if x19 else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "AFK_PUBLISH_REPORT_TYPE":
        message = x20
        packet = _u64(process, message + 8) if message else None
        report_type = _u8(process, packet + 8) if packet else None
        category = _u8(process, packet + 9) if packet else None
        outer_header = _reg(frame, "x21")
        iface = _u16(process, outer_header + 2) if outer_header else None
        packet_len = _u64(process, message + 0x10) if message else None
        if iface == 12 and not (report_type == 0x11 and category == 0):
            print("afk_report iface=%s report_type=%s category=%s "
                  "packet=%s bytes=%s" % (
                iface,
                hex(report_type) if report_type is not None else None,
                hex(category) if category is not None else None,
                hex(packet) if packet else None,
                packet_len,
            ), flush=True)
        if report_type == 0x11 and category == 0:
            body = _read_blob(process, packet + 0x10, 32) if packet else None
            name = (body.split(b"\0", 1)[0].decode("ascii", "replace")
                    if body else None)
            print("afk_publish_report iface=%s packet=%s bytes=%s name=%r" % (
                iface, hex(packet) if packet else None, packet_len, name),
                flush=True)
    if label == "AFK_PUBLISH_PARSE_ENTRY":
        body_len = x1 if x1 is not None else 0
        iface = _reg(frame, "x4")
        name_bytes = _read_blob(process, x0, min(32, body_len)) if x0 else None
        name = (name_bytes.split(b"\0", 1)[0].decode("ascii", "replace")
                if name_bytes else None)
        AFK_PUBLISH_THREAD_CONTEXT[thread.GetThreadID()] = (iface, name)
        if name is not None:
            AFK_PUBLISH_IFACE_BY_NAME[name] = iface
        props_len = max(0, body_len - 32)
        props = _read_blob(process, x0 + 32, props_len) if x0 and props_len else None
        print("afk_publish_parse iface=%s body=%s len=%s name=%r props_len=%s "
              "props=%s props_hex=%s" % (
            iface, hex(x0) if x0 else None, body_len, name, props_len,
            json.dumps(_parse_epic_props(props), sort_keys=True)
            if props else None,
            props.hex() if props else None), flush=True)
    if label == "AFK_PUBLISH_DECODE_RETURN":
        raw = _read_blob(process, x24, x20) if x24 and x20 else None
        iface, name = AFK_PUBLISH_THREAD_CONTEXT.get(
            thread.GetThreadID(), (None, None))
        print("afk_publish_decode iface=%s name=%r dictionary=%s props_len=%s props=%s" % (
            iface, name, hex(x0) if x0 else None, x20,
            json.dumps(_parse_epic_props(raw), sort_keys=True) if raw else None),
            flush=True)
    if label == "AFK_PUBLISH_NUB_READY":
        name_bytes = _read_blob(process, _reg(frame, "sp"), 32)
        name = (name_bytes.split(b"\0", 1)[0].decode("ascii", "replace")
                if name_bytes else None)
        iface = AFK_PUBLISH_IFACE_BY_NAME.get(name)
        if x23:
            AFK_PUBLISH_NUB_CONTEXT[x23] = (iface, name)
        print("afk_publish_nub iface=%s nub=%s name=%r" % (
            iface, hex(x23) if x23 else None, name), flush=True)
    if label == "AFK_PUBLISH_HANDOFF_CALL":
        iface, name = AFK_PUBLISH_NUB_CONTEXT.get(x0, (None, None))
        print("afk_publish_handoff_call iface=%s name=%r nub=%s owner=%s lr_static=%s" % (
            iface, name, hex(x0) if x0 else None, hex(x1) if x1 else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None), flush=True)
    if label == "AFK_PUBLISH_HANDOFF_RETURN":
        iface, name = AFK_PUBLISH_NUB_CONTEXT.pop(x0, (None, None))
        print("afk_publish_handoff_return iface=%s name=%r result=%s owner=%s" % (
            iface, name, hex(x0) if x0 is not None else None,
            hex(x19) if x19 else None), flush=True)
    if label == "AFK_PUBLISH_HELPER_ENTRY":
        print("afk_publish_helper_entry iface=%s name=%r nub=%s owner=%s" % (
            *AFK_PUBLISH_NUB_CONTEXT.get(x0, (None, None)),
            hex(x0) if x0 else None, hex(x1) if x1 else None), flush=True)
    if label in ("AFK_PUBLISH_HELPER_STAGE1_RETURN",
                 "AFK_PUBLISH_HELPER_STAGE2_RETURN"):
        iface, name = AFK_PUBLISH_NUB_CONTEXT.get(x19, (None, None))
        print("afk_publish_helper_stage label=%s iface=%s name=%r nub=%s "
              "result=%s" % (
            label, iface, name, hex(x19) if x19 else None,
            hex(x0) if x0 is not None else None), flush=True)
    if label == "AFK_PUBLISH_HELPER_STAGE2_CALL":
        iface, name = AFK_PUBLISH_NUB_CONTEXT.get(x19, (None, None))
        target = _xpac(_reg(frame, "x8"))
        target_static = target - KERNEL_SLIDE if target else None
        print("afk_publish_helper_stage2_call iface=%s name=%r nub=%s "
              "target=%s target_static=%s owner=%s" % (
            iface, name, hex(x19) if x19 else None,
            hex(target) if target else None,
            hex(target_static) if target_static else None,
            hex(x20) if x20 else None), flush=True)
    if label == "AFK_PUBLISH_HELPER_FINAL_DISPATCH":
        iface, name = AFK_PUBLISH_NUB_CONTEXT.get(x0, (None, None))
        target = _xpac(_reg(frame, "x16"))
        target_static = target - KERNEL_SLIDE if target else None
        print("afk_publish_final_dispatch iface=%s name=%r nub=%s "
              "target=%s target_static=%s slot=%s" % (
            iface, name, hex(x0) if x0 else None,
            hex(target) if target else None,
            hex(target_static) if target_static else None,
            hex(x1) if x1 else None), flush=True)
    if label.startswith("DCPMIPI_PROXY_"):
        x0_is_instance = (
            label.endswith("_ENTRY") or
            label in ("DCPMIPI_PROXY_ALLOC_RETURN",
                      "DCPMIPI_PROXY_CTOR_A_RETURN",
                      "DCPMIPI_PROXY_CTOR_B_RETURN"))
        proxy = x0 if x0_is_instance else x19
        vtable = _u64(process, proxy) if proxy else None
        bare_vtable = _xpac(vtable)
        static_vtable = (bare_vtable - KERNEL_SLIDE
                         if bare_vtable is not None else None)
        print("dcpmipi_proxy_start label=%s proxy=%s provider=%s raw_x0=%s "
              "super_result_x20=%s vtable_static=%s lr_static=%s" % (
            label,
            hex(proxy) if proxy else None,
              hex(x1) if label == "DCPMIPI_PROXY_START_ENTRY" and x1 else None,
            hex(x0) if x0 is not None else None,
            hex(x20) if x20 is not None else None,
            hex(static_vtable) if static_vtable is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label.startswith("DCPAV_PROXY_CTOR_"):
        vtable = _u64(process, x0) if x0 else None
        bare_vtable = _xpac(vtable)
        static_vtable = (bare_vtable - KERNEL_SLIDE
                         if bare_vtable is not None else None)
        phase = "entry" if label.endswith("_ENTRY") else "return"
        print("dcpav_proxy_ctor phase=%s label=%s this=%s vtable=%s "
              "vtable_static=%s "
              "subobject_88=%s subobject_610=%s subobject_618=%s lr_static=%s" % (
            phase,
            label,
            hex(x0) if x0 else None,
            hex(vtable) if vtable else None,
            hex(static_vtable) if static_vtable is not None else None,
            hex(_u64(process, x0 + 0x88))
            if x0 and _u64(process, x0 + 0x88) else None,
            hex(_u64(process, x0 + 0x610))
            if x0 and _u64(process, x0 + 0x610) else None,
            hex(_u64(process, x0 + 0x618))
            if x0 and _u64(process, x0 + 0x618) else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_PROXY_BASE_START_ENTRY":
        vtable = _u64(process, x0) if x0 else None
        bare_vtable = _xpac(vtable)
        static_vtable = (bare_vtable - KERNEL_SLIDE
                         if bare_vtable is not None else None)
        print("dcpav_proxy_base_start_entry proxy=%s provider=%s table=%s "
              "count=%s vtable=%s vtable_static=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            hex(x2) if x2 else None,
            x3,
            hex(vtable) if vtable else None,
            hex(static_vtable) if static_vtable is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_PROXY_BASE_START_BODY_ENTRY":
        print("dcpav_proxy_base_start_body proxy=%s provider=%s table=%s "
              "count=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            hex(x2) if x2 else None,
            x3,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_PROXY_BASE_LOCATION_RESULT":
        provider = _reg(frame, "x19")
        proxy = _reg(frame, "x20")
        property_vtable = _u64(process, x0) if x0 else None
        property_vtable_static = (_xpac(property_vtable) - KERNEL_SLIDE
                                  if property_vtable else None)
        print("dcpav_proxy_base_epiclocation proxy=%s provider=%s value=%s "
              "value_vtable=%s value_vtable_static=%s lr_static=%s" % (
            hex(proxy) if proxy else None,
            hex(provider) if provider else None,
            hex(x0) if x0 else None,
            hex(property_vtable) if property_vtable else None,
            hex(property_vtable_static) if property_vtable_static else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_PROXY_BASE_START_RETURN":
        print("dcpav_proxy_base_start_return result=%s proxy=%s provider=%s "
              "lr_static=%s" % (
            hex(x0) if x0 is not None else None,
            hex(_reg(frame, "x20")) if _reg(frame, "x20") else None,
            hex(_reg(frame, "x19")) if _reg(frame, "x19") else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_POWER_CONTROLLER_PROXY_START_ENTRY":
        vtable = _u64(process, x0) if x0 else None
        bare_vtable = _xpac(vtable)
        static_vtable = (bare_vtable - KERNEL_SLIDE
                         if bare_vtable is not None else None)
        print("dcpav_power_controller_proxy_start_entry proxy=%s provider=%s "
              "vtable=%s vtable_static=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            hex(vtable) if vtable else None,
            hex(static_vtable) if static_vtable is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_POWER_CONTROLLER_PROXY_AFTER_REGISTER_CALL":
        print("dcpav_power_controller_proxy_after_register_call raw_x0=%s proxy=%s "
              "base_start_result_x20=%s lr_static=%s" % (
            hex(x0) if x0 is not None else None,
            hex(_reg(frame, "x19")) if _reg(frame, "x19") else None,
            hex(_reg(frame, "x20")) if _reg(frame, "x20") is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_POWER_CONTROLLER_PROXY_START_RETURN":
        print("dcpav_power_controller_proxy_start_return result=%s proxy=%s "
              "lr_static=%s" % (
            hex(x0) if x0 is not None else None,
            hex(_reg(frame, "x19")) if _reg(frame, "x19") else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_VIDEO_INTERFACE_PROXY_START_ENTRY":
        vtable = _u64(process, x0) if x0 else None
        bare_vtable = _xpac(vtable)
        static_vtable = (bare_vtable - KERNEL_SLIDE
                         if bare_vtable is not None else None)
        VIDEO_INTERFACE_VTABLE_STATIC_LOW48 = static_vtable
        print("dcpav_video_interface_proxy_start_entry proxy=%s provider=%s "
              "vtable=%s vtable_static=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            hex(vtable) if vtable else None,
            hex(static_vtable) if static_vtable is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_VIDEO_INTERFACE_BASE_START_CALL":
        print("dcpav_video_interface_base_start_call proxy=%s provider=%s "
              "table=%s count=%s vtable_static=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            hex(x2) if x2 else None,
            x3,
            hex(VIDEO_INTERFACE_VTABLE_STATIC_LOW48)
            if VIDEO_INTERFACE_VTABLE_STATIC_LOW48 is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_VIDEO_INTERFACE_BASE_START_RETURN":
        print("dcpav_video_interface_base_start_return result=%s proxy=%s "
              "provider=%s lr_static=%s" % (
            hex(x0) if x0 is not None else None,
            hex(x19) if x19 else None,
            hex(x20) if x20 else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_VIDEO_INTERFACE_REGISTER_SERVICE_CALL":
        print("dcpav_video_interface_register_service_call proxy=%s "
              "vtable_static=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            hex(VIDEO_INTERFACE_VTABLE_STATIC_LOW48)
            if VIDEO_INTERFACE_VTABLE_STATIC_LOW48 is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_VIDEO_INTERFACE_PROXY_START_RETURN":
        print("dcpav_video_interface_proxy_start_return result=%s proxy=%s "
              "lr_static=%s" % (
            hex(x0) if x0 is not None else None,
            hex(x19) if x19 else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label in (
            "DCPAV_COMMON_SEND_MESSAGE_ENTRY",
            "DCPAV_COMMON_SEND_MESSAGE_RETURN",
            "DCPAV_COMMON_HANDLE_COMMAND_ENTRY"):
        obj = x19 if label == "DCPAV_COMMON_SEND_MESSAGE_RETURN" else x0
        vtable = _u64(process, obj) if obj else None
        vtable_static = (_xpac(vtable) - KERNEL_SLIDE
                         if vtable is not None else None)
        import os
        trace_all = os.environ.get("RFT_TRACE_DCPAV_ALL_MESSAGES") == "1"
        if (not trace_all and
                (VIDEO_INTERFACE_VTABLE_STATIC_LOW48 is None or
                 vtable_static != VIDEO_INTERFACE_VTABLE_STATIC_LOW48)):
            return False
        msg = x1 if label != "DCPAV_COMMON_SEND_MESSAGE_RETURN" else None
        msg_id = _u32(process, msg + 4) if msg else None
        msg_len = _u32(process, msg + 8) if msg else None
        magic = _u32(process, msg + 12) if msg else None
        msg_flags = _u16(process, msg + 2) if msg else None
        # DCPAVIPC has a 0x40-byte header; payload bytes begin at +0x40.
        payload_hex = (_blob_hex(process, msg + 0x40, min(msg_len, 0x60))
                       if msg and msg_len is not None else None)
        if label == "DCPAV_COMMON_SEND_MESSAGE_RETURN":
            print("dcpav_common_send_message_return proxy=%s result=%s "
                  "vtable_static=%s lr_static=%s" % (
                hex(obj) if obj else None,
                hex(x0) if x0 is not None else None,
                hex(vtable_static),
                hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
            ), flush=True)
        else:
            print("dcpav_common_%s proxy=%s message=%s command_index=%s "
                  "payload_len=%s flags=%s magic=%s arg2=%s payload_hex=%s "
                  "vtable_static=%s lr_static=%s" % (
                "send_message_entry" if label == "DCPAV_COMMON_SEND_MESSAGE_ENTRY"
                else "handle_command_entry",
                hex(obj) if obj else None,
                hex(msg) if msg else None,
                hex(msg_id) if msg_id is not None else None,
                hex(msg_len) if msg_len is not None else None,
                hex(msg_flags) if msg_flags is not None else None,
                hex(magic) if magic is not None else None,
                hex(x2) if x2 is not None else None,
                payload_hex,
                hex(vtable_static),
                hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
            ), flush=True)
    if label in ("DCPAV_COMMON_SEND_ACTION_BIT1_SET_ENTRY",
                 "DCPAV_COMMON_SEND_ACTION_BIT1_CLEAR_ENTRY"):
        action_vtable = _u64(process, x0) if x0 else None
        proxy = _u64(process, x0 + 0x20) if x0 else None
        proxy_vtable = _u64(process, proxy) if proxy else None
        message_len = _u64(process, x0 + 0x28) if x0 else None
        message = _u64(process, x0 + 0x30) if x0 else None
        message_flags = _u32(process, x0 + 0x38) if x0 else None
        payload_len = _u32(process, message + 8) if message else None
        payload_hex = (_blob_hex(process, message + 0x40,
                                 min(payload_len, 0x60))
                       if message and payload_len is not None else None)
        print("dcpav_common_send_action label=%s action=%s action_vtable=%s "
              "proxy=%s proxy_vtable_static=%s message=%s total_len=%s "
              "payload_len=%s payload_hex=%s flags=%s regs_x1=%s regs_x2=%s "
              "regs_x3=%s regs_x4=%s regs_x5=%s fields_20_40=%s "
              "lr_static=%s" % (
            label,
            hex(x0) if x0 else None,
            hex(_xpac(action_vtable) - KERNEL_SLIDE)
            if action_vtable else None,
            hex(proxy) if proxy else None,
            hex(_xpac(proxy_vtable) - KERNEL_SLIDE)
            if proxy_vtable else None,
            hex(message) if message else None,
            hex(message_len) if message_len is not None else None,
            hex(payload_len) if payload_len is not None else None,
            payload_hex,
            hex(message_flags) if message_flags is not None else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_reg(frame, "x4")) if _reg(frame, "x4") is not None else None,
            hex(_reg(frame, "x5")) if _reg(frame, "x5") is not None else None,
            _blob_hex(process, x0 + 0x20, 0x20) if x0 else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_COMMON_CLEAR_ACTION_HELPER_ENTRY":
        proxy = x0
        endpoint = _u64(process, proxy + 0xb8) if proxy else None
        endpoint_vtable = _u64(process, endpoint) if endpoint else None
        message = _reg(frame, "x4")
        message_total_len = x3
        payload_len = _u32(process, message + 8) if message else None
        message_hex = (_blob_hex(process, message + 0x40,
                                 min(payload_len, 0x60))
                       if message and payload_len is not None else None)
        print("dcpav_clear_action_helper_entry proxy=%s command_type=%s "
              "message=%s message_total_len=%s payload_len=%s "
              "message_flags=%s endpoint=%s endpoint_vtable_static=%s "
              "payload_hex=%s flags_word=%s message_ref=%s lr_static=%s" % (
            hex(proxy) if proxy else None,
            hex(x1) if x1 is not None else None,
            hex(message) if message else None,
            hex(message_total_len) if message_total_len is not None else None,
            hex(payload_len) if payload_len is not None else None,
            hex(_reg(frame, "x6")) if _reg(frame, "x6") is not None else None,
            hex(endpoint) if endpoint else None,
            hex(_xpac(endpoint_vtable) - KERNEL_SLIDE)
            if endpoint_vtable else None,
            message_hex,
            hex(_reg(frame, "x7")) if _reg(frame, "x7") is not None else None,
            hex(_reg(frame, "x5"))
            if _reg(frame, "x5") is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_COMMON_AFK_QUEUED_COMMAND_ENTRY":
        endpoint = x0
        endpoint_vtable = _u64(process, endpoint) if endpoint else None
        options = x2
        fields = ([hex(_u64(process, options + off))
                   if _u64(process, options + off) is not None else None
                   for off in range(0, 0x30, 8)] if options else None)
        print("dcpav_afk_queued_command_entry endpoint=%s command_type=%s "
              "options=%s options_fields=%s options_hex=%s flag=%s "
              "action=%s output=%s length=%s endpoint_vtable_static=%s "
              "lr_static=%s" % (
            hex(endpoint) if endpoint else None,
            hex(x1) if x1 is not None else None,
            hex(options) if options else None,
            fields,
            _blob_hex(process, options, 0x30) if options else None,
            hex(x3) if x3 is not None else None,
            hex(_reg(frame, "x4"))
            if _reg(frame, "x4") is not None else None,
            hex(_reg(frame, "x6"))
            if _reg(frame, "x6") is not None else None,
            hex(_reg(frame, "x7"))
            if _reg(frame, "x7") is not None else None,
            hex(_xpac(endpoint_vtable) - KERNEL_SLIDE)
            if endpoint_vtable else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "DCPAV_COMMON_AFK_QUEUED_COMMAND_RETURN":
        print("dcpav_afk_queued_command_return return_x0=%s preserved_x19=%s "
              "lr_static=%s" % (
            hex(x0) if x0 is not None else None,
            hex(x19) if x19 is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label in ("DCPAV_COMMON_CONTROLLER_RESPONSE_SITE",
                 "DCPAV_COMMON_DP_DEVICE_RESPONSE_SITE"):
        sp = _reg(frame, "sp")
        out_a = _reg(frame, "x22")
        out_b = _reg(frame, "x21")
        print("dcpav_response_site label=%s send_result=%s stack_preparse=%s "
              "out_a_ptr=%s out_b_ptr=%s "
              "x19=%s x20=%s x23=%s lr_static=%s" % (
            label,
            hex(x0) if x0 is not None else None,
            _blob_hex(process, sp, 0x80) if sp else None,
            hex(out_a) if out_a else None,
            hex(out_b) if out_b else None,
            hex(x19) if x19 else None,
            hex(x20) if x20 else None,
            hex(x23) if x23 else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label in (
            "DCPAV_COMMON_CLEAR_ACTION_ENDPOINT_CALL",
            "DCPAV_COMMON_CLEAR_ACTION_ENDPOINT_RETURN",
            "DCPAV_COMMON_CLEAR_ACTION_RESPONSE_CALL",
            "DCPAV_COMMON_CLEAR_ACTION_RESPONSE_RETURN",
            "DCPAV_COMMON_CLEAR_ACTION_HELPER_RETURN"):
        proxy = x19 - 0xc8 if x19 else None
        endpoint = _u64(process, proxy + 0xb8) if proxy else None
        endpoint_vtable = _u64(process, endpoint) if endpoint else None
        response_ref = _reg(frame, "x1")
        response_value = _u64(process, response_ref) if response_ref else None
        target = (_reg(frame, "x9")
                  if label == "DCPAV_COMMON_CLEAR_ACTION_ENDPOINT_CALL"
                  else _reg(frame, "x8")
                  if label == "DCPAV_COMMON_CLEAR_ACTION_RESPONSE_CALL"
                  else None)
        print("dcpav_clear_action_result label=%s x0=%s x1=%s x2=%s "
              "x3=%s x4=%s x5=%s x6=%s response_ref=%s response_value=%s "
              "endpoint=%s endpoint_vtable_static=%s call_target_static=%s "
              "stack_8_28=%s lr_static=%s" % (
            label,
            hex(x0) if x0 is not None else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_reg(frame, "x4")) if _reg(frame, "x4") is not None else None,
            hex(_reg(frame, "x5")) if _reg(frame, "x5") is not None else None,
            hex(_reg(frame, "x6")) if _reg(frame, "x6") is not None else None,
            hex(response_ref) if response_ref else None,
            hex(response_value) if response_value is not None else None,
            hex(endpoint) if endpoint else None,
            hex(_xpac(endpoint_vtable) - KERNEL_SLIDE)
            if endpoint_vtable else None,
            hex(_xpac(target) - KERNEL_SLIDE) if target else None,
            _blob_hex(process, _reg(frame, "sp") + 8, 0x20)
            if _reg(frame, "sp") else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
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
    if label in ("LAYER_OBJECT_FETCH_RETURN", "ENCODER_OBJECT_VCALL"):
        obj = x0 if label == "LAYER_OBJECT_FETCH_RETURN" else _reg(frame, "x21")
        obj_type = _u8(process, obj + 0xc) if obj else None
        if obj_type != 0x1f:
            if hit >= cfg["limit"]:
                bp_loc.GetBreakpoint().SetEnabled(False)
            return False
    if label == "COPY_RENDER_LAYER_ENTRY":
        COPY_CALAYER_KEYS.add((process.GetProcessID(), x0))
        print("copy_render_layer_entry calayer=%s selector=%s arg2=%s "
              "layer_flags=%s commit_flags=%s class=%s progname=%s lr=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            hex(x2) if x2 else None,
            hex(x3) if x3 is not None else None,
            hex(_reg(frame, "x4")) if _reg(frame, "x4") is not None else None,
            _objc_class_name(process, x0) if x0 else None,
            _progname(process),
            hex(lr) if lr else None,
        ), flush=True)
    if label == "CALAYER_SET_CONTENTS_ENTRY":
        layer = _u64(process, x0 + 0x10) if x0 else None
        old_contents = _u64(process, layer + 0x98) if layer else None
        key = (process.GetProcessID(), x0)
        new_contents = _reg(frame, "x2")
        print("calayer_set_contents progname=%s pid=%s calayer=%s class=%s "
              "matches_copy_source=%s new_contents=%s new_class=%s "
              "layer=%s old_contents=%s lr=%s" % (
            _progname(process), process.GetProcessID(),
            hex(x0) if x0 else None,
            _objc_class_name(process, x0) if x0 else None,
            key in COPY_CALAYER_KEYS,
            hex(new_contents) if new_contents else None,
            _objc_class_name(process, new_contents) if new_contents else None,
            hex(layer) if layer else None,
            hex(old_contents) if old_contents else None,
            hex(lr) if lr else None,
        ), flush=True)
    if label == "COPY_RENDER_CONTENTS_SOURCE_RETURN":
        source = _reg(frame, "x22")
        result = x0
        print("copy_render_contents_source calayer=%s result=%s result_type=%s "
              "render_layer=%s prior68=%s state=%s lr=%s" % (
            hex(source) if source else None,
            hex(result) if result else None,
            _u8(process, result + 0xc) if result else None,
            hex(_reg(frame, "x21")) if _reg(frame, "x21") else None,
            hex(_u64(process, _reg(frame, "x21") + 0x68))
            if _reg(frame, "x21") and _u64(process, _reg(frame, "x21") + 0x68) else None,
            hex(_reg(frame, "x23")) if _reg(frame, "x23") else None,
            hex(lr) if lr else None,
        ), flush=True)
    if label in ("COPY_RENDER_CONTENTS_DECISION", "COPY_RENDER_CONTENTS_STORE"):
        source = _reg(frame, "x22")
        render_layer = _reg(frame, "x21")
        candidate = _reg(frame, "x19")
        converted = _reg(frame, "x27")
        prior = _u64(process, render_layer + 0x68) if render_layer else None
        print("copy_render_contents label=%s calayer=%s render_layer=%s "
              "candidate=%s candidate_type=%s converted=%s converted_type=%s "
              "prior68=%s state=%s lr=%s" % (
            label,
            hex(source) if source else None,
            hex(render_layer) if render_layer else None,
            hex(candidate) if candidate else None,
            _u8(process, candidate + 0xc) if candidate else None,
            hex(converted) if converted else None,
            _u8(process, converted + 0xc) if converted else None,
            hex(prior) if prior else None,
            hex(_reg(frame, "x23")) if _reg(frame, "x23") else None,
            hex(lr) if lr else None,
        ), flush=True)
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
    if label.startswith("IOMFB_DIGITAL_OUT_"):
        owner = _u64(process, x0 + 0xd8) if label == "IOMFB_DIGITAL_OUT_THUNK_ENTRY" and x0 else None
        if label == "IOMFB_DIGITAL_OUT_SCALAR23_STUB" and x0:
            owner = _u64(process, x0 + 0xd8)
        owner_vtable = _u64(process, owner) if owner else None
        input_ptr = _u64(process, x2 + 0x20) if label == "IOMFB_DIGITAL_OUT_SCALAR23_STUB" and x2 else None
        input_ids = (_u32(process, input_ptr), _u32(process, input_ptr + 8)) if input_ptr else None
        print("iomfb_digital_out_kernel_path label=%s owner=%s color_id=%s timing_id=%s "
              "shim=%s status_candidate=%s x22=%s x23=%s x24=%s "
              "client_vtable=%s input_ids=%s owner_vtable_static=%s "
              "rpc_out=%s" % (
            label, hex(owner) if owner else None,
            _reg(frame, "w1") if label.endswith("ENTRY") else None,
            _reg(frame, "w2") if label.endswith("ENTRY") else None,
            hex(x19) if x19 else None,
            hex(_reg(frame, "x21")) if _reg(frame, "x21") is not None else None,
            hex(_reg(frame, "x22")) if _reg(frame, "x22") is not None else None,
            hex(x23) if x23 is not None else None,
            hex(x24) if x24 is not None else None,
            hex(_canonical_kernel_address(_u64(process, x0))) if x0 and _u64(process, x0) is not None else None,
            input_ids,
            hex(_xpac(owner_vtable) - KERNEL_SLIDE) if owner_vtable else None,
            hex(_u32(process, _reg(frame, "sp") + 4))
            if label in ("IOMFB_DIGITAL_OUT_AP_RPC_CALL", "IOMFB_DIGITAL_OUT_AP_RPC_REPLY",
                         "IOMFB_DIGITAL_OUT_AP_RETURN") and _reg(frame, "sp") and
            _u32(process, _reg(frame, "sp") + 4) is not None else None,
        ), flush=True)
    if label == "IOMFB_AP_START_ENTRY":
        obj = x0
        if lr and hit <= cfg["limit"]:
            rbp = target.BreakpointCreateByAddress(lr)
            CONFIG[rbp.GetID()] = {
                "label": "IOMFB_AP_START_RETURN", "limit": 1,
                "obj": obj, "provider": x1,
            }
            HITS["IOMFB_AP_START_RETURN"] = 0
            interpreter = target.GetDebugger().GetCommandInterpreter()
            result = lldb.SBCommandReturnObject()
            interpreter.HandleCommand(
                "breakpoint command add -F render_gate_callbacks.on_break %d" %
                rbp.GetID(), result)
        vtable, obj_class = _iomfb_ap_class(process, obj)
        print("iomfb_ap_start_entry obj=%s class=%s vtable=%s provider=%s registry=%s "
            "external_display_state430=%s name58e8=%s lr_static=%s" % (
            hex(obj) if obj else None,
            obj_class,
            hex(vtable) if vtable else None,
            hex(x1) if x1 else None,
            hex(_u64(process, obj + 0x198))
            if obj and _u64(process, obj + 0x198) else None,
            hex(_u8(process, obj + 0x430)) if obj else None,
            hex(_u64(process, obj + 0x58e8))
            if obj and _u64(process, obj + 0x58e8) else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOMFB_AP_START_RETURN":
        obj = cfg.get("obj")
        print("iomfb_ap_start_return obj=%s provider=%s result=%s "
              "registry=%s external_display_state430=%s name58e8=%s lr_static=%s" % (
            hex(obj) if obj else None,
            hex(cfg.get("provider")) if cfg.get("provider") else None,
            hex(x0) if x0 is not None else None,
            hex(_u64(process, obj + 0x198))
            if obj and _u64(process, obj + 0x198) else None,
            hex(_u8(process, obj + 0x430)) if obj else None,
            hex(_u64(process, obj + 0x58e8))
            if obj and _u64(process, obj + 0x58e8) else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOMFB_HOTPLUG_NOTIFY_ENTRY":
        obj = x0
        head = _u64(process, obj + 0x1a0) if obj else None
        head_value = _u64(process, head + 0x10) if head else None
        head_predicate = _u32(process, head_value + 0x10c) if head_value else None
        vtable, obj_class = _iomfb_ap_class(process, obj)
        print("iomfb_hotplug_notify_entry obj=%s class=%s vtable=%s event_x1=%s "
              "tiled_info_x2=%s predicate_x3=%s state389=%s state38a=%s state38b=%s "
              "state380=%s state384=%s external_state430=%s head1a0=%s head_value=%s "
              "head_value_10c=%s lr_static=%s" % (
            hex(obj) if obj else None,
            obj_class,
            hex(vtable) if vtable else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_u8(process, obj + 0x389)) if obj else None,
            hex(_u8(process, obj + 0x38a)) if obj else None,
            hex(_u8(process, obj + 0x38b)) if obj else None,
            hex(_u32(process, obj + 0x380)) if obj else None,
            hex(_u32(process, obj + 0x384)) if obj else None,
            hex(_u8(process, obj + 0x430)) if obj else None,
            hex(head) if head else None,
            hex(head_value) if head_value else None,
            hex(head_predicate) if head_predicate is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label.startswith("IOMFB_HOTPLUG_LATCH_"):
        obj = _reg(frame, "x19") or x0
        head = _u64(process, obj + 0x1a0) if obj else None
        head_value = _u64(process, head + 0x10) if head else None
        head_predicate = _u32(process, head_value + 0x10c) if head_value else None
        vtable, obj_class = _iomfb_ap_class(process, obj)
        w8 = _reg(frame, "w8")
        x20 = _reg(frame, "x20")
        x21 = _reg(frame, "x21")
        print("iomfb_hotplug_latch_write kind=%s obj=%s class=%s vtable=%s "
              "new_w8=%s event_x20=%s predicate_x21=%s state389=%s state38a=%s "
              "state38b=%s state380=%s state384=%s external_state430_before=%s "
              "head1a0=%s head_value=%s head_value_10c=%s lr_static=%s" % (
            label,
            hex(obj) if obj else None,
            obj_class,
            hex(vtable) if vtable else None,
            hex(w8) if w8 is not None else None,
            hex(x20) if x20 is not None else None,
            hex(x21) if x21 is not None else None,
            hex(_u8(process, obj + 0x389)) if obj else None,
            hex(_u8(process, obj + 0x38a)) if obj else None,
            hex(_u8(process, obj + 0x38b)) if obj else None,
            hex(_u32(process, obj + 0x380)) if obj else None,
            hex(_u32(process, obj + 0x384)) if obj else None,
            hex(_u8(process, obj + 0x430)) if obj else None,
            hex(head) if head else None,
            hex(head_value) if head_value else None,
            hex(head_predicate) if head_predicate is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOMFB_AP_FN698_ENTRY":
        obj = x0
        vtable, obj_class = _iomfb_ap_class(process, obj)
        arg2_bytes = _blob_hex(process, x2, 0x60) if x2 else None
        arg3_bytes = _blob_hex(process, x3, 0x30) if x3 else None
        head = _u64(process, obj + 0x1a0) if obj else None
        print("iomfb_ap_event_registration obj=%s class=%s vtable=%s selector=%s "
              "arg2=%s arg3=%s state_a8=%s state_b0=%s state_b8=%s "
              "state_103=%s state_58e8=%s subscription_head=%s external_state430=%s "
              "arg2_bytes60=%s arg3_bytes30=%s lr_static=%s" % (
            hex(obj) if obj else None,
            obj_class,
            hex(vtable) if vtable else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_u8(process, obj + 0xa8)) if obj else None,
            hex(_u64(process, obj + 0xb0)) if obj else None,
            hex(_u64(process, obj + 0xb8)) if obj else None,
            hex(_u8(process, obj + 0x103)) if obj else None,
            hex(_u64(process, obj + 0x58e8))
            if obj and _u64(process, obj + 0x58e8) else None,
            hex(head) if head else None,
            hex(_u8(process, obj + 0x430)) if obj else None,
            arg2_bytes,
            arg3_bytes,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOMFB_PENDING_HOTPLUG_REPLAY":
        obj = x0
        target_fn = _xpac(_reg(frame, "x8"))
        vtable, obj_class = _iomfb_ap_class(process, obj)
        print("iomfb_pending_hotplug_replay obj=%s class=%s vtable=%s "
              "target=%s target_static=%s arg1=%s arg2=%s predicate=%s "
              "pending_a8=%s pending_b0=%s pending_b8=%s pending_103=%s external_state430=%s" % (
            hex(obj) if obj else None,
            obj_class,
            hex(vtable) if vtable else None,
            hex(target_fn) if target_fn else None,
            hex(target_fn - KERNEL_SLIDE) if target_fn else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_u8(process, obj + 0xa8)) if obj else None,
            hex(_u64(process, obj + 0xb0)) if obj else None,
            hex(_u64(process, obj + 0xb8)) if obj else None,
            hex(_u8(process, obj + 0x103)) if obj else None,
            hex(_u8(process, obj + 0x430)) if obj else None,
        ), flush=True)
    if label == "IOMFB_D575_HANDLER":
        print("iomfb_d575_hotplug_handler x0=%s x1=%s x2=%s x3=%s x4=%s "
              "x5=%s lr_static=%s" % (
            hex(x0) if x0 is not None else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_reg(frame, "x4")) if _reg(frame, "x4") is not None else None,
            hex(_reg(frame, "x5")) if _reg(frame, "x5") is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOMFB_D575_HOTPLUG_DISPATCH":
        obj = x0
        target_fn = _xpac(_reg(frame, "x9"))
        print("iomfb_d575_hotplug_dispatch receiver=%s receiver_vtable=%s "
              "target=%s target_static=%s arg1=%s arg2=%s predicate=%s "
              "arg4=%s arg5=%s lr_static=%s" % (
            hex(obj) if obj else None,
            hex(_u64(process, obj)) if obj and _u64(process, obj) else None,
            hex(target_fn) if target_fn else None,
            hex(target_fn - KERNEL_SLIDE) if target_fn else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_reg(frame, "x4")) if _reg(frame, "x4") is not None else None,
            hex(_reg(frame, "x5")) if _reg(frame, "x5") is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOMFB_USERCLIENT_EXTERNAL_METHOD":
        client = x0
        ap = _u64(process, client + 0xd8) if client else None
        print("iomfb_userclient_external_method client=%s client_vtable=%s "
              "ap=%s selector_x3=%s arg1=%s arg2=%s arg4=%s arg5=%s "
              "descriptor_count=%s lr_static=%s" % (
            hex(client) if client else None,
            hex(_u64(process, client)) if client and _u64(process, client) else None,
            hex(ap) if ap else None,
            hex(x3) if x3 is not None else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 is not None else None,
            hex(_reg(frame, "x4")) if _reg(frame, "x4") is not None else None,
            hex(_reg(frame, "x5")) if _reg(frame, "x5") is not None else None,
            _u32(process, client + 0x38) if client else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOMFB_AP_EVENT_METHOD_CALL":
        client = _reg(frame, "x21")
        selector = _reg(frame, "x19")
        descriptor = _reg(frame, "x22")
        row_base = descriptor - 8 if descriptor else None
        row_bytes = _blob_hex(process, row_base, 0xb0) if row_base else None
        print("iomfb_ap_event_method_call client=%s ap=%s selector=%s "
              "method_row=%s method_row_bytes_b0=%s output=%s arg4=%s "
              "arg5=%s lr_static=%s" % (
            hex(client) if client else None,
            hex(x0) if x0 else None,
            hex(selector) if selector is not None else None,
            hex(row_base) if row_base else None,
            row_bytes,
            hex(x3) if x3 is not None else None,
            hex(_reg(frame, "x4")) if _reg(frame, "x4") is not None else None,
            hex(_reg(frame, "x5")) if _reg(frame, "x5") is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOMFB_AP_EDT_READ_ENTRY":
        key = _cstring(process, x2, 128)
        outp = _reg(frame, "x4")
        if key in ("framebuffer-width", "framebuffer-height") and lr:
            rbp = target.BreakpointCreateByAddress(lr)
            CONFIG[rbp.GetID()] = {
                "label": "IOMFB_AP_EDT_READ_RETURN", "limit": 1,
                "key": key, "service": x0, "outp": outp,
                "count": x3,
            }
            HITS["IOMFB_AP_EDT_READ_RETURN"] = 0
            interpreter = target.GetDebugger().GetCommandInterpreter()
            result = lldb.SBCommandReturnObject()
            interpreter.HandleCommand(
                "breakpoint command add -F render_gate_callbacks.on_break %d" %
                rbp.GetID(), result)
        print("iomfb_ap_edt_read_entry service=%s key=%s required=%s count=%s "
              "outp=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            key,
            hex(x1) if x1 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(outp) if outp else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOMFB_AP_EDT_READ_RETURN":
        outp = cfg.get("outp")
        print("iomfb_ap_edt_read_return service=%s key=%s result=%s "
              "value=%s count=%s" % (
            hex(cfg.get("service")) if cfg.get("service") else None,
            cfg.get("key"),
            hex(x0) if x0 is not None else None,
            hex(_u32(process, outp)) if outp and _u32(process, outp) is not None else None,
            hex(cfg.get("count")) if cfg.get("count") is not None else None,
        ), flush=True)
    if label == "IOAV_VIDEO_ELEMENTS_ENTRY":
        obj_vtable, obj_class = _ioav_video_class(process, x0)
        arg_vtable = _u64(process, x1) if x1 else None
        print("ioav_video_elements_entry obj=%s class=%s vtable=%s arg1=%s "
              "arg1_vtable=%s arg2=%s arg3=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            obj_class,
            hex(obj_vtable) if obj_vtable else None,
            hex(x1) if x1 else None,
            hex(arg_vtable) if arg_vtable else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_VIDEO_CHILD_SETUP_ENTRY":
        vtable, obj_class = _ioav_video_class(process, x0)
        print("ioav_video_child_setup_entry obj=%s class=%s vtable=%s "
              "arg1=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            obj_class,
            hex(vtable) if vtable else None,
            hex(x1) if x1 else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_VIDEO_CHILD_SETUP_RESULT":
        parent = x20
        parent_vtable, parent_class = _ioav_video_class(process, parent)
        child_vtable, child_class = _ioav_video_class(process, x0)
        print("ioav_video_child_setup_result parent=%s parent_class=%s "
              "parent_vtable=%s child=%s child_class=%s child_vtable=%s "
              "lr_static=%s" % (
            hex(parent) if parent else None,
            parent_class,
            hex(parent_vtable) if parent_vtable else None,
            hex(x0) if x0 else None,
            child_class,
            hex(child_vtable) if child_vtable else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_VIDEO_WITH_PROPERTIES_CALLSITE":
        receiver = x20
        vtable, obj_class = _ioav_video_class(process, receiver)
        print("ioav_video_with_properties_callsite obj=%s class=%s "
              "vtable=%s lr_static=%s" % (
            hex(receiver) if receiver else None,
            obj_class,
            hex(vtable) if vtable else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_VIDEO_INTERFACE_TRACK_ENTRY":
        owner_vtable, owner_class = _ioav_video_class(process, x0)
        client_vtable, client_class = _ioav_video_class(process, x1)
        print("ioav_video_interface_track_entry owner=%s owner_class=%s "
              "owner_vtable=%s client=%s client_class=%s client_vtable=%s "
              "lr_static=%s" % (
            hex(x0) if x0 else None,
            owner_class,
            hex(owner_vtable) if owner_vtable else None,
            hex(x1) if x1 else None,
            client_class,
            hex(client_vtable) if client_vtable else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_VIDEO_INTERFACE_TRACK_COMPARE":
        owner = x19
        client = x20
        owner_vtable, owner_class = _ioav_video_class(process, owner)
        print("ioav_video_interface_track_compare owner=%s owner_class=%s "
              "owner_vtable=%s client=%s queried_id=%s stored_id=%s "
              "lr_static=%s" % (
            hex(owner) if owner else None,
            owner_class,
            hex(owner_vtable) if owner_vtable else None,
            hex(client) if client else None,
            hex(x0) if x0 is not None else None,
            hex(_u32(process, owner + 0x2f8))
            if owner and _u32(process, owner + 0x2f8) is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_VIDEO_INTERFACE_TRACK_CHILD_CALL":
        owner = x19
        client = x20
        owner_vtable, owner_class = _ioav_video_class(process, owner)
        print("ioav_video_interface_track_child_call owner=%s owner_class=%s "
              "owner_vtable=%s client=%s client_id=%s lr_static=%s" % (
            hex(owner) if owner else None,
            owner_class,
            hex(owner_vtable) if owner_vtable else None,
            hex(client) if client else None,
            hex(_u32(process, client + 0x90))
            if client and _u32(process, client + 0x90) is not None else None,
              hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_INTERFACE_START_ENTRY":
        vtable, interface_class = _ioav_video_class(process, x0)
        print("ioav_interface_start object=%s class=%s vtable=%s "
              "provider=%s provider_vtable=%s provider_class=%s "
              "lr_static=%s" % (
            hex(x0) if x0 else None,
            interface_class,
            hex(vtable) if vtable else None,
            hex(x1) if x1 else None,
            hex(_u64(process, x1)) if x1 and _u64(process, x1) else None,
            _ioav_video_class(process, x1)[1] if x1 else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label.startswith("IOAV_META_ALLOC_ENTRY_"):
        names = cfg.get("meta_names", label.split("IOAV_META_ALLOC_ENTRY_", 1)[1])
        meta_vtable = _u64(process, x0) if x0 else None
        print("ioav_meta_alloc_entry classes=%s metaclass=%s "
              "metaclass_vtable=%s lr_static=%s" % (
            names,
            hex(x0) if x0 else None,
            hex(meta_vtable) if meta_vtable else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
        if lr:
            retaddr = _canonical_kernel_address(lr)
            rbp = target.BreakpointCreateByAddress(retaddr)
            ret_label = "IOAV_META_ALLOC_RETURN_" + str(bp_id)
            CONFIG[rbp.GetID()] = {
                "label": ret_label,
                "limit": 1,
                "meta_names": names,
            }
            HITS[ret_label] = 0
            interpreter = target.GetDebugger().GetCommandInterpreter()
            result = lldb.SBCommandReturnObject()
            interpreter.HandleCommand(
                "breakpoint command add -F render_gate_callbacks.on_break %d" %
                rbp.GetID(), result)
            print("ioav_meta_alloc_return_armed classes=%s address=%s ok=%s" % (
                names, hex(retaddr), result.Succeeded()), flush=True)
    if label.startswith("IOAV_META_ALLOC_RETURN_"):
        names = cfg.get("meta_names", "unknown")
        result_vtable = _u64(process, x0) if x0 else None
        print("ioav_meta_alloc_return classes=%s object=%s "
              "object_vtable=%s" % (
            names,
            hex(x0) if x0 else None,
            hex(result_vtable) if result_vtable else None,
        ), flush=True)
    if label == "MGF_INTERNAL_DISPLAY_PLUG_ENTRY":
        interface_vtable, interface_class = _ioav_video_class(process, x1)
        print("mgf_internal_display_plug_entry handler=%s interface=%s "
              "interface_class=%s interface_vtable=%s arg2=%s arg3=%s "
              "lr_static=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            interface_class,
            hex(interface_vtable) if interface_vtable else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "MGF_INTERNAL_DISPLAY_COLOR_SELECT_RESULT":
        interface = _u64(process, x19 + 0x10) if x19 else None
        result_vtable = _u64(process, x0) if x0 else None
        interface_vtable, interface_class = _ioav_video_class(process, interface)
        print("mgf_internal_display_color_select handler=%s interface=%s "
              "interface_class=%s interface_vtable=%s selected=%s "
              "selected_vtable=%s lr_static=%s" % (
            hex(x19) if x19 else None,
            hex(interface) if interface else None,
            interface_class,
            hex(interface_vtable) if interface_vtable else None,
            hex(x0) if x0 else None,
            hex(result_vtable) if result_vtable else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label in ("MGF_INTERNAL_DISPLAY_NO_COLOR",
                 "MGF_INTERNAL_DISPLAY_NO_TIMING",
                 "MGF_INTERNAL_DISPLAY_OPEN_FAILED"):
        interface = _u64(process, x19 + 0x10) if x19 else None
        interface_vtable, interface_class = _ioav_video_class(process, interface)
        print("mgf_internal_display_failure label=%s handler=%s interface=%s "
              "interface_class=%s interface_vtable=%s x0=%s x1=%s x20=%s "
              "lr_static=%s" % (
            label,
            hex(x19) if x19 else None,
            hex(interface) if interface else None,
            interface_class,
            hex(interface_vtable) if interface_vtable else None,
            hex(x0) if x0 is not None else None,
            hex(x1) if x1 is not None else None,
            hex(x20) if x20 else None,
                hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label in ("MGF_INTERNAL_DISPLAY_ALLOC_ENTRY",
                 "MGF_IOAV_HANDLER_CTOR_ENTRY"):
        obj = x0
        vtable = _u64(process, obj) if obj else None
        print("mgf_ioav_handler_lifecycle label=%s object_or_meta=%s "
              "vtable=%s x1=%s x2=%s x3=%s lr_static=%s" % (
            label,
            hex(obj) if obj else None,
            hex(vtable) if vtable else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "MGF_INTERNAL_DISPLAY_FACTORY_ENTRY":
        provider_vtable = _u64(process, x0) if x0 else None
        callback_static = (_xpac(x1) - KERNEL_SLIDE) if x1 else None
        print("mgf_internal_display_factory provider=%s provider_vtable=%s "
              "callback=%s callback_static=%s context=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            hex(provider_vtable) if provider_vtable else None,
            hex(x1) if x1 else None,
            hex(callback_static) if callback_static else None,
            hex(x2) if x2 else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
        if lr and hit <= cfg["limit"]:
            retaddr = _canonical_kernel_address(lr)
            rbp = target.BreakpointCreateByAddress(retaddr)
            CONFIG[rbp.GetID()] = {
                "label": "MGF_INTERNAL_DISPLAY_FACTORY_RETURN", "limit": 1,
                "provider": x0, "callback": x1, "context": x2,
            }
            HITS["MGF_INTERNAL_DISPLAY_FACTORY_RETURN"] = 0
            interpreter = target.GetDebugger().GetCommandInterpreter()
            result = lldb.SBCommandReturnObject()
            interpreter.HandleCommand(
                "breakpoint command add -F render_gate_callbacks.on_break %d" %
                rbp.GetID(), result)
            print("mgf_internal_display_factory_return_armed address=%s ok=%s" % (
                hex(retaddr), result.Succeeded()), flush=True)
    if label == "MGF_INTERNAL_DISPLAY_FACTORY_RETURN":
        obj_vtable = _u64(process, x0) if x0 else None
        print("mgf_internal_display_factory_return object=%s object_vtable=%s "
              "provider=%s callback=%s context=%s" % (
            hex(x0) if x0 else None,
            hex(obj_vtable) if obj_vtable else None,
            hex(cfg.get("provider")) if cfg.get("provider") else None,
            hex(cfg.get("callback")) if cfg.get("callback") else None,
            hex(cfg.get("context")) if cfg.get("context") else None,
        ), flush=True)
    if label.startswith("IOAV_CONTROLLER_"):
        controller_vtable = _u64(process, x0) if x0 else None
        print("ioav_controller_trace label=%s self=%s self_vtable=%s "
              "x1=%s x2=%s x3=%s x4=%s x5=%s x6=%s "
              "lr_static=%s" % (
            label,
            hex(x0) if x0 else None,
            hex(controller_vtable) if controller_vtable else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_reg(frame, "x4")) if _reg(frame, "x4") is not None else None,
            hex(_reg(frame, "x5")) if _reg(frame, "x5") is not None else None,
            hex(_reg(frame, "x6")) if _reg(frame, "x6") is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_CONTROLLER_START_ENTRY" and lr and hit <= cfg["limit"]:
        retaddr = _canonical_kernel_address(lr)
        rbp = target.BreakpointCreateByAddress(retaddr)
        CONFIG[rbp.GetID()] = {
            "label": "IOAV_CONTROLLER_START_RETURN", "limit": 1,
            "controller": x0, "provider": x1,
        }
        HITS["IOAV_CONTROLLER_START_RETURN"] = 0
        interpreter = target.GetDebugger().GetCommandInterpreter()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F render_gate_callbacks.on_break %d" %
            rbp.GetID(), result)
        print("ioav_controller_start_return_armed address=%s ok=%s" % (
            hex(retaddr), result.Succeeded()), flush=True)
    if label == "IOAV_CONTROLLER_START_RETURN":
        print("ioav_controller_start_return result=%s controller=%s "
              "provider=%s" % (
            hex(x0) if x0 is not None else None,
            hex(cfg.get("controller")) if cfg.get("controller") else None,
            hex(cfg.get("provider")) if cfg.get("provider") else None,
        ), flush=True)
    if label == "MGF_IOAV_HANDLER_BASE_METHOD78_ENTRY":
        handler_vtable, handler_class = _ioav_handler_class(process, x0)
        print("mgf_ioav_handler_method78 handler=%s handler_class=%s "
              "handler_vtable=%s x1=%s x2=%s x3=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            handler_class,
            hex(handler_vtable) if handler_vtable else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "MGF_IOAV_HANDLER_EVENT_SETUP_ENTRY":
        handler_vtable, handler_class = _ioav_handler_class(process, x0)
        service_vtable = _u64(process, x2) if x2 else None
        service_video_vtable, service_video_class = _ioav_video_class(process, x2)
        print("mgf_ioav_handler_event_setup handler=%s handler_class=%s "
              "handler_vtable=%s event=%s service=%s service_vtable=%s "
              "video_class=%s video_vtable=%s arg3=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            handler_class,
            hex(handler_vtable) if handler_vtable else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 else None,
            hex(service_vtable) if service_vtable else None,
            service_video_class,
            hex(service_video_vtable) if service_video_vtable else None,
            hex(x3) if x3 is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "MGF_IOAV_HANDLER_EVENT_ENTRY":
        handler_vtable, handler_class = _ioav_handler_class(process, x0)
        service_vtable = _u64(process, x2) if x2 else None
        service_video_vtable, service_video_class = _ioav_video_class(process, x2)
        print("mgf_ioav_handler_event_entry handler=%s handler_class=%s "
              "handler_vtable=%s event=%s service=%s service_vtable=%s "
              "service_video_class=%s service_video_vtable=%s arg3=%s "
              "lr_static=%s" % (
            hex(x0) if x0 else None,
            handler_class,
            hex(handler_vtable) if handler_vtable else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 else None,
            hex(service_vtable) if service_vtable else None,
            service_video_class,
            hex(service_video_vtable) if service_video_vtable else None,
            hex(x3) if x3 is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "MGF_IOAV_HANDLER_EVENT_CAST_RESULT":
        handler = x20
        handler_vtable, handler_class = _ioav_handler_class(process, handler)
        video_vtable, video_class = _ioav_video_class(process, x0)
        print("mgf_ioav_handler_event_cast_result handler=%s "
              "handler_class=%s handler_vtable=%s event=%s cast_result=%s "
              "video_class=%s video_vtable=%s lr_static=%s" % (
            hex(handler) if handler else None,
            handler_class,
            hex(handler_vtable) if handler_vtable else None,
            hex(x21) if x21 is not None else None,
            hex(x0) if x0 else None,
            video_class,
            hex(video_vtable) if video_vtable else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label in ("MGF_IOAV_HANDLER_PLUG_CALLSITE",
                 "MGF_IOAV_HANDLER_UNPLUG_CALLSITE"):
        handler = x20
        video = x19
        handler_vtable, handler_class = _ioav_handler_class(process, handler)
        video_vtable, video_class = _ioav_video_class(process, video)
        print("mgf_ioav_handler_dispatch label=%s event=%s handler=%s "
              "handler_class=%s handler_vtable=%s video=%s video_class=%s "
              "video_vtable=%s lr_static=%s" % (
            label,
            hex(x21) if x21 is not None else None,
            hex(handler) if handler else None,
            handler_class,
            hex(handler_vtable) if handler_vtable else None,
            hex(video) if video else None,
            video_class,
            hex(video_vtable) if video_vtable else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "MGF_IOAV_HANDLER_EVENT_DISPATCH_RETURN":
        handler = x20
        video = x19
        handler_vtable, handler_class = _ioav_handler_class(process, handler)
        video_vtable, video_class = _ioav_video_class(process, video)
        print("mgf_ioav_handler_dispatch_return event=%s handler=%s "
              "handler_class=%s video=%s video_class=%s result=%s" % (
            hex(x21) if x21 is not None else None,
            hex(handler) if handler else None,
            handler_class,
            hex(video) if video else None,
            video_class,
            hex(x0) if x0 is not None else None,
        ), flush=True)
    if label == "IOAV_VIDEO_ELEMENTS_PARSE_RESULT":
        obj = _reg(frame, "x19")
        parsed = _reg(frame, "x20")
        obj_vtable, obj_class = _ioav_video_class(process, obj)
        parsed_vtable = _u64(process, parsed) if parsed else None
        print("ioav_video_elements_parse_result obj=%s class=%s vtable=%s "
              "parsed=%s parsed_vtable=%s accepted=%s lr_static=%s" % (
            hex(obj) if obj else None,
            obj_class,
            hex(obj_vtable) if obj_vtable else None,
            hex(parsed) if parsed else None,
            hex(parsed_vtable) if parsed_vtable else None,
            bool(_reg(frame, "x21")),
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_COLOR_ELEMENTS_SET_CALL":
        obj_vtable, obj_class = _ioav_video_class(process, x0)
        value_vtable = _u64(process, x2) if x2 else None
        print("ioav_color_elements_set_call obj=%s class=%s key=%s "
              "value=%s value_vtable=%s arg3=%s" % (
            hex(x0) if x0 else None,
            obj_class,
            _cstring(process, x1),
            hex(x2) if x2 else None,
            hex(value_vtable) if value_vtable else None,
            hex(x3) if x3 is not None else None,
        ), flush=True)
    if label == "IOAV_ABSTRACT_VIDEO_WITH_PROPERTIES_ENTRY":
        vtable, obj_class = _ioav_video_class(process, x0)
        print("ioav_abstract_video_with_properties_entry obj=%s class=%s "
              "vtable=%s properties=%s arg2=%s arg3=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            obj_class,
            hex(vtable) if vtable else None,
            hex(x1) if x1 else None,
            hex(x2) if x2 else None,
            hex(x3) if x3 else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_ABSTRACT_VIDEO_WITH_PROPERTIES_RETURN":
        obj = _reg(frame, "x21")
        vtable, obj_class = _ioav_video_class(process, obj)
        print("ioav_abstract_video_with_properties_return obj=%s class=%s "
              "vtable=%s result=%s lr_static=%s" % (
            hex(obj) if obj else None,
            obj_class,
            hex(vtable) if vtable else None,
            hex(x0) if x0 is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_COLOR_ELEMENTS_PATH_C_ENTRY":
        print("ioav_color_elements_path_entry path=%s x0=%s x1=%s x2=%s "
              "x3=%s x4=%s x5=%s x6=%s x7=%s lr_static=%s" % (
            label,
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            hex(x2) if x2 else None,
            hex(x3) if x3 else None,
            hex(_reg(frame, "x4")) if _reg(frame, "x4") is not None else None,
            hex(_reg(frame, "x5")) if _reg(frame, "x5") is not None else None,
            hex(_reg(frame, "x6")) if _reg(frame, "x6") is not None else None,
            hex(_reg(frame, "x7")) if _reg(frame, "x7") is not None else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_COLOR_ELEMENTS_PATH_C_SET_CALL":
        receiver = x0
        target_fn = _xpac(_reg(frame, "x9"))
        receiver_vtable, receiver_class = _ioav_video_class(process, receiver)
        print("ioav_color_elements_path_c_set_call receiver=%s key=%s "
              "value=%s receiver_class=%s receiver_vtable=%s target=%s "
              "target_static=%s context=%s lr_static=%s" % (
            hex(x0) if x0 else None,
            _cstring(process, x1, 128),
            hex(x2) if x2 else None,
            receiver_class,
            hex(receiver_vtable) if receiver_vtable else None,
            hex(target_fn) if target_fn else None,
            hex(target_fn - KERNEL_SLIDE) if target_fn else None,
            hex(_reg(frame, "x19")) if _reg(frame, "x19") else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label in ("IOAV_COLOR_ELEMENTS_PATH_B_SET_RETURN",
                 "IOAV_COLOR_ELEMENTS_PATH_C_SET_RETURN"):
        print("ioav_color_elements_path_set_return path=%s status=%s "
              "context=%s lr_static=%s" % (
            label,
            hex(x0) if x0 is not None else None,
            hex(_reg(frame, "x19")) if _reg(frame, "x19") else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_COLOR_ELEMENTS_SET_RETURN":
        obj = _reg(frame, "x19")
        obj_vtable, obj_class = _ioav_video_class(process, obj)
        print("ioav_color_elements_set_return obj=%s class=%s status=%s "
              "cached_color=%s lr_static=%s" % (
            hex(obj) if obj else None,
            obj_class,
            hex(x0) if x0 is not None else None,
            hex(_u64(process, obj + 0x420))
            if obj and _u64(process, obj + 0x420) else None,
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label == "IOAV_VIDEO_ELEMENTS_FAILURE":
        obj = _reg(frame, "x19")
        parsed = _reg(frame, "x20")
        obj_vtable, obj_class = _ioav_video_class(process, obj)
        print("ioav_video_elements_failure obj=%s class=%s "
              "parsed=%s accepted=%s lr_static=%s" % (
            hex(obj) if obj else None,
            obj_class,
            hex(parsed) if parsed else None,
            bool(_reg(frame, "x21")),
            hex(_xpac(lr) - KERNEL_SLIDE) if lr else None,
        ), flush=True)
    if label in ("ENCODER_D13_SOURCE_SUB", "ENCODER_TYPE31_STORE",
                 "ENCODER_D13_STORE"):
        import struct

        def _fpreg(name):
            value = frame.FindRegister(name)
            if not value.IsValid():
                return None, None
            try:
                bits = value.GetValueAsUnsigned()
            except Exception:
                bits = None
            return bits, value.GetValue()

        d10_bits, d10_text = _fpreg("d10")
        d8_bits, d8_text = _fpreg("d8")
        d13_bits, d13_text = _fpreg("d13")
        x21 = _reg(frame, "x21")
        cursor = (_reg(frame, "x8")
                  if label != "ENCODER_D13_SOURCE_SUB" else None)
        queue_begin = (_u64(process, x21 + 0x18)
                       if x21 and cursor else None)
        queue_end = (_u64(process, x21 + 0x20)
                     if x21 and cursor else None)
        window = b""
        if cursor and cursor >= 8:
            err = lldb.SBError()
            window = process.ReadMemory(cursor - 8, 32, err) if err.Success() else b""

        def _double(bits):
            return struct.unpack("<d", (int(bits) & 0xffffffffffffffff).to_bytes(
                8, "little"))[0] \
                if bits is not None else None

        print("encoder_record label=%s thread=0x%x encoder=%s qbegin=%s "
              "qend=%s cursor=%s qoffset=%s d8=%s d10=%s d13=%s "
              "d8_bits=%s d10_bits=%s d13_bits=%s window=%s" % (
            label,
            _reg(frame, "tpidr_el1") or 0,
            hex(x21) if x21 else None,
            hex(queue_begin) if queue_begin else None,
            hex(queue_end) if queue_end else None,
            hex(cursor) if cursor else None,
            (cursor - queue_begin) if cursor and queue_begin else None,
            d8_text, d10_text, d13_text,
            hex(d8_bits) if d8_bits is not None else None,
            hex(d10_bits) if d10_bits is not None else None,
            hex(d13_bits) if d13_bits is not None else None,
            window.hex(),
        ), flush=True)
    if label in ("LAYER_OBJECT_FETCH_RETURN", "ENCODER_OBJECT_VCALL"):
        obj = x0 if label == "LAYER_OBJECT_FETCH_RETURN" else _reg(frame, "x21")
        encoder = _reg(frame, "x19") if label == "ENCODER_OBJECT_VCALL" else None
        vtable = _u64(process, obj) if obj else None
        vtable_entry = _u64(process, vtable + 0x20) if vtable else None
        err = lldb.SBError()
        raw = process.ReadMemory(obj, 0xa0, err) if obj else b""
        qbegin = _u64(process, encoder + 0x18) if encoder else None
        qcursor = _u64(process, encoder + 0x20) if encoder else None
        import struct
        rgba = struct.unpack("<4f", raw[0x10:0x20]) if raw and len(raw) >= 0x20 else None
        position = struct.unpack("<2d", raw[0x38:0x48]) if raw and len(raw) >= 0x48 else None
        anchor = struct.unpack("<2d", raw[0x48:0x58]) if raw and len(raw) >= 0x58 else None
        size = struct.unpack("<2d", raw[0x58:0x68]) if raw and len(raw) >= 0x68 else None
        def _raw_u64(off):
            return int.from_bytes(raw[off:off + 8], "little") if raw and len(raw) >= off + 8 else None
        print("type31_object label=%s object=%s type=%s vtable=%s "
              "vtable_entry20=%s method_target=%s encoder=%s qoffset=%s "
              "rgba10=%s flags30=%s position38=%s anchor48=%s size58=%s "
              "contents68=%s field70=%s mask78=%s ext88=%s raw=%s" % (
            label,
            hex(obj) if obj else None,
            _u8(process, obj + 0xc) if obj else None,
            hex(vtable) if vtable else None,
            hex(vtable_entry) if vtable_entry else None,
            hex(_reg(frame, "x8")) if label == "ENCODER_OBJECT_VCALL" and _reg(frame, "x8") else None,
            hex(encoder) if encoder else None,
            qcursor - qbegin if qcursor and qbegin else None,
            rgba,
            hex(_raw_u64(0x30)) if _raw_u64(0x30) is not None else None,
            position, anchor, size,
            hex(_raw_u64(0x68)) if _raw_u64(0x68) else None,
            hex(_raw_u64(0x70)) if _raw_u64(0x70) else None,
            hex(_raw_u64(0x78)) if _raw_u64(0x78) else None,
            hex(_raw_u64(0x88)) if _raw_u64(0x88) else None,
            raw.hex() if raw and err.Success() else "<read-error>",
        ), flush=True)
    if label == "LAYER_GEOMETRY_FLAG22_GATE":
        layer = _reg(frame, "x21")
        local = _reg(frame, "x28")
        flags = _u32(process, layer + 0x30) if layer else None
        def _layer_f64(off):
            if not layer:
                return None
            err = lldb.SBError()
            raw = process.ReadMemory(layer + off, 8, err)
            return struct.unpack("<d", raw)[0] if err.Success() and raw else None
        rgba = None
        if layer:
            err = lldb.SBError()
            raw = process.ReadMemory(layer + 0x10, 16, err)
            if err.Success() and raw:
                import struct
                rgba = struct.unpack("<4f", raw)
        print("layer_frame_gate layer=%s flags30=%s bit22=%s rgba10=%s "
              "position38=(%s,%s) anchor48=(%s,%s) size58=(%s,%s) "
              "contents68=%s field70=%s mask78=%s ext88=%s local_state=%s "
              "local10=%s x8=%s" % (
            hex(layer) if layer else None,
            hex(flags) if flags is not None else None,
            bool(flags & (1 << 22)) if flags is not None else None,
            rgba,
            _layer_f64(0x38), _layer_f64(0x40),
            _layer_f64(0x48), _layer_f64(0x50),
            _layer_f64(0x58), _layer_f64(0x60),
            hex(_u64(process, layer + 0x68)) if layer and _u64(process, layer + 0x68) else None,
            hex(_u64(process, layer + 0x70)) if layer and _u64(process, layer + 0x70) else None,
            hex(_u64(process, layer + 0x78)) if layer and _u64(process, layer + 0x78) else None,
            hex(_u64(process, layer + 0x88)) if layer and _u64(process, layer + 0x88) else None,
            hex(local) if local else None,
            hex(_u64(process, local + 0x10)) if local and _u64(process, local + 0x10) else None,
            hex(_reg(frame, "x8")) if _reg(frame, "x8") is not None else None,
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
                # PACKED=1179|(2556<<14) also feeds update_surface stack WxH path
                # at 0x18445b440 when x24=sp+0x38.
                if os.environ.get("RFT_POKE_2B8_M1") == "1":
                    val = 0xffffffffffffffff
                elif os.environ.get("RFT_POKE_2B8_PACKED") == "1":
                    val = 1179 | (2556 << 14)  # 0x027f049b
                else:
                    val = 1
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
    if label == "RFT_POST_V170_TIME":
        # First insn after V170_OK's bl 0x1843f6ddc — proves the time helper returned.
        x19 = _reg(frame, "x19")
        x25 = _reg(frame, "x25")
        print("rft_post_v170_time x19=%s x25=%s d0_time_ret_ok" % (
            hex(x19) if x19 else None,
            hex(x25) if x25 else None,
        ), flush=True)
    if label == "RFT_POST_V170_CMP648":
        # Stops *before* cmp x8,x9 — but ldr x8/x9 already ran. Force must
        # rewrite x8 (and optionally +0x648) so b.eq falls through.
        import os
        x19 = _reg(frame, "x19")
        x8 = _reg(frame, "x8")
        x9 = _reg(frame, "x9")
        a = _u64(process, x19 + 0x648) if x19 else None
        b = _u64(process, x19 + 0x40) if x19 else None
        forced = False
        if (os.environ.get("RFT_FORCE_648_NEQ") == "1" and
                x8 is not None and x9 is not None and x8 == x9):
            reg = frame.FindRegister("x8")
            ok = reg.SetValueFromCString("0") if reg.IsValid() else False
            if ok and x19:
                err = lldb.SBError()
                process.WriteMemory(x19 + 0x648, (0).to_bytes(8, "little"), err)
            forced = bool(ok)
            x8 = 0 if ok else x8
            a = 0 if ok else a
        print("rft_post_v170_cmp648 x8=%s x9=%s +648=%s +40=%s eq=%s forced_neq=%s" % (
            hex(x8) if x8 is not None else None,
            hex(x9) if x9 is not None else None,
            hex(a) if a is not None else None,
            hex(b) if b is not None else None,
            (x8 == x9) if (x8 is not None and x9 is not None) else None,
            forced,
        ), flush=True)
    if label == "RFT_POST_V170_GFLAG":
        # tbnz global byte → 0x184414dbc
        w8 = _reg(frame, "w8")
        print("rft_post_v170_gflag w8=%s bit0=%s" % (
            hex(w8) if w8 is not None else None,
            (w8 & 1) if w8 is not None else None,
        ), flush=True)
    if label == "ALLOC_LDP_WH":
        # About to: ldp w1, w2, [x24, #8]; then bl DISPLAY_ALLOCATE.
        # x24 is one of: display+0xd0 (common), sp+0x38 (packed from +0x2b8),
        # or display+0xa0. GATE91 natural [x24+8]=0 → CREATE 0×0 → alloc null.
        import os
        x24 = _reg(frame, "x24")
        x19 = _reg(frame, "x19")
        nat_w = _u32(process, x24 + 8) if x24 else None
        nat_h = _u32(process, x24 + 0xc) if x24 else None
        b8w = _u32(process, x19 + 0xb8) if x19 else None
        b8h = _u32(process, x19 + 0xbc) if x19 else None
        path = "?"
        if x24 and x19:
            if x24 == x19 + 0xd0:
                path = "disp+0xd0"
            elif x24 == x19 + 0xa0:
                path = "disp+0xa0"
            else:
                path = "stack/other"
        w, h = nat_w, nat_h
        forced = False
        if os.environ.get("RFT_FORCE_ALLOC_WH") == "1" and x24:
            err = lldb.SBError()
            process.WriteMemory(x24 + 8, (1179).to_bytes(4, "little"), err)
            process.WriteMemory(x24 + 0xc, (2556).to_bytes(4, "little"), err)
            forced = err.Success()
            w, h = 1179, 2556
        print("alloc_ldp_wh x24=%s path=%s nat=(%s,%s) +b8=(%s,%s) "
              "out=(%s,%s) forced=%s" % (
                  hex(x24) if x24 else None, path, nat_w, nat_h,
                  b8w, b8h, w, h, forced,
              ), flush=True)
    if label == "ALLOCATE_IOSURFACE_RETURN":
        if x0:
            LAST_ALLOC_SURFACE = x0
            print("alloc_surface_saved x0=%s" % hex(x0), flush=True)
    if label == "SWAP_SET_LAYER_ENTRY":
        import os
        forced = False
        if (os.environ.get("RFT_FORCE_SWAP_X2") == "1" and not x2 and
                LAST_ALLOC_SURFACE):
            reg = frame.FindRegister("x2")
            ok = reg.SetValueFromCString(hex(LAST_ALLOC_SURFACE)) if reg.IsValid() else False
            forced = bool(ok)
            if forced:
                x2 = LAST_ALLOC_SURFACE
        print("swap_set_layer x0=%s x1=%s x2=%s x3=%s last_alloc=%s forced_x2=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 else None,
            hex(x3) if x3 else None,
            hex(LAST_ALLOC_SURFACE) if LAST_ALLOC_SURFACE else None,
            forced,
        ), flush=True)

    if label == "HANDOFF_SINK_ENTRY":
        slot = _u64(process, x0 + 0xb38) if x0 else None
        # GATE128: _kern_SwapSetLayer may want IOSurfaceGetID (w8 here) rather
        # than IOSurface* in x2. GATE127 pointer → GENERIC_MAP NoMemory.
        import os
        id_forced = False
        if (os.environ.get("RFT_SET_LAYER_ID_FROM_W8") == "1" and
                w8 is not None and w8 != 0 and x2 and x2 > 0x10000):
            r = frame.FindRegister("x2")
            if r.IsValid():
                r.SetValueFromCString(hex(w8))
                id_forced = True
                x2 = w8
        print("handoff_sink x0=%s +0xb38=%s x1=%s x2=%s x3=%s will_fail=%s "
              "id_from_w8=%s" % (
            hex(x0) if x0 else None,
            hex(slot) if slot else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 else None,
            hex(x3) if x3 else None,
            (not x0) or (not slot),
            id_forced,
        ), flush=True)
    if label == "HANDOFF_SINK_FAIL":
        print("handoff_sink_fail returning e00002c2 x0_in=%s" % (
            hex(x0) if x0 else None), flush=True)
    if label in ("FINISH_MODE_ELIGIBILITY_RETURN", "FINISH_MODE_ELIGIBILITY_JOIN"):
        display = _reg(frame, "x20")
        stack = _reg(frame, "sp")
        mode = _u64(process, display + 0x2b8) if display else None
        gate = _u32(process, stack + 0x120) if stack else None
        mode_kind = ((mode >> 55) & 0x1f) if mode is not None else None
        mode_bit28 = ((mode >> 28) & 1) if mode is not None else None
        class_ok = (16 <= mode_kind < 29) if mode_kind is not None else None
        passthrough_off = _u8(process, display + 0x755e) if display else None
        print("finish_mode_eligibility stage=%s display=%s vt6c0_return=%s "
              "mode=%s mode_kind=%s bit28=%s class_ok=%s +755e=%s stored_gate=%s "
              "last_alloc=%s" % (
                  label,
                  hex(display) if display else None,
                  hex(_reg(frame, "x0") or 0),
                  hex(mode) if mode is not None else None,
                  mode_kind, mode_bit28, class_ok, passthrough_off,
                  gate,
                  hex(LAST_ALLOC_SURFACE) if LAST_ALLOC_SURFACE else None,
              ), flush=True)
    if label == "SWAP_CLEAR_FLAG_SET":
        display = _reg(frame, "x19")
        flag_ptr = _reg(frame, "x23")
        old = _u8(process, flag_ptr) if flag_ptr else None
        print("swap_clear_flag_set display=%s flag_ptr=%s old=%s "
              "expected_ptr=%s value=1 last_alloc=%s" % (
                  hex(display) if display else None,
                  hex(flag_ptr) if flag_ptr else None,
                  old,
                  hex(display + 0x7545) if display else None,
                  hex(LAST_ALLOC_SURFACE) if LAST_ALLOC_SURFACE else None,
              ), flush=True)
    if label == "FINISH_SWAP_CLEAR_GATE":
        import os
        x23 = _reg(frame, "x23")
        w8 = _reg(frame, "x8")
        b1 = _u8(process, x23 + 1) if x23 else None
        b = _u8(process, x23 + 0x10) if x23 else None
        skipped = False
        # tbz w8,#0 -> skip clear set_layer; w8 = (w26|w27) | [x23+0x10].
        if os.environ.get("RFT_SKIP_CLEAR_SWAP") == "1" and x23:
            err = lldb.SBError()
            process.WriteMemory(x23 + 0x10, b"\x00", err)
            if w8 is not None:
                reg = frame.FindRegister("x8")
                if reg.IsValid():
                    reg.SetValueFromCString(hex(w8 & ~1))
            skipped = err.Success()
            b = _u8(process, x23 + 0x10)
            w8 = _reg(frame, "x8")
        print("finish_swap_clear_gate x23=%s +1=%s +0x10=%s bit0=%s "
              "w8=%s skip=%s" % (
            hex(x23) if x23 else None, b1, b,
            (b & 1) if b is not None else None,
            hex(w8) if w8 is not None else None, skipped,
        ), flush=True)
    if label == "FINISH_POST_CLEAR_CA14":
        import os
        # tbz w24,#0 → cb50 skips update_surface; force bit0 to continue.
        w24 = _reg(frame, "x24")
        w8 = _reg(frame, "x8")   # [x26+0x44] flags
        w9 = _reg(frame, "x9")   # from [x20+0x3b8]
        forced = False
        if os.environ.get("RFT_FORCE_POST_CLEAR_X22") == "1":
            if w24 is not None:
                reg = frame.FindRegister("x24")
                if reg.IsValid() and reg.SetValueFromCString(hex(w24 | 1)):
                    w24 = w24 | 1
                    forced = True
            # ca20 tbnz w9,#0 → cc0c; clear bit0 so we don't bail.
            if w9 is not None:
                reg = frame.FindRegister("x9")
                if reg.IsValid():
                    reg.SetValueFromCString(hex(w9 & ~0x100))  # pre-tst value
                    # Also ensure post-csel path: zero x25's contribution via w9=0
                    # after ca1c is hard; clear x25 bit0 instead.
                    r25 = frame.FindRegister("x25")
                    if r25.IsValid():
                        v25 = _reg(frame, "x25") or 0
                        r25.SetValueFromCString(hex(v25 & ~1))
            # ca28 tbz w8,#1 → cb84; set bit1 of layer flags.
            if w8 is not None:
                reg = frame.FindRegister("x8")
                if reg.IsValid():
                    reg.SetValueFromCString(hex(w8 | 2))
                    w8 = w8 | 2
            w9 = _reg(frame, "x9")
        print("finish_post_clear_ca14 w24=%s bit0=%s w8=%s w9=%s forced=%s" % (
            hex(w24) if w24 is not None else None,
            (w24 & 1) if w24 is not None else None,
            hex(w8) if w8 is not None else None,
            hex(w9) if w9 is not None else None,
            forced,
        ), flush=True)
    if label == "FINISH_UPDATE_SURFACE_BL":
        print("finish_update_surface_bl x0=%s w1=%s w2=%s last_alloc=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 is not None else None,
            hex(LAST_ALLOC_SURFACE) if LAST_ALLOC_SURFACE else None,
        ), flush=True)
    if label == "FINISH_SWAP_X22_GATE":
        import os
        x9 = _reg(frame, "x9")  # [x20+0x66e8]
        x22 = _reg(frame, "x22")
        x23 = _reg(frame, "x23")
        x25 = _reg(frame, "x25")
        x26 = _reg(frame, "x26")
        w24 = _reg(frame, "x24")
        w8 = _reg(frame, "x8")
        b15 = _u8(process, x23 + 0x15) if x23 else None
        forced = False
        if os.environ.get("RFT_FORCE_X22_SURFACE") == "1":
            # GATE102: x9 garbage → d054; x26=IOKit err ≠ w25 → d0f8
            # (null x2 path). Zero x9, sync w26/w25, set w8=1, w24=2,
            # and install last alloc as x22 so d0c0 runs.
            if x9:
                reg = frame.FindRegister("x9")
                if reg.IsValid() and reg.SetValueFromCString("0"):
                    x9 = 0
                    forced = True
            if x25 is not None:
                reg = frame.FindRegister("x26")
                if reg.IsValid() and reg.SetValueFromCString(hex(x25)):
                    x26 = x25
                    forced = True
            reg = frame.FindRegister("x8")
            if reg.IsValid() and reg.SetValueFromCString("1"):
                w8 = 1
                forced = True
            reg = frame.FindRegister("x24")
            if reg.IsValid() and reg.SetValueFromCString("2"):
                w24 = 2
                forced = True
            if (not x22) and LAST_ALLOC_SURFACE:
                reg = frame.FindRegister("x22")
                if reg.IsValid() and reg.SetValueFromCString(hex(LAST_ALLOC_SURFACE)):
                    x22 = LAST_ALLOC_SURFACE
                    forced = True
            elif LAST_ALLOC_SURFACE and x22 != LAST_ALLOC_SURFACE:
                # update_surface failed (x22 cleared at cb48); still present.
                reg = frame.FindRegister("x22")
                if reg.IsValid() and reg.SetValueFromCString(hex(LAST_ALLOC_SURFACE)):
                    x22 = LAST_ALLOC_SURFACE
                    forced = True
        print("finish_x22_gate x9_66e8=%s w24=%s w8=%s w25=%s w26=%s "
              "x23+15=%s x22=%s last_alloc=%s forced=%s" % (
            hex(x9) if x9 else None,
            hex(w24) if w24 is not None else None,
            hex(w8) if w8 is not None else None,
            hex(x25) if x25 is not None else None,
            hex(x26) if x26 is not None else None,
            b15,
            hex(x22) if x22 else None,
            hex(LAST_ALLOC_SURFACE) if LAST_ALLOC_SURFACE else None,
            forced,
        ), flush=True)
    if label == "KERN_SWAP_BEGIN_THUNK":
        import os
        if x0:
            LAST_IOMFB = x0
        slot = _u64(process, x0 + 0xc20) if x0 else None
        end_slot = _u64(process, x0 + 0xc28) if x0 else None
        wait_slot = _u64(process, x0 + 0xc60) if x0 else None
        conn = _u32(process, x0 + 0x14) if x0 else None
        conn_poked = False
        # GATE119/120: once, call IOServiceOpen(matched_svc, task, 0, &obj+0x14)
        # then re-enter this thunk so Begin sees the new connect.
        if (os.environ.get("RFT_FORCE_OPEN_MATCHED_SVC") == "1" and x0 and
                LAST_MATCHED_SVC and OPEN_INJECT_STATE is None):
            open_rt = RUNTIME_ADDR.get("IOSERVICE_OPEN")
            cont = RUNTIME_ADDR.get("KERN_SWAP_BEGIN_THUNK")
            if open_rt and cont:
                LAST_IOMFB = x0
                err = lldb.SBError()
                process.WriteMemory(x0 + 0x14, (0).to_bytes(4, "little"), err)
                task = LAST_TASK_PORT or 0x203
                for reg, val in (
                    ("x0", LAST_MATCHED_SVC),
                    ("x1", task),
                    ("x2", 0),
                    ("x3", x0 + 0x14),
                    ("lr", cont),
                    ("pc", open_rt),
                ):
                    r = frame.FindRegister(reg)
                    if r.IsValid():
                        r.SetValueFromCString(hex(val))
                OPEN_INJECT_STATE = "pending"
                print("kern_swap_begin_inject_open svc=%s task=%s &conn=%s "
                      "iomfb=%s open=%s" % (
                    hex(LAST_MATCHED_SVC), hex(task), hex(x0 + 0x14),
                    hex(x0), hex(open_rt)), flush=True)
                if hit >= cfg["limit"]:
                    bp_loc.GetBreakpoint().SetEnabled(False)
                return False
        if OPEN_INJECT_STATE == "pending":
            # x0 is IOReturn from IOServiceOpen — restore the IOMFB object.
            open_ret = x0
            OPEN_INJECT_STATE = "done"
            if LAST_IOMFB:
                r = frame.FindRegister("x0")
                if r.IsValid():
                    r.SetValueFromCString(hex(LAST_IOMFB))
                x0 = LAST_IOMFB
            conn = _u32(process, LAST_IOMFB + 0x14) if LAST_IOMFB else None
            if conn:
                LAST_GOOD_CONN = conn
                if conn not in LAST_CONNS:
                    LAST_CONNS.append(conn)
            print("kern_swap_begin_inject_open_done open_ret=%s iomfb=%s "
                  "conn=%s" % (
                hex(open_ret) if open_ret is not None else None,
                hex(LAST_IOMFB) if LAST_IOMFB else None,
                hex(conn) if conn is not None else None), flush=True)
        # GATE116/124/125/131: poke a real IOMFB connect into +0x14 before kern Begin.
        # 1) nearest backboardd Open obj within window (true sibling)
        # 2) else LAST_BB_IOMFB_CONN (most recent backboardd kern-slotted Open)
        # GATE130 failed: early Opens were foreign-process / far from virt
        # swap objs → conn_poked=False → Begin 0xe00002bc.
        if (os.environ.get("RFT_POKE_OPEN_CONN") == "1" and x0 and not conn):
            poke_src = None
            sibling_obj = None
            sibling_dist = None
            poke_why = None
            window = int(os.environ.get("RFT_POKE_SIBLING_WINDOW", "0x10000"), 0)
            for o, c in reversed(IOMFB_OPEN_PAIRS):
                if not o or not c:
                    continue
                dist = abs(int(x0) - int(o))
                if dist == 0:
                    continue
                if dist < window and (sibling_dist is None or dist < sibling_dist):
                    sibling_dist = dist
                    sibling_obj = o
                    poke_src = c
                    poke_why = "sibling"
            if not poke_src and LAST_BB_IOMFB_CONN:
                poke_src = LAST_BB_IOMFB_CONN
                sibling_obj = LAST_BB_IOMFB
                sibling_dist = (abs(int(x0) - int(LAST_BB_IOMFB))
                               if LAST_BB_IOMFB else None)
                poke_why = "last_bb_kern"
            if not poke_src and os.environ.get("RFT_POKE_CONN_FALLBACK") == "1":
                poke_src = LAST_IOMFB_CONN or LAST_GOOD_CONN
                poke_why = "legacy_fallback"
            idx = int(os.environ.get("RFT_POKE_CONN_INDEX", "0") or "0")
            if (not poke_src and LAST_CONNS and
                    os.environ.get("RFT_POKE_ANY_CONN") == "1"):
                if 0 <= idx < len(LAST_CONNS):
                    poke_src = LAST_CONNS[idx]
                else:
                    poke_src = LAST_CONNS[0]
                poke_why = "any_conn"
            if poke_src:
                err = lldb.SBError()
                process.WriteMemory(
                    x0 + 0x14, int(poke_src).to_bytes(4, "little"), err)
                conn_poked = err.Success()
                if conn_poked:
                    conn = poke_src
                    print("kern_swap_begin_poke conn=%s sibling=%s dist=%s "
                          "why=%s bb_conn=%s npairs=%d" % (
                        hex(poke_src),
                        hex(sibling_obj) if sibling_obj else None,
                        hex(sibling_dist) if sibling_dist is not None else None,
                        poke_why,
                        hex(LAST_BB_IOMFB_CONN) if LAST_BB_IOMFB_CONN else None,
                        len(IOMFB_OPEN_PAIRS)), flush=True)
            else:
                print("kern_swap_begin_poke_miss x0=%s npairs=%d "
                      "bb_conn=%s last_iomfb_conn=%s" % (
                    hex(x0) if x0 else None,
                    len(IOMFB_OPEN_PAIRS),
                    hex(LAST_BB_IOMFB_CONN) if LAST_BB_IOMFB_CONN else None,
                    hex(LAST_IOMFB_CONN) if LAST_IOMFB_CONN else None,
                ), flush=True)
        redirected = False
        begin_rt = RUNTIME_ADDR.get("KERN_SWAP_BEGIN")
        end_rt = RUNTIME_ADDR.get("KERN_SWAP_END")
        set_rt = RUNTIME_ADDR.get("KERN_SWAP_SET_LAYER")
        set_slot = _u64(process, x0 + 0xb38) if x0 else None
        # vphone-style: point Begin/End/SetLayer fps at _kern_* regardless of virt.
        # GATE125: Begin+End w0=0 with sibling connect, but KERN_SWAP_SET_LAYER
        # never hit (+0xb38 still virt) → End accepted IDs never registered, no A408.
        if os.environ.get("RFT_FORCE_KERN_PATH") == "1" and x0:
            err = lldb.SBError()
            if begin_rt:
                process.WriteMemory(
                    x0 + 0xc20, int(begin_rt).to_bytes(8, "little"), err)
            if end_rt and err.Success():
                process.WriteMemory(
                    x0 + 0xc28, int(end_rt).to_bytes(8, "little"), err)
            # GATE129/130: SetLayer force is optional — pointer SetLayer +
            # 0x700 still NoMemory; virt SetLayer + kern Begin/End maps OK.
            if (os.environ.get("RFT_FORCE_KERN_SET_LAYER", "1") == "1" and
                    set_rt and err.Success()):
                process.WriteMemory(
                    x0 + 0xb38, int(set_rt).to_bytes(8, "little"), err)
            redirected = err.Success()
            slot = _u64(process, x0 + 0xc20)
            end_slot = _u64(process, x0 + 0xc28)
            set_slot = _u64(process, x0 + 0xb38)
        elif (os.environ.get("RFT_FORCE_KERN_SWAP_BEGIN") == "1" and x0 and
              begin_rt):
            err = lldb.SBError()
            process.WriteMemory(x0 + 0xc20, int(begin_rt).to_bytes(8, "little"), err)
            redirected = err.Success()
            slot = _u64(process, x0 + 0xc20)
        print("kern_swap_begin_thunk x0=%s +0xc20=%s +0xc28=%s +0xb38=%s "
              "+0xc60=%s conn=%s will_fail=%s redirected=%s conn_poked=%s "
              "nconns=%d" % (
            hex(x0) if x0 else None,
            hex(slot) if slot else None,
            hex(end_slot) if end_slot else None,
            hex(set_slot) if set_slot else None,
            hex(wait_slot) if wait_slot else None,
            hex(conn) if conn is not None else None,
            (not x0) or (not slot),
            redirected,
            conn_poked,
            len(LAST_CONNS),
        ), flush=True)
    if label == "IOMFB_OPEN":
        # IOMobileFramebufferOpen: alloc, then IOServiceOpen(svc,..., &obj+0x14).
        # x0=service, x3=&out_obj. Arm return to dump *out and connect.
        outp = _reg(frame, "x3")
        if lr and hit <= 16:
            rbp = target.BreakpointCreateByAddress(lr)
            CONFIG[rbp.GetID()] = {
                "label": "IOMFB_OPEN_RETURN", "limit": 1, "outp": outp,
            }
            HITS["IOMFB_OPEN_RETURN"] = 0
            interpreter = target.GetDebugger().GetCommandInterpreter()
            result = lldb.SBCommandReturnObject()
            interpreter.HandleCommand(
                "breakpoint command add -F render_gate_callbacks.on_break %d" %
                rbp.GetID(), result)
        print("iomfb_open svc=%s owningTask=%s type=%s outp=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 else None,
            hex(outp) if outp else None,
        ), flush=True)
    if label == "IOMFB_OPEN_RETURN":
        outp = cfg.get("outp")
        obj = _u64(process, outp) if outp else None
        conn = _u32(process, obj + 0x14) if obj else None
        c20 = _u64(process, obj + 0xc20) if obj else None
        c28 = _u64(process, obj + 0xc28) if obj else None
        if conn:
            LAST_GOOD_CONN = conn
            LAST_IOMFB_CONN = conn
            if obj:
                LAST_IOMFB = obj
                kept = _record_bb_iomfb_open(process, obj, conn, c20)
            else:
                kept = False
        else:
            kept = False
        print("iomfb_open_return w0=%s obj=%s conn+0x14=%s +0xc20=%s +0xc28=%s "
              "bb_kept=%s" % (
            hex(x0) if x0 is not None else None,
            hex(obj) if obj else None,
            hex(conn) if conn is not None else None,
            hex(c20) if c20 else None,
            hex(c28) if c28 else None,
            kept if conn else False,
        ), flush=True)
    if label == "IOMFB_GET_MAIN_DISPLAY":
        outp = x0  # typically &ref
        if lr and hit <= 8:
            rbp = target.BreakpointCreateByAddress(lr)
            CONFIG[rbp.GetID()] = {
                "label": "IOMFB_GET_MAIN_DISPLAY_RETURN", "limit": 1,
                "outp": outp,
            }
            HITS["IOMFB_GET_MAIN_DISPLAY_RETURN"] = 0
            interpreter = target.GetDebugger().GetCommandInterpreter()
            result = lldb.SBCommandReturnObject()
            interpreter.HandleCommand(
                "breakpoint command add -F render_gate_callbacks.on_break %d" %
                rbp.GetID(), result)
        print("iomfb_get_main_display outp=%s" % (
            hex(outp) if outp else None), flush=True)
    if label == "IOMFB_GET_MAIN_DISPLAY_RETURN":
        outp = cfg.get("outp")
        obj = _u64(process, outp) if outp else None
        conn = _u32(process, obj + 0x14) if obj else None
        print("iomfb_get_main_display_return w0=%s obj=%s conn=%s" % (
            hex(x0) if x0 is not None else None,
            hex(obj) if obj else None,
            hex(conn) if conn is not None else None,
        ), flush=True)
    if label == "IOMFB_OPEN_BY_NAME":
        outp = x1  # IOReturn OpenByName(name, IOMobileFramebufferRef *out)
        if hit <= 4:
            registry_base, registry = _iomfb_name_registry(process)
            print("iomfb_name_registry progname=%s base=%s entries=%s" % (
                _progname(process), hex(registry_base), registry), flush=True)
        if lr and hit <= 8:
            rbp = target.BreakpointCreateByAddress(lr)
            CONFIG[rbp.GetID()] = {
                "label": "IOMFB_OPEN_BY_NAME_RETURN", "limit": 1, "outp": outp,
            }
            HITS["IOMFB_OPEN_BY_NAME_RETURN"] = 0
            interpreter = target.GetDebugger().GetCommandInterpreter()
            result = lldb.SBCommandReturnObject()
            interpreter.HandleCommand(
                "breakpoint command add -F render_gate_callbacks.on_break %d" %
                rbp.GetID(), result)
        print("iomfb_open_by_name name=%s outp=%s" % (
            hex(x0) if x0 else None, hex(outp) if outp else None),
            flush=True)
    if label == "IOMFB_OPEN_BY_NAME_RETURN":
        outp = cfg.get("outp")
        obj = _u64(process, outp) if outp else None
        conn = _u32(process, obj + 0x14) if obj else None
        c20 = _u64(process, obj + 0xc20) if obj else None
        c28 = _u64(process, obj + 0xc28) if obj else None
        if conn:
            LAST_GOOD_CONN = conn
            LAST_IOMFB_CONN = conn
            LAST_IOMFB = obj
            kept = _record_bb_iomfb_open(process, obj, conn, c20) if obj else False
        else:
            kept = False
        print("iomfb_open_by_name_return w0=%s obj=%s conn+0x14=%s "
              "+0xc20=%s +0xc28=%s bb_kept=%s" % (
            hex(x0) if x0 is not None else None,
            hex(obj) if obj else None,
            hex(conn) if conn is not None else None,
            hex(c20) if c20 else None,
            hex(c28) if c28 else None,
            kept if conn else False,
        ), flush=True)
    if label == "IOMFB_OPEN_BY_NAME_TYPE":
        # At cmp w8,#2: w8 already loaded from info+0x8. type==2 → virt path
        # (no Open). info+0x14 = io_service_t for _iomfb_get_display→Open.
        import os
        x26 = _reg(frame, "x26")
        typ = _u32(process, x26 + 0x8) if x26 else None
        svc = _u32(process, x26 + 0x14) if x26 else None
        w8 = _reg(frame, "w8")
        forced = False
        injected = False
        # GATE120: stash a matched backboardd service into the virt entry so
        # get_display→Open can run during AppleDisplay construction.
        if (os.environ.get("RFT_INJECT_MATCHED_INTO_INFO") == "1" and
                x26 and LAST_MATCHED_SVC and not svc):
            err = lldb.SBError()
            process.WriteMemory(
                x26 + 0x14, int(LAST_MATCHED_SVC).to_bytes(4, "little"), err)
            if err.Success():
                svc = LAST_MATCHED_SVC
                injected = True
        if (os.environ.get("RFT_FORCE_IOMFB_GET_DISPLAY") == "1" and
                (typ == 2 or w8 == 2) and svc):
            r = frame.FindRegister("w8")
            if r.IsValid():
                r.SetValueFromCString("0")
                forced = True
        print("iomfb_open_by_name_type progname=%s info=%s entry_index=%s type=%s w8=%s svc+0x14=%s "
              "injected=%s force_get_display=%s" % (
            _progname(process),
            hex(x26) if x26 else None,
            _iomfb_name_registry_index(x26),
            hex(typ) if typ is not None else None,
            hex(w8) if w8 is not None else None,
            hex(svc) if svc is not None else None,
            injected, forced,
        ), flush=True)
    if label == "IOMFB_GET_DISPLAY":
        # _iomfb_get_display(info, &out): Open(service=info+0x14, ...).
        info = x0
        typ = _u32(process, info + 0x8) if info else None
        svc = _u32(process, info + 0x14) if info else None
        print("iomfb_get_display info=%s type=%s svc=%s outp=%s" % (
            hex(info) if info else None,
            hex(typ) if typ is not None else None,
            hex(svc) if svc is not None else None,
            hex(x1) if x1 else None,
        ), flush=True)
    if label == "APPLEDISPLAY_POST_OPEN":
        # After ldr x23,[sp,#0x28]; x23 is the opened IOMFB (or null).
        obj = _reg(frame, "x23")
        conn = _u32(process, obj + 0x14) if obj else None
        print("appledisplay_post_open obj=%s conn+0x14=%s" % (
            hex(obj) if obj else None,
            hex(conn) if conn is not None else None,
        ), flush=True)
    if label == "IOSERVICE_OPEN":
        # Only log when out-connect pointer looks like obj+0x14 (iomfb open)
        # or early samples. Always arm return for first N to collect connects.
        out_conn = _reg(frame, "x3")
        if x1:
            LAST_TASK_PORT = x1
        interesting = (out_conn and (out_conn & 0xfff) == 0x14)
        if hit <= 16 or interesting:
            print("ioservice_open svc=%s task=%s type=%s &conn=%s" % (
                hex(x0) if x0 else None,
                hex(x1) if x1 is not None else None,
                hex(x2) if x2 else None,
                hex(out_conn) if out_conn else None,
            ), flush=True)
            if lr and hit <= 24:
                rbp = target.BreakpointCreateByAddress(lr)
                CONFIG[rbp.GetID()] = {
                    "label": "IOSERVICE_OPEN_RETURN", "limit": 1,
                    "out_conn": out_conn, "svc": x0,
                }
                HITS["IOSERVICE_OPEN_RETURN"] = 0
                interpreter = target.GetDebugger().GetCommandInterpreter()
                result = lldb.SBCommandReturnObject()
                interpreter.HandleCommand(
                    "breakpoint command add -F render_gate_callbacks.on_break %d" %
                    rbp.GetID(), result)
    if label == "IOSERVICE_OPEN_RETURN":
        out_conn = cfg.get("out_conn")
        svc = cfg.get("svc")
        conn = _u32(process, out_conn) if out_conn else None
        prog = _progname(process)
        # GATE117/118: only keep connects from the swap process (backboardd).
        kept = False
        if conn and prog == "backboardd":
            if conn not in LAST_CONNS:
                LAST_CONNS.append(conn)
            # Prefer later connects unless an index is forced at poke time.
            LAST_GOOD_CONN = conn
            kept = True
        print("ioservice_open_return w0=%s svc=%s conn=%s &conn=%s "
              "prog=%s kept=%s nconns=%d" % (
            hex(x0) if x0 is not None else None,
            hex(svc) if svc else None,
            hex(conn) if conn is not None else None,
            hex(out_conn) if out_conn else None,
            prog, kept, len(LAST_CONNS),
        ), flush=True)
    if label == "IOSERVICE_GET_MATCHING":
        # x0=master, x1=matching CFDictionary. Arm return to capture service.
        # Best-effort: scan nearby memory for a class-name hint.
        hint = None
        if x1:
            err = lldb.SBError()
            raw = process.ReadMemory(x1, 0x200, err)
            if err.Success() and raw:
                for key in (b"IOMobileFramebuffer", b"AppleCLCD", b"AppleDCP",
                            b"IODisplay", b"backlight", b"AGX"):
                    if key in raw:
                        hint = key.decode()
                        break
                if not hint:
                    # CFString payloads often sit behind an indirection; scan
                    # a few pointers from the dict object header.
                    for off in range(0, 0x80, 8):
                        p = int.from_bytes(raw[off:off+8], "little")
                        if not p or p < 0x100000000:
                            continue
                        err2 = lldb.SBError()
                        blob = process.ReadMemory(p & 0x0000ffffffffffff, 64, err2)
                        if not err2.Success() or not blob:
                            continue
                        for key in (b"IOMobileFramebuffer", b"AppleCLCD",
                                    b"AppleDCP", b"IODisplay", b"CLCD"):
                            if key in blob:
                                hint = key.decode()
                                break
                        if hint:
                            break
        if lr and hit <= 32:
            rbp = target.BreakpointCreateByAddress(lr)
            CONFIG[rbp.GetID()] = {
                "label": "IOSERVICE_GET_MATCHING_RETURN", "limit": 1,
                "hint": hint,
            }
            HITS["IOSERVICE_GET_MATCHING_RETURN"] = 0
            interpreter = target.GetDebugger().GetCommandInterpreter()
            result = lldb.SBCommandReturnObject()
            interpreter.HandleCommand(
                "breakpoint command add -F render_gate_callbacks.on_break %d" %
                rbp.GetID(), result)
        if hit <= 16 or hint:
            print("ioservice_get_matching master=%s matching=%s hint=%s" % (
                hex(x0) if x0 is not None else None,
                hex(x1) if x1 else None,
                hint,
            ), flush=True)
    if label == "IOSERVICE_GET_MATCHING_RETURN":
        # x0 = io_service_t (or 0).
        prog = _progname(process)
        hint = cfg.get("hint")
        print("ioservice_get_matching_return svc=%s prog=%s hint=%s" % (
            hex(x0) if x0 else None, prog, hint), flush=True)
        import os
        if (os.environ.get("RFT_OPEN_MATCHED_SVC") == "1" and x0 and
                prog == "backboardd"):
            # Prefer display-related matches when the hint is visible.
            if hint and any(k in hint for k in (
                    "IOMobile", "CLCD", "DCP", "IODisplay")):
                LAST_MATCHED_SVC = x0
                print("ioservice_matched_svc_preferred svc=%s hint=%s" % (
                    hex(x0), hint), flush=True)
            elif not LAST_MATCHED_SVC:
                LAST_MATCHED_SVC = x0
                print("ioservice_matched_svc_candidate svc=%s prog=%s hint=%s" % (
                    hex(x0), prog, hint), flush=True)
    if label == "KERN_SWAP_BEGIN":
        # Real Begin: IOConnectCallScalarMethod([x0+0x14], sel=4); any
        # non-zero maps to 0xe00002bc (0x22a391224).
        conn = _u32(process, x0 + 0x14) if x0 else None
        if lr and hit <= 8:
            rbp = target.BreakpointCreateByAddress(lr)
            CONFIG[rbp.GetID()] = {"label": "KERN_SWAP_BEGIN_RETURN", "limit": 1}
            HITS["KERN_SWAP_BEGIN_RETURN"] = 0
            RUNTIME_ADDR["KERN_SWAP_BEGIN_RETURN"] = lr
            interpreter = target.GetDebugger().GetCommandInterpreter()
            result = lldb.SBCommandReturnObject()
            interpreter.HandleCommand(
                "breakpoint command add -F render_gate_callbacks.on_break %d" %
                rbp.GetID(), result)
        print("kern_swap_begin x0=%s x1=%s x2=%s conn+0x14=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 else None,
            hex(conn) if conn is not None else None,
        ), flush=True)
    if label == "KERN_SWAP_BEGIN_RETURN":
        print("kern_swap_begin_return w0=%s" % (
            hex(x0) if x0 is not None else None), flush=True)
    if label in ("IOCONNECT_SCALAR_STUB", "IOCONNECT_STRUCT_STUB"):
        # w0=io_connect_t, w1=selector. SwapBegin uses scalar sel4; SwapEnd
        # struct sel5. Filter noise: only log sel 4/5 or first few hits.
        sel = x1
        if sel in (4, 5, 0xf, 0x17) or hit <= 4:
            scalar_input = _reg(frame, "x2") if label == "IOCONNECT_SCALAR_STUB" else None
            scalar_count = _reg(frame, "x3") if label == "IOCONNECT_SCALAR_STUB" else None
            print("ioconnect_%s conn=%s sel=%s lr=%s input=%s count=%s values=%s" % (
                "scalar" if label.endswith("SCALAR_STUB") else "struct",
                hex(x0) if x0 is not None else None,
                hex(sel) if sel is not None else None,
                hex(lr) if lr else None,
                hex(scalar_input) if scalar_input else None,
                scalar_count,
                [hex(_u64(process, scalar_input + i * 8)) if _u64(process, scalar_input + i * 8) is not None else None
                 for i in range(min(scalar_count, 4))]
                if scalar_input and scalar_count is not None and 0 <= scalar_count <= 4 else None,
            ), flush=True)
    if label == "KERN_SWAP_END":
        # Real End: IOConnectCallStructMethod([x0+0x14], sel=5, +0x18, 0x6e0).
        # 0x10000003 = MACH_SEND_INVALID_DEST (bad port).
        buf = None
        conn = _u32(process, x0 + 0x14) if x0 else None
        if x0:
            err = lldb.SBError()
            raw = process.ReadMemory(x0 + 0x18, 64, err)
            if err.Success() and raw:
                buf = raw.hex()
        if lr and hit <= 8:
            rbp = target.BreakpointCreateByAddress(lr)
            CONFIG[rbp.GetID()] = {"label": "KERN_SWAP_END_RETURN", "limit": 1}
            HITS["KERN_SWAP_END_RETURN"] = 0
            interpreter = target.GetDebugger().GetCommandInterpreter()
            result = lldb.SBCommandReturnObject()
            interpreter.HandleCommand(
                "breakpoint command add -F render_gate_callbacks.on_break %d" %
                rbp.GetID(), result)
        print("kern_swap_end x0=%s x1=%s conn+0x14=%s buf+0x18[0:64]=%s "
              "(IOConnect sel=5)" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 is not None else None,
            hex(conn) if conn is not None else None,
            buf,
        ), flush=True)
    if label == "KERN_SWAP_END_RETURN":
        print("kern_swap_end_return w0=%s" % (
            hex(x0) if x0 is not None else None), flush=True)
    if label == "KERN_SWAP_END_THUNK":
        # Public SwapEnd: ldr x1,[x0,#0xc28]; braaz x1 (NOT +0xc60 = Wait).
        import os
        slot = _u64(process, x0 + 0xc28) if x0 else None
        conn = _u32(process, x0 + 0x14) if x0 else None
        redirected = False
        end_rt = RUNTIME_ADDR.get("KERN_SWAP_END")
        if ((os.environ.get("RFT_FORCE_KERN_SWAP_END") == "1" or
             os.environ.get("RFT_FORCE_KERN_PATH") == "1") and x0 and end_rt):
            err = lldb.SBError()
            process.WriteMemory(x0 + 0xc28, int(end_rt).to_bytes(8, "little"), err)
            redirected = err.Success()
            slot = _u64(process, x0 + 0xc28)
        print("kern_swap_end_thunk x0=%s +0xc28=%s conn=%s will_fail=%s "
              "redirected=%s" % (
            hex(x0) if x0 else None,
            hex(slot) if slot else None,
            hex(conn) if conn is not None else None,
            (not x0) or (not slot),
            redirected,
        ), flush=True)
    if label == "KERN_SWAP_WAIT_THUNK":
        slot = _u64(process, x0 + 0xc60) if x0 else None
        print("kern_swap_wait_thunk x0=%s +0xc60=%s" % (
            hex(x0) if x0 else None, hex(slot) if slot else None), flush=True)
    if label in ("VIRT_SWAP_BEGIN", "VIRT_SWAP_WAIT", "VIRT_SWAP_END"):
        print("%s x0=%s" % (label.lower(), hex(x0) if x0 else None), flush=True)
    if label == "FINISH_SWAP_X22":
        x22 = _reg(frame, "x22")
        print("finish_swap_x22 x22=%s last_alloc=%s" % (
            hex(x22) if x22 else None,
            hex(LAST_ALLOC_SURFACE) if LAST_ALLOC_SURFACE else None,
        ), flush=True)
    if label == "FINISH_SWAP_X22_RET":
        import os
        # Invoke public SwapEnd (+0xc28 → _kern_/_virt_SwapEnd). GATE112 fix:
        # earlier forced Wait's +0xc60 by mistake.
        # GATE113: if kern path + null connect, poke LAST_GOOD_CONN from Open.
        forced = False
        conn_poked = False
        if (os.environ.get("RFT_FORCE_POST_X22_END") == "1" and LAST_IOMFB and
                HITS.get("FINISH_SWAP_X22", 0) > 0):
            end_thunk = RUNTIME_ADDR.get("KERN_SWAP_END_THUNK")
            end_rt = RUNTIME_ADDR.get("KERN_SWAP_END")
            cont = RUNTIME_ADDR.get("FINISH_SWAP_X22_RET")
            if end_thunk and cont:
                conn = _u32(process, LAST_IOMFB + 0x14)
                poke_src = None
                if (os.environ.get("RFT_POKE_OPEN_CONN") == "1" and not conn):
                    poke_src = LAST_GOOD_CONN
                    if (not poke_src and LAST_CONNS and
                            os.environ.get("RFT_POKE_ANY_CONN") == "1"):
                        poke_src = LAST_CONNS[0]
                if poke_src:
                    err = lldb.SBError()
                    process.WriteMemory(
                        LAST_IOMFB + 0x14,
                        int(poke_src).to_bytes(4, "little"), err)
                    conn_poked = err.Success()
                if end_rt and (os.environ.get("RFT_FORCE_KERN_SWAP_END") == "1" or
                               os.environ.get("RFT_FORCE_KERN_PATH") == "1"):
                    err = lldb.SBError()
                    process.WriteMemory(
                        LAST_IOMFB + 0xc28,
                        int(end_rt).to_bytes(8, "little"), err)
                nxt = cont + 4
                for reg, val in (("x0", LAST_IOMFB), ("x1", 0), ("x2", 0),
                                 ("lr", nxt), ("pc", end_thunk)):
                    r = frame.FindRegister(reg)
                    if r.IsValid():
                        r.SetValueFromCString(hex(val))
                forced = True
        c28 = _u64(process, LAST_IOMFB + 0xc28) if LAST_IOMFB else None
        conn = _u32(process, LAST_IOMFB + 0x14) if LAST_IOMFB else None
        print("finish_swap_x22_ret iomfb=%s forced_end=%s +0xc28=%s conn=%s "
              "conn_poked=%s last_good_conn=%s nconns=%d" % (
            hex(LAST_IOMFB) if LAST_IOMFB else None, forced,
            hex(c28) if c28 else None,
            hex(conn) if conn is not None else None,
            conn_poked,
            hex(LAST_GOOD_CONN) if LAST_GOOD_CONN else None,
            len(LAST_CONNS)), flush=True)
    if label == "SWAP_ASYNC_CB":
        print("swap_async_cb x0=%s" % (hex(x0) if x0 else None), flush=True)
    if label == "SWAP_ASYNC_WORK":
        b8 = _u8(process, x0 + 8) if x0 else None
        print("swap_async_work x0=%s +8=%s (1=queue,else=begin_thunk)" % (
            hex(x0) if x0 else None, b8), flush=True)
    if label == "SWAP_TRY_BEGIN_ASYNC":
        print("swap_try_begin x0=%s x1=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 is not None else None,
        ), flush=True)
    if label == "KERN_SWAP_SET_LAYER":
        print("kern_swap_set_layer x0=%s x1=%s x2=%s x3=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 is not None else None,
            hex(x2) if x2 else None,
            hex(x3) if x3 else None,
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
    if label == "PREPARE_LAYER_CONTEXT_FLAGS_CALL":
        out_ref = _reg(frame, "x4")
        low = _u64(process, out_ref) if out_ref else None
        high = _u64(process, out_ref + 8) if out_ref else None
        print("prepare_layer_context_flags_call x0=%s x1=%s node=%s shapes=%s "
              "out_ref=%s before_low=%s before_high=%s lr_static=%s" % (
            hex(_reg(frame, "x0")) if _reg(frame, "x0") is not None else None,
            hex(_reg(frame, "x1")) if _reg(frame, "x1") is not None else None,
            hex(_reg(frame, "x2")) if _reg(frame, "x2") is not None else None,
            hex(_reg(frame, "x3")) if _reg(frame, "x3") is not None else None,
            hex(out_ref) if out_ref else None,
            hex(low) if low is not None else None,
            hex(high) if high is not None else None,
            hex(_xpac(_reg(frame, "lr")) - DSC_SLIDE)
            if _reg(frame, "lr") else None,
        ), flush=True)
    if label == "PREPARE_LAYER_CONTEXT_FLAGS_RETURN":
        sp = _reg(frame, "sp")
        low = _u64(process, sp + 0xf0) if sp is not None else None
        high = _u64(process, sp + 0xf8) if sp is not None else None
        print("prepare_layer_context_flags_return sp=%s low=%s high=%s "
              "has_28000=%s bit18=%s bit22=%s bit23=%s bit40=%s "
              "update=%s context=%s layer=%s" % (
            hex(sp) if sp is not None else None,
            hex(low) if low is not None else None,
            hex(high) if high is not None else None,
            bool(low & 0x28000) if low is not None else None,
            bool(low & (1 << 18)) if low is not None else None,
            bool(low & (1 << 22)) if low is not None else None,
            bool(low & (1 << 23)) if low is not None else None,
            bool(low & (1 << 40)) if low is not None else None,
            hex(_reg(frame, "x21")) if _reg(frame, "x21") else None,
            hex(_reg(frame, "x20")) if _reg(frame, "x20") else None,
            hex(_reg(frame, "x24")) if _reg(frame, "x24") else None,
        ), flush=True)
    if label == "PREPARE_LAYER_MASK_INPUTS":
        import os
        global LAYER_STATE70_WATCH
        state = _reg(frame, "x22")
        local = _reg(frame, "x26")
        state70 = _reg(frame, "x10")
        print("prepare_layer_mask_inputs global=%s local=%s node=%s state=%s "
              "state70=%s state78=%s local40=%s local60=%s out_ref=%s" % (
            hex(_reg(frame, "x0")) if _reg(frame, "x0") else None,
            hex(local) if local else None,
            hex(_reg(frame, "x23")) if _reg(frame, "x23") else None,
            hex(state) if state else None,
            hex(state70) if state70 is not None else None,
            hex(_reg(frame, "x28")) if _reg(frame, "x28") is not None else None,
            hex(_reg(frame, "x11")) if _reg(frame, "x11") is not None else None,
            hex(_u64(process, local + 0x60)) if local else None,
            hex(_reg(frame, "x24")) if _reg(frame, "x24") else None,
        ), flush=True)
        if (os.environ.get("RFT_WATCH_LAYER_STATE70") == "1" and state and
                state70 is not None and (state70 & (1 << 23)) and
                LAYER_STATE70_WATCH is None):
            target = process.GetTarget()
            watch_addr = state + 0x70
            watch_err = lldb.SBError()
            watchpoint = target.WatchAddress(watch_addr, 8, False, True, watch_err)
            if watch_err.Success() and watchpoint and watchpoint.IsValid():
                result = lldb.SBCommandReturnObject()
                lldb.debugger.GetCommandInterpreter().HandleCommand(
                    "watchpoint command add -F render_gate_callbacks.on_layer_state70_watch %d" %
                    watchpoint.GetID(), result)
                if result.Succeeded():
                    LAYER_STATE70_WATCH = (watch_addr, watchpoint.GetID())
                    print("LAYER_STATE70_WATCH_READY id=%d address=0x%x value=%s" % (
                        watchpoint.GetID(), watch_addr, hex(state70)), flush=True)
                else:
                    target.DeleteWatchpoint(watchpoint.GetID())
                    print("LAYER_STATE70_WATCH_FAILED address=0x%x error=%s" % (
                        watch_addr, result.GetError() or "command failed"), flush=True)
            else:
                print("LAYER_STATE70_WATCH_FAILED address=0x%x error=%s" % (
                    watch_addr, watch_err.GetCString()), flush=True)
    if label == "PREPARE_LAYER_FLAGS_MASK":
        x8 = _reg(frame, "x8")
        mask = _reg(frame, "x10")
        frame_base = _reg(frame, "x19")
        print("prepare_layer_flags_mask raw=%s mask=%s raw_bit22=%s "
              "mask_bit22=%s shifted=%s out_ref=%s" % (
            hex(x8) if x8 is not None else None,
            hex(mask) if mask is not None else None,
            bool(x8 & (1 << 22)) if x8 is not None else None,
            bool(mask & (1 << 22)) if mask is not None else None,
            hex(_reg(frame, "x9")) if _reg(frame, "x9") is not None else None,
            hex(_u64(process, frame_base + 0x1d0)) if frame_base else None,
        ), flush=True)
    if label == "PREPARE_LAYER_FLAGS_WRITE":
        out_ref = _reg(frame, "x11")
        before = _u64(process, out_ref) if out_ref else None
        final = _reg(frame, "x8")
        print("prepare_layer_flags_write out_ref=%s before=%s final=%s "
              "mapped_high=%s retained_input=%s final_bit22=%s "
              "local_state=%s" % (
            hex(out_ref) if out_ref else None,
            hex(before) if before is not None else None,
            hex(final) if final is not None else None,
            hex(_reg(frame, "x9")) if _reg(frame, "x9") is not None else None,
            hex(_reg(frame, "x10")) if _reg(frame, "x10") is not None else None,
            bool(final & (1 << 22)) if final is not None else None,
            hex(_reg(frame, "x20")) if _reg(frame, "x20") else None,
        ), flush=True)
    if label == "PREPARE_LAYER_STATE_FLAGS_MERGE_IN":
        target_state = _reg(frame, "x20")
        print("prepare_layer_state_flags_merge_in state=%s old70=%s old78=%s "
              "add_low=%s add_high=%s" % (
            hex(target_state) if target_state else None,
            hex(_u64(process, target_state + 0x70)) if target_state else None,
            hex(_u64(process, target_state + 0x78)) if target_state else None,
            hex(_reg(frame, "x8")) if _reg(frame, "x8") is not None else None,
            hex(_reg(frame, "x9")) if _reg(frame, "x9") is not None else None,
        ), flush=True)
    if label == "PREPARE_LAYER_STATE_FLAGS_MERGE_OUT":
        target_state = _reg(frame, "x20")
        old_low = _u64(process, target_state + 0x70) if target_state else None
        old_high = _u64(process, target_state + 0x78) if target_state else None
        print("prepare_layer_state_flags_merge_out state=%s old70=%s old78=%s "
              "new70=%s new78=%s source_low=%s source_high=%s" % (
            hex(target_state) if target_state else None,
            hex(old_low) if old_low is not None else None,
            hex(old_high) if old_high is not None else None,
            hex(_reg(frame, "x10")) if _reg(frame, "x10") is not None else None,
            hex(_reg(frame, "x9")) if _reg(frame, "x9") is not None else None,
            hex(_reg(frame, "x8")) if _reg(frame, "x8") is not None else None,
            hex(_reg(frame, "x11")) if _reg(frame, "x11") is not None else None,
        ), flush=True)
    if label == "LAYER_STATE_RESET_ENTRY":
        reset_state = _reg(frame, "x1")
        lr_static = _xpac(lr) - DSC_SLIDE if lr else None
        print("layer_state_reset_entry owner=%s state=%s type_now=%s type_arg=%s "
              "arg2=%s flags70=%s flags78=%s pair80=%s pair88=%s clear90=%s "
              "caller_static=%s" % (
            hex(x0) if x0 else None,
            hex(reset_state) if reset_state else None,
            hex(_u32(process, reset_state + 0x60)) if reset_state else None,
            hex(_reg(frame, "x3")) if _reg(frame, "x3") is not None else None,
            hex(x2) if x2 else None,
            hex(_u64(process, reset_state + 0x70)) if reset_state else None,
            hex(_u64(process, reset_state + 0x78)) if reset_state else None,
            hex(_u64(process, reset_state + 0x80)) if reset_state else None,
            hex(_u64(process, reset_state + 0x88)) if reset_state else None,
            hex(_u64(process, reset_state + 0x90)) if reset_state else None,
            hex(lr_static) if lr_static else None,
        ), flush=True)
    if label == "LAYER_STATE_RESET_CLEAR":
        reset_state = x19
        lr_static = _xpac(lr) - DSC_SLIDE if lr else None
        print("layer_state_reset_clear owner=%s state=%s type_arg=%s "
              "flags70=%s flags78=%s pair80=%s pair88=%s clear90=%s "
              "watch_candidate=%s caller_static=%s" % (
            hex(_reg(frame, "x21")) if _reg(frame, "x21") else None,
            hex(reset_state) if reset_state else None,
            hex(_reg(frame, "x20")) if _reg(frame, "x20") is not None else None,
            hex(_u64(process, reset_state + 0x70)) if reset_state else None,
            hex(_u64(process, reset_state + 0x78)) if reset_state else None,
            hex(_u64(process, reset_state + 0x80)) if reset_state else None,
            hex(_u64(process, reset_state + 0x88)) if reset_state else None,
            hex(_u64(process, reset_state + 0x90)) if reset_state else None,
            hex(reset_state + 0x90) if reset_state else None,
            hex(lr_static) if lr_static else None,
        ), flush=True)
    if label.startswith("UPDATE_FLAGS_STORE_"):
        # All these PCs are the store instruction itself. Log the value about
        # to be written and the pre-store slot so the conditional producers of
        # bit 22 can be distinguished without changing guest state.
        source_reg = {
            "UPDATE_FLAGS_STORE_433880": "x8",
            "UPDATE_FLAGS_STORE_433A70": "x9",
            "UPDATE_FLAGS_STORE_433A8C": "x9",
            "UPDATE_FLAGS_STORE_4343E8": "x8",
            "UPDATE_FLAGS_STORE_434538": "x9",
            "UPDATE_FLAGS_STORE_434794": "x8",
            "UPDATE_FLAGS_STORE_4347C0": "x8",
            "UPDATE_FLAGS_STORE_4348D8": "x8",
            "UPDATE_FLAGS_STORE_434904": "x10",
            "UPDATE_FLAGS_STORE_434958": "x8",
            "UPDATE_FLAGS_STORE_434AD8": "x8",
            "UPDATE_FLAGS_STORE_434BE0": "x8",
            "UPDATE_FLAGS_STORE_435020": "x8",
            "UPDATE_FLAGS_STORE_4350DC": "x9",
            "UPDATE_FLAGS_STORE_43537C": "x9",
            "UPDATE_FLAGS_STORE_4355CC": "x26",
            "UPDATE_FLAGS_STORE_4355E8": "x8",
            "UPDATE_FLAGS_STORE_4357C0": "x8",
            "UPDATE_FLAGS_STORE_435E6C": "x22",
            "UPDATE_FLAGS_STORE_435E80": "x22",
            "UPDATE_FLAGS_STORE_435EAC": "x8",
            "UPDATE_FLAGS_STORE_435F00": "x8",
        }[label]
        new_flags = _reg(frame, source_reg)
        old_flags = _u64(process, x19 + 0x5d0) if x19 else None
        lr_static = _xpac(lr) - DSC_SLIDE if lr else None
        print("update_context_flags_store label=%s object=%s slot=%s "
              "source_reg=%s old=%s new=%s old_bit22=%s new_bit22=%s "
              "new_bit23=%s new_bit29=%s flags5d8=%s source_regs="
              "x8:%s,x9:%s,x10:%s,x13:%s,x20:%s,x22:%s,x26:%s "
              "lr_static=%s" % (
            label,
            hex(x19) if x19 else None,
            hex(x19 + 0x5d0) if x19 else None,
            source_reg,
            hex(old_flags) if old_flags is not None else None,
            hex(new_flags) if new_flags is not None else None,
            bool(old_flags & (1 << 22)) if old_flags is not None else None,
            bool(new_flags & (1 << 22)) if new_flags is not None else None,
            bool(new_flags & (1 << 23)) if new_flags is not None else None,
            bool(new_flags & (1 << 29)) if new_flags is not None else None,
            hex(_u64(process, x19 + 0x5d8)) if x19 else None,
            *(hex(_reg(frame, r)) if _reg(frame, r) is not None else None
              for r in ("x8", "x9", "x10", "x13", "x20", "x22", "x26")),
            hex(lr_static) if lr_static else None,
        ), flush=True)
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
    if label == "PREPARE_LAYER0_ENTRY":
        import struct
        node = x1
        layer = x2
        def _entry_f32(addr):
            err = lldb.SBError()
            raw = process.ReadMemory(addr, 4, err)
            if not err.Success() or not raw:
                return None
            import struct
            return struct.unpack('<f', raw)[0]
        def _entry_f64(addr):
            err = lldb.SBError()
            raw = process.ReadMemory(addr, 8, err)
            return struct.unpack('<d', raw)[0] if err.Success() and raw else None
        node_bounds = tuple(_entry_f32(node + off) for off in
                            (0x10, 0x14, 0x18, 0x1c)) if node else None
        layer_rgba = tuple(_entry_f32(layer + off) for off in
                           (0x10, 0x14, 0x18, 0x1c)) if layer else None
        local_bounds = tuple(_entry_f32(x3 + off) for off in
                             (0x10, 0x14, 0x18, 0x1c)) if x3 else None
        layer_state = _u64(process, layer + 0x88) if layer else None
        print("prepare_layer0_entry global=%s node=%s node_type=%s "
              "node_bounds=%s layer=%s layer_type=%s layer_vt=%s "
              "layer_rgba=%s layer_flags30=%s layer_position38=%s "
              "layer_anchor48=%s layer_size58=%s contents68=%s field70=%s "
              "mask78=%s layer_state=%s layer_rect_bytes=%s "
              "local=%s local_bounds=%s" % (
            hex(x0) if x0 else None,
            hex(node) if node else None,
            _u8(process, node + 0xc) if node else None,
            node_bounds,
            hex(layer) if layer else None,
            _u8(process, layer + 0xc) if layer else None,
            hex(_u64(process, layer)) if layer and _u64(process, layer) else None,
            layer_rgba,
            hex(_u32(process, layer + 0x30)) if layer else None,
            tuple(_entry_f64(layer + off) for off in (0x38, 0x40)) if layer else None,
            tuple(_entry_f64(layer + off) for off in (0x48, 0x50)) if layer else None,
            tuple(_entry_f64(layer + off) for off in (0x58, 0x60)) if layer else None,
            hex(_u64(process, layer + 0x68)) if layer and _u64(process, layer + 0x68) else None,
            hex(_u64(process, layer + 0x70)) if layer and _u64(process, layer + 0x70) else None,
            hex(_u64(process, layer + 0x78)) if layer and _u64(process, layer + 0x78) else None,
            hex(layer_state) if layer_state else None,
            process.ReadMemory(layer + 0x50, 16, lldb.SBError()).hex()
            if layer else None,
            hex(x3) if x3 else None,
            local_bounds,
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
    if label in ("MUT_W25_ALPHA_CLASSIFY_RESULT",
                 "MUT_W25_ALPHA_ACCUM_RESULT",
                 "MUT_W25_VCALL_ACCUM_RESULT"):
        import struct
        rgba_obj = _reg(frame, "x1")
        update_ctx = _reg(frame, "x19")
        def _rgba_f32(off):
            if not rgba_obj:
                return None
            err = lldb.SBError()
            raw = process.ReadMemory(rgba_obj + off, 4, err)
            return struct.unpack("<f", raw)[0] if err.Success() and raw else None
        rgba = tuple(_rgba_f32(off) for off in (0x10, 0x14, 0x18, 0x1c))
        layer_aux = _u64(process, rgba_obj + 0x88) if rgba_obj else None
        aux_flags = _u64(process, layer_aux + 0x50) if layer_aux else None
        accum = _reg(frame, "w25")
        print("mut_w25_producer label=%s update_ctx=%s rgba_obj=%s rgba=%s "
              "rgba_aux=%s rgba_aux50=%s w25=%s w9=%s w11=%s w20=%s "
              "w23=%s w24=%s w0=%s x8=%s x10=%s lr_static=%s" % (
            label,
            hex(update_ctx) if update_ctx else None,
            hex(rgba_obj) if rgba_obj else None,
            rgba,
            hex(layer_aux) if layer_aux else None,
            hex(aux_flags) if aux_flags is not None else None,
            hex(accum) if accum is not None else None,
            hex(_reg(frame, "w9")) if _reg(frame, "w9") is not None else None,
            hex(_reg(frame, "w11")) if _reg(frame, "w11") is not None else None,
            hex(_reg(frame, "w20")) if _reg(frame, "w20") is not None else None,
            hex(_reg(frame, "w23")) if _reg(frame, "w23") is not None else None,
            hex(_reg(frame, "w24")) if _reg(frame, "w24") is not None else None,
            hex(_reg(frame, "w0")) if _reg(frame, "w0") is not None else None,
            hex(_reg(frame, "x8")) if _reg(frame, "x8") else None,
            hex(_reg(frame, "x10")) if _reg(frame, "x10") else None,
            hex(_xpac(lr) - DSC_SLIDE) if lr else None,
        ), flush=True)
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
        import struct as _struct
        rgba10 = (s2, s3, s1, s0)
        def _layer_f64(off):
            if not x1:
                return None
            err = lldb.SBError()
            raw = process.ReadMemory(x1 + off, 8, err)
            return _struct.unpack('<d', raw)[0] if err.Success() and raw else None
        print("mut_layer_fields x1=%s rgba10=%s flags30=%s position38=%s "
              "anchor48=%s size58=%s contents68=%s field70=%s mask78=%s "
              "ext88=%s" % (
            hex(x1) if x1 else None,
            rgba10,
            hex(_u32(process, x1 + 0x30)) if x1 else None,
            tuple(_layer_f64(off) for off in (0x38, 0x40)) if x1 else None,
            tuple(_layer_f64(off) for off in (0x48, 0x50)) if x1 else None,
            tuple(_layer_f64(off) for off in (0x58, 0x60)) if x1 else None,
            hex(_u64(process, x1 + 0x68)) if x1 and _u64(process, x1 + 0x68) else None,
            hex(_u64(process, x1 + 0x70)) if x1 and _u64(process, x1 + 0x70) else None,
            hex(_u64(process, x1 + 0x78)) if x1 and _u64(process, x1 + 0x78) else None,
            hex(x8) if x8 else None,
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
        # Historical misnamed intervention: x1+0x10 is CALayer backgroundColor
        # RGBA, not geometry. Keep only for reproducing old forced-color runs.
        # GATE93: NEW_SERVER never hits and +0xb8 is 0 on every display we see,
        # so fall back to A453 constants when DISPLAY_WH_OBJ is unset.
        if (os.environ.get("RFT_BOUNDS_FROM_DISPLAY") == "1" and x1 and
                (s2 is None or abs(s2) < 1e-6)):
            dw = dh = None
            src = None
            if DISPLAY_WH_OBJ:
                dw = _u32(process, DISPLAY_WH_OBJ + 0xb8)
                dh = _u32(process, DISPLAY_WH_OBJ + 0xbc)
                src = DISPLAY_WH_OBJ
            if not dw or not dh:
                dw, dh = 1179, 2556
                src = 0
            err = lldb.SBError()
            process.WriteMemory(
                x1 + 0x10,
                struct.pack('<ffff', 0.0, 0.0, float(dw), float(dh)),
                err)
            reg = frame.FindRegister("x9")
            ok = reg.SetValueFromCString("0") if reg.IsValid() else False
            print("rft_bounds_from_display (legacy writes RGBA) src=%s wh=(%s,%s) mem_ok=%s w9_ok=%s" % (
                hex(src) if src else "A453", dw, dh, err.Success(), ok),
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
    if label == "DISPLAY_UPDATE_FRAMEBUFFER_ENTRY":
        display = x0
        print("display_update_framebuffer_entry progname=%s display=%s reason=%s signal_type=%s "
              "mode2b8=%s mode2c0=%s flags3b8=%s info6490=%s modes6b10=%s" % (
            _progname(process),
            hex(display) if display else None,
            hex(x1) if x1 is not None else None,
            _u8(process, display + 0x2b4) if display else None,
            hex(_u64(process, display + 0x2b8)) if display and _u64(process, display + 0x2b8) is not None else None,
            hex(_u64(process, display + 0x2c0)) if display and _u64(process, display + 0x2c0) is not None else None,
            hex(_u32(process, display + 0x3b8)) if display and _u32(process, display + 0x3b8) is not None else None,
            hex(_u64(process, display + 0x6490)) if display and _u64(process, display + 0x6490) is not None else None,
            hex(_u64(process, display + 0x6b10)) if display and _u64(process, display + 0x6b10) is not None else None,
        ), flush=True)
    if label == "DISPLAY_MODE_UPDATE_FLAGS":
        display = _reg(frame, "x19")
        flags = _reg(frame, "x27")
        print("display_mode_update_flags display=%s signal_type=%s update_flags=%s bit1=%s "
              "mode2b8=%s state28_3=%s" % (
            hex(display) if display else None,
            _u8(process, display + 0x2b4) if display else None,
            hex(flags) if flags is not None else None,
            (flags >> 1) & 1 if flags is not None else None,
            hex(_u64(process, display + 0x2b8)) if display and _u64(process, display + 0x2b8) is not None else None,
            _u8(process, _reg(frame, "x28") + 3) if _reg(frame, "x28") else None,
        ), flush=True)
    if label in ("DISPLAY_MODE_SIGNAL_TYPE2_BRANCH", "DISPLAY_MODE_SIGNAL_TYPE1_BRANCH"):
        display = _reg(frame, "x19")
        print("display_mode_signal_branch label=%s display=%s signal_type=%s w8=%s" % (
            label,
            hex(display) if display else None,
            _u8(process, display + 0x2b4) if display else None,
            hex(_reg(frame, "x8")) if _reg(frame, "x8") is not None else None,
        ), flush=True)
    if label in ("DISPLAY_MODE_ARRAY_RETURN", "DISPLAY_MODE_ARRAY_RETURN_TYPE2"):
        display = _reg(frame, "x19")
        sp = _reg(frame, "sp")
        fp = _reg(frame, "x29")
        info = _u64(process, display + 0x6490) if display else None
        dispatch = _u64(process, info + 0xd48) if info else None
        dispatch_static = _xpac(dispatch) - DSC_SLIDE if dispatch else None
        array = _u64(process, sp + 0x1c0) if sp else None
        mode_ref = _u64(process, fp - 0xc0) if fp else None
        mode_value = _u64(process, mode_ref) if mode_ref else None
        array_head = _blob_hex(process, array, 0x50) if array else None
        timing_head = _blob_hex(process, mode_ref, 0x50) if mode_ref else None
        print("display_mode_array_return label=%s progname=%s display=%s signal_type=%s status=%s "
              "info=%s dispatch=%s dispatch_static=%s array_slot=%s array=%s "
              "array_head=%s mode_ref_slot=%s mode_ref=%s mode_value=%s timing_head=%s" % (
            label,
            _progname(process),
            hex(display) if display else None,
            _u8(process, display + 0x2b4) if display else None,
            hex(x0) if x0 is not None else None,
            hex(info) if info else None,
            hex(dispatch) if dispatch else None,
            hex(dispatch_static) if dispatch_static is not None else None,
            hex(sp + 0x1c0) if sp else None,
            hex(array) if array else None,
            array_head,
            hex(fp - 0xc0) if fp else None,
            hex(mode_ref) if mode_ref else None,
            hex(mode_value) if mode_value is not None else None,
            timing_head,
        ), flush=True)
    if label in ("DISPLAY_MODE_COLOR_ARRAY_COUNT_CALL", "DISPLAY_MODE_TIMING_ARRAY_COUNT_CALL"):
        obj = _reg(frame, "x0")
        print("display_mode_array_count_call label=%s progname=%s array=%s head=%s "
              "words=%s" % (
            label,
            _progname(process),
            hex(obj) if obj else None,
            _blob_hex(process, obj, 0x50) if obj else None,
            [hex(_u64(process, obj + off)) if obj and _u64(process, obj + off) is not None else None
             for off in range(0, 0x30, 8)] if obj else None,
        ), flush=True)
    if label == "IOMFB_MODE_KERN_ENTRY":
        obj = x0
        out_modes = x1
        out_timing = x2
        print("iomfb_mode_kern_entry obj=%s service=%s connect=%s cached=%s "
              "out_modes=%s out_timing=%s" % (
            hex(obj) if obj else None,
            hex(_u32(process, obj + 0x10)) if obj else None,
            hex(_u32(process, obj + 0x14)) if obj else None,
            hex(_u64(process, obj + 0xaf0)) if obj and _u64(process, obj + 0xaf0) else None,
            hex(out_modes) if out_modes else None,
            hex(out_timing) if out_timing else None,
        ), flush=True)
    if label in (
        "IOMFB_MODE_COLOR_PROPERTY_RESULT",
        "IOMFB_MODE_TIMING_PROPERTY_RESULT",
    ):
        obj = _reg(frame, "x20")
        value = _reg(frame, "x0")
        print("iomfb_mode_property_result label=%s progname=%s obj=%s "
              "service=%s connect=%s value=%s value_isa=%s" % (
            label,
            _progname(process),
            hex(obj) if obj else None,
            hex(_u32(process, obj + 0x10)) if obj else None,
            hex(_u32(process, obj + 0x14)) if obj else None,
            hex(value) if value else None,
            hex(_u64(process, value)) if value and _u64(process, value) else None,
        ), flush=True)
    if label in (
        "IOMFB_MODE_COLOR_TYPE_COMPARE",
        "IOMFB_MODE_TIMING_TYPE_COMPARE",
    ):
        property_type = _reg(frame, "x26" if label == "IOMFB_MODE_COLOR_TYPE_COMPARE" else "x23")
        cfarray_type = _reg(frame, "x8")
        print("iomfb_mode_property_type label=%s progname=%s property_type=%s "
              "cfarray_type=%s type_match=%s" % (
            label,
            _progname(process),
            hex(property_type) if property_type is not None else None,
            hex(cfarray_type) if cfarray_type is not None else None,
            property_type == cfarray_type if property_type is not None and cfarray_type is not None else None,
        ), flush=True)
    if label == "IOMFB_MODE_KERN_RETURN":
        obj = _reg(frame, "x20")
        out_modes = _reg(frame, "x22")
        out_timing = _reg(frame, "x21")
        print("iomfb_mode_kern_return status=%s obj=%s service=%s connect=%s cached=%s "
              "out_modes_ptr=%s modes=%s out_timing_ptr=%s timing=%s" % (
            hex(_reg(frame, "x19")) if _reg(frame, "x19") is not None else None,
            hex(obj) if obj else None,
            hex(_u32(process, obj + 0x10)) if obj else None,
            hex(_u32(process, obj + 0x14)) if obj else None,
            hex(_u64(process, obj + 0xaf0)) if obj and _u64(process, obj + 0xaf0) else None,
            hex(out_modes) if out_modes else None,
            hex(_u64(process, out_modes)) if out_modes else None,
            hex(out_timing) if out_timing else None,
            hex(_u64(process, out_timing)) if out_timing else None,
        ), flush=True)
    if label == "IOMFB_MODE_VIRT_DISPATCH":
        obj = x0
        table = _u64(process, obj + 0xec8) if obj else None
        target = _u64(process, table + 8) if table else None
        print("iomfb_mode_virt_dispatch obj=%s impl_table=%s target=%s "
              "target_static=%s out_modes=%s out_timing=%s" % (
            hex(obj) if obj else None,
            hex(table) if table else None,
            hex(target) if target else None,
            hex(_xpac(target) - DSC_SLIDE) if target else None,
            hex(x1) if x1 else None,
            hex(x2) if x2 else None,
        ), flush=True)
    if label == "IOMFB_MODE_VIRT_RETURN":
        sp = _reg(frame, "sp")
        out_modes = _reg(frame, "x21")
        out_timing = _u64(process, sp + 0x20) if sp else None
        print("iomfb_mode_virt_return status=%s out_modes_ptr=%s modes=%s "
              "out_timing_ptr=%s timing=%s" % (
            hex(_reg(frame, "x20")) if _reg(frame, "x20") is not None else None,
            hex(out_modes) if out_modes else None,
            hex(_u64(process, out_modes)) if out_modes else None,
            hex(out_timing) if out_timing else None,
            hex(_u64(process, out_timing)) if out_timing and _u64(process, out_timing) is not None else None,
        ), flush=True)
    if label in ("IOMFB_NAME_MATCH_TYPE_STORE", "IOMFB_NAME_MATCH_APPEND_COUNT"):
        entry = _reg(frame, "x23")
        base = 0x26fdcc6b0 + DSC_SLIDE
        count = _u32(process, 0x26fdcc6a8 + DSC_SLIDE)
        type_value = _reg(frame, "w8")
        if label == "IOMFB_NAME_MATCH_APPEND_COUNT":
            count = type_value
            type_value = _u32(process, entry + 0x8) if entry else None
        index = (count - 1 if label == "IOMFB_NAME_MATCH_APPEND_COUNT" else count)
        print("iomfb_name_match_store label=%s progname=%s count=%s index=%s "
              "entry=%s type=%s name_ref=%s aux=%s service=%s flags98_99=%s "
              "pc=%s pc_static=%s lr=%s" % (
            label,
            _progname(process),
            count,
            index,
            hex(entry) if entry else None,
            type_value,
            hex(_u64(process, entry)) if entry and _u64(process, entry) else None,
            hex(_u32(process, entry + 0x10)) if entry and _u32(process, entry + 0x10) is not None else None,
            hex(_u32(process, entry + 0x14)) if entry and _u32(process, entry + 0x14) is not None else None,
            (_u8(process, entry + 0x98), _u8(process, entry + 0x99)) if entry else (None, None),
            hex(frame.GetPC()) if frame.GetPC() else None,
            hex(_xpac(frame.GetPC()) - DSC_SLIDE) if frame.GetPC() else None,
            hex(_xpac(_reg(frame, "lr")) - DSC_SLIDE) if _reg(frame, "lr") else None,
        ), flush=True)
    if label == "DISPLAY_MODE_ARRAY_COUNT_BRANCH":
        display = _reg(frame, "x19")
        print("display_mode_array_count display=%s signal_type=%s count=%s "
              "take_multi_mode_path=%s x23=%s x27=%s" % (
            hex(display) if display else None,
            _u8(process, display + 0x2b4) if display else None,
            x0,
            x0 is not None and x0 > 1,
            hex(_reg(frame, "x23")) if _reg(frame, "x23") is not None else None,
            hex(_reg(frame, "x27")) if _reg(frame, "x27") is not None else None,
        ), flush=True)
    if label == "DISPLAY_MODE_TIMING_ARRAY_COUNT_BRANCH":
        print("display_mode_timing_array_count count=%s w24_before=%s" % (
            _reg(frame, "x0"),
            hex(_reg(frame, "x24")) if _reg(frame, "x24") is not None else None,
        ), flush=True)
    if label in ("DISPLAY_MODE_PREFERRED_MODE_GATE", "DISPLAY_MODE_SINGLE_MODE_GATE"):
        base = 0x1e7058000 + DSC_SLIDE
        supports_apt_once = _u64(process, base + 0xe10)
        supports_apt = _u8(process, base + 0xc58)
        # Symbols identify +0x600 as CADeviceIsVirtualized()::once and
        # +0xbf9 as its cached is_virtualized boolean. These are policy/cache
        # fields, not current/default display-mode storage.
        is_virtualized_once = _u64(process, base + 0x600)
        is_virtualized = _u8(process, 0x1e7053000 + DSC_SLIDE + 0xbf9)
        print("display_mode_fallback_gate label=%s x24=%s x8=%s "
              "supports_apt_once_e10=%s supports_apt_c58=%s "
              "is_virtualized_once_600=%s is_virtualized_bf9=%s" % (
            label,
            hex(_reg(frame, "x24")) if _reg(frame, "x24") is not None else None,
            hex(_reg(frame, "x8")) if _reg(frame, "x8") is not None else None,
            hex(supports_apt_once) if supports_apt_once is not None else None,
            supports_apt,
            hex(is_virtualized_once) if is_virtualized_once is not None else None,
            is_virtualized,
        ), flush=True)
    if label == "DISPLAY_MODE_SIGNAL_TYPE2_STATE":
        display = _reg(frame, "x19")
        state = _u8(process, _reg(frame, "x28") + 3) if _reg(frame, "x28") else None
        print("display_mode_type2_state display=%s signal_type=%s x28=%s state=%s "
              "proceed=%s" % (
            hex(display) if display else None,
            _u8(process, display + 0x2b4) if display else None,
            hex(_reg(frame, "x28")) if _reg(frame, "x28") else None,
            state,
            state == 1,
        ), flush=True)
    if label == "DISPLAY_UPDATE_DIGITAL_MODES_ENTRY":
        mode_set = x1
        mode_ref = x2
        content_gate = _u8(process, x0 + 0x755b) if x0 else None
        print("display_update_digital_modes_entry display=%s signal_type=%s modeset=%s "
              "snapshot=%s mode_ref=%s mode=%s color_array=%s timing_array=%s "
              "content_gate_755b=%s content_gate_bit0=%s" % (
            hex(x0) if x0 else None,
            _u8(process, x0 + 0x2b4) if x0 else None,
            hex(mode_set) if mode_set else None,
            _mode_set_snapshot(process, mode_set),
            hex(mode_ref) if mode_ref else None,
            hex(_u64(process, mode_ref)) if mode_ref and _u64(process, mode_ref) is not None else None,
            hex(_reg(frame, "x3")) if _reg(frame, "x3") else None,
            hex(_reg(frame, "x4")) if _reg(frame, "x4") else None,
            content_gate,
            bool(content_gate & 1) if content_gate is not None else None,
        ), flush=True)
    if label == "DISPLAY_UPDATE_DIGITAL_MODES_ATTRIBUTES_RESULT":
        print("display_update_digital_modes_attributes_result value=%s saved=%s" % (
            hex(x0) if x0 is not None else None,
            hex(_reg(frame, "x20")) if _reg(frame, "x20") is not None else None,
        ), flush=True)
    if label == "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_COUNT_GATE":
        print("display_update_digital_modes_timing_count_gate count=%s" % (
            _reg(frame, "x25"),
        ), flush=True)
    if label == "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_LOOP":
        print("display_update_digital_modes_timing_loop index=%s count=%s array=%s" % (
            _reg(frame, "x27"),
            _reg(frame, "x25"),
            hex(_reg(frame, "x24")) if _reg(frame, "x24") else None,
        ), flush=True)
    if label == "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_ELEMENT":
        print("display_update_digital_modes_timing_element element=%s" % (
            hex(x0) if x0 else None,
        ), flush=True)
    if label == "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_ATTRIBUTES":
        print("display_update_digital_modes_timing_attributes horizontal=%s "
              "vertical=%s" % (
            hex(_reg(frame, "x21")) if _reg(frame, "x21") else None,
            hex(x0) if x0 else None,
        ), flush=True)
    if label == "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_FILTER":
        print("display_update_digital_modes_timing_filter mask=%s proceed=%s" % (
            hex(_reg(frame, "w8")) if _reg(frame, "w8") is not None else None,
            bool(_reg(frame, "w8") & 1) if _reg(frame, "w8") is not None else None,
        ), flush=True)
    if label.startswith("DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_"):
        print("display_update_digital_modes_milestone label=%s x0=%s x19=%s "
              "x20=%s x21=%s x23=%s w8=%s" % (
            label,
            hex(x0) if x0 is not None else None,
            hex(_reg(frame, "x19")) if _reg(frame, "x19") is not None else None,
            hex(_reg(frame, "x20")) if _reg(frame, "x20") is not None else None,
            hex(_reg(frame, "x21")) if _reg(frame, "x21") is not None else None,
            hex(_reg(frame, "x23")) if _reg(frame, "x23") is not None else None,
            hex(_reg(frame, "w8")) if _reg(frame, "w8") is not None else None,
        ), flush=True)
    if label.startswith("DISPLAY_UPDATE_DIGITAL_MODES_COLOR_"):
        sp = _reg(frame, "sp")
        print("display_update_digital_modes_color label=%s index=%s color=%s "
              "x21=%s w19=%s w24=%s w25=%s w28=%s f0=%s "
              "tree_root=%s tree_end=%s" % (
            label,
            _reg(frame, "x23"),
            hex(_reg(frame, "x20")) if _reg(frame, "x20") else None,
            hex(_reg(frame, "x21")) if _reg(frame, "x21") else None,
            _reg(frame, "w19"), _reg(frame, "w24"),
            _reg(frame, "w25"), _reg(frame, "w28"),
            _u32(process, sp + 0xf0) if sp else None,
            hex(_u64(process, sp + 0x170)) if sp and _u64(process, sp + 0x170) else None,
            hex(_u64(process, sp + 0x128)) if sp and _u64(process, sp + 0x128) else None,
        ), flush=True)
        if label == "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2E2C":
            node = _reg(frame, "x8")
            print("display_update_digital_modes_color_lookup sought=%s node=%s key=%s "
                  "left=%s right=%s" % (
                _reg(frame, "w1"), hex(node) if node else None,
                _u32(process, node + 0x1c) if node else None,
                hex(_u64(process, node)) if node and _u64(process, node) else None,
                hex(_u64(process, node + 0x8)) if node and _u64(process, node + 0x8) else None,
            ), flush=True)
    if label == "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_NEXT":
        print("display_update_digital_modes_timing_next index=%s count=%s" % (
            _reg(frame, "x27"),
            _reg(frame, "x25"),
        ), flush=True)
    if label in ("DISPLAY_UPDATE_DIGITAL_MODES_RETURN_A", "DISPLAY_UPDATE_DIGITAL_MODES_RETURN_B"):
        sp = _reg(frame, "sp")
        fp = _reg(frame, "x29")
        display = _reg(frame, "x19")
        mode_set = sp + 0xc0 if sp else None
        mode_ref = fp - 0xa8 if fp else None
        color_array = _u64(process, sp + 0x1c0) if sp else None
        timing_array = _u64(process, fp - 0xc0) if fp else None
        print("display_update_digital_modes_return label=%s display=%s signal_type=%s "
              "modeset=%s snapshot=%s mode_ref=%s mode=%s color_array=%s timing_array=%s" % (
            label,
            hex(display) if display else None,
            _u8(process, display + 0x2b4) if display else None,
            hex(mode_set) if mode_set else None,
            _mode_set_snapshot(process, mode_set),
            hex(mode_ref) if mode_ref else None,
            hex(_u64(process, mode_ref)) if mode_ref and _u64(process, mode_ref) is not None else None,
            hex(color_array) if color_array else None,
            hex(timing_array) if timing_array else None,
        ), flush=True)
    if label == "DISPLAY_FETCH_CURRENT_MODE_RETURN":
        display = _reg(frame, "x19")
        print("display_fetch_current_mode_return display=%s signal_type=%s mode=%s "
              "current2b8=%s current2c0=%s" % (
            hex(display) if display else None,
            _u8(process, display + 0x2b4) if display else None,
            hex(x0) if x0 is not None else None,
            hex(_u64(process, display + 0x2b8)) if display and _u64(process, display + 0x2b8) is not None else None,
            hex(_u64(process, display + 0x2c0)) if display and _u64(process, display + 0x2c0) is not None else None,
        ), flush=True)
    if label in ("IOMFB_SET_DIGITAL_OUT_MODE_KERN", "IOMFB_SET_DIGITAL_OUT_MODE_VIRT"):
        print("iomfb_set_digital_out_mode label=%s obj=%s color_id=%s "
              "timing_id=%s" % (
            label,
            hex(x0) if x0 else None,
            _reg(frame, "w1"),
            _reg(frame, "w2"),
        ), flush=True)
    if label in ("IOMFB_SET_DIGITAL_OUT_MODE_IOCONNECT_RETURN",
                 "IOMFB_SET_DIGITAL_OUT_MODE_KERN_EXIT"):
        print("iomfb_set_digital_out_mode_result label=%s status=%s" % (
            label,
            hex(_reg(frame, "w0")) if _reg(frame, "w0") is not None else None,
        ), flush=True)
    if label in ("DISPLAY_DIGITAL_OUT_CALLER_STATUS",
                 "DISPLAY_DIGITAL_OUT_CALLER_CONTINUE",
                 "DISPLAY_DIGITAL_OUT_CALLER_FAILURE"):
        print("display_digital_out_caller label=%s saved_status=%s "
              "branch=%s" % (
            label,
            hex(_reg(frame, "w26")) if _reg(frame, "w26") is not None else None,
            "continue" if label.endswith("CONTINUE") else
            "failure" if label.endswith("FAILURE") else "test",
        ), flush=True)
    if label == "DISPLAY_SET_2B8_SENTINEL":
        x19 = _reg(frame, "x19")
        print("display_set_sentinel obj=%s prior2b8=%s prior2c0=%s" % (
            hex(x19) if x19 else None,
            hex(_u64(process, x19 + 0x2b8)) if x19 and _u64(process, x19 + 0x2b8) is not None else None,
            hex(_u64(process, x19 + 0x2c0)) if x19 and _u64(process, x19 + 0x2c0) is not None else None,
        ), flush=True)
    if label == "DISPLAY_MODE_FALLBACK_MINUS_ONE":
        x19 = _reg(frame, "x19")
        base = 0x1e7058000 + DSC_SLIDE
        print("display_mode_fallback_minus_one display=%s current2b8=%s current2c0=%s "
              "x23=%s x24=%s x25=%s x26=%s x27=%s cached=(supports_apt_once_e10:%s "
              "supports_apt_c58:%s is_virtualized_once_600:%s is_virtualized_bf9:%s)" % (
            hex(x19) if x19 else None,
            hex(_u64(process, x19 + 0x2b8)) if x19 and _u64(process, x19 + 0x2b8) is not None else None,
            hex(_u64(process, x19 + 0x2c0)) if x19 and _u64(process, x19 + 0x2c0) is not None else None,
            hex(_reg(frame, "x23")) if _reg(frame, "x23") is not None else None,
            hex(_reg(frame, "x24")) if _reg(frame, "x24") is not None else None,
            hex(_reg(frame, "x25")) if _reg(frame, "x25") is not None else None,
            hex(_reg(frame, "x26")) if _reg(frame, "x26") is not None else None,
            hex(_reg(frame, "x27")) if _reg(frame, "x27") is not None else None,
            hex(_u64(process, base + 0xe10)) if _u64(process, base + 0xe10) is not None else None,
            _u8(process, base + 0xc58),
            hex(_u64(process, base + 0x600)) if _u64(process, base + 0x600) is not None else None,
            _u8(process, 0x1e7053000 + DSC_SLIDE + 0xbf9),
        ), flush=True)
    if label == "DISPLAY_MODE_ADD_MODE":
        mode_set = x0
        mode_ptr = x1
        print("display_mode_add_mode modeset=%s snapshot=%s mode_ptr=%s mode=%s" % (
            hex(mode_set) if mode_set else None,
            _mode_set_snapshot(process, mode_set),
            hex(mode_ptr) if mode_ptr else None,
            hex(_u64(process, mode_ptr)) if mode_ptr and _u64(process, mode_ptr) is not None else None,
        ), flush=True)
    if label == "DISPLAY_SET_ALL_MODES_CALL":
        print("display_set_all_modes_call display=%s modeset=%s snapshot=%s "
              "mode0=%s mode1=%s current2b8=%s current2c0=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            _mode_set_snapshot(process, x1),
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_u64(process, x0 + 0x2b8)) if x0 and _u64(process, x0 + 0x2b8) is not None else None,
            hex(_u64(process, x0 + 0x2c0)) if x0 and _u64(process, x0 + 0x2c0) is not None else None,
        ), flush=True)
    if label == "DISPLAY_MODE_SET_ENTRY":
        print("display_mode_set_entry obj=%s x1=%s mode_set=%s x2=%s x3=%s "
              "current2b8=%s current2c0=%s" % (
            hex(x0) if x0 else None,
            hex(x1) if x1 else None,
            _mode_set_snapshot(process, x1),
            hex(x2) if x2 is not None else None,
            hex(x3) if x3 is not None else None,
            hex(_u64(process, x0 + 0x2b8)) if x0 and _u64(process, x0 + 0x2b8) is not None else None,
            hex(_u64(process, x0 + 0x2c0)) if x0 and _u64(process, x0 + 0x2c0) is not None else None,
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
    if label in ("DISPLAY_MODE_POST_STORE", "DISPLAY_POST_SET_V308_RETURN"):
        global MODE_2B8_WATCH, MODE_6B00_WATCH
        display = _reg(frame, "x19")
        mode = _u64(process, display + 0x2b8) if display else None
        print("display_post_store label=%s display=%s current2b8=%s "
              "current2c0=%s pending6b00=%s x0=%s" % (
            label,
            hex(display) if display else None,
            hex(mode) if mode is not None else None,
            hex(_u64(process, display + 0x2c0)) if display and _u64(process, display + 0x2c0) is not None else None,
            hex(_u64(process, display + 0x6b00)) if display and _u64(process, display + 0x6b00) is not None else None,
            hex(x0) if x0 is not None else None,
        ), flush=True)
        if (label == "DISPLAY_MODE_POST_STORE" and MODE_2B8_WATCH is None and
                display and mode not in (None, 0, 0xffffffffffffffff)):
            target = process.GetTarget()
            watch_err = lldb.SBError()
            watchpoint = target.WatchAddress(display + 0x2b8, 8, False, True, watch_err)
            if watch_err.Success() and watchpoint and watchpoint.IsValid():
                result = lldb.SBCommandReturnObject()
                lldb.debugger.GetCommandInterpreter().HandleCommand(
                    "watchpoint command add -F render_gate_callbacks.on_mode_2b8_watch %d" %
                    watchpoint.GetID(), result)
                if result.Succeeded():
                    MODE_2B8_WATCH = (display, mode, watchpoint.GetID())
                    print("MODE_2B8_WATCH_ARMED display=%s mode=%s watchpoint=%d" % (
                        hex(display), hex(mode), watchpoint.GetID()), flush=True)
                else:
                    target.DeleteWatchpoint(watchpoint.GetID())
                    print("MODE_2B8_WATCH_FAILED command=%s" % result.GetError(), flush=True)
            else:
                print("MODE_2B8_WATCH_FAILED install=%s" % watch_err.GetCString(), flush=True)
            if MODE_2B8_WATCH is not None and MODE_6B00_WATCH is None:
                watch_err = lldb.SBError()
                watchpoint = target.WatchAddress(display + 0x6b00, 8, False, True, watch_err)
                if watch_err.Success() and watchpoint and watchpoint.IsValid():
                    result = lldb.SBCommandReturnObject()
                    lldb.debugger.GetCommandInterpreter().HandleCommand(
                        "watchpoint command add -F render_gate_callbacks.on_mode_6b00_watch %d" %
                        watchpoint.GetID(), result)
                    if result.Succeeded():
                        MODE_6B00_WATCH = (display, watchpoint.GetID())
                        print("MODE_6B00_WATCH_ARMED display=%s value=%s watchpoint=%d" % (
                            hex(display),
                            hex(_u64(process, display + 0x6b00)) if _u64(process, display + 0x6b00) is not None else None,
                            watchpoint.GetID()), flush=True)
                    else:
                        target.DeleteWatchpoint(watchpoint.GetID())
                        print("MODE_6B00_WATCH_FAILED command=%s" % result.GetError(), flush=True)
                else:
                    print("MODE_6B00_WATCH_FAILED install=%s" % watch_err.GetCString(), flush=True)
    if label in ("DISPLAY_POWER_MODE_LOOKUP_RETURN", "DISPLAY_POWER_MODE_NORMALIZE",
                 "DISPLAY_POWER_MODE_COPY_SOURCE"):
        display = _reg(frame, "x20")
        print("display_power_mode_choice label=%s display=%s lookup_x0=%s "
              "candidate_x24=%s normalize_x9=%s current2b8=%s target2c0=%s "
              "pending6b00=%s hotplug6498=%s state68a0=%s" % (
            label,
            hex(display) if display else None,
            hex(x0) if x0 is not None else None,
            hex(_reg(frame, "x24")) if _reg(frame, "x24") is not None else None,
            hex(_reg(frame, "x9")) if _reg(frame, "x9") is not None else None,
            hex(_u64(process, display + 0x2b8)) if display and _u64(process, display + 0x2b8) is not None else None,
            hex(_u64(process, display + 0x2c0)) if display and _u64(process, display + 0x2c0) is not None else None,
            hex(_u64(process, display + 0x6b00)) if display and _u64(process, display + 0x6b00) is not None else None,
            _u8(process, display + 0x6498) if display else None,
            hex(_u64(process, display + 0x68a0)) if display and _u64(process, display + 0x68a0) is not None else None,
        ), flush=True)
    if label.startswith("DISPLAY_HOTPLUG_MODE_"):
        display = _reg(frame, "x19")
        sp = _reg(frame, "sp")
        first = _u32(process, sp + 0xc) if sp else None
        second = _u32(process, sp + 0x8) if sp else None
        print("display_hotplug_mode_lookup label=%s display=%s key0=%s key1=%s "
              "x0=%s x8=%s w22=%s list6888=%s list6898=%s pending6b00=%s" % (
            label,
            hex(display) if display else None,
            hex(first) if first is not None else None,
            hex(second) if second is not None else None,
            hex(x0) if x0 is not None else None,
            hex(_reg(frame, "x8")) if _reg(frame, "x8") is not None else None,
            hex(_reg(frame, "x22")) if _reg(frame, "x22") is not None else None,
            hex(_u64(process, display + 0x6888)) if display and _u64(process, display + 0x6888) is not None else None,
            hex(_u64(process, display + 0x6898)) if display and _u64(process, display + 0x6898) is not None else None,
            hex(_u64(process, display + 0x6b00)) if display and _u64(process, display + 0x6b00) is not None else None,
        ), flush=True)
    if label in ("DISPLAY_POWER_TARGET_STATE", "DISPLAY_POWER_STORE_STATE5"):
        display = _reg(frame, "x20")
        state = _u64(process, display + 0x330) if display else None
        print("display_power_trace label=%s display=%s id=%s power_assertions=%s raw_power_state=%s "
              "target_w24=%s state_ptr=%s state4=%s state5=%s lr_static=%s" % (
            label,
            hex(display) if display else None,
            _u32(process, display + 0x18) if display else None,
            _u32(process, display + 0x68e8 + 0x978) if display else None,
            _u8(process, display + 0x68e8 + 0x97c) if display else None,
            _reg(frame, "w24"),
            hex(state) if state else None,
            _u8(process, state + 4) if state else None,
            _u8(process, state + 5) if state else None,
            hex(_xpac(_reg(frame, "lr")) - DSC_SLIDE) if _reg(frame, "lr") else None,
        ), flush=True)
    if label == "DISPLAY_SET_POWER_STATE_LOCKED":
        display = x0
        state = _u64(process, display + 0x330) if display else None
        vtable = _u64(process, display) if display else None
        print("display_set_power_state_locked display=%s id=%s vtable=%s mode2b8=%s "
              "power_state_w1=%s arg_w2=%s state_ptr=%s state4=%s state5=%s lr_static=%s" % (
            hex(display) if display else None,
            _u32(process, display + 0x18) if display else None,
            hex(vtable) if vtable else None,
            hex(_u64(process, display + 0x2b8)) if display and _u64(process, display + 0x2b8) is not None else None,
            _reg(frame, "w1"),
            _reg(frame, "w2"),
            hex(state) if state else None,
            _u8(process, state + 4) if state else None,
            _u8(process, state + 5) if state else None,
            hex(_xpac(_reg(frame, "lr")) - DSC_SLIDE) if _reg(frame, "lr") else None,
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
        # GATE236: ignore_update_p's return depends on these additional raw
        # state bytes, not just the sentinel byte and update flags. Capture
        # them before the indirect call so the return can be reduced to its
        # actual branch inputs without changing guest state.
        state330 = _u64(process, x0v + 0x330) if x0v else None
        ignore_state = _u8(process, x0v + 0x7595) if x0v else None
        pending_state = _u8(process, x0v + 0x7558) if x0v else None
        state4 = _u8(process, state330 + 4) if state330 else None
        state5 = _u8(process, state330 + 5) if state330 else None
        print("rft_v170_target obj=%s vt=%s bare=%s +0x2b8=%s +0x2bb=%s bit4=%s x1_140=%s" % (
            hex(x0v) if x0v else None,
            hex(vt) if vt else None,
            hex(bare) if bare is not None else None,
            hex(q) if q is not None else None,
            flag2bb,
            ((flag2bb >> 4) & 1) if flag2bb is not None else None,
            hex(x1_140) if x1_140 is not None else None,
        ), flush=True)
        print("rft_ignore_inputs display=%s state330=%s state330_4=%s state330_5=%s +0x7558=%s +0x7595=%s" % (
            hex(x0v) if x0v else None,
            hex(state330) if state330 else None,
            state4,
            state5,
            pending_state,
            ignore_state,
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


def install_kernel(debugger):
    """Install the opt-in kernel hooks; can run while QEMU is still at reset."""
    import os
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    global MIPI_AP_PROBE_MATCHES
    MIPI_AP_PROBE_MATCHES = 0
    sites = 0
    armed_labels = []
    # A focused early-boot publication trace can opt into an explicit
    # allowlist without changing the established default kernel probe set.
    # This is useful when several otherwise independent publisher families
    # (AP MIPI, DCP MIPI, AFK report, and IOAV consumers) must be observed in
    # one boot but unrelated kernel breakpoints would add noise.
    kernel_label_filter = set(filter(None, os.environ.get(
        "RFT_KERNEL_LABELS", "").split(",")))
    for addr, label, limit in KERNEL:
        if kernel_label_filter and label not in kernel_label_filter:
            continue
        kernel_slim = os.environ.get("RFT_KERNEL_SLIM", "")
        if kernel_slim == "mipi-ap" and not label.startswith("MIPI_AP_"):
            continue
        if kernel_slim == "clcd2" and not label.startswith("CLCD2_"):
            continue
        if label.startswith("IOAV_META_ALLOC_ENTRY_") and os.environ.get("RFT_TRACE_IOAV_ALLOCATORS") != "1":
            continue
        if label.startswith("IOAV_CONTROLLER_") and os.environ.get("RFT_TRACE_IOAV_CONTROLLER") != "1":
            continue
        if (label.startswith("IOAV_") and
                not label.startswith(("IOAV_META_ALLOC_ENTRY_", "IOAV_CONTROLLER_")) and
                os.environ.get("RFT_TRACE_IOAV_PROPERTIES") != "1"):
            continue
        if label.startswith("MGF_INTERNAL_DISPLAY_") and os.environ.get("RFT_TRACE_INTERNAL_DISPLAY") != "1":
            continue
        if label.startswith("MGF_IOAV_HANDLER_") and os.environ.get("RFT_TRACE_IOAV_HANDLER") != "1":
            continue
        if label.startswith("IOMFB_AP_") and os.environ.get("RFT_TRACE_IOMFB_AP") != "1":
            continue
        if label.startswith("MIPI_AP_") and os.environ.get("RFT_TRACE_MIPI_AP") != "1":
            continue
        if label.startswith("CLCD2_") and os.environ.get("RFT_TRACE_CLCD2") != "1":
            continue
        if label.startswith(("DCPAV_PROXY_", "DCPAV_POWER_CONTROLLER_", "DCPAV_VIDEO_", "DCPAV_COMMON_")) and os.environ.get("RFT_TRACE_DCPAV_PROXY") != "1":
            continue
        if label.startswith("DCPMIPI_PROXY_") and os.environ.get("RFT_TRACE_DCPMIPI_PROXY") != "1":
            continue
        if label.startswith("AFK_PUBLISH_") and os.environ.get("RFT_TRACE_AFK_PUBLISH") != "1":
            continue
        runtime = addr + KERNEL_SLIDE
        before = target.GetNumBreakpoints()
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint set --address 0x%x" % runtime, result)
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("kernel bp failed %s: %s" % (label, result.GetError()))
        bp = target.GetBreakpointAtIndex(before)
        CONFIG[bp.GetID()] = {"label": label, "limit": limit}
        RUNTIME_ADDR[label] = runtime
        HITS[label] = 0
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F render_gate_callbacks.on_break %d" %
            bp.GetID(), result)
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" % (
            bp.GetID(), label, runtime), flush=True)
        armed_labels.append(label)
        sites += 1
    print("EARLY_KERNEL_TRACE_READY slide=0x%x sites=%d" % (
        KERNEL_SLIDE, sites), flush=True)
    if kernel_label_filter:
        print("EARLY_KERNEL_TRACE_LABELS " + json.dumps(
            sorted(armed_labels), separators=(",", ":")), flush=True)


def install_iomfb_mode_provider_only(debugger, slide):
    """Install only the AP-side provider/property hooks for mode queries.

    This is the small post-shell counterpart to ``install_kernel``.  The
    selected addresses live in the shared-cache ``SITES`` table (despite the
    historical ``KERN`` labels); they expose the kernel-backed
    IOMobileFramebuffer mode provider and the registry properties it reads.
    Keeping this list narrow avoids arming the full render-gate probe merely
    to distinguish a null ColorElements/TimingElements result.
    """
    global DSC_SLIDE
    DSC_SLIDE = int(slide)
    selected = {
        "IOMFB_MODE_KERN_ENTRY",
        "IOMFB_MODE_COLOR_PROPERTY_RESULT",
        "IOMFB_MODE_COLOR_TYPE_COMPARE",
        "IOMFB_MODE_TIMING_PROPERTY_RESULT",
        "IOMFB_MODE_TIMING_TYPE_COMPARE",
        "IOMFB_MODE_KERN_RETURN",
        "IOMFB_MODE_VIRT_DISPATCH",
        "IOMFB_MODE_VIRT_RETURN",
    }
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    armed = []
    # The shared-cache sites are part of USER (there is no separate SITES
    # table in this module). Select by label so this narrow installer does
    # not arm the rest of the render-gate breakpoints.
    available = {label: (address, limit) for address, label, limit in USER}
    missing = selected - available.keys()
    if missing:
        raise RuntimeError("mode-provider sites missing from USER table: %s" %
                           sorted(missing))
    for label in sorted(selected):
        address, limit = available[label]
        runtime = DSC_SLIDE + address
        before = target.GetNumBreakpoints()
        bp = target.BreakpointCreateByAddress(runtime)
        if not bp.IsValid() or bp.GetNumLocations() < 1:
            raise RuntimeError("mode-provider bp failed %s at 0x%x" %
                               (label, runtime))
        if target.GetNumBreakpoints() != before + 1:
            raise RuntimeError("mode-provider bp registration mismatch %s" %
                               label)
        CONFIG[bp.GetID()] = {"label": label, "limit": limit}
        RUNTIME_ADDR[label] = runtime
        HITS[label] = 0
        command = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F render_gate_callbacks.on_break %d" %
            bp.GetID(), command)
        proof = lldb.SBCommandReturnObject()
        interpreter.HandleCommand("breakpoint command list %d" % bp.GetID(),
                                  proof)
        proof_text = proof.GetOutput() or proof.GetError() or ""
        if (not command.Succeeded() or not proof.Succeeded() or
                "render_gate_callbacks.on_break" not in proof_text):
            bp.SetEnabled(False)
            raise RuntimeError("mode-provider callback proof failed %s: %s" %
                               (label, proof_text))
        armed.append({"label": label, "id": bp.GetID(),
                      "address": "0x%x" % runtime,
                      "locations": bp.GetNumLocations()})
    if len(armed) != len(selected):
        raise RuntimeError("mode-provider site set incomplete: %s" % armed)
    print("IOMFB_MODE_PROVIDER_TRACE_READY " + json.dumps({
        "slide": "0x%x" % DSC_SLIDE, "armed": armed,
        "read_only": True,
    }, sort_keys=True), flush=True)


def install(debugger, slide):
    import os
    global DSC_SLIDE, DISPLAY_WH_OBJ, LAYER_STATE70_WATCH, LAYER_STATE70_WATCH_HITS
    global COPY_CALAYER_KEYS
    target = debugger.GetSelectedTarget()
    interpreter = debugger.GetCommandInterpreter()
    slide = int(slide)
    DSC_SLIDE = slide
    DISPLAY_WH_OBJ = None
    LAYER_STATE70_WATCH = None
    LAYER_STATE70_WATCH_HITS = 0
    COPY_CALAYER_KEYS = set()
    global LAST_ALLOC_SURFACE, IOMFB_OPEN_PAIRS
    global LAST_BB_IOMFB_CONN, LAST_BB_IOMFB, LAST_IOMFB_CONN, LAST_IOMFB
    LAST_ALLOC_SURFACE = None
    IOMFB_OPEN_PAIRS = []
    LAST_BB_IOMFB_CONN = None
    LAST_BB_IOMFB = None
    LAST_IOMFB_CONN = None
    LAST_IOMFB = None
    slim = os.environ.get("RFT_SLIM", "")
    if os.environ.get("RFT_WATCH_IOMFB_NAME_COUNT") == "1":
        watch_addr = 0x26fdcc6a8 + slide
        watch_err = lldb.SBError()
        watchpoint = target.WatchAddress(watch_addr, 4, False, True, watch_err)
        if watch_err.Success() and watchpoint and watchpoint.IsValid():
            result = lldb.SBCommandReturnObject()
            interpreter.HandleCommand(
                "watchpoint command add -F render_gate_callbacks.on_iomfb_name_count_watch %d" %
                watchpoint.GetID(), result)
            print("IOMFB_NAME_COUNT_WATCH_READY id=%d address=0x%x "
                  "command_ok=%s error=%s" % (
                watchpoint.GetID(), watch_addr, result.Succeeded(),
                result.GetError() or ""), flush=True)
        else:
            print("IOMFB_NAME_COUNT_WATCH_FAILED address=0x%x error=%s" % (
                watch_addr, watch_err.GetCString()), flush=True)
    if slim == "1":
        # Store-poke / flags path only — late attach without 80 hot breakpoints.
        want = {
            "LAYER_WALK_ENTRY", "LAYER_WALK_X27_INIT",
            "LAYER_WALK_TST_28000", "LAYER_WALK_MASK_STORE",
            "NAT20000_FN_ENTRY", "NAT20000_X23_NULL", "NAT20000_BEFORE_GATE",
            "NAT20000_GATE", "NAT20000_ORR_60", "NAT20000_ORR_68",
            "MUT_ORR_BIT23", "MUT_W25_BIT22_GATE", "MUT_ORR_BIT22", "MUT_MERGE_SP0F0",
            "MUT_W25_ALPHA_CLASSIFY_RESULT", "MUT_W25_ALPHA_ACCUM_RESULT",
            "MUT_W25_VCALL_ACCUM_RESULT",
            "MUT_BOUNDS_CHECK", "MUT_W25_SET1", "MUT_VIRT_A8_RET",
            "FILL88_GATE", "FILL88_STORE",
            "SET168_STORE", "SET78_FILTER_GATE", "SET78_FILTER_STORE",
            "SET78_ALLOC_CALL", "SET78_ALLOC_STORE",
            "PREPARE_LAYER0_ENTRY", "LAYER_GEOMETRY_FLAG22_GATE",
            "BOUNDS_STR_X28_10", "BOUNDS_COPY_Q",
            "ENCODER_D13_SOURCE_SUB", "ENCODER_TYPE31_STORE", "ENCODER_D13_STORE",
            "COPY_RENDER_LAYER_ENTRY", "COPY_RENDER_CONTENTS_SOURCE_RETURN",
            "COPY_RENDER_CONTENTS_DECISION", "COPY_RENDER_CONTENTS_STORE",
            "LAYER_OBJECT_FETCH_RETURN", "ENCODER_OBJECT_VCALL",
            "AD_SIZE_FN_ENTRY", "AD_SIZE_BIT4_GATE", "AD_SIZE_LOAD_WH", "AD_SIZE_SCVTF",
            "AD_SIZE_PARENT", "AD_SIZE_SKIP_GATE",
            "CONFIG_APPLY_GATE", "CONFIG_SIBLING_A", "CONFIG_SIBLING_B",
            "HOTPLUG_SETUP_FN_ENTRY", "UPDATE_FB_GET_HOTPLUG", "UPDATE_FB_HOTPLUG_OR",
            "GET_HOTPLUG_STATE_ENTRY", "GET_HOTPLUG_STATE_RETURN",
            "NAT8000_FN_ENTRY", "NAT8000_FN_MASK_AND",
            "NAT8000_AFTER_VIRT", "NAT8000_ORR_70",
            "UPDATE_FLAGS_MASK_CHECK", "UPDATE_FLAGS_ORR_2", "UPDATE_FLAGS_ORR_8",
            "PREPARE_LAYER_CONTEXT_FLAGS_CALL", "PREPARE_LAYER_CONTEXT_FLAGS_RETURN",
            "PREPARE_LAYER_MASK_INPUTS", "PREPARE_LAYER_FLAGS_MASK",
            "PREPARE_LAYER_FLAGS_WRITE",
            "PREPARE_LAYER_STATE_FLAGS_MERGE_IN",
            "PREPARE_LAYER_STATE_FLAGS_MERGE_OUT",
            "LAYER_STATE_RESET_ENTRY", "LAYER_STATE_RESET_CLEAR",
            "UPDATE_FLAGS_STORE_433880", "UPDATE_FLAGS_STORE_433A70",
            "UPDATE_FLAGS_STORE_433A8C", "UPDATE_FLAGS_STORE_4343E8",
            "UPDATE_FLAGS_STORE_434538", "UPDATE_FLAGS_STORE_434794",
            "UPDATE_FLAGS_STORE_4347C0", "UPDATE_FLAGS_STORE_4348D8",
            "UPDATE_FLAGS_STORE_434904", "UPDATE_FLAGS_STORE_434958",
            "UPDATE_FLAGS_STORE_434AD8", "UPDATE_FLAGS_STORE_434BE0",
            "UPDATE_FLAGS_STORE_435020", "UPDATE_FLAGS_STORE_4350DC",
            "UPDATE_FLAGS_STORE_43537C", "UPDATE_FLAGS_STORE_4355CC",
            "UPDATE_FLAGS_STORE_4355E8", "UPDATE_FLAGS_STORE_4357C0",
            "UPDATE_FLAGS_STORE_435E6C", "UPDATE_FLAGS_STORE_435E80",
            "UPDATE_FLAGS_STORE_435EAC", "UPDATE_FLAGS_STORE_435F00",
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
            # GATE86/87: post-V170 path never hit CALL_OUTER; slim had dropped these.
            "RFT_POST_V170_TIME", "RFT_POST_V170_V1B8", "RFT_POST_V170_CMP648",
            "RFT_POST_V170_BEQ648", "RFT_POST_V170_NEQ648", "RFT_POST_V170_ALT_11D00",
            "RFT_POST_V170_GFLAG", "RFT_POST_V170_20C", "RFT_ALT_MERGE_13810",
            "RFT_WP_12000", "RFT_WP_13000",
            "RFT_FULL_EXIT_14D54", "RFT_FULL_EPILOG_14DA8",
            "CALL_OUTER_A", "OUTER_A_FN_ENTRY",
            "SERVER_UPDATE_OUTER_ENTRY", "CREATE_SURFACE_ENTRY",
            "ALLOC_LDP_WH", "ALLOC_BL_DISPLAY",
            "DISPLAY_ALLOCATE_SURFACE_ENTRY", "ALLOCATE_IOSURFACE_ENTRY",
            "ALLOCATE_IOSURFACE_RETURN", "SWAP_SET_LAYER_ENTRY",
            "HANDOFF_SINK_ENTRY", "HANDOFF_SINK_FAIL",
            "FINISH_MODE_ELIGIBILITY_RETURN", "FINISH_MODE_ELIGIBILITY_JOIN",
            "SWAP_CLEAR_FLAG_SET",
            "FINISH_SWAP_CLEAR_GATE", "FINISH_POST_CLEAR_CA14",
            "FINISH_UPDATE_SURFACE_BL",
            "FINISH_SWAP_X22_GATE", "FINISH_SWAP_X22", "FINISH_SWAP_X22_RET",
            "SWAP_TRY_BEGIN_ASYNC", "SWAP_ASYNC_CB", "SWAP_ASYNC_WORK",
            "KERN_SWAP_BEGIN_THUNK", "KERN_SWAP_BEGIN", "KERN_SWAP_BEGIN_RETURN",
            "KERN_SWAP_END_THUNK", "KERN_SWAP_WAIT_THUNK",
            "VIRT_SWAP_BEGIN", "VIRT_SWAP_WAIT", "VIRT_SWAP_END",
            "KERN_SWAP_SET_LAYER", "KERN_SWAP_END", "KERN_SWAP_END_RETURN",
            "IOMFB_OPEN", "IOMFB_GET_MAIN_DISPLAY", "IOMFB_OPEN_BY_NAME",
            "IOMFB_OPEN_BY_NAME_TYPE", "IOMFB_GET_DISPLAY",
            "APPLEDISPLAY_POST_OPEN", "IOSERVICE_OPEN",
            "IOSERVICE_GET_MATCHING",
            "IOCONNECT_SCALAR_STUB", "IOCONNECT_STRUCT_STUB",
            "RFT_TIME_DECISION", "RFT_SHORT_SKIP", "RFT_FULL_PATH",
            "BIT17_LIST_WALK", "BIT17_CLEAR_STORE", "BIT17_ORR_LSL8_STORE",
            "FLAGS_INIT_3E", "FLAGS_ORR_FINISH", "FLAGS_ORR_BIT13_A",
            "FLAGS_ORR_BIT13_B", "FLAGS_BIT9_INJECT",
            "DISP_CTOR_FLAGS_0A", "DISP_CTOR_ORR_BIT14", "DISP_CTOR_ORR_LSL8",
            "BIT17_ORR_DIRECT", "DISP_CTOR_CALL_240", "DISP_CTOR_CALL_COMPUTED",
            "APPLEDISPLAY_NEW_SERVER_ENTRY", "ACCEL_SERVER_CTOR_ENTRY",
            "DISPLAY_CTOR_AFTER_2B8", "DISPLAY_SET_2B8_SENTINEL",
            "DISPLAY_MODE_SET_ENTRY", "DISPLAY_MODE_SET_STORE",
            "DISPLAY_MODE_POST_STORE", "DISPLAY_POST_SET_V308_RETURN",
            "DISPLAY_MODE_FALLBACK_MINUS_ONE", "DISPLAY_MODE_ADD_MODE",
            "DISPLAY_SET_ALL_MODES_CALL", "DISPLAY_UPDATE_FRAMEBUFFER_ENTRY",
            "DISPLAY_MODE_UPDATE_FLAGS", "DISPLAY_MODE_SIGNAL_TYPE2_BRANCH",
            "DISPLAY_MODE_SIGNAL_TYPE1_BRANCH", "DISPLAY_MODE_ARRAY_RETURN",
            "DISPLAY_MODE_ARRAY_RETURN_TYPE2",
            "IOMFB_MODE_KERN_ENTRY", "IOMFB_MODE_KERN_RETURN",
            "IOMFB_SET_DIGITAL_OUT_MODE_KERN", "IOMFB_SET_DIGITAL_OUT_MODE_VIRT",
            "IOMFB_SET_DIGITAL_OUT_MODE_IOCONNECT_RETURN",
            "IOMFB_SET_DIGITAL_OUT_MODE_KERN_EXIT",
            "DISPLAY_DIGITAL_OUT_CALLER_STATUS",
            "DISPLAY_DIGITAL_OUT_CALLER_CONTINUE",
            "DISPLAY_DIGITAL_OUT_CALLER_FAILURE",
            "IOMFB_MODE_COLOR_PROPERTY_RESULT", "IOMFB_MODE_COLOR_TYPE_COMPARE",
            "IOMFB_MODE_TIMING_PROPERTY_RESULT", "IOMFB_MODE_TIMING_TYPE_COMPARE",
            "IOMFB_MODE_VIRT_DISPATCH", "IOMFB_MODE_VIRT_RETURN",
            "IOMFB_NAME_MATCH_TYPE_STORE", "IOMFB_NAME_MATCH_APPEND_COUNT",
            "DISPLAY_MODE_ARRAY_COUNT_BRANCH", "DISPLAY_MODE_SIGNAL_TYPE2_STATE",
            "DISPLAY_MODE_COLOR_ARRAY_COUNT_CALL", "DISPLAY_MODE_TIMING_ARRAY_COUNT_CALL",
            "DISPLAY_MODE_TIMING_ARRAY_COUNT_BRANCH", "DISPLAY_MODE_PREFERRED_MODE_GATE",
            "DISPLAY_MODE_SINGLE_MODE_GATE",
            "DISPLAY_UPDATE_DIGITAL_MODES_RETURN_A",
            "DISPLAY_UPDATE_DIGITAL_MODES_RETURN_B",
            "DISPLAY_FETCH_CURRENT_MODE_RETURN", "DISPLAY_UPDATE_DIGITAL_MODES_ENTRY",
            "DISPLAY_UPDATE_DIGITAL_MODES_ATTRIBUTES_RESULT",
            "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_COUNT_GATE",
            "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_LOOP",
            "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_ELEMENT",
            "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_ATTRIBUTES",
            "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_FILTER",
            "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_2034",
            "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_2090",
            "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_20A4",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_214C",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2150",
            "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_219C",
            "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_23B4",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2548",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_25F8",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_27BC",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_28C4",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_29D8",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2D14",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2D88",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2DD0",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2E24",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2E28",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2E2C",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2E40",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2E50",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_2E54",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_3564",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_3628",
            "DISPLAY_UPDATE_DIGITAL_MODES_COLOR_3680",
            "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_3690",
            "DISPLAY_UPDATE_DIGITAL_MODES_MILESTONE_37A4",
            "DISPLAY_UPDATE_DIGITAL_MODES_TIMING_NEXT",
            "APPLEDISPLAY_MODE_BRANCH", "APPLEDISPLAY_MODE_SKIP",
        }
    elif slim == "mipi-ap":
        # Small mode-source discriminator: observe the active display's
        # provider/property queries without installing the broad renderer set.
        want = {
            "IOMFB_MODE_KERN_ENTRY",
            "IOMFB_MODE_COLOR_PROPERTY_RESULT",
            "IOMFB_MODE_COLOR_TYPE_COMPARE",
            "IOMFB_MODE_TIMING_PROPERTY_RESULT",
            "IOMFB_MODE_TIMING_TYPE_COMPARE",
            "IOMFB_MODE_KERN_RETURN",
            "IOMFB_MODE_VIRT_DISPATCH",
            "IOMFB_MODE_VIRT_RETURN",
        }
        if os.environ.get("RFT_TRACE_CALAYER_CONTENTS") == "1":
            want.add("CALAYER_SET_CONTENTS_ENTRY")
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
        RUNTIME_ADDR[label] = runtime
        HITS[label] = 0
        result = lldb.SBCommandReturnObject()
        interpreter.HandleCommand(
            "breakpoint command add -F render_gate_callbacks.on_break %d" %
            bp.GetID(), result)
        print("COMMAND_LIST_PROOF id=%d label=%s address=0x%x" % (
            bp.GetID(), label, runtime), flush=True)
    # EARLY_KERNEL_TRACE's LLDB process detaches after slide resolution. The
    # remote breakpoints are not a substitute for hooks in this new session,
    # so install the opt-in kernel hooks again alongside the userland probes.
    install_kernel(debugger)

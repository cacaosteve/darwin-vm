# QuartzCore render-bounds follow-up (2026-09-23)

## Result

The profile-correct six-CPU boot can submit a real IOSurface and A408 when the
diagnostic copies the staged display size into empty type-`0x1f` mutator bounds.
The captured surface is a uniform blue fill, not a Home or Setup screen. With
the intervention disabled, the same boot reaches an empty type-`0x1f` mutator
but produces no `CREATE_SURFACE`, framebuffer handoff, or A408 during the
120-second probe window. The remaining gate is therefore natural scene or
mutator geometry production; display dimensions and the AP-to-DCP transport
are already sufficient for the forced surface path.

## Valid runs

All runs below used the known-good `bp-patched-dev6` `system.tc`, `bootkc`, and
device tree; `-smp 6`; `DARWIN_SMP_PV=1`; the established DCP level-4, D120, and
D586 settings; and a fresh qcow2 child of `/tmp/dvm/SKS_INSTALL1/disk.qcow2`.
Each probe used a fresh qcow2 child, leaving the shared parent unchanged. There
were no XNU panics. GATE67 and GATE68 are excluded because their composite
callback module was not registered correctly in LLDB; they provide no runtime
evidence.

| Run | Bounds intervention | Observation |
| --- | --- | --- |
| `UI_RENDER_GATE66` | `RFT_BOUNDS_FROM_DISPLAY=1` | `SERVER_RENDER_UPDATE`, `DISPLAY_ALLOCATE_SURFACE`, `CREATE_SURFACE`, `ALLOCATE_IOSURFACE`, and non-null `FB_SWAP_SET_LAYER` all fired. The model accepted A408 and completed nested D594 callbacks. No `AD_SIZE_PARENT` hit appeared before the handoff. |
| `UI_RENDER_GATE69` | `RFT_BOUNDS_FROM_DISPLAY=1` | Captured a 1179×2556 BGRA IOSurface from `backboardd`; A408 presented it and D594 completed. All 3,013,524 visible pixels were the same color. |
| `UI_RENDER_GATE70` | disabled | First `MUT_BOUNDS_CHECK` object had type byte `31` (`0x1f`) and four zero floats. No surface creation, framebuffer handoff, A408, or `AD_SIZE_PARENT` hit occurred in 120 seconds. |
| `UI_RENDER_GATE72` | disabled | Repeated the natural control with a live CA Mach-message receive hook. The first eight receives in `backboardd` had IDs `0x9c41` and `0`; no message ID `0x36` or surface handoff was observed in 120 seconds. |

## Pixel capture

`UI_RENDER_GATE69.events/success.VISIBLE_IOSURFACE.json` records width 1179,
height 2556, stride 4864, fourcc `BGRA`, 12,432,384 captured bytes, and source
SHA-256
`a82bc91cba4e4f1b3f06c27edfa32af0a1ec0d357fbe51527b2ec269e9b5a503`.
The active pixel bytes are identical BGRA `[255, 0, 0, 255]` at every pixel;
the converted image is solid blue. This is evidence of a completed IOSurface
handoff, not rendered UI content.

![GATE69 captured IOSurface](/tmp/dvm/UI_RENDER_GATE69.frame.png)

The rendered image is also at `/tmp/dvm/UI_RENDER_GATE69.frame.png`, with the
raw capture under `/tmp/dvm/UI_RENDER_GATE69.events/`.

## Natural-control details

In GATE70 and GATE72, the empty type-`0x1f` object was observed before any
geometry write. Other mutators in those same runs had different bounds, and a
later `MUT_W25_BIT22_GATE` sample showed `w25=1` with `will_orr_bit22=False`.
The dedicated `MUT_W25_SET1` site did not fire, so that later gate is not yet
correlated to the first empty object. Keep the “empty bounds → `w25=1`” link
open until the same mutator instance is traced across both points.

The CA receive probe uses the existing render-server `mach_msg` return site
(`0x1843f9804`) and samples ordinary IDs as a positive control before filtering
for `0x36`. It confirms the hook is live, but no `0x36` arrived at that receive
site in the bounded run. The same run reached `HOTPLUG_SETUP_FN_ENTRY` six
times and returned `IOReturn=0, state=0` from `get_hotplug_state`; repeated
A385 polls returned zeroed outputs. This does not identify whether a sender
attempted message `0x36` on another port or path.

## Send-side trace attempt (GATE73)

`UI_RENDER_GATE73` installed `mach_msg2_trap` and `mach_msg_trap` callbacks and
confirmed ordinary outgoing IDs (`0xce`, `0xd5a`) from SpringBoard and
backboardd. It produced no `0x36` match, but this is **inconclusive**: both
breakpoints are global shared-cache syscall entrypoints, so every process
stops in LLDB before the callback can filter by process. The 120-second run
collected only 647 serial lines, reached `DVM_INPUT_INITIALIZING`, and had no
render-gate or mutator breakpoint hits. Treat the global syscall tracing as
too intrusive to answer the display question; do not interpret its missing
`0x36` as evidence that the message was not sent.

## Next probe

Keep bounds injection off. The complete dyld shared-cache set is restored under
`~/dvm-artifacts/extract/dyld/` (including `.02` and `.symbols`, copied from the
mounted `rootfs_cx.dmg` System volume on 2026-09-23).

Static RE on that cache shows:

- `CA::Render::Server::server_thread` (`0x1843f9664`) dispatches msgh_id in the
  `0x9c40`–`0x9c45` family (GATE72 already sampled `0x9c41` on the receive
  side). Literal `mov #0x36` sites in QuartzCore are error/type codes, not
  msgh_id stores; `server_thread` does not dispatch msgh_id `0x36`.
- The client builder at `0x184404ab0` selects among `0x9c42`/`0x9c43`/`0x9c44`/
  `0x9c45` and stores msgh_id with `stp wzr, w9, [x24, #0x10]` at
  `0x184404c54`. The send wrapper at `0x184404f6c` calls `mach_msg`
  (`0x18813aff0`) with `MACH_SEND_MSG`.

`tools/re/ca_msg36_send_callbacks.py` now breaks on those QuartzCore sites by
default when `DVM_CA_MSG36_SEND_TRACE` is unset/1; global syscall traps remain
behind `DVM_CA_MSG36_SEND_GLOBAL=1`. Receive hooks accept the `0x9c4x` set.
`render_gate_callbacks.LAST_MUT` records bounds/`w25` with `tpidr_el1` so send
and receive events can correlate the same thread.

Run `UI_RENDER_GATE74` with bounds injection off and the QC-path send tracer
enabled; correlate msgid traffic with the empty type-`0x1f` mutator and the
`w25` bit-22 decision on the same `tpidr_el1`.

### GATE74 result (2026-09-24)

Rebuilt `/tmp/dvm` after the host `IOMediaBSDClient` reboot: parent is a fresh
qcow2 child of durable `~/dvm-artifacts/bp-patched-dev6/boot1/disk.qcow2`;
harness is `/tmp/dvm/setup_gate_probe_bootkc.sh` (BOOTKC/MEM/smp6 + Early-boot
slide wait). `MEM=6G`, `RFT_BOUNDS_FROM_DISPLAY=0`, QC-path send tracer on.

| Metric | Value |
| --- | --- |
| Verdict | `EXIT:0`, 0 XNU panics, guest left frozen |
| Slide | `0x1a7e4000` after Early boot +25 s |
| COMMAND_LIST_PROOF | 143 (hooks installed; no SyntaxError) |
| `CA_MSG_RECV` | 21×, all msgh_id `0x9c41`, size 28, empty body, `last_mutator=null` |
| `CA_MSG_SEND` / `CA_COMMIT_CALL` | 0 runtime hits |
| `CREATE_SURFACE` / `ALLOCATE_IOSURFACE` / A408 | 0 |
| `MUT_BOUNDS_CHECK` / `MUT_W25_*` | 0 runtime hits |
| Render path | `OUTER_A`→`RFT_SHORT_SKIP` ×13; `+0x2bb` bit4 clear |
| SpringBoard | `CADISPLAY_MAIN` entry/return ×8; no critical-process reboot in window |

Natural control still never creates a surface. Receive-side traffic is only the
empty `0x9c41` keepalive family; no commit-class `0x9c42`–`0x9c45` arrives, and
the mutator bounds/`w25` sites never run because the server stays on the short
skip. The immediate short-skip predicate is now decoded below; its upstream
flag producer and the missing client commit remain open.

### Why `RFT_SHORT_SKIP` wins (static decode + GATE74)

The branch at `0x1844100e8` goes to the full path only when both conditions
hold: the count of server display-list entries differs from the number whose
flags have bit 17 set, and the temporary entry list is non-null. The relevant
code loads the total count from `[server+0x80]`, walks the 16-byte entries at
`[server+0x70]`, counts bit 17 in each entry's word at `+0x0c`, and compares
those values. If the list is empty or every entry has bit 17 set, it takes the
short path.

GATE74 recorded equality on all 13 decisions: `0 == 0` at the first hit, then
`1 == 1` for the remaining list snapshots. So the direct reason is that the
server sees no entry with bit 17 clear. The trace does **not** establish what
bit 17 means or which code sets it; it also does not prove this server-side
eligibility flag is the same producer as the separate client commit builder.
The next causal probe should identify the list-entry constructor/updater and
the writer of `[entry+0x0c].bit17`, while retaining the existing hooks on the
client commit-builder path.

### GATE75 result (2026-09-24)

Natural probe (`RFT_BOUNDS_FROM_DISPLAY=0`, `RFT_SLIM=1`) with new hooks:
`BIT17_LIST_WALK` (`0x18440ff88`), `BIT17_CLEAR_STORE` (`0x18441ceb0`),
`BIT17_ORR_LSL8_STORE` (`0x18441cfe4`), plus `CA_WALK360_RET` (`0x184401e84`).
`MEM=6G`, same bp-patched-dev6 parent lineage. `EXIT:0`, 86 proofs, 0
SyntaxError. SpringBoard later hit critical-process reboot → `Halt/Restart`
(consequence of reboot, not the display gate).

| Observation | Evidence |
| --- | --- |
| Short skip | `RFT_SHORT_SKIP` ×14; `RFT_FULL_PATH` never |
| Predicate | All non-empty decisions: `count==bit17_count==1`, temp list **non-null** |
| Sole entry | Always one display-list object; `+0xc=0x2420a`, **bit17=1** every walk |
| Known mutators | `BIT17_CLEAR_STORE` and `BIT17_ORR_LSL8_STORE` **0 hits** — bit17 was already set before those sites |
| Commit path | `CA_WALK360_RET` / `CA_COMMIT_CALL` / msgid store: proofs only, **0 runtime** |
| CA recv | Still only empty `0x9c41` ×21 |

So GATE75 answers *which* object is ineligible (the single list entry with
`+0xc=0x2420a`) but not *who first set bit 17*. The two known +0xc RMW sites
that clear or conditionally inject bit17 never ran. Next: find the constructor
or earlier writer that installs `0x2420a` (or sets bit17) on that entry family,
still separately from the absent `ctx+0x360` walk/commit traffic.

### GATE76 result (2026-09-24)

Natural probe with GATE76 flag-builder hooks (`FLAGS_INIT_3E`,
`FLAGS_ORR_FINISH`, `FLAGS_ORR_BIT13_*`, `FLAGS_BIT9_INJECT`). `EXIT:0`, 91
proofs, 0 panics; SpringBoard later critical-rebooted (Halt/Restart consequence).
All new FLAGS_* sites: **0 runtime hits**. The sole list entry remained
`+0xc=0x2420a` with runtime vt `0x1f9304ab0` (static `0x1e904cab0`, slide
`0x102b8000`). So the `0x3e` clone path is not how this display-list object is
built.

### Who builds `0x2420a` (static, pre-GATE77)

The entry's vtable `0x1e904cab0` is installed by the constructor at
`0x1845189dc`:

1. `mov w8,#0xa; str [x0,#0xc]` at `0x184518a1c` — starts with bits 1+3 only.
2. Late `orr w8,w8,w20,lsl#8; str [x19,#0xc]` at `0x184518ed0` — copies ctor
   arg `w3` (kept in `w20`) into bits 8+. With `w20==0x242`, that yields exactly
   `0xa | (0x242<<8) == 0x2420a` (bits 1,3,9,14,17).
3. One known caller passes `w3=#0x240` at `0x184773b44` (bits 14+17 only →
   `0x2400a`); another at `0x18451ae64` computes `w3` from an input word.

A separate direct `orr #0x20000` store exists at `0x1845023ec` (five BL
callers). GATE77 hooks the ctor flag sites and both callers to see which path
produces the live `0x2420a` before the first `BIT17_LIST_WALK`.

### GATE77 result (2026-09-24)

Natural (no poke). Caller `DISP_CTOR_CALL_240` passes `w3=#0x240`;
`DISP_CTOR_ORR_LSL8` stores `+0xc=0x2400a`; list walk later sees `0x2420a`
(`bit17=1`). `RFT_FULL_PATH` **never** hits; only `RFT_SHORT_SKIP` /
`OUTER_A_FN_ENTRY`. Post-ctor someone ORs bit9 (`0x2400a` → `0x2420a`);
`BIT17_ORR_DIRECT` never runs. SpringBoard critical-reboot → Halt/Restart.

### GATE78 result (2026-09-24) — `RFT_CLEAR_CTOR_W3_BIT9=1`

Poke clears `w3` bit9 at the `#0x240` callsite (`0x240` → `0x40`) and at
`ORR_LSL8` when needed. Ctor stores `+0xc=0x400a` (bits 1+3+14, **no bit17**).
Walk sees `0x420a` (`bit17=0`; post-ctor still ORs bit9 only). Decision at
`0x1844100e8`: `slot_1c8=1`, `x26=bit17_count=0` → `b.ne` **FULL** (`×12`).
GATE77 had **0** FULL hits.

Still no `RFT_FULL_BEFORE_V170` / `CALL_OUTER_A` / `CREATE_SURFACE`. Clearing
bit17 is necessary for FULL fallthrough but not sufficient for CREATE. Naturalize
later: stop passing `w3` bit9 at `0x184773b44`.

### GATE79 result (2026-09-24) — same poke + early FULL markers

FULL×13 reaches `blraa` on `[ctx.vtable+0x178]` (`ctx=[server+0x60]`,
`w1|=8`, float args d0/d1). Return is **always `w0=0xff`** →
`cbnz` to `RFT_FULL_SKIP_V178` (`0x184411b4c`). Zero hits on V178_OK / +0x548 /
v248 / v218 / vf8 / v170 / CALL_OUTER / CREATE.

At the skip landing, `cmp w0,#0xff` treats 0xff as a **named sentinel** (other
nonzero goes to `0x184411cb0`). Next: identify the `+0x178` method on the ctx
vtable and why it returns 0xff (GATE80 dumps `recv`/`vt`/`slot178`).

### GATE80 result (2026-09-24)

`vt=0x1e9047c58` (runtime `0x1f3e7bc58`, slide `0xae34000`), method at
`vt+0x178` = `0x18442f208`. Body:

1. `state = [self+0x330]`; require `state[0]==1 || state[1]==1`, else **`return 0xff`**.
2. With `w1` bit2 clear (call passes `w1|=8`): require `state[3]==0`, else 0xff.
3. Further: `state[5]==1` or return `0xe9`; `[self+0x2b8]!=0` or `0xfe`; etc.
4. Success returns `0`.

All 12 hits: `w0=0xff` — failing step 1. Same `+0x330` blob the hotplug hooks
already name (`hotplug_slot_330`). GATE81 dumps `state[0..5]` at the return.

### GATE81 result (2026-09-24)

`state330` is non-null but **`bytes=(0,0,0,0,0,0)`** on every v178 call;
`+0x2b8=None`. So the 0xff is exactly “hotplug latch never armed.”
`GET_HOTPLUG_STATE` / `UPDATE_FB_GET_HOTPLUG` still have **0 runtime hits**
(GATE58). Next: find who stores `1` into `state[0]`/`state[1]`, and what
installs the `+0x2b8` pointer (would be the next failure `0xfe` after the latch).

### GATE82 result (2026-09-24) — `RFT_POKE_HOTPLUG_LATCH=1`

At `RFT_TIME_DECISION`, poke `state[0]=1` and `state[5]=1`. v178 then returns
**`w0=0xfe` every time** with `bytes=(1,0,0,0,0,1)` and `+0x2b8=None` — exactly
the `cbz [self+0x2b8] → 0xfe` arm in `0x18442f208`. Latch was the 0xff cause;
**null `+0x2b8` is the next gate**. Still no V178_OK / CREATE.

Naturalize: (1) arm the `+0x330` latch (IOMFB hotplug / `get_hotplug_state`,
see `docs/re/iomfb-hotplug-swap-gate.md`), (2) install the `+0x2b8` object the
older bit4/`V170` work already tracked.

### GATE83 result (2026-09-24) — latch + `+0x2b8=-1`

v178 returns **`w0=0`** (×9) → `V178_OK` → CHECK_548 → v248/v218/vf8 →
**V170**. But poke used `-1`, so `+0x2bb=255`/`bit4=1` → `v170_ret w0=0x1`
(skip CALL_OUTER). Layer walk runs with `has_28000=False`. No CREATE.
GATE84: poke `+0x2b8=1` (non-null, bit4 clear).

### GATE84 result (2026-09-24) — latch + `+0x2b8=1`

Confirmed: `+2b8=0x1`, `+0x2bb=0`/`bit4=0`, v178 **`w0=0`**, `V178_OK` →
CHECK_548 → v248 → TBNZ_109CC (`w0=0`, fallthrough). Also first runtime
`GET_HOTPLUG_STATE` hit: `ior=0xe00002c2 state=0`. Probe SIGINT’d (~3 min
post-slide) before V218/V170 — only 33 hits. GATE85 repeats with `SECS=360`.

### Commit-path linkage (static)

Three BL sites call the msg builder `0x184404ab0`. The primary in-process one
is `0x184401eb0` inside `0x184401db0`: it copies `[walk+0x14]` into
`ctx+0x1e8` after `bl 0x18469aadc` on `ctx+0x360` (the same layer-walk slot as
`LAYER_WALK_ORR_80` / mask `0x28000` work), then sends with port
`[ctx+0x228]` and `w1=[ctx+0x210]+0xa8`. So empty mutator bounds and the
missing `0x28000` mask bit are upstream of whether a useful `0x9c4x` commit
is posted. `ca_msg36_send_callbacks` now also breaks at `CA_COMMIT_CALL`
(`0x184401eb0`).

## Artifacts

- LLDB logs: `/tmp/dvm/UI_RENDER_GATE66.lldb.log`,
  `/tmp/dvm/UI_RENDER_GATE69.lldb.log`, `/tmp/dvm/UI_RENDER_GATE70.lldb.log`,
  `/tmp/dvm/UI_RENDER_GATE72.lldb.log`
- Serial and model logs: `/tmp/dvm/probe/UI_RENDER_GATE{66,69,70,72}.serial.log`
  and matching `.stderr.log` files
- Pixel event and raw data: `/tmp/dvm/UI_RENDER_GATE69.events/`
- Inconclusive send-trace logs: `/tmp/dvm/UI_RENDER_GATE73.lldb.log`,
  `/tmp/dvm/UI_RENDER_GATE73.probe.out`, and
  `/tmp/dvm/probe/UI_RENDER_GATE73.serial.log`
- Composite callbacks: `tools/re/render_gate_surface_callbacks.py` and
  `tools/re/ca_msg36_callbacks.py`; the QC-path send tracer in
  `tools/re/ca_msg36_send_callbacks.py` (sites `0x184404c54` /
  `0x184404f6c`). Global syscall traps stay opt-in via
  `DVM_CA_MSG36_SEND_GLOBAL=1` and must not be enabled for a full boot.
- Restored host cache set: `~/dvm-artifacts/extract/dyld/dyld_shared_cache_arm64e*`
  (77 subcaches + `.symbols`).

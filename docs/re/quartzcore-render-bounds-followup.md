# QuartzCore render-bounds follow-up (2026-09-23)

## Result

The earlier forced runs did reach IOSurface and A408, but they do **not** prove
that natural geometry was the blocker. GATE166 established that the four floats
at type-`0x1f` object `+0x10` are `backgroundColor` RGBA. The historical
`RFT_BOUNDS_FROM_DISPLAY` intervention writes `(0,0,1179,2556)` into those color
channels, which explains the uniform blue test surface. The first natural
`0x9c42` layer object already had position `(589.5,1278)` and size
`1179×2556` at `+0x38/+0x58`; its `contents` object at `+0x68` was null. The
`MUT_BOUNDS_CHECK` label is therefore a color/coverage classification, not a
geometry check. Natural scanout and Home/Setup pixels remain unachieved; the
first natural type-31 layer already has display-sized geometry. GATE244–245
show that the sampled display-sized opaque-black layers use a null image
contents pointer, while the active display's unsupported-mode query leaves
`Mode(-1)` in place and V170 exits before `Server::render_update`. The most
directly established display-update gate is therefore mode publication;
`contents` and the bit-22 classification remain separate renderer inputs.
Older gate notes below preserve names and interpretations used at the time;
when they say “bounds,” read that as the four-float field at `+0x10` until
explicitly corrected here.

## Valid runs

All runs below used the known-good `bp-patched-dev6` `system.tc`, `bootkc`, and
device tree; `-smp 6`; `DARWIN_SMP_PV=1`; the established DCP level-4, D120, and
D586 settings; and a fresh qcow2 child of `/tmp/dvm/SKS_INSTALL1/disk.qcow2`.
Each probe used a fresh qcow2 child, leaving the shared parent unchanged. There
were no XNU panics. GATE67 and GATE68 are excluded because their composite
callback module was not registered correctly in LLDB; they provide no runtime
evidence.

| Run | Historical intervention | Observation |
| --- | --- | --- |
| `UI_RENDER_GATE66` | `RFT_BOUNDS_FROM_DISPLAY=1` | `SERVER_RENDER_UPDATE`, `DISPLAY_ALLOCATE_SURFACE`, `CREATE_SURFACE`, `ALLOCATE_IOSURFACE`, and non-null `FB_SWAP_SET_LAYER` all fired. The model accepted A408 and completed nested D594 callbacks. No `AD_SIZE_PARENT` hit appeared before the handoff. |
| `UI_RENDER_GATE69` | `RFT_BOUNDS_FROM_DISPLAY=1` | Captured a 1179×2556 BGRA IOSurface from `backboardd`; A408 presented it and D594 completed. All 3,013,524 visible pixels were the same color. |
| `UI_RENDER_GATE70` | disabled | First `MUT_BOUNDS_CHECK` type-31 object had four zero RGBA floats. This is a color classification, not proof of zero geometry. No surface creation, framebuffer handoff, A408, or `AD_SIZE_PARENT` hit occurred in 120 seconds. |
| `UI_RENDER_GATE72` | disabled | Repeated the natural control with a live CA Mach-message receive hook. The first eight receives in `backboardd` had IDs `0x9c41` and `0`; no message ID `0x36` or surface handoff was observed in 120 seconds. |
| `UI_RENDER_GATE154` | `RFT_BOUNDS_FROM_DISPLAY=1` | Reached `0x9c42`, `CREATE_SURFACE`, framebuffer handoff, and A408 twice. `dart-disp0` query returned active; the IOMMU map helper and generic map returned 0. The 1179×2556 IOSurface was a uniform blue fill. The intervention changed RGBA, not geometry. |
| `UI_RENDER_GATE155` | disabled | After initial short skips, observed natural `0x9c42` send/receive and full-path execution. The RGBA color check yields `w25=1`; V170 returns 1 and no surface handoff/A408 occurs. |
| `UI_RENDER_GATE162` | disabled | Matched full 592-byte sender and receiver bodies byte-for-byte. Four `00 00 1f 02` markers each precede 16 zero bytes, which GATE166 reclassifies as RGBA payload, not bounds. The client context-layer walk returned null, yet the builder still sent `0x9c42`. |
| `UI_RENDER_GATE163` | disabled | Captured the 585-byte encoder queue before building `0x9c42`; it equals body bytes `[4:589]` exactly. The four zero RGBA records are already queued; sender and receiver body hashes match. |
| `UI_RENDER_GATE165` | disabled | `commit_transaction` appends a 9-byte type-`0x1f` timing tail at queue offset 576. It does not write the four earlier color records. The layer commit callback sends objects through `CA::Render::encode_set_object`. |
| `UI_RENDER_GATE166` | disabled | Object is identical at fetch return and serializer entry. Its natural RGBA is black/opaque, position is `(589.5,1278)`, size is `1179×2556`, and `contents+0x68` is null. Static source confirms `+0x10` is backgroundColor and `+0x58/+0x60` are layer size. |
| `UI_RENDER_GATE167` | disabled | Backboardd's active server had two list entries at `+0xc=0x40420a` (bit 17 clear), so it naturally entered the full path. V178/V248 passed, VF8 returned 1, then V170 returned 1 with display mode `+0x2b8=-1`/bit 4 set. No surface handoff/A408. The earlier-added `Layer::set_contents` hook was not on `_copyRenderLayer`'s direct field-copy path and provides no content-absence evidence. |
| `UI_RENDER_GATE168` | disabled | Seven `_copyRenderLayer` source `contents` getter returns were all null; none reached the non-null decision/store. The active display still had `+0x2b8=-1`; V170 returned 1 into the skip. No surface/update/A408. |
| `UI_RENDER_GATE169` | disabled | Confirmed that `update_framebuffer_locked` inserts `Mode(-1)` into an initially empty `ModeSet`, then passes `x2=-1, x3=0` to `set_all_modes` on the active display. Its `+0x2b8/+0x2c0` remain `-1`, and V170 returns 1. |
| `UI_RENDER_GATE170` | disabled | Active display is signal type 1, but `_IOMobileFramebufferGetSupportedDigitalOutModes` returns `0xe00002bc`; GATE184–186 tie this to a null `ColorElements` IORegistry property read on BackBoard's selected service. |
| `UI_RENDER_GATE175` | disabled | Identified the type-2 provider dispatch as MediaToolbox `_FigVirtualFramebuffer_GetSupportedDigitalOutModes`; it did not reproduce the type-1 query. |
| `UI_RENDER_GATE177` | disabled | Natural repeat showed only type-2 displays before SpringBoard's critical-process restart; later shutdown timeout panics are secondary. |
| `UI_RENDER_GATE178` | disabled | Type-2 mode provider returned status 0 while both output locals remained null; this differs from GATE170's kernel provider path. |
| `UI_RENDER_GATE179` | disabled | Natural repeat again showed only type-2 displays before SpringBoard's critical-process restart. |
| `UI_RENDER_GATE180` | disabled | Early open snapshots showed indices 0–4 as type 2 and stopped before the later type-0 index-5 append; they do not establish that type 0 is absent. Type-2 outputs were null. |
| `UI_RENDER_GATE181` | disabled | Natural repeat reached SpringBoard's critical-process restart. Its count watchpoint was eight bytes past `_s_display_infos_len`, so its lack of hits is inconclusive. |
| `UI_RENDER_GATE182` | disabled | Early stop caught a type-0 descriptor in `thermalmonitord`; this was not the BackBoard display path. |
| `UI_RENDER_GATE183` | disabled | Caught BackBoard appending type-0 descriptor index 5 and successfully opening its service; the display still kept the `-1` mode sentinel. Stopped before the type-1 query. |
| `UI_RENDER_GATE184` | disabled | Reproduced the type-1 kernel-backed mode query on the index-5 service; it returned `0xe00002bc` with null outputs. |
| `UI_RENDER_GATE185` | disabled | The selected service's `ColorElements` property read returned null; the API never requested `TimingElements` and left both outputs null. |
| `UI_RENDER_GATE186` | disabled | Reproduced the missing `ColorElements` property on BackBoard's selected IORegistry service. The IOAV video-interface property-publication method was not entered before the mode query. |
| `UI_RENDER_GATE197` | disabled | The DCP AV video-interface EPIC endpoint was announced and opened, but the candidate constructor hooks did not fire. This did not establish that service creation failed. |
| `UI_RENDER_GATE198` | disabled | `DCPAVProxy::start` returned 1 on 13 observed calls, and the `EPICLocation` property lookup returned non-null on all 13. The old missing-location diagnosis is not the current gate. |
| `UI_RENDER_GATE199` | disabled | The seven-handler `DCPAVPowerControllerProxy` path reached `registerService`; its base start returned 1. This did not establish video-interface proxy startup. |
| `UI_RENDER_GATE201` | disabled | Repeated the power-controller path and stopped at BackBoard's natural type-1 mode query; `ColorElements` remained null (`0xe00002bc`). The actual video-interface proxy was not traced by this run. |
| `UI_RENDER_GATE202` | invalid | The corrected video-interface entry was reached, but its LLDB callback raised `UnboundLocalError` while reading `x20`; do not use this run for startup or message conclusions. |
| `UI_RENDER_GATE203` | disabled | The actual five-handler video-interface proxy started, its shared base start returned 1, it called `registerService`, and its subclass returned 1. No common `sendMessage` or `handleCommand` call on that proxy object appeared before BackBoard's natural mode query; `ColorElements` remained null (`0xe00002bc`). |
| `UI_RENDER_GATE204` | disabled | Expanded common DCPAV message tracing to all proxy vtables and found three successful command sends (interfaces 1, 8, and 10). Its payload snapshot used the wrong offset and is superseded by GATE207. |
| `UI_RENDER_GATE205` | disabled | Reached the command-gate action for those sends. The bit-1 action labels were initially reversed; GATE206 disassembly corrected the branch interpretation. |
| `UI_RENDER_GATE206` | disabled | Decoded the flags branch: all three observed commands take the bit-1-clear route through the helper and AFK endpoint vtable slots `+0x548`/`+0x578`; this is transport-path evidence, not a reply-schema explanation. |
| `UI_RENDER_GATE207` | disabled | Corrected the DCPAV payload offset to `message+0x40`; all three request payloads were zero-filled. QEMU received AP commands on interfaces 1, 8, and 10 and queued its synthetic zero-tail responses. |
| `UI_RENDER_GATE208` | invalid for response snapshots | Reached the mode-query boundary, but the new response callback raised `UnboundLocalError` because `x23` was not captured. |
| `UI_RENDER_GATE209` | disabled | Clean response-site snapshots: transport returned 0 for the controller and DP-device sends, with zero response bytes before parser/output stores. The natural mode query still found null `ColorElements` (`0xe00002bc`). |
| `UI_RENDER_GATE210` | invalid boot profile | Used the repository-default boot kernel/device tree with the SKS system volume and failed at launchd (`libSystem.B.dylib` not found). It did not reach the display probe. |
| `UI_RENDER_GATE211` | disabled | With the known-good dev6 boot profile, interface 16 was announced but never opened. No `DCPMIPIControllerProxy::start` hook fired before the natural type-1 query (`0xe00002bc`). |
| `UI_RENDER_GATE212` | disabled | Added the class allocator and constructor candidates; none fired, and interface 16 still had no `REPORT OPEN`. This moves the observed gap earlier than proxy `start`, to announcement handling or IOKit matching/instance creation. |
| `UI_RENDER_GATE213` | invalid | The new early PUBLISH callback raised `UnboundLocalError`; do not use its PUBLISH observations. |
| `UI_RENDER_GATE214–219` | disabled | PUBLISH decode/handoff progressed through the external DPTX/HDCP announce chain and stalled waiting for an external AV-interface match; these services are not the internal MIPI panel. |
| `UI_RENDER_GATE220` | disabled | Skipping LPDPTX advanced the announce queue to HDCP but still did not expose the MIPI endpoint. |
| `UI_RENDER_GATE221` | disabled | Skipping the external DPTX/HDCP services let `dcpmipi-controller-epic` publish, match, start, register, and open interface 12. `ColorElements` remained null at BackBoard's natural query. |
| `UI_RENDER_GATE222` | disabled | Confirmed MIPI proxy startup and armed IOAV property hooks. They stayed silent; the DCPAV common-message callback still had its default video-vtable filter. |
| `UI_RENDER_GATE223` | disabled | Enabled all-vtable DCPAV tracing: only controller, SAC, and DP-device proxies sent commands; no `handleCommand` or video-proxy message ran. The IOAV video setup and `withProperties` path also stayed silent. |
| `UI_RENDER_GATE224` | disabled | Added the IOAV interface-tracker entry, ID-compare, and conditional-child-call hooks. None ran before the same null-`ColorElements` query. |
| `UI_RENDER_GATE225` | disabled | AppleCLCD2 `start` returned success on `disp0` with provider equal to its registry object. No framebuffer width/height DeviceTree read occurred; the type-1 query still returned `0xe00002bc`. |
| `UI_RENDER_GATE226` | disabled | Confirmed interface 12 emitted only its initial MIPI PUBLISH report; no later AFK report arrived after the AP opened it. |
| `UI_RENDER_GATE227` | disabled | Added breakpoints at `IOAVHandlerInternalDisplay::plugEvent_async_gated` and its color/timing/open failure branches. None ran; BackBoard still got null `ColorElements` (`0xe00002bc`). |
| `UI_RENDER_GATE228` | disabled | Traced the base `IOAVHandler::IOAVEvent` cast and plug/unplug dispatch. No entry or branch ran before BackBoard's query. |
| `UI_RENDER_GATE229` | disabled | Traced internal-display handler allocation/base construction, its setup method, and the service-event registration helper. None ran; the AP-side handler is absent before the mode query, not merely waiting inside its event callback. |
| `UI_RENDER_GATE230` | disabled | Resolved the five video-class allocator targets from their live metaclass `+0x68` slots and traced `IOAVInterface::start`. The start hook stayed quiet; allocator hooks were installed only in the post-early session, so this run cannot exclude earlier allocations. |
| `UI_RENDER_GATE231` | disabled | Re-armed those five allocator targets as static early-boot breakpoints and retained the natural type-1 query. No allocator or `IOAVInterface::start` entry ran before the query; it still returned null (`0xe00002bc`), with zero XNU panics. |
| `UI_RENDER_GATE232` | invalid boot profile | Setup defaults paired the wrong boot inputs; launchd failed to load `libSystem.B.dylib`. Do not use it as display evidence. |
| `UI_RENDER_GATE233` | disabled | Repeated the known-good boot profile with a reset-time internal-display factory hook. The natural type-1 query still returned `0xe00002bc`; the factory remained unentered in the observed interval. |
| `UI_RENDER_GATE234` | disabled | Full 240-second run with reset-time factory, metaclass allocator, video-class allocator, and `IOAVInterface::start` hooks. None ran; type-1 still returned `0xe00002bc`, with zero XNU panics. |
| `UI_RENDER_GATE235` | disabled | Added reset-time `IOAVController` start/open/add-device hooks and renderer-path hooks. No controller/factory entry ran. Some auxiliary lists still short-skipped on bit 17, but the active display's bit-17-clear entries entered FULL; all 32 sampled `IOMFBDisplay::ignore_update_p` calls returned 1 before any surface creation. |
| `UI_RENDER_GATE244` | disabled | Eleven copied CALayers yielded null `contents`; display-sized type-31 layers were opaque black. The active type-1 mode query again returned `0xe00002bc`, leaving `Mode(-1)` and exiting at V170 before surface creation. |
| `UI_RENDER_GATE245` | disabled | Direct `-[CALayer setContents:]` tracing paired two BackBoard display-sized black layers with `setContents:nil`; all 11 copied-layer getters were null. This is consistent with solid-color backgrounds. The mode gate and no-surface result persisted. |

### GATE154 result (2026-09-25) — display DART is available; forced-color surface reaches A408

This run used the profile-correct six-vCPU PV boot (`bp-patched-dev6` kernel,
tree, and trust cache) and explicitly set `RFT_BOUNDS_FROM_DISPLAY=1`. It
produced commit-class `0x9c42` receives, an injected type-`0x1f` RGBA value
derived from A453 (1179×2556), `SERVER_RENDER_UPDATE`, `CREATE_SURFACE`, and two
non-null framebuffer handoffs. Thus GATE153's lack of a surface was a missed
intervention setting, not evidence that the forced render path had regressed.

The display DART query now identifies the actual provider as `dart-disp0`:
`flags+0x988=0xc0000065` (manual-availability property present),
`state+0x99a=1`, and query result `1`. The serial log also records the display
DART power-up path returning 0. The IOMMU map helper and generic map return 0;
the kernel builds A408 twice, and the device-model trace reports A408
completion with status 0. This contrasts with GATE149, where the same provider
query returned 0 and the attempted map failed as “inactive DART.” The
`manual-availability` device-tree property was already present; the remaining
question is what boot/power sequence difference left the DART unavailable in
GATE149, rather than whether a property must be added.

The slim IOMMU callback now filters state-transition hits by the provider name
`dart-disp0`, preventing unrelated `dart-dcp` startup transitions from using
the display trace's bounded hit budget.

Both GATE154 IOSurface captures are 1179×2556, stride 4864, and byte-for-byte
identical to the known uniform blue fill. A408 acceptance and a successful map
therefore establish the forced submission pipeline, but they still do not
produce Home/Setup pixels. GATE153 should be treated as a setup miss: its
profile/PV configuration was correct, but the shell command omitted
`RFT_BOUNDS_FROM_DISPLAY=1`. The guest was deliberately quit after its second
successful A408; the wrapper recorded zero XNU panics.

### GATE155 result (2026-09-25) — natural commit arrives; color classification skips rendering

GATE155 used the same profile-correct boot, six-vCPU PV setup, DCP settings,
and callback set as GATE154, with only `RFT_BOUNDS_FROM_DISPLAY=0`. It first
recorded repeated `0x9c41` keepalives and `RFT_SHORT_SKIP`; roughly three
minutes into startup, the client send tracer caught a `0x9c42` send and the
server receive hook caught that message. The first 120-second natural probes
therefore ended before this delayed traffic appeared.

After the `0x9c42`, the display list includes entries with `+0xc=0x40420a`
(bit 17 clear), and the full path reaches V178 successfully. The captured
type-`0x1f` `backgroundColor` RGBA remains `(0,0,0,1)` or all zeros; there is no
`rft_bounds_from_display` write. At `MUT_W25_BIT22_GATE`, `w25=1`, so bit 22
is not ORed. V170 then returns `1` while `+0x2b8=-1` (`+0x2bb=255`, bit 4
set), taking the shared skip. There are no `SERVER_RENDER_UPDATE`,
`CREATE_SURFACE`, framebuffer handoff, or A408 runtime hits.

The control ran its full 360-second window with zero XNU panics; it did not
reach the shell.

This directly narrows the old “missing `0x9c42`–`0x9c45` producer” theory:
the message builder and send path do run naturally, but this updater's color
classification and later render gates skip the natural path. GATE154's
injected RGBA changes that downstream decision (`w25` becomes 0, bit 22 is
set) and unlocks a uniform blue surface. The next target is layer contents,
then the independent natural source of the display-list eligibility state.

### GATE162 result (2026-09-26) — zero RGBA values are already on the wire

GATE162 used the established profile-correct six-vCPU/PV boot, natural bounds
(no injection), and paired client-send/server-receive body capture. The first
616-byte `0x9c42` contained 592 body bytes. Its sender and receiver captures
were byte-for-byte identical (SHA-256
`35b3eadaf085ba715f150853e531b0b7ed5bd6e5dcd7a2ab385ff31def1fe3ad`). Thus
the server does not remove nonzero layer data that was present in this message.

The body has four `00 00 1f 02` byte sequences at offsets `0x30`, `0xaa`,
`0x124`, and `0x1ae`; each is followed by exactly 16 zero bytes. GATE166
identifies those 16-byte regions as RGBA payloads, not geometry. No
little-endian integer or float encoding of 1179 or 2556 occurs in the body,
but GATE166 separately shows that natural layer geometry is present in its
object fields.

On the receiver side, `PREPARE_LAYER0_ENTRY` and the first `MUT_BOUNDS_CHECK`
refer to the same type-31 pointer. Its four RGBA floats are zero at the first
point and black/opaque for the captured layer at the second. `BOUNDS_STR_X28_10`
has no runtime hit in this run. This joins wire bytes to the color payload
before the mutator's color/coverage classifier.

The send-side trace also records `ctx+0x360` walk result null,
`ctx+0x1e8=0`, and `x2=0` at the client builder call; `x4=0` there as well.
Despite that, the builder emits a 616-byte `0x9c42`. Static disassembly shows
the builder serializes a pending byte queue from `[encoder+0x18, encoder+0x20)`
and can append auxiliary records. Therefore the next probe snapshots that
queue at the call site, before serialization consumes it, and compares it
with the four wire records. This tests whether the zero RGBA records are
already encoded in the client's pending queue or introduced while the builder
assembles the packet. GATE162 had zero XNU panics and stopped at the first natural
mutator-bounds check; it did not reach the shell or display UI.

### GATE163 result (2026-09-26) — zero-color records are queued before packet assembly

GATE163 repeated the natural bounds control with an opt-in snapshot of the
encoder queue at `CA_COMMIT_CALL`. For the first 616-byte `0x9c42`, the queue
was 585 bytes, auxiliary pointer/count were zero, and the extra payload length
was zero. The complete Mach body was 592 bytes and matched exactly as:

```text
body[0:4]              = 00 00 00 00
body[4:589]            = 585 captured encoder-queue bytes
body[589:592]          = 00 00 00
```

The four type-tag patterns were at queue offsets `0x2c`, `0xa6`, `0x120`,
and `0x1aa`, exactly four bytes before their wire offsets. Each is followed
by 16 zero bytes. Sender and receiver body hashes both equal
`d586857ac3780cfdb0a56156a609355f6656a011ab06003b0ebf0137bb531e5f`.
This proves the `0x9c42` packet assembler and server decoder do not create or
erase those zero RGBA regions: they exist in the queue before serialization.

Static disassembly of `CA::Context::commit_transaction` found a direct queue
writer at `0x184401e20`/`0x184401e34`, but GATE165 shows it is only a 9-byte
timing record at offset 576: the `0x1f` byte is followed by
`d13 = d10 - d8` (~0.00637 in the first large queue). It runs after all four
geometry records, so it does not explain them. GATE164 is invalid: its callback
raised `UnboundLocalError` before producing usable evidence; the guest was
cleanly quit and did not panic.

GATE165's useful static lead is the `CA::Context::commit_transaction` layer
callback at `0x1844c0070`. It obtains a render object, then calls
`CA::Render::encode_set_object` at `0x1844bec00`. That encoder invokes the
object's virtual serializer; `CA::Render::Context::set_object` separately
recognizes objects whose type byte at `object+0xc` is `0x1f`. GATE166 then
confirmed the fetched type-31 object is byte-identical at serializer entry and
already contains display-sized geometry. GATE165 had zero XNU panics, stopped
at the first natural mutator check, and did not reach the shell or UI.

### GATE167 result (2026-09-26) — active server passes bit 17; V170 blocks at the mode sentinel

GATE167 used a fresh child of the known-good SKS parent, six-vCPU PV boot,
natural colors (`RFT_BOUNDS_FROM_DISPLAY=0`), and stopped on the first
`MUT_BOUNDS_CHECK`. It had zero XNU panics and did not reach the shell. The
condition stop arrived after the delayed 616-byte `0x9c42` commit; callback
logging continued briefly while the bounded LLDB session drained.

The server that processes the observed CA commits (`server=0x79810ec000` in
this boot) had a one-entry list and then a two-entry list. Its entries had
`+0xc=0x40420a`, with bit 17 clear, and its temporary list was non-null. The
count therefore differed from the bit-17 count, and `RFT_FULL_PATH` ran
naturally. Other display-server instances in the same trace had a one-entry
`0x2420a` list with bit 17 set and took `RFT_SHORT_SKIP`; those auxiliary
servers are not evidence that the active commit server is still blocked by
bit 17.

For the active server, V178 returned 0, V248 returned 0, and VF8 returned 1.
The path then reached V170, which returned 1 while the display's `+0x2b8` was
the `-1` mode sentinel (`+0x2bb=255`, bit 4 set). That is the immediate
render-update stop in this run; there were no `CALL_OUTER_A`,
`SERVER_RENDER_UPDATE`, `CREATE_SURFACE`, framebuffer handoff, or A408 hits.
The natural layer objects still had 1179×2556 geometry and null `contents+0x68`.

GATE167's `Layer::set_contents` hooks at `0x1844bcde0`/`0x1844bce70` had no
hits. They were the wrong place to establish whether CALayer supplied
contents; GATE168 follows below.

### GATE168 result (2026-09-26) — source contents are null; mode fallback is next

GATE168 used the natural color path (`RFT_BOUNDS_FROM_DISPLAY=0`) and stopped
on the first active-server V170 return. The run had zero XNU panics, 922 serial
lines, and no shell. `_copyRenderLayer` ran seven times. Each corresponding
`contents` getter return at `0x1844c061c` was `x0=0`; the render-layer objects
kept `+0x68=null`. `COPY_RENDER_CONTENTS_DECISION` and
`COPY_RENDER_CONTENTS_STORE` had zero hits, as expected for the null path.
This establishes the property value at those seven observed copies; it does
not establish that SpringBoard will never submit a content-backed layer.

The active V170 receiver was the object initialized with `+0x2b8=+0x2c0=-1`.
It entered `set_all_modes` (`0x184681b04`) with `x2=-1, x3=0`, retained the
sentinel, and returned `w0=1` at V170, taking the `tbnz w0,#0` skip. Five other
display objects reached the setter's store with mode 0; none was the active
receiver. There were no `SERVER_RENDER_UPDATE`, `CREATE_SURFACE`, framebuffer
handoff, or A408 hits before the stop.

Static disassembly then linked that call to
`IOMFBDisplay::update_framebuffer_locked` at `0x1847af650`. Its nearby
fallback explicitly creates a `Mode(-1)` at `0x1847af5e0` and calls
`ModeSet::add_mode` at `0x1847af5f0`. This is a testable explanation for the
sentinel, not yet a runtime-proven branch; GATE169 adds snapshots at the
fallback, add-mode call, and set-all-modes call.

### GATE169 result (2026-09-26) — the active display takes the `Mode(-1)` fallback

GATE169 was a natural-color run with the added mode-set snapshots and stopped
on V170. At the fallback site `0x1847af5e0`, the active display was already
`+0x2b8=+0x2c0=-1`. Immediately before `ModeSet::add_mode`, the vector was
empty (`begin=end=0`); the inserted mode was `0xffffffffffffffff`. At the
`set_all_modes` call (`0x1847af650`), the vector contained exactly that one
sentinel, with arguments `x2=-1, x3=0`. The setter left both display fields at
`-1`, and the next V170 sample returned 1 into the skip. This proves the
runtime fallback is the direct source of the active display's mode sentinel.

The run had zero XNU panics, 833 serial lines, and no shell. It stopped before
`SERVER_RENDER_UPDATE`, surface creation, framebuffer handoff, or A408. GATE170
then narrowed the upstream failure to supported-mode enumeration: the active
type-1 display calls `_IOMobileFramebufferGetSupportedDigitalOutModes` and gets
`0xe00002bc`; the empty set is followed by the already-proven fallback.

### GATE170 result (2026-09-26) — supported-mode enumeration returns an error

GATE170 was a natural-color run stopped at the first active-server V170 return.
The active `IOMFBDisplay` had signal type 1, and its supported-digital-modes
call returned `0xe00002bc`. The mode set remained empty, so
`update_framebuffer_locked` inserted `Mode(-1)` and passed it to
`set_all_modes`; V170 then returned 1 and took the short skip. The run had zero
XNU panics, 817 serial lines, and no shell, and it produced no render update,
surface, framebuffer handoff, or A408.

The GATE170 callback printed `x1` and `x2` after the call, where they no longer
held the two output values. It therefore did not establish whether the API
returned an array or which implementation handled the request. The corrected
observer reads the caller’s array/mode locals and the display info object’s
`+0xd48` dispatch pointer. Static inspection of IOMobileFramebuffer adds a
specific lead: `_kern_GetSupportedDigitalOutModes` starts with the same
`0xe00002bc` error and only clears it after it successfully reads both
`ColorElements` and `TimingElements` CFData properties. The virtual provider is
a separate dispatch path. Until GATE175 records the active dispatch target and
provider return, missing property data was a hypothesis rather than a proven
cause. GATE184/185 later confirmed the kernel provider and showed the first
property it requests, `ColorElements`, returns null.

### GATE171 result (2026-09-26) — no type-1 query; rerun is inconclusive

GATE171 used the documented six-CPU PV boot, patched dev6 kernel/device tree,
and `system.tc`, with natural colors. It did not reproduce GATE170’s active
type-1 display: the observed display objects were signal type 0 or 2, and the
supported-mode return breakpoint never fired. SpringBoard later entered a
critical restart, so this run does not distinguish the mode provider or
contradict GATE170. Treat it as a non-reproducing startup, not mode evidence.

GATE172–174 are discarded setup attempts: the repo `firmware/bootkc` variant
failed before early userspace with launchd unable to load `libSystem`; a BP
kernel run without the PV adapter failed secondary-CPU startup; and the merged
trust-cache variants again failed at the early `libSystem` lookup. None reached
the mode query. GATE175–181 established the distinct type-2 path and captured
several early registry snapshots, but did not watch the later type-0 append.

### GATE182–183 result (2026-09-26) — BackBoard appends type 0 at index 5

GATE182 stopped on an unrelated `thermalmonitord` type-0 entry. GATE183
filtered on `progname=backboardd` and caught `_iomfb_match_callback` computing
type 0 at `0x22a3957a8`, then appending the descriptor at `0x22a3957c4`. The
existing count was 5, so this is descriptor index 5; its service is
`0x13903`. `IOMFB_OPEN_BY_NAME` selected that same entry and the open returned
success. This explains the earlier snapshots: GATE180/181 sampled during the
first type-2 opens, before this later append. GATE181's watchpoint result is
invalid because it watched `base+0x6a8` instead of `_s_display_infos_len` at
`base-8`.

GATE183's new `AppleDisplay` still held `-1` at `+0x2b8/+0x2c0` and skipped the
constructor's mode setter. GATE184 followed the natural boot through the later
active display mode query.

### GATE184–186 result (2026-09-26) — `ColorElements` is missing on the selected service

GATE184 reproduced the active signal-type-1 display. BackBoard appended
type-0 descriptor index 5 for service `0x11603`; `IOMFB_OPEN_BY_NAME` selected
it and `IOServiceOpen` succeeded. The resulting display object later entered
`update_framebuffer_locked`, dispatched to `_kern_GetSupportedDigitalOutModes`,
and got `0xe00002bc`; both mode outputs were null and the empty ModeSet again
fell through to `Mode(-1)`.

GATE185 broke inside that provider. Its first IORegistry property request,
`ColorElements`, returned null. The old callback called the service port at
object `+0x10` a connection; GATE186's open trace distinguishes it from the
actual user-client connection at `+0x14`.

GATE186 repeated the same active query with no injected mode or bounds. The
BackBoard descriptor's index-5 `io_service_t` was `0x11d03`; `IOServiceOpen`
created connection `0x13e03`. Static disassembly shows
`_kern_GetSupportedDigitalOutModes` passes the `+0x10` service handle to
`_IORegistryEntryCreateCFProperty(service, "ColorElements", ..., 0)`. It gets
null and returns `0xe00002bc` before requesting `TimingElements` or reaching
either CFData type check. Thus the missing value is a property on the selected
IORegistry service, not a failed DCP mode RPC through the open connection.

The GATE186 trace also set breakpoints on the shared
`IOAVAbstractVideoInterface::fn_0xa8` path and its `ColorElements` setter call.
None fired before the active mode query. This only rules out that method path
in the observed interval; it does not prove that IOAVFamily is never involved.
For later runs, enable these bounded kernel hooks with
`RFT_TRACE_IOAV_PROPERTIES=1`.
DeviceTree inspection gives a concrete configuration difference to test: the
internal `disp0` node has `mipi-support-ioav=1` and `display-default-color=0`,
but no `display-timing-info` or framebuffer width/height. The `dispext0` node
does carry `display-timing-info` and `4096×2160` framebuffer dimensions. That
does not yet establish that these fields create `ColorElements`, but it points
the next trace at the internal display's registry properties and the code
that translates DCP/DeviceTree display data into mode-element properties.

### GATE187–189 result (2026-09-26) — AppleCLCD2 starts; no EDT size read was observed

GATE187–189 reproduced the same active type-1 mode query and null
`ColorElements` result. In GATE189, the corrected early kernel hook caught
`IOMobileFramebufferAP::start` on the AppleCLCD2 object for `disp0`; `start`
returned success. Its `+0x430` byte was zero at entry and return. Later
cross-checking against the D575 analysis established that this byte is
external-display state, not a mode-configuration flag; its zero value is
expected for the internal display and does not diagnose `ColorElements`. The
DeviceTree width/height read helper did not run in that early trace.

Treat the later “no hits” from these first early-kernel runs cautiously: the
early LLDB session detached at slide resolution, and the normal probe session
did not yet reinstall the kernel hooks. GATE190 therefore cannot establish
that the config or IOAV methods stayed absent for the full boot.

### GATE190–191 harness correction

GATE190 still reproduced the mode-query failure and captured successful
AppleCLCD2 startup, but its post-start kernel no-hits are invalid because the
hooks were lost when LLDB detached. GATE191 is also invalid for hit analysis:
the runner was edited while that run was active, and it exited with a shell
parse error. Do not use either run to infer that a later kernel path is absent.

The runner now stops QEMU at the first cache-backed EL0 boundary before
detaching early LLDB, then installs the opt-in kernel hooks again in the
normal probe session. This preserves the early `start` observation while
covering the remainder of the run without an uninstrumented gap.

### GATE192–194 result (2026-09-26) — the AP hot-plug path is idle; mode publication remains unresolved

GATE192 used the corrected early/late hook sequence and again stopped at
BackBoard's signal-type-1 query with `ColorElements` null and status
`0xe00002bc`. Across the observed boot interval, the D575 handler and its
`hotPlug_notify_gated` target did not fire. The framebuffer-width/height read
and the traced IOAV parser/setter methods also did not fire.

GATE193 traced both static call sites for `hotPlug_notify_gated` and the
methods that contain them. The normal IOMobileFramebuffer user-client path did
run: AppleCLCD2's `fn_0x698` received selectors 0, 3, and 9. Disassembly shows
that method inserts selector-indexed client records into notification lists;
selector 0 can replay a pending hot-plug event if one exists. These are event
subscriptions, not display-mode configuration calls. The `+0xa8`, `+0xb0`,
`+0xb8`, and `+0x103` fields are pending hot-plug state, and `+0x430` is
external-display state. Their zero values are consistent with an internal
display that received no D575 hot-plug event.

GATE194 captured the dispatcher arguments and the full selector rows. The
`0xa0d9e4c` routine is the known D575 input handler: it consumes a 0x58-byte
hot-plug event payload and calls `hotPlug_notify_gated`; it is not a general
mode-configuration record producer. GATE194 observed no such event before
BackBoard's mode query. This is expected absent hot-plug and does not explain
the null mode property. The 0xb0-byte rows passed to `fn_0x698` are per-client
notification bookkeeping, distinct from the D575 payload.

The only mode-publication evidence from these runs is still the independent
GATE186/GATE192 result: BackBoard requests `ColorElements` from the selected
`disp0` service and gets null; the traced IOAV parser/setter path did not run.
Do not treat the `+0x430` byte or missing D575 event as a display-mode gate.

GATE192 artifacts: `/tmp/dvm/UI_RENDER_GATE192.lldb.log`,
`/tmp/dvm/UI_RENDER_GATE192.early-kernel-lldb.log`,
`/tmp/dvm/probe/UI_RENDER_GATE192.serial.log`, and the matching stderr log.
GATE193 and GATE194 artifacts are the equivalent files with those run names.
Static references: `/tmp/dvm/gate186-mgfdcp.disasm.txt`,
`/tmp/dvm/gate189-all-methods.txt`, the `IOMobileGraphicsFamily` dispatcher
disassembly around `0xa0e50b4`, and `docs/re/iomfb-d575-external-cancel.md`
for the independently identified D575/hot-plug ABI.

### GATE197–203 result (2026-09-26) — video proxy starts, but its command path is idle

These runs used the known-good SKS inputs, six vCPUs, natural display values,
and stopped at BackBoard's first active signal-type-1 mode query. They had no
XNU panics. GATE197's constructor-candidate hooks did not fire, but that was
not evidence that no proxy object existed: GATE198/199 directly observed the
shared start path.

GATE198 recorded 13 calls through `DCPAVProxy::start`; each observed
`EPICLocation` lookup returned a non-null object and each base start returned
1. These are calls, not a count of unique endpoints. The earlier static
analysis in `docs/re/dcpav-proxy.md` correctly described what would happen if
`EPICLocation` were absent, but that condition was fixed in the current QEMU
source and binary and is not the present blocker.

GATE199/201 followed the seven-handler start path at `0xfffffff009b091c0`.
Rechecking its descriptor table shows that this is
`DCPAVPowerControllerProxy` (`SetRemoteDevicePower`), not
`DCPAVVideoInterfaceProxy`. Its shared base start returned 1 and the subclass
called `IOService::registerService`; its code then returns the base-start
result. These runs establish power-controller startup only. The QEMU log's
interface-4 announce/open and lack of an AP `0xc0` command remain device-model
observations, but they do not prove whether the video proxy was started or
sent a request. GATE201 stopped at BackBoard's natural mode query;
`ColorElements` was still null and the result was `0xe00002bc`, with no kernel
panic.

GATE202 reached the true five-handler video-interface start but its callback
failed while formatting the base-start return (`x20` had not been captured in
the Python frame). That run is invalid for behavioral conclusions. GATE203
fixed the callback and confirmed the runtime vtable at `0xfff0081d1990`, the
five-entry command table at `0xfffffff0081d20e8`, a base-start result of 1,
the `registerService` call, and a subclass return of 1. The exact object's
vtable was used to filter common `sendMessage` and `handleCommand` hooks; none
hit before BackBoard's active type-1 mode query. Thus the proxy exists and
starts, but its command path stays idle during this boot. This does not yet
exclude another DCPAV proxy originating a related request.

The old GATE197 constructor breakpoints are candidate addresses only, and their
no-hits must not be used to infer failed service construction. Likewise,
GATE199/201's breakpoint immediately after `registerService` sees the raw
register value left by that void call; it is not a registration IOReturn.
Static code shows the power-controller subclass then returns its saved
successful base-start result. The actual video-interface start routine is a
different five-handler path at `0xfffffff009b07570`, with the table at
`0xfffffff0081d20e8`: `ForwardMessage`, `Terminate`,
`FrameReceiveCallback`, `HPDCallback`, and `EDIDCallback`. GATE203 confirmed
that this path starts and registers successfully; its common command hooks
remain quiet through BackBoard's first mode query. Whether another proxy
originates a related request, and which command should cause IOAV property
publication, remain open.

GATE197–203 artifacts are `/tmp/dvm/UI_RENDER_GATE197.*` through
`/tmp/dvm/UI_RENDER_GATE203.*`, plus the matching serial and stderr files
under `/tmp/dvm/probe/`.

### GATE204–209 result (2026-09-26) — other proxies send, but QEMU replies are synthetic

GATE204 broadened the common `DCPAVProxy::sendMessage` trace from the video
proxy to every proxy vtable. It found three requests before BackBoard's first
active type-1 mode query: `DCPAVControllerProxy` on AFK interface 1 (command
index `0xc`, 0x20-byte payload), `DCPAVRemoteSACControllerProxy` on interface
8 (index `0x7`, 0x10-byte payload), and `DCPDPDeviceProxy` on interface 10
(index `0xc`, 0x50-byte payload). All `sendMessage` calls returned 0. GATE204
read the message header as payload, and GATE205's action names for the flags
branch were reversed; do not use those early payload/branch interpretations.

GATE206 disassembly corrected the branch: `DCPAVProxy::sendMessage` tests bit
1 of the message flags. Set takes the direct `AFKEndpointInterface` slot
`+0x558`; clear enters helper `0x9b03928`, then endpoint slot `+0x548`, which
dispatches to concrete slot `+0x578`. All three sampled requests take the
clear route (the DP-device request has derived action flags `0x4`, still bit 1
clear). This verifies the AP transport path end to end.

GATE207 fixed payload reads to start at `message+0x40`; every byte in the
three request payloads was zero in that run. The QEMU trace independently
shows that it received the AP's command frames on interfaces 1, 8, and 10.
With `DARWIN_DCP_REPLY=1`, the device model queues a **synthetic** response:
it allocates a zeroed body and copies only the four-byte command header. This
is a transport probe, not a firmware reply implementation, so successful
`sendMessage` returns and the zero response tails do not establish the
firmware's expected values or protocol semantics.

The response-site hooks in GATE209 ran after `sendMessage` returned and
before the proxy methods parsed their stack replies. Both sampled stack spans
were zero-filled; the DCPDP proxy's output pointers had not yet been written
at that hook. Its method later reads response fields from stack offsets
`+0x40`, `+0x50`, `+0x53`, and `+0x60`, which the synthetic responder leaves
zero. The controller hook likewise sees a zero response block before its
follow-up parsing. GATE208's snapshots are invalid because of its callback
exception; GATE209 fixes that callback. Neither run changes the natural
boundary: BackBoard's type-1 query still gets no `ColorElements` property,
with no XNU panic.

These results refine the earlier “video proxy is idle” finding: that specific
five-handler `DCPAVVideoInterfaceProxy` still has no observed send/dispatch,
but controller, SAC, and DP-device proxies do send. The DP-device interface
belongs to the DCPDP family, which the repo's firmware notes associate with
external DisplayPort paths; it is not evidence of internal-panel mode setup.
The AV controller and SAC exchanges may be related, but their synthetic
replies have no verified semantics. Do not infer that the missing property is
caused by a zero value in one of these replies.

GATE204–212 artifacts are `/tmp/dvm/UI_RENDER_GATE204.*` through
`/tmp/dvm/UI_RENDER_GATE212.*`, plus `/tmp/dvm/probe/UI_RENDER_GATE204.*` through
`/tmp/dvm/probe/UI_RENDER_GATE212.*`. GATE223–225 logs are
`/tmp/dvm/UI_RENDER_GATE223.early-kernel-lldb.log`,
`/tmp/dvm/UI_RENDER_GATE223.lldb.log`, `/tmp/dvm/probe/UI_RENDER_GATE223.*`,
and corresponding GATE224/GATE225 files. GATE226/227 logs are
`/tmp/dvm/UI_RENDER_GATE226.early-kernel-lldb.log`,
`/tmp/dvm/UI_RENDER_GATE227.early-kernel-lldb.log`,
`/tmp/dvm/UI_RENDER_GATE227.lldb.log`, and `/tmp/dvm/probe/UI_RENDER_GATE227.*`.
GATE228 artifacts are `/tmp/dvm/UI_RENDER_GATE228.early-kernel-lldb.log`,
`/tmp/dvm/UI_RENDER_GATE228.lldb.log`, and `/tmp/dvm/probe/UI_RENDER_GATE228.*`;
GATE229 artifacts use the corresponding `UI_RENDER_GATE229` paths. Both runs
completed without XNU panics and stopped at the natural BackBoard type-1 mode
query. GATE230 and GATE231 have corresponding early-kernel LLDB, regular LLDB,
and `/tmp/dvm/probe/UI_RENDER_GATE23{0,1}.*` artifacts. GATE208 is invalid for
response-site interpretation; use GATE209 for that evidence. GATE210 is an
invalid boot-profile attempt, and GATE213's early callback is invalid. Use
GATE221 and GATE224 for successful MIPI startup/open evidence, GATE226 for the
post-open MIPI report inventory, GATE227 for internal-display handler entry,
and GATE223 for the unfiltered DCPAV message inventory.

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

GATE166 supersedes the earlier “empty bounds” interpretation: the four floats
at `+0x10` are backgroundColor RGBA. It also shows display-sized geometry in
the first natural layer, while `contents+0x68` is null. The dedicated
`MUT_W25_SET1` site did not fire in the cited natural runs, so keep the causal
link between a particular layer's color classification and later `w25` samples
open until the same object is traced at both points.

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

GATE221 and GATE224 show that `dcpmipi-controller-epic` is now successfully
consumed: the PUBLISH dictionary is decoded, `DCPMIPIControllerProxy::start`
and `registerService` succeed, and QEMU sees AP `REPORT OPEN` on interface 12.
GATE223's corrected `RFT_TRACE_DCPAV_ALL_MESSAGES=1` run records exactly the
controller, SAC, and DP-device sends; it sees no inbound `handleCommand` and no
message on the five-handler DCPAV video proxy. The DP-device exchange remains
an external DisplayPort path, and the controller/SAC synthetic zero replies
still have no verified semantics.

GATE223–224 also close the specific IOAV hypothesis for this interval. The
`IOAVVideoInterface` tracker entry and ID comparison, its conditional child
setup, `IOAVAbstractVideoInterface::withProperties`, and the ColorElements
parser/setter all remain unentered before BackBoard's query. GATE225 shows
that AppleCLCD2 for `disp0` does start and return success on the selected
registry object, but its instrumented start path never requests
`framebuffer-width` or `framebuffer-height`. The mode provider then reads
`ColorElements` from that registry service, gets null, and returns
`0xe00002bc`.

GATE226 instrumented every AFK report for MIPI interface 12 and saw only its
initial `dcpmipi-controller-epic` PUBLISH; there was no later report after
`REPORT OPEN`. GATE227 traced the consumer that emits
`found no suitable color element in IOAVVideoInterface`:
`IOAVHandlerInternalDisplay::plugEvent_async_gated` at
`0xfffffff00a0dea80`, plus its selector result and color/timing/open-failure
branches. The entry and every branch stayed quiet through BackBoard's mode
query. This is a no-dispatch result, not evidence that the handler tried and
rejected a mode. Combined with the earlier silent IOAV tracker and property
parser, it moves the gap upstream of color-element selection: no internal
video-interface plug event reached this handler.

GATE228 put a breakpoint on the base `IOAVHandler::IOAVEvent` body at
`0xfffffff00a0eb1d4`, including its service cast and event-0/event-1 virtual
dispatch sites. None fired before the natural type-1 mode query. GATE229 then
traced the class-specific internal-display allocation path at
`0xfffffff00a0de720`, base constructor at `0xfffffff00a0ea794`, base method
`+0x78` at `0xfffffff00a0ea860`, and the event-registration helper at
`0xfffffff00a0eb150`. All remained unentered. Static disassembly shows the
class-specific path allocates a 0x50-byte object, constructs the base, and
installs the internal-display vtable; this boot did not take that path. The
missing event is therefore downstream of an earlier missing handler/interface
setup, not a cast failure or a rejected color/timing mode. BackBoard's
`ColorElements` result remained null (`0xe00002bc`), with zero XNU panics.

GATE230 read each video-class metaclass allocator slot (`+0x68`) after the
early boot attach and resolved the `IOAVAbstractVideoInterface`,
`IOAVSimpleVideoInterface`, `IOAVSliceVideoInterface`,
`IOAVSplitDisplayVideoInterface`, and `IOAVVideoInterface` allocation targets.
Its `IOAVInterface::start` hook was installed from reset and did not run.
Because the class-data reads failed in that early session, GATE230 alone could
not tell whether an allocator had run before the later breakpoints were armed.
GATE231 moved the five resolved code addresses into the reset-time hook set.
None of those allocator entries, nor `IOAVInterface::start`, ran before the
same natural BackBoard query. This rules out those five class allocations as
having occurred during the observed boot window; it does not identify which
upstream match or firmware event should request one.

A static scan of all 324 kernelcache `__PRELINK_INFO` entries narrows the
matching side. None of the five video-interface classes
(`IOAVAbstractVideoInterface`, `IOAVVideoInterface`, `IOAVSimpleVideoInterface`,
`IOAVSliceVideoInterface`, or `IOAVSplitDisplayVideoInterface`) has a direct
IOKit personality. The only related direct personality is `IOAVSplitDisplay`,
whose provider class is `IOAVSliceVideoInterface`; the separate `IOAVService`
personality expects `IOAVDevice`. This means IOKit personality matching can
consume a slice interface once one exists, but the scanned personalities do
not explain who creates the first video-interface object. Programmatic
allocation remains possible and is not ruled out by the plist scan.

The handler's own class-registration path is now decoded. Its metaclass vtable
starts at `0xfffffff008298e98`; authenticated slot `+0x68` at
`0xfffffff008298f00` resolves to the class allocator
`0xfffffff00a0de720` (raw pointer at extracted-kext file offset `0x34438`,
decoded with the kernelcache pointer base `0xfffffff007004000`). That allocator
creates the 0x50-byte `IOAVHandlerInternalDisplay` object, calls the
`IOAVHandler` base constructor, and installs the derived vtable. A scan of the
extracted gate186 kext executable sections found references to its metaclass
global (`0xfffffff00b8837f0`) only in `IOMobileGraphicsFamily` registration,
accessor, and allocation code; no other kext has a fixed call or direct
reference to that global that explains who requests the object. The matching
prelink XML also contains no `IOAVHandlerInternalDisplay`, `IOAVHandler`, or
`IOAVVideoInterface` personality. Together with GATE229's quiet allocator and
constructor hooks, a registry/class lookup or runtime-created match request
remains possible, but no initiating caller has been identified. GATE234 and
GATE235 subsequently put the named factory, video allocators, interface start,
and IOAV controller setup methods under reset-time observation; none ran in
those windows. That makes the missing firmware-facing input a stronger lead,
while leaving indirect or generic registry creation paths open.

The iOS 27.0 `firmware/bootkc` was also run through `ipsw kernel symbolicate`
with the matching 27.0 XNU signature set. The pass matched 5,210 of 6,511
signatures (80.0%) and generated C++ class/vtable symbols. Static vtable
layout plus the IOService declaration order now resolves the base
`matchPropertyTable` overloads: slot 149 (`+0x4a8`, dictionary plus score)
targets `0xfffffff00b22c400`, and slot 150 (`+0x4b0`, dictionary only) targets
`0xfffffff00b22c6a4`. Four checked IOAV vtables inherit those base targets.
The overloads of `OSMetaClass::allocClassWithName` still do not have safe
function addresses; exported names alone remain insufficient breakpoint
targets.

I also checked the nearest named IOAV matching helpers before adding hooks.
`IOAVController::openServiceMatchingProviderProperty`
(`0xfffffff009d93b00`) delegates to `openServiceMatching`
(`0xfffffff009d93dc0`), while the only direct caller found for
`IOAVWaitForServiceMatchingProviderProperty`
(`0xfffffff009dbb8d0`) is
`IOAVCommandDelegateService::registerCommandDelegateService` at
`0xfffffff009ddd7bc`. That caller is setting up the command-delegate service;
it does not explain the absent video-interface allocation, so these helpers
are not the next probe targets.

The base matcher targets are callable breakpoint addresses, but a broad
unfiltered stop on them would be noisy. If later evidence points to a registry
match, filter the provider/dictionary tightly to the IOAV classes. The
`allocClassWithName` exports still need address resolution before they can be
probed safely.

On the firmware-facing side, GATE231's QEMU log shows interface 4
(`dcpav-video-interface-epic`) receiving only the AP's zero-body `REPORT OPEN`
before the natural query. GATE223's all-vtable DCPAV trace also saw no video-
proxy send or command dispatch. Interface 12 likewise had no post-open report
in GATE226. The QEMU EPIC model publishes service names and can answer AP
commands with explicitly synthetic probe responses, but it does not generate
spontaneous DCP reports (`qemu-sptm/hw/arm/darwin_dcp.c`). These observations
make missing firmware-originated traffic a plausible upstream input, not a
proven AP expectation; they do not identify a report subtype or payload.

### GATE235 details (2026-09-28) — active entries reach FULL, then IOMFB ignores the update

This was a 240-second natural run using the known-good `bp-patched-dev6`
bootkc, device tree, and trust cache, with reset-time hooks. It had zero XNU
panics. BackBoard's type-1 mode query again returned `0xe00002bc` with null
mode outputs. No `IOAVController::start`, provider-open/matching/add-device
method, internal-display factory, or video-interface allocator/start hook ran.

The display-list result is mixed by server, which corrects the earlier broad
reading of GATE74/75. Auxiliary server lists containing `+0xc=0x2420a` had
bit 17 set and took `RFT_SHORT_SKIP`. The active display server also had
entries, but its observed `+0xc=0x40420a` entries had bit 17 clear; those
decisions entered `RFT_FULL_PATH`. Thus bit 17 explains the short skip for
that auxiliary list, not a universal inability of the active display renderer
to enter its full path.

On the active full path, the live `+0x170` vtable target resolved at static
`0x1844561a0` to
`CA::WindowServer::IOMFBDisplay::ignore_update_p(CA::Render::Update const*)`.
All 32 sampled returns were `1`; the call's display had `+0x2b8=-1`
(`+0x2bb=0xff`, bit 4 set) and its update had `+0x140=0x80010` (mask
`0xa` clear). That return branches to the shared render skip at
`0x184411c48`. The adjacent `+0x178` method is
`IOMFBDisplay::can_update(unsigned int, double, double)` at static
`0x18442f208`; after one initial `0xe9`, its sampled calls returned 0.
The `+0x248` method is `Display::needs_display_roi_shape_tracking() const`
and returned 0. These are distinct from the bit-17 count test.

No `CREATE_SURFACE`, display/Iosurface allocator, client commit-builder send,
or `0x9c42`–`0x9c45` send was observed. Natural display-sized layer geometry
therefore still does not amount to a committed frame. The immediate natural
skip is now located at `ignore_update_p`; the sentinel mode state is consistent
with the known missing `ColorElements` result, but the trace does not prove
that supplying that property alone would produce a commit.

A target-hardware scope check weakens the assumption that the quiet generic
IOAV setup is necessarily the missing internal-panel path. The matching
device driver, `com.apple.driver.AppleMobileDispH17P-DCP`, contains the
`AppleCLCD2` personality and no direct `IOAV`, `ColorElements`,
`TimingElements`, or `initIOAVHandlerInternalDisplay` strings in its extracted
Mach-O. The named `AppleCLCD::initIOAVHandlerInternalDisplay` implementation
appears in the local iOS 27 H13G/H14G DFR symbol sets, not the H17P-DCP set.
Separately, `IOAVController::start` has explicit HDMI CEC, AV-protection, and
audio-delegate setup; its absence alone does not diagnose the internal panel.
These are scope clues, not proof that another component cannot allocate an
IOAV object dynamically.

The next no-injection trace should follow the H17P framebuffer/mode state and
the DCP link's natural property/update path. Keep the missing
`ColorElements` read as a measured boundary, but do not assume another IOAV
matching scan—or filling that property alone—will unlock commits. The AP-side
firmware report setup identified at `0x404dfd4` is static only; QEMU does not
execute that DCP task, so its pending flags still need a real firmware-side
trace before they can be connected to this mode failure.

Artifacts: `/tmp/dvm/UI_RENDER_GATE235.early-kernel-lldb.log`,
`/tmp/dvm/UI_RENDER_GATE235.lldb.log`,
`/tmp/dvm/probe/UI_RENDER_GATE235.serial.log`, and
`/tmp/dvm/probe/UI_RENDER_GATE235.stderr.log`.

### GATE236 result (2026-09-28) — capture the complete natural `ignore_update_p` predicate

GATE235's immediate stop was correctly located at
`IOMFBDisplay::ignore_update_p`, but attributing its return directly to the
whole `+0x2b8=-1` mode sentinel was too broad. Static disassembly shows the
method tests bit 4 of byte `+0x2bb`; when set, it evaluates the update flags
and additional display state. It does not read the `+0x2b8` qword itself.

A fresh 240-second natural run sampled the active display before all 32 V170
calls. Every sample was identical:

```
+0x2b8=0xffffffffffffffff  +0x2bb=0xff (bit 4 set)
update+0x140=0x80010        (mask 0xa clear)
display+0x7595=0            display+0x7558=0
state330[4]=0               state330[5]=1
```

With `state330[4]=0` and `state330[5]=1`, the method selects the
`update+0x140 & 0xa` result. It is zero in this run, so the final invert
returns `ignore=1` and branches to the shared render skip. The method also
preserves zero in its pending byte at `+0x7558`. Thus the exact measured cause
is the natural update mask combined with state byte 5; the all-ones mode
sentinel only selects this predicate's path. This does not connect the missing
`ColorElements` property to the returned value.

Static QuartzCore disassembly identifies the main physical-display producer
candidate for state byte 5: `IOMFBDisplay::update_power_state_locked(bool)`
stores a selected power-state value there at `0x1845bdaa4`; its value is 1
when the display power-assertion count is nonzero, otherwise it comes from a
raw power-state byte. `Display::set_power_state_locked` also writes bytes 4
and 5 from its state argument. GATE237 adds read-only runtime samples at those
sites to establish which path produced the natural 1. Until that trace lands,
the writer attribution remains static, not observed runtime causality.

There were still no `CREATE_SURFACE`, display/Iosurface allocator,
client commit-builder send, or `0x9c42`–`0x9c45` send hits; no XNU panics; and
the guest did not reach the shell. The render skip is therefore now reduced to
its actual inputs, but it does not by itself explain why the client did not
submit a renderable content update.

Artifacts: `/tmp/dvm/UI_RENDER_GATE236.early-kernel-lldb.log`,
`/tmp/dvm/UI_RENDER_GATE236.lldb.log`,
`/tmp/dvm/probe/UI_RENDER_GATE236.serial.log`, and
`/tmp/dvm/probe/UI_RENDER_GATE236.stderr.log`.

### GATE239 result (2026-09-29) — observe `prepare_layer`'s natural output

The short-lived `/tmp/dvm/SKS_INSTALL1/disk.qcow2` parent used by GATE236 had
been cleaned up. GATE239 therefore used a fresh child of the preserved,
profile-correct `bp-patched-dev6/boot1/disk.qcow2` chain, the recovery lineage
used in earlier natural probes. It used the matching `bootkc`, device tree,
trust cache, six vCPUs, and no bounds or flag injection. The run had 0 XNU
panics, did not reach the shell, and completed its 240-second collection.

The new call/return hooks at `Update::add_context`'s `prepare_layer` callsite
both hit 48 times. The returned low flag word was `0x10000800000` in 26
samples, `0x10000800008` in 2, zero in 19, and `0x8` once. The two recurring
layer/context pairs account for the baseline word and its occasional extra
bit 3; the other pair also has zero-only samples. This confirms directly that
the helper's output slot is the `sp+0xf0` word later consumed by
`UPDATE_FLAGS_MASK_CHECK`.

Across all 32 consumer checks, `sp+0xf0` was `0x10000800000` 30 times and
`0x10000800008` twice; bit 22 was clear in every sample. `sp+0x170` was zero
30 times and `0x20` twice, so the required `0x28000` mask was absent on every
check. Neither `UPDATE_FLAGS_ORR_2` nor `UPDATE_FLAGS_ORR_8` ran. The full path
still reached `ignore_update_p` 32 times; all 32 returned 1 with the natural
`update+0x140=0x80010`, and the renderer skipped. There were no
`SERVER_UPDATE_OUTER_ENTRY`, `CREATE_SURFACE_ENTRY`, IOSurface-allocation, or
`MUT_ORR_BIT22` hits, and the QEMU model log had no A408. This run did not
install the separate CA message send/receive tracer, so it adds no evidence
about `0x9c42`–`0x9c45`.

The next causal probe is now narrower: at static `0x184432a70`, sample the raw
`[state+0x70/+0x78]` pair and `LocalState+0x40`; at `0x18443b0a4`, capture the
word before its mask; and at `0x18443b0b8`, capture the final output store.
Those sites show whether bit 22 is absent from the inputs or removed during
the helper's final mask/merge. In parallel, retain a separate client
commit-builder send trace; these server flags still do not explain the missing
sized commit.

Artifacts: `/tmp/dvm/UI_RENDER_GATE239.lldb.log`,
`/tmp/dvm/UI_RENDER_GATE239.probe.out`,
`/tmp/dvm/UI_RENDER_GATE239.slide.json`,
`/tmp/dvm/probe/UI_RENDER_GATE239.serial.log`,
`/tmp/dvm/probe/UI_RENDER_GATE239.stderr.log`, and the fresh child
`/tmp/dvm/data-seed/ui-render-gate239.qcow2`.

### GATE240 result (2026-09-29) — bit 22 is absent before the helper's final mask

GATE240 repeated the natural six-vCPU `bp-patched-dev6` boot on another fresh
child of the durable `boot1/disk.qcow2` parent, with bounds and flag forcing
off. It completed 240 seconds with 0 XNU panics and did not reach the shell.

At `prepare_layer` entry, 48 snapshots showed `state+0x78=0`. The recurring
display-layer path had `state+0x70=0x10000800000`, `LocalState+0x40=0`, and
`LocalState+0x60=0` in 20 samples. The other snapshots include child-layer
paths and are not the same state. At the helper's final mask site
(`0x18443b0a4`), all seven observed raw words lacked bit 22; the applied mask
`0x407c15583c06020` includes bit 22. The subsequent output stores at
`0x18443b0b8` all remained bit-22-clear. Therefore the mask does not remove
the needed bit: it is already absent from the raw flags reaching that stage.

The 32 `UPDATE_FLAGS_MASK_CHECK` samples again had `sp+0xf0` equal to
`0x10000800000` 30 times and `0x10000800008` twice, with bit 22 clear in all
32. `sp+0x170` was zero 30 times and `0x20` twice; `0x28000` was absent in
all samples and neither ORR site ran. The full path again reached
`ignore_update_p` 32 times and skipped all 32; `SERVER_UPDATE_OUTER_ENTRY`,
`CREATE_SURFACE_ENTRY`, `ALLOCATE_IOSURFACE_ENTRY`, and `MUT_ORR_BIT22` had no
runtime hits. The QEMU model log contained no A408. As in GATE239, this run
did not install the separate CA message tracer and adds no evidence about
`0x9c42`–`0x9c45`.

Next, trace the pair update at `0x18443b068`–`0x18443b074` and identify the
earlier writer that puts `0x10000800000` into the recurring state's `+0x70`.
Keep the client commit-builder send path as a separate trace: the missing
`0x28000`/bit-22 flags explain this renderer skip but still do not prove why
the client does not post a sized commit.

Artifacts: `/tmp/dvm/UI_RENDER_GATE240.lldb.log`,
`/tmp/dvm/UI_RENDER_GATE240.probe.out`,
`/tmp/dvm/UI_RENDER_GATE240.slide.json`,
`/tmp/dvm/probe/UI_RENDER_GATE240.serial.log`,
`/tmp/dvm/probe/UI_RENDER_GATE240.stderr.log`, and the fresh child
`/tmp/dvm/data-seed/ui-render-gate240.qcow2`.

### PLUGIN149 result (2026-10-10) — Home entity exists; active display still ignores updates

P149 used a fresh child of the durable P147 render-gate child, the accepted
DVMI2 trust cache and the existing CLASS16/D572/D582/ECHO/A407 hypothesis
profile; A408 remained off. SpringBoard returned a non-null Home entity
(`0x7b...` in the callback's integer form `529153456336`) and the host helper
ACKed Home and upward-swipe input. The framebuffer captures before and after
those inputs remained uniformly black (1179×2556; SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The
run does not establish a visible Home or an unlocked frame.

The pre-V170 observer was runtime-validated: SpringBoard returned the
non-null Home entity, and the active display receiver at `x0=0x793ecf8000`
and its `Update*` reached `RFT_FULL_PATH`. At each of the 70
sampled calls, `display+0x330` pointed to `0x7521fe4000`, bytes 4/5 were
`0/1`, the mode byte at `+0x2bb` was `0xff`, update flags at `+0x140` were
`0x80010` (mask `0xa` clear), and V170 returned 1 (`ignore`). The full path
therefore still exits at `IOMFBDisplay::ignore_update_p` before a Server
render or surface allocation in the observed interval.

P149 also corrects a tempting power-state attribution. The three sampled
`DISPLAY_POWER_TARGET_STATE`/`DISPLAY_POWER_STORE_STATE5` pairs used receiver
`0x793ed08000` (display ID 2) and state pointer `0x7521fe8000`; these are
`+0x8000` and `+0x4000`, respectively, from the active V170 display and state
above. They cannot explain the active display's state byte 5. The separate
read-only state watch armed on the active pointer logged no writes before
LLDB coverage ended. LLDB stopped at `CAN_UPDATE_OUTER_AFTER_INNER`
(`0x188e772e8`, breakpoint 81) at about 03:50:44 host time; later periodic
black-frame samples are not later LLDB coverage. The Home retry and later
guest runtime therefore add no evidence about the state writer or post-V170
callbacks.

The next probe should resolve the active display's actual state-byte writer
and the mutator branch that supplies bit 22, while keeping LLDB free of raw
state watchpoints and the can-update callback that ended P149's session. Do
not attribute the other display's power writes to this display or infer a
DCP report from the mode sentinel. Durable child and records are
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin149-posthotplug-branch1.qcow2`
and
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN149_POSTHOTPLUG_BRANCH1/`;
the child passed `qemu-img check -U`.

### GATE241 result (2026-09-29) — the reset site needs an address correction

GATE241 traced seven `prepare_layer` pair merges. Each target state's `+0x70`
and `+0x78` began at zero; the low contributions were `0x8000800000` (3),
`0x40` (2), and `0x10000800000` (2), with no bit 22. A bounded watchpoint
reported one `0x10000800008 → 0` event at static PC `0x184449824`, but its
callback rejected LLDB's extra argument. That event was initially read as the
writer clearing the watched `+0x70` word; GATE242's direct PC probes show that
`0x184449824` actually stores a zero pair at the reset receiver's `+0x90`.
Treat GATE241's watchpoint-address/PC association as unresolved rather than as
evidence that this instruction cleared `+0x70`.

### GATE242 result (2026-09-29) — reset moves and filters the active flag pair

GATE242 used the same natural six-vCPU `bp-patched-dev6` boot recipe and a new
child of the durable `boot1/disk.qcow2` parent. All force options were off;
the state watch and direct reset-entry/reset-clear probes were enabled. The
240-second run had no XNU panics and did not reach the shell.

The helper at `0x18444979c` compares the state type at `+0x60` with its third
argument and returns immediately when they match. On type changes it carries
the old `+0x70/+0x78` pair to `+0x80/+0x88`, applies the low-word mask
`0xdbf83e000c3fff83` to the active low word, stores that filtered word back
at `+0x70`, and zeros the separate `+0x90/+0x98` pair. This is called both
from the update/context path (return PC `0x184432f34`, 14 entry samples) and
from `prepare_layer`'s caller (return PC `0x184440b74`, 6 samples). For
example, on a `0xf → 0x10` transition the recurring state changed from
`+0x70 = 0x10000800008` to zero, while `+0x80` retained the prior value.
The corrected dynamic watchpoint was installed at `state+0x70` but did not
fire during this run, so the static instruction probes provide the reliable
write attribution.

The seven pair merges again produced only GATE241's three values, and all 48
`prepare_layer` output samples still lacked bit 22. The raw candidates at the
final mask site were `0x8000800000` (3), `0x40` (2), and `0x10000800000` (2);
the mask includes bit 22, but none of those inputs had it. The full path again
had 32 `UPDATE_FLAGS_MASK_CHECK` samples with bit 22 and `0x28000` absent;
`RFT_SHORT_SKIP` hit 14 times, while `CREATE_SURFACE_ENTRY`,
`ALLOCATE_IOSURFACE_ENTRY`, and `MUT_ORR_BIT22` had no runtime hits. The
display-list probes also observed both bit-17-set entries (`+0xc = 0x2420a`)
and bit-17-clear entries (`+0xc = 0x40420a`) in different server lists, so a
blanket “every list entry has bit 17” statement does not describe this boot's
whole observed population.

Static inspection then exposed an earlier producer worth tracing: the update
routine at `0x18443501c` masks `x19+0x5d0` with `0xfffffffffe3fffff`, explicitly
clearing bits 22–24. Several later static stores can OR `0x20c00000`, which
includes bits 22 and 23. GATE242 did not instrument those stores, so it had not
established whether those branches execute or why the final raw candidate
lacks bit 22.

### GATE243 result (2026-09-29) — the bit-22 restore branch is skipped

GATE243 repeated the natural six-vCPU boot with force options off and bounded
hooks at every direct update-context `+0x5d0` store. The 240-second run had no
XNU panics and did not reach the shell. Of the instrumented stores, only two
sites ran: the zeroing store at `0x184435020` ran 4 times, and
`0x184433a70` ran 8 times. The latter ORs bit 23 (`0x800000`) into the
accumulator, yielding `0x8000800000` or `0x800000`; none of the 12 observed
new values had bit 22.

The adjacent bit-22 restore path at `0x184433a80`–`0x184433a8c` did not run.
At the branch on `w25` (`0x184433a74`), all 9 samples had `w25=1`, so the
`tbz w25,#0` branch skipped the `0x20400000` OR (which would set bits 22 and
29). `MUT_ORR_BIT22` and the `0x184433a8c` store both had zero hits. This
accounts for why the accumulator reaches `prepare_layer` without bit 22 on
these paths; it does not yet explain why `w25` remains 1.

Separately, all 19 natural layer objects sampled at the geometry gate had bit
22 set in their `+0x30` flags (`0x2406038` or `0x2486038`). Thirteen carried
1179×2556 bounds; the others had 0×0 or 723×0. The metadata bit is therefore
present on the layer objects but is not propagated through this update-context
gate. The full path again had 32 mask checks with bit 22
and `0x28000` absent, 14 `RFT_SHORT_SKIP` hits, and no runtime entry into
`CREATE_SURFACE`, `ALLOCATE_IOSURFACE`, or `MUT_ORR_BIT22`.

Next, trace the live producers of `w25`: its conditional set at
`0x184433594`, the subsequent accumulator updates at `0x18443370c` and
`0x18443374c`, and the final value at the bit-22 branch. The direct
`mov w25,#1` site at `0x18443353c` and the virtual-return hook did not run in
GATE243, so the important `w25=1` path comes from another producer. Keep the
client CA commit-message path separate; GATE243 did not install that tracer
and adds no evidence about `0x9c42`–`0x9c45`.

Artifacts: `/tmp/dvm/UI_RENDER_GATE243.lldb.log`,
`/tmp/dvm/UI_RENDER_GATE243.probe.out`,
`/tmp/dvm/UI_RENDER_GATE243.slide.json`,
`/tmp/dvm/probe/UI_RENDER_GATE243.serial.log`,
`/tmp/dvm/probe/UI_RENDER_GATE243.stderr.log`, and the fresh child
`/tmp/dvm/data-seed/ui-render-gate243.qcow2`.

### GATE244 result (2026-09-29) — opaque background layers have no contents image

GATE244 repeated the natural six-vCPU boot with force options off. It had no
XNU panics and did not reach the shell. The active display's type-1
`_IOMobileFramebufferGetSupportedDigitalOutModes` call again returned
`0xe00002bc` with null mode outputs. QuartzCore added `Mode(-1)` to the mode
set, retained the `+0x2b8=-1` sentinel, and the render-update virtual returned
1 at V170. The full render path therefore still stopped before
`SERVER_RENDER_UPDATE` and surface creation.

The commit-side callbacks observed 11 `_copyRenderLayer` calls and 11 matching
type-31 render objects reaching the virtual encoder. Every observed
`-[CALayer contents]` getter returned null; `COPY_RENDER_CONTENTS_DECISION`
and `COPY_RENDER_CONTENTS_STORE` did not run. The sampled type-31 objects
include display-sized 1179×2556 layers with opaque-black RGBA. This confirms
that these layers have no image-backed contents, but does not make that a
renderer failure: an opaque `backgroundColor` can be rendered without a
`contents` image, and the active display mode gate stops the update earlier.

At the update-context gate, all nine sampled opaque-black display-sized
type-31 layers again yielded `w25=1`; neither later accumulator hook ran, so
that value reached the bit-22 branch unchanged and skipped the `0x20400000`
OR. `RFT_SHORT_SKIP` hit 14 times, and no IOSurface allocation, framebuffer
handoff, or A408 occurred. This explains the bit-22 decision for those layers,
not the separate missing-mode cause or why no other layer color is committed.

Next, trace the direct `-[CALayer setContents:]` implementation and correlate
its receiver with the copied layer objects. Treat a lack of image assignment
as a property of these sampled layers unless the trace links it to the
display-sized background path; the active V170 `Mode(-1)` stop remains the
first established display-update blocker. GATE244 did not install a CA
message tracer, so it adds no new evidence about `0x9c42`–`0x9c45`.

Artifacts: `/tmp/dvm/UI_RENDER_GATE244.lldb.log`,
`/tmp/dvm/UI_RENDER_GATE244.probe.out`,
`/tmp/dvm/UI_RENDER_GATE244.slide.json`,
`/tmp/dvm/probe/UI_RENDER_GATE244.serial.log`,
`/tmp/dvm/probe/UI_RENDER_GATE244.stderr.log`, and the fresh child
`/tmp/dvm/data-seed/ui-render-gate244.qcow2`.

### GATE245 result (2026-09-29) — `setContents:nil` matches the display-sized black layers

GATE245 repeated the natural six-vCPU dev6 boot with all force options off and
the direct setter hook enabled. The 240-second run
had no XNU panics and did not reach the shell. It again observed 11
`_copyRenderLayer` calls and 11 type-31 render objects. Every corresponding
`-[CALayer contents]` return was null; none reached the non-null decision or
store.

The direct setter hook reached its 128-hit cap: two hits were in `backboardd`
and the remaining 126 were in `com.apple.datamigrator`. All 128 passed
`x2=0`. The two BackBoard receivers, `0x74cf08d6e0` and `0x74cf08e730`, later
appeared as sources in `_copyRenderLayer`; the resulting type-31 objects at
`0x74cf134320` and `0x74cf1345a0` carry display-sized 1179×2556 opaque-black
backgroundColor and `contents+0x68=null`. This directly links the null setter
to two copied display-sized background layers. The hit cap means this is a
bounded sample; it does not establish that no other process or later layer
ever receives non-null contents.

This makes the sampled nil contents unsurprising for solid-color layers and
does not explain the lack of a natural frame. The active signal-type-1 mode
query still returned `0xe00002bc`; `update_framebuffer_locked` inserted
`Mode(-1)`, `set_all_modes` retained the sentinel, and V170 returned 1 for all
32 sampled full-path updates. `CALL_OUTER_A`, `SERVER_UPDATE_OUTER_ENTRY`,
IOSurface allocation, framebuffer handoff, and A408 remained at zero. The
next useful work is upstream of the mode provider: determine why the internal
IOAV display handler/factory path does not run and therefore does not publish
`ColorElements`. Do not treat `setContents:nil` on these black layers as the
cause of the mode or renderer gate.

Artifacts: `/tmp/dvm/UI_RENDER_GATE245.lldb.log`,
`/tmp/dvm/UI_RENDER_GATE245.probe.out`,
`/tmp/dvm/UI_RENDER_GATE245.slide.json`,
`/tmp/dvm/probe/UI_RENDER_GATE245.serial.log`,
`/tmp/dvm/probe/UI_RENDER_GATE245.stderr.log`, and the fresh child
`/tmp/dvm/data-seed/ui-render-gate245.qcow2`.

The direct setter breakpoint is intentionally opt-in via
`RFT_TRACE_CALAYER_CONTENTS=1`; `-[CALayer setContents:]` is hot in unrelated
processes and the first run showed `datamigrator` can consume most of a
bounded hit budget.

GATE242 did not install the separate CA message tracer and adds no new evidence
about `0x9c42`–`0x9c45`.

Artifacts: `/tmp/dvm/UI_RENDER_GATE241.lldb.log`,
`/tmp/dvm/UI_RENDER_GATE241.probe.out`,
`/tmp/dvm/UI_RENDER_GATE241.slide.json`,
`/tmp/dvm/probe/UI_RENDER_GATE241.serial.log`,
`/tmp/dvm/probe/UI_RENDER_GATE241.stderr.log`, and the fresh child
`/tmp/dvm/data-seed/ui-render-gate241.qcow2`; plus the matching GATE242 files
and `/tmp/dvm/data-seed/ui-render-gate242.qcow2`.

The decrypted iOS 27 developer OS and SystemCryptex images were checked
read-only on 2026-09-26. Their standalone firmware files include Rose,
SmartIO, NFC, and SLAM assets, but not `t8140dcp.im4p`. Follow-up recovered the
matching iPhone17,3 / 24A5430a DCP component directly from the IPSW with HTTP
byte ranges: the 12.17 GB archive was not downloaded; 7,335,946 bytes of ZIP
range data yielded the 2,565,814-byte IM4P at
`/tmp/dvm/gate232-firmware/t8140dcp.im4p`, raw bundle at
`/tmp/dvm/gate232-firmware/dcpfw`, and larger-task Mach-O at
`/tmp/dvm/gate232-firmware/machos/dcpfw.macho`.

The recovered image makes static tracing possible again. Its `__OS_LOG`
call-sites reference the exact UBDL offsets for `sendOpenReport`,
`sendPublishReport`, `sendTerminate`, and `sendCloseReport` in separate
control-bit-gated paths. The `0x4046e2c` callback is registered through an
event-source slot at `0x4046b98` and `0x404e080`; its report methods still
dispatch indirectly through slots `+0x20`, `+0x58`, and `+0x28`, while
terminate uses `0x40470a4`. AP-side `handleOpen`/`handleClose` turn out to be
IOKit service lifecycle callbacks, and the separate `handleReport` function's
`0xc1` comparison uses an event-context argument rather than a parsed EPIC
header. These observations do not prove an open/close wire subtype or the
report payload. **Correction (2026-09-26):** the earlier claim that the larger
task's `__DATA_CONST` and `_rtk_mtab` were absent was based on a bad segment
map. The locally reconstructed Mach-O is zero at fileoff `0x952000` because
the reconstruction omitted this segment, and `dcpfw+0xb11000` is inside the
smaller task's `__DATA`. The larger task's actual packed `__DATA_CONST` begins
at `dcpfw+0xf31000`. Its `_rtk_mtab` contains 208 authenticated function
pointers into the larger task's text, plus two negative offset-to-top values
and four zero slots. For the vptr `0x49f11e0` installed at `0x4027b28`, the
report callback's immediate slot targets at `+0x20`, `+0x58`, and `+0x28` are
`0x40fc76c`, `0x41036a8`, and `0x40fc5e0`. So the bundle does contain these
static method targets; tracing the loader is no longer needed to identify
them. A post-load snapshot remains useful to confirm final in-memory pointer
values and actual dispatch. `sendPublishReport` still most likely maps to
iOS 27 `REPORT/PUBLISH 0x11` based on the AP receiver and matching name, but
its encoded payload remains unproven. Keep the mode query natural, bounds
injection off, and do not synthesize report or color/timing bytes.
Layer contents remain separate:
GATE168 sampled backboardd's seven observed copies and does not establish what
SpringBoard's own layers later provide.

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
`render_gate_callbacks.LAST_MUT` records the legacy-named four-float value and
`w25` with `tpidr_el1` so send
and receive events can correlate the same thread.

Static follow-up for GATE168/169: extracted QuartzCore from the restored dyld
cache to `/tmp/dvm/gate168-extract/QuartzCore`. Its
`IOMFBDisplay::update_framebuffer_locked` path adds a `Mode(-1)` in a fallback
block before calling `set_all_modes`; GATE169 confirmed the runtime path. Keep
the characterized timing-tail hooks optional, and do not add global Mach
syscall breakpoints.

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

### GATE85 result (2026-09-24) — latch + `+0x2b8=1`, full window

Reached **`RFT_FULL_V170_OK`** (`rft_v170_ret w0=0x0 bit0=0`, `+0x2bb=0`).
Full chain: V178_OK → CHECK_548 → V248 → TBNZ → BR_10DF4 → V218 → VF8_OK →
V170_OK. **No `CALL_OUTER_A` / `CREATE_SURFACE` / `UPDATE_FLAGS_ORR_*`.**
`RFT_WP_12000` / `RFT_WP_13000` also 0 hits — execution never advanced ~0x400
bytes past V170_OK before SpringBoard critical-reboot → Halt/Restart.
Layer walk inside FULL still `has_28000=False` / mask store `0x20` only.
Second FULL started (v178=0 again) then reboot cut it off.

GATE86: post-V170 breadcrumbs (`0x1844115ec`…`0x1844117bc`) plus
`RFT_FORCE_MASK_STORE_20080=1` to see if CALL_OUTER+CREATE land once the
mask bit is present.

### GATE86 result (2026-09-24) — + mask force `0x20000`

`V170_OK` **×6** (`w0=0 bit0=0`), `V178_OK` ×9/10, FULL×12, mask store
`has_28000_x9=True` (`x9=0x20020`). Still **0** `CALL_OUTER_A` /
`CREATE_SURFACE` / `UPDATE_FLAGS_ORR_*`. Next hit after each V170_OK is
`FINISH_UPDATE_ENTRY` (other module), then SpringBoard reboot.

Post-V170 breadcrumbs were **not installed**: `RFT_SLIM=1` allow-list omitted
them. GATE87 adds them to slim and dumps `+0x648`/`+0x40` at the cmp.

### GATE87 result (2026-09-24) — post-V170 breadcrumbs live

`V170_OK` ×2 → `POST_V170_TIME` ×2 → `CMP648` ×2 with **`+648 == +40`
always** (`eq=True`). That takes `b.eq #0x184411d00`, which later merges at
`0x184413810` — **past** `CALL_OUTER` at `0x1844134cc`. Zero hits on
`POST_V170_GFLAG` / `WP_12000` / `CALL_OUTER` (fallthrough never runs).

`+0x648` is a frame/list pointer compared to `+0x40`; equal means “no delta”
and the FULL path finishes without `SERVER_UPDATE_OUTER`. GATE88:
`RFT_FORCE_648_NEQ=1` clears `+0x648` at the cmp to force the CALL_OUTER
fallthrough.

### GATE88 status (2026-09-24)

Same poke config, but SpringBoard critical-reboot cut the FULL path after
`V178_OK` / `V248` / `TBNZ` — never reached `CMP648` (41 hits). GATE89 retries.

### GATE89 result (2026-09-24) — memory-only 648 poke (wrong)

`forced_neq=True` wrote `+0x648=0` but **`BEQ648`/`ALT_11D00` still ×6**,
`NEQ648` ×0. Cause: bp is on `cmp x8,x9` *after* `ldr x8/x9`; clearing
memory does not change the registers already in `x8`/`x9`. GATE90 sets
`x8=0` at the cmp.

### GATE90 result (2026-09-24) — force `x8≠x9` → CREATE_SURFACE

`forced_neq=True` with **`x8=0`**: `NEQ648` ×4, `ALT_11D00` ×0,
`GFLAG bit0=0`, `POST_V170_20C` ×4. Immediately afterward:
`SERVER_RENDER_UPDATE` → `IOMFB_UPDATE_SURFACE` → `DISPLAY_ALLOCATE_SURFACE` →
**`CREATE_SURFACE_ENTRY` ×8** → `ALLOCATE_IOSURFACE`. The `+648==+40` “no
delta” compare was the remaining FULL-path gate after V170.

`CALL_OUTER_A` at `0x1844134cc` still 0 (CREATE arrived via
`SERVER_RENDER_UPDATE_ENTRY` / allocate path instead). Naturalize next: who
should leave `+0x648 ≠ +0x40` (populate the delta list at `+0x640/+0x648`)
without a register poke — plus latch/`+0x2b8`/w3-bit9 from earlier.

### GATE91 result (2026-09-24) — + bounds nonempty

`rft_force_bounds_nonempty` fired, NEQ648 path still reaches CREATE, but
`DISPLAY_ALLOCATE` / `CREATE_SURFACE` still see **`x1=0 x2=0`**;
`ALLOCATE_IOSURFACE_RETURN x0=0`. Mutator-bounds poke does not feed this
allocate site. Static: `0x18445b684` does `ldp w1,w2,[x24,#8]` then
`bl 0x1846820e4`. GATE92 forces 1179×2556 there (`RFT_FORCE_ALLOC_WH=1`).

### GATE92 result (2026-09-24) — force alloc WxH

`alloc_ldp_wh` ×3 with `x24=display+0xd0`, force 1179×2556 →
`CREATE_SURFACE` / `ALLOCATE_IOSURFACE` with **`x1=1179 x2=2556`**,
`ALLOCATE_IOSURFACE_RETURN` **nonzero** (`surface=0x7944d20000`,
`0x7da0d1c000`, `0x7ae2d14000`). `SERVER_RENDER_OUTER` runs after each.
**`FB_SWAP_SET_LAYER` / `FB_SWAP_HANDOFF` / `IOMFB_A408_BUILD` = 0** —
surface exists but never handed to IOMFB. Natural `[display+0xd8]` was
empty while `+0xb8` already holds A453 size (GATE60). Next: combine with
`RFT_BOUNDS_FROM_DISPLAY=1` (GATE66/69 A408 path) and/or naturalize
`+0xd8` from `+0xb8`; try packed `+0x2b8` (`RFT_POKE_2B8_PACKED`).

### GATE93 result (2026-09-24) — bounds-from-display + packed 2b8

Packed `+0x2b8=0x27f049b` passes v178 (bit4 clear). Surfaces still allocate
1179×2556. **`rft_bounds_from_display` never fired**: `APPLEDISPLAY_NEW_SERVER`
×0 and every probed display has `+0xb8=(0,0)`, so `DISPLAY_WH_OBJ` stays
null; mutator bounds remain `(0,0,0,1)` and `w25=1`. GATE94 falls back to
A453 constants in bounds-from-display and also enables
`RFT_FORCE_BOUNDS_NONEMPTY`.

### GATE94 result (2026-09-24) — bounds force + FULL pokes

`rft_bounds_from_display src=A453` + `force_bounds_nonempty` → `w25=0` →
`MUT_ORR_BIT22` ×4. Still `alloc_ldp` nat=(0,0) force 1179×2556 → nonzero
IOSurface ×3+. **Still zero `FB_SWAP_*` / A408** in 360s. FULL-path pokes
may be on a branch that allocates but never hands off — GATE95 is
bounds-only (GATE66/69 shape) as a control.

### GATE95 result (2026-09-24) — bounds-only control

No FULL pokes → `RFT_SHORT_SKIP` ×12 (bit17 still set), **no CREATE**,
SpringBoard `Halt/Restart Timed Out`. Confirms FULL/bit9 stack is required
for allocate on this profile. Static: `SWAP_SET_LAYER` at `0x184478ba4`
`cbz x2` — our hits always have `x2=0`; `CALL_OUTER_A` never fires (CREATE
comes only from `IOMFB_UPDATE_SURFACE`). GATE96 retries GATE39-era SHORT +
mask-force `0x20080` + bounds + alloc WH (historically reached A408).

### GATE96 result (2026-09-24) — SHORT + mask + bounds

Same as GATE95: `RFT_SHORT_SKIP` ×13, no layer-walk/mask-store (SHORT exits
before walk), SB reboot. Mask force cannot help while bit17 keeps RFT on
SHORT. GATE97 = FULL stack + bounds + alloc WH + **`RFT_FORCE_SWAP_X2=1`**
(poke last IOSurface into `SWAP_SET_LAYER` x2 to test the `cbz x2` gate).

### GATE97 result (2026-09-24) — aborted

`SyntaxError: LAST_ALLOC_SURFACE assigned before global` broke callback
import (`hits: 0`). Fixed; GATE98 is the real swap-x2 probe.

### GATE98 result (2026-09-24) — force SWAP x2 → FB_SWAP

After `alloc_surface_saved x0=0x79d511c000`, `RFT_FORCE_SWAP_X2` set
`SWAP_SET_LAYER` x2 to that surface → **`FB_SWAP_SET_LAYER` ×2** and
**`FB_SWAP_HANDOFF` ×2**. Surface metadata: **1179×2556 ARGB**
(`pixel_format=0x42475241`), `alloc_size=12435456`, `bytes_per_row=4864`.
Pixel capture failed (`guest-memory-read-failed`) so no `VISIBLE_IOSURFACE`
stop. **`IOMFB_A408_BUILD` still 0** — handoff without kernel map/A408.
Naturalize next: who should pass the IOSurface as `SWAP_SET_LAYER` x2
(today always null until poke); then why handoff does not reach A408.

### GATE99 result (2026-09-24) — HANDOFF sink

`FB_SWAP_HANDOFF` trampoline `0x188150560` → `0x22a391ea0`:
`ldr x4,[x0,#0xb38]; braaz x4` (else `0xe00002c2`). With forced swap,
sink hits with **`+0xb38=0x23bfe6eec` non-null** and `x2=surface` —
`HANDOFF_SINK_FAIL` never fires — yet **`H17P_SWAP_SUBMIT` / A408 still 0**.
The `+0xb38` callee (`0x22a396eec`) is **set_layer** (stores surface ID +
rects); it is not swap_submit.

`finish_update` has both `mov x2,#0` clear calls (`0x18446c72c`) and a
**real-surface** call `mov x2,x22; bl SWAP` at `0x18446d0c8`. We are taking
the clear path; GATE100 should take the `x22` path (or stop forcing x2 on
clear calls only).

### GATE100 result (2026-09-24) — clear-gate, not x22

`FINISH_SWAP_CLEAR_GATE` ×1 with `[x23+0x10].bit0=1` → clear `mov x2,#0`
path; **`FINISH_SWAP_X22` = 0**. `SWAP_TRY_BEGIN_ASYNC` ×1 then
CREATE/ALLOC 1179×2556; `RFT_FORCE_SWAP_X2` still produced `FB_SWAP`×2 /
HANDOFF set_layer (`+0xb38` ok). **`IOMFB_A408_BUILD` / `H17P_SWAP_SUBMIT`
= 0**. Kernel commit is `_kern_SwapEnd` at `0x22a391334` (IOConnect sel 5).

### GATE101 result (2026-09-24) — skip-clear misses x22 and SwapEnd

`RFT_SKIP_CLEAR_SWAP` cleared `[x23+0x10]`/w8 bit0 (`skip=True`) → no clear
`set_layer`, so **no `FB_SWAP_HANDOFF`**. **`FINISH_SWAP_X22_GATE` /
`KERN_SWAP_*` / A408 = 0**. `SWAP_TRY_BEGIN_ASYNC` only queues work
(`0x184464f60`); Begin thunk is `0x22a3911b0` via stub `0x1881503f0`.
Skipping clear does not fall into x22; post-clear `0x18446ca14` `tbz w24,#0`
skips `update_surface` (`0x18446ca68`) unless bit0 stays set. GATE102 keeps
FORCE_SWAP_X2 (FB_SWAP) and forces the ca14→x22 path.

### GATE102 result (2026-09-25) — x22 gate yes, d0c0 no; Begin thunk ≠ Begin

`RFT_FORCE_POST_CLEAR_X22` reached `FINISH_UPDATE_SURFACE_BL` and
`FINISH_SWAP_X22_GATE` (forced x22=alloc). Upstream `bl 0x1845c2e44` returned
`0xe00002d7`, so `cb48` cleared x22 and `x26` became the error; at the gate
`x9=[+0x66e8]` was PAC garbage → `d054`, then `cmp w26,w25` failed → **d0f8
null-x2 path** (FINISH_SWAP_X22 / d0c0 never). Still `FB_SWAP`×3 via
FORCE_SWAP_X2. `KERN_SWAP_BEGIN_THUNK` (`0x22a3911b0` = `braaz [x0+0xc20]`)
×11; **`KERN_SWAP_BEGIN` / `KERN_SWAP_END` / A408 = 0**. GATE103 forces
x9=0, w26=w25, w8=1, w24=2 at the x22 gate.

### GATE103 result (2026-09-25) — natural x22 SWAP; still no SwapEnd

x22-gate force worked: **`FINISH_SWAP_X22` ×1** with natural
`SWAP_SET_LAYER x2=alloc` (`forced_x2=False`) and `FB_SWAP_HANDOFF`×3.
Begin thunk `+0xc20` → local begin `0x22a396be0` (buffer prep, **not**
IOConnect sel 4). **`KERN_SWAP_BEGIN` / `KERN_SWAP_END` / A408 still 0**.
End dispatcher is thunk `0x22a391298` (`braaz [x0+0xc60]`). GATE104 traces
that slot and whether finish_update ever calls it after x22.

### GATE104 result (2026-09-25) — End thunk fires early, not after x22

`KERN_SWAP_END_THUNK` ×2 with `+0xc60` → local `0x22a396bcc`
(`braaz [[x0+0xec8]+0x20]`), both **before** CREATE/X22. After
`FINISH_SWAP_X22` + FB_SWAP×3, **no End**. Real `KERN_SWAP_END` still 0.
Caller of early End is `0x184471df8` → stub `0x1881505b0`. GATE105 forces
End after x22 ret (`RFT_FORCE_POST_X22_END`) and redirects local End to
IOConnect sel 5 (`RFT_FORCE_KERN_SWAP_END`).

### GATE105 result (2026-09-25) — KERN_SWAP_END ×3, still no A408

`RFT_FORCE_KERN_SWAP_END` redirected `+0xc60` → real `0x22a391334`;
`RFT_FORCE_POST_X22_END` invoked End after `FINISH_SWAP_X22_RET` with
`iomfb` from Begin. **`KERN_SWAP_END` ×3** (2 early + 1 post-x22).
**`H17P_SWAP_SUBMIT` / `IOMFB_A408_BUILD` / stderr A408 still 0**. Begin
remains local-only (`0x22a396be0`, no IOConnect sel 4). Next: End IOReturn +
buffer contents, and/or force real `KERN_SWAP_BEGIN` before End.

### GATE106 result (2026-09-25) — real Begin fails; End is MACH_SEND_INVALID_DEST

`RFT_FORCE_KERN_SWAP_BEGIN` redirected `+0xc20` → real
`_kern_SwapBegin` (`0x22a3911cc`). **`KERN_SWAP_BEGIN` ×10**, all return
`w0=0xe00002bc` (`kIOReturnUnsupported`) — the wrapper at `0x22a391224`
maps any non-zero from `IOConnectCallScalarMethod([x0+0x14], sel=4)` to
that code. Local Begin (`0x22a396be0`) only bzeros `+0x18`/`0x6e0` and
bumps `+0xeb8`; it never opens IOConnect. Forced End still returns
`w0=0x10000003` (`MACH_SEND_INVALID_DEST` per `docs/re/native-input.md`)
both early (zero buffer) and post-x22 (buffer head
`0x7248f85`/`0x727a70b`). **A408/H17P = 0**. Root: bad/missing port at
`[iomfb+0x14]`. Local End remaps `x0:=[+0xec0]` before
`braaz [[+0xec8]+0x20]` — GATE105/106 skipped that by writing real End
into `+0xc60`.

### GATE107 result (2026-09-25) — local End → ObjC; port lives on +0xec0

No Begin/End redirects. Post-x22 End thunk kept `+0xc60` → local
`0x22a396bcc`. **`KERN_SWAP_END_LOCAL` ×2**: remaps to
`ec0=0x73b0cd0000`, target runtime `0x19ad4eb7c` = static
`0x196f8eb7c` (dyld `.05` ObjC trampoline: `autda`/`braa`).
**`conn[+0x14]=0` on iomfb; `ec0_conn=0x73`** (live mach port).
Real `_kern_SwapEnd` never entered; A408/H17P = 0. Forced End used
`x1=0`. Next: stitch `ec0` port onto iomfb `+0x14` and run real End so
sel5 uses the filled `+0x18` buffer with a valid port.

### GATE108 result (2026-09-25) — stitch True, but End entry skipped

Post-x22 `KERN_SWAP_END_LOCAL` reported `stitched=True` /
`conn=0x79=ec0_conn`, but **`KERN_SWAP_END` hit count = 0** and no
A408/H17P. Setting `pc` to real End from inside the local-End breakpoint
callback does not re-fire the entry breakpoint on continue. Early Ends
correctly left `stitched=False` (pre-x22).

### GATE109 — stitch at x22-ret, then End thunk → real End slot

Same FULL+x22 forces. At `FINISH_SWAP_X22_RET`: copy `ec0` port into
iomfb `+0x14`, write real `_kern_SwapEnd` into `+0xc60`, jump End thunk
so entry/return probes fire. Success: `IOMFB_A408_BUILD` /
`H17P_SWAP_SUBMIT` / `kern_swap_end_return w0=0`.

### GATE109 result (2026-09-25) — real End runs; port still invalid

Stitch-at-x22-ret worked: `forced_end=True stitched=True`, then
**`KERN_SWAP_END` ×1** with `conn+0x14=0x77` and non-zero buffer head
(`0x5d59bca` / `0x5d95e51`). Return **`w0=0x10000003`**
(`MACH_SEND_INVALID_DEST`) — so `ec0+0x14` is not a live send right in
this task (misread field, or dead name). A408/H17P still 0.

### GATE110 result (2026-09-25) — bounds-only no longer reaches A408

`RFT_BOUNDS_FROM_DISPLAY` + nonempty only (no FULL/x22/End):
**`RFT_SHORT_SKIP` ×12**, list entries still `bit17=1` (`+0xc=0x2420a`).
No CREATE / FB_SWAP / A408. So GATE66/69's bounds-only recipe is
insufficient on this build without `RFT_CLEAR_CTOR_W3_BIT9` (and the
FULL poke chain). Also: `ec0+0x14` values like `0x77` match the **high
half of a LE pointer at `+0x10`**, not an `io_connect_t` — stitching that
into iomfb was never going to yield a valid send right (GATE109).

### GATE111 result (2026-09-25) — no live connect; ObjC End skips sel5

FULL+x22+natural local End (no stitch). **`iomfb_port_scan []`**,
`+0x14=0`. IOConnect stubs saw only `conn=0x0` (sel `0xc`/`0x58`/`0`);
**never sel 5**. Post-x22 End stays on ObjC trampoline
(`0x196f8eb7c`); real `_kern_SwapEnd` not entered. Callers at
`0x22a39683c` do `ldr w0,[x19,#0x14]` then scalar sel `0xc` — confirm
the field is the connect and it stays zero. Almost no `str` to
`[xN,#0x14]` in the IOMFB text range: connect is installed elsewhere
(or never). `ec0+0x14` remains a false lead (pointer high half).

### Next

Find who opens the IOMFB userclient and writes connect; or find an
instance whose `+0xc20` already points at real `_kern_SwapBegin` (kernel
path) rather than local `0x22a396be0`.

### GATE112 discovery — we are on `_virt_Swap*`, not a broken End

`ipsw dyld a2s` on the “local” slots:

| Static | Symbol |
|---|---|
| `0x22a396be0` | **`_virt_SwapBegin`** |
| `0x22a396bcc` | **`_virt_SwapWait`** (was misread as local End; lives in `+0xc60`) |
| `0x22a396c70` | **`_virt_SwapEnd`** |
| `0x22a396eec` | **`_virt_SwapSetLayer`** |
| `0x22a3911cc` | `_kern_SwapBegin` |
| `0x22a391334` | `_kern_SwapEnd` |
| `0x22a3912b4` | `_kern_SwapWait` |
| `0x22a3918f4` | `_kern_SwapSetLayer` |

Every Begin thunk in GATE111 had `+0xc20 → _virt_SwapBegin`. That path is
the iOS 27 in-process swap family (no userclient) — same reason
`vphone-cli` ships `cfw_patch_iomfb_force_kern.py` to force `_kern_Swap*`.
ObjC at `+0xec0` is likely the virt completion side, not kernel A408.

**Trampoline slots (corrected):** Begin `+0xc20`, **End `+0xc28`**, Wait
`+0xc60`, SetLayer `+0xb38`. We had been forcing Wait's slot as End.
`vphone` rewrites the public trampoline's first insn to `b _kern_*`.

### GATE113 result (2026-09-25) — Open never runs; OpenByName is virt type 2

`_IOMobileFramebufferOpen` **0 hits**. `GetMainDisplay` ×4 all return
`0xe00002c0` with null obj. `OpenByName` ×8 from AppleDisplay ctor
(`lr=0x1845f3d68`) never reaches Open. Static: OpenByName at
`0x22a392428` branches `type==2` to the virt alloc path (no
`IOServiceOpen`); non-2 calls `_iomfb_get_display` → Open with
`service=info+0x14`. Swap objects still `conn+0x14=0`; Begin/End
`0xe00002bc` / `0x10000003`. `last_good_conn=None`.

### GATE114 result (2026-09-25) — force reaches Open; service port is 0

`RFT_FORCE_IOMFB_GET_DISPLAY` correctly diverted type-2 entries into
`_iomfb_get_display` → `_IOMobileFramebufferOpen` (5×). Every matched
display-info has `type=0x2` and **`svc+0x14=0`**, so Open is called with
a null service and returns `0x10000003`; AppleDisplay stores null and
SpringBoard eventually reboots. Virt stubs are not hiding a live
`io_service_t` — there isn't one in the OpenByName table. Kernel serial
does show `IOMFB: service matched: AppleDCPExpert`.

### GATE115 result (2026-09-25) — 16 connects collected; poke site never hit

`IOServiceOpen` returned live connects (`nconns` up to 16, first
`conn=0x907`). OpenByName stayed on virt type-2 (`svc=0`,
`force_get_display=False`); AppleDisplay stored objects with
`conn+0x14=0` that are the same pointers later used for kern Swap.
`FINISH_SWAP_X22_RET` never fired this boot, so
`RFT_POKE_OPEN_CONN`/`ANY` did not run; Begin/End still
`0xe00002bc`/`0x10000003`. SpringBoard eventually rebooted.

### GATE116 result (2026-09-25) — poke works; wrong-process connect

`conn_poked=True` with `conn=0x1913` (first of 16 collected opens).
Begin still `0xe00002bc`, End still `0x10000003` despite non-zero
`+0x14`. Those connects are almost certainly other processes' port
names — swap runs in `backboardd`, so a foreign `io_connect_t` is
`MACH_SEND_INVALID_DEST` / Unsupported.

### GATE117 result (2026-09-25) — backboardd connect reaches kernel as BadArgument

Filter kept 2 backboardd opens (`conn=0x2c1b` svc=`0x2813`,
`conn=0x5803` svc=`0x4803`). Poke of `0x2c1b` into swap `+0x14`:
Begin still `0xe00002bc`, but End changed from `0x10000003`
(INVALID_DEST) to **`0xe00002c1` (BadArgument)** — the port is live in
backboardd, just not an IOMFB userclient (or wrong SwapEnd struct).
Neither connect came from `_IOMobileFramebufferOpen` (still 0 hits).

### GATE118 result (2026-09-25) — second connect → Unsupported

`RFT_POKE_CONN_INDEX=1` poked `conn=0x7d03`. Begin still
`0xe00002bc`; End **`0xe00002c2` (Unsupported)** — different from
index-0's `0xe00002c1` (BadArgument). Neither borrowed backboardd
connect is a working IOMFB Swap UC. One backboardd
`GetMatchingService` hit returned `svc=0x5b03`.

### GATE119 result (2026-09-25) — inject Open failed; x0 clobber

`IOServiceOpen(matched svc=0x7a03)` returned `0x10000003`
(INVALID_DEST); thunk then treated that IOReturn as the IOMFB pointer
(`x0=0x10000003`). No connect installed. Borrowed-connect pokes remain
the only non-null `+0x14` path.

### GATE120 result (2026-09-25) — inject reaches Open; service not openable

`RFT_INJECT_MATCHED_INTO_INFO` + `RFT_FORCE_IOMFB_GET_DISPLAY` correctly
wrote `svc=0x5903` into the type-2 info entry and entered
`_IOMobileFramebufferOpen` ×5. Every Open returned **`0x10000003`
(INVALID_DEST)**; AppleDisplay stored null. Matching-dict hint scan
found no `IOMobile`/`CLCD`/`DCP` strings on backboardd matches. Kernel
serial still only shows `IOMFB: service matched: AppleDCPExpert` — no
`AppleCLCD2` `Registering:` / `AP DRIVER START!`. Conclusion: there is
no live IOMFB userclient to open; borrowed/matched ports are the wrong
class. Next gate is AppleCLCD2 bring-up, not more connect poking.

### GATE121 result (2026-09-25) — `AP DRIVER START!` does fire

Stopped on `IOMFB: AP DRIVER START!` (regex too eager). So the IOMFB
link gets past the init handshake on this image; AppleCLCD2 registration
was not given time. No OpenByName inject this run.

### GATE122 result (2026-09-25) — AppleCLCD2 does register

With `io=0x1f`, serial shows `IOMFB: AP DRIVER START!` then
`Registering: .../disp0@0/AppleCLCD2` (stop fired early on the bare
`disp0@0` nub line, but the CLCD2 register line is present in the same
log). So a kernel IOMFB/CLCD stack **does** bind; OpenByName's type-2
`svc=0` entries are a userspace discovery/cache problem, not a missing
driver.

### GATE123 result (2026-09-25) — real IOMFB Open works; swap uses virt sibling

With `io=0x1f` / AppleCLCD2 registered, OpenByName hits both:
- `type=0` + live `svc` → `_IOMobileFramebufferOpen` **succeeds**
  (`w0=0`, e.g. `obj=0x762d071400 conn=0x13703`, kern `+0xc20/+0xc28`)
- `type=2` + `svc=0` → virt object (`conn=0`, virt fps)

AppleDisplay stores both. **Swap/Begin always runs on the virt sibling**
(`x0=0x762d072800 conn=0`), so kern Force still gets
`0xe00002bc`/`0x10000003`. A408=0. The missing stitch is copying the
type-0 object's connect onto the virt swap object.

### GATE124 result (2026-09-25) — poke works; sibling connect → Begin `w0=0`

`RFT_POKE_OPEN_CONN=1` + `FORCE_KERN_PATH` poked `LAST_IOMFB_CONN` into virt
swap `+0x14`. Early Begins used stale `0xc303` / wrong `0xad0f` →
`0xe00002bc` Unsupported / End `0xe00002c2` BadArgument. Late in the run,
type-0 Open produced `obj=0x75970bc000 conn=0x11803`; virt sibling
`0x75970bfc00` Begin with that poke returned **`w0=0`**. Probe hit SECS=300
before End/A408 on that path. A408=0. Lesson: prefer sibling Open pair, not
last Open globally.

### GATE125 result (2026-09-25) — sibling poke: Begin+End `w0=0`; no A408

Sibling-prefer poke (`dist=0x1400`, conn from type-0 Open) made
**`kern_swap_begin_return w0=0`** and **`kern_swap_end_return w0=0`**
(including post-x22 End with non-zero buffer head `0x6dd1a58`/`0x6e102b6`).
CREATE/FB_SWAP×3. **`KERN_SWAP_SET_LAYER` hit=0** — `+0xb38` stayed
`_virt_SwapSetLayer`. No `H17P_SWAP_SUBMIT` / `IOMFB_A408_BUILD` / stderr
A408. Kern Begin/End alone are not enough without kern SetLayer registering
surfaces.

### GATE126 result (2026-09-25) — kern SetLayer hits; End → `kIOReturnNoMemory`

`RFT_FORCE_KERN_PATH` now also writes `+0xb38 → _kern_SwapSetLayer`
(`0x2483618f4`). **`KERN_SWAP_SET_LAYER` ×16+** including post-handoff with
`x2=IOSurface*` (`0x73b3158060`, GetID=`0x1`, 1179×2556). Begin still
`w0=0`. Post-x22 End runs `IOMFB_GENERIC_MAP` then returns **`w0=0xe00002bd`**
(`kIOReturnNoMemory`). A408/H17P still 0. Early empty SetLayer Ends still
returned 0. Next: why generic map/End NoMemory on the real surface (map
return, DART, SetLayer ID vs pointer ABI).

### GATE127 result (2026-09-25) — GENERIC_MAP returns NoMemory on real surface

Empty SetLayer Ends: `IOMFB_GENERIC_MAP_RETURN w0=0`, End `w0=0`.
Post-handoff with `x2=IOSurface*`: **`IOMFB_GENERIC_MAP_RETURN w0=0xe00002bd`**,
End same. `IOMFB_SURFACE_MAP_ENTRY` never hits (fails inside generic map).
GATE125 (virt SetLayer) mapped the surface End with End `w0=0`. So kern
SetLayer + surface pointer is the NoMemory trigger.

### GATE128 result (2026-09-25) — ID-as-x2 applies; surface End path stalls

`RFT_SET_LAYER_ID_FROM_W8=1` rewrote handoff `x2` from IOSurface* to
`w8=0x1` (`id_from_w8=True`). Empty Ends still map/End `w0=0`. After the
ID SetLayer, **no further `GENERIC_MAP` / `kern_swap_end` on that frame**
(path diverged; A408=0). Surface capture still
`guest-memory-read-failed`.

### GATE129 result (2026-09-25) — cache `0x700` forced; map still NoMemory

`SURFACE_CACHE_REQUEST` recorded `requested_cache=1792` (`0x700`).
Post-surface `IOMFB_GENERIC_MAP_RETURN` still **`w0=0xe00002bd`**;
`finish_x22_gate` also saw **`w25=0xe00002d1`** (`kIOReturnBadMedia`, the
documented cache-mismatch code). A408=0. So 0x700 at create_surface alone
does not clear the map failure under kern SetLayer + sibling connect.

### GATE130 result (2026-09-25) — virt SetLayer untested: Begin had `conn=0`

`RFT_FORCE_KERN_SET_LAYER=0` left `+0xb38` virt (`0x243106eec`). Cache
`0x700` requested. But every `KERN_SWAP_BEGIN_THUNK` logged
`conn=0 conn_poked=False` → Begin `0xe00002bc`, End `0x10000003`/
`0xe00002c2`. No GENERIC_MAP / A408. Root cause: sibling window found no
peer (swap objs at `0x73ded5xxxx` vs early Opens at unrelated addresses),
and `IOMFB_OPEN_PAIRS` had been filled by non-backboardd Opens so the
GATE125 no-fallback policy left the virt object unconnected.

### GATE131 result (2026-09-25) — sibling poke fixed; virt path builds A407

Backboardd-only Open pairs + `LAST_BB_*` fallback: first surface frame
logged `kern_swap_begin_poke … why=sibling … conn=0x12503
conn_poked=True`, Begin `w0=0`, End `w0=0`, `+0xb38` stayed virt
(`0x231362eec`). Cache request `0x700`. Real 1179×2556 handoff still
fires (`FB_SWAP_SET_LAYER` / `SURFACE_JSON`).

Post-surface `IOMFB_GENERIC_MAP` returns **`w0=0`** and takes
**`IOMFB_A407_BUILD`** (×6 including the surface End) — never
`PRIMARY_MAP` / `SURFACE_MAP` / `A408` / `H17P`. `finish_x22_gate` still
sees `w25=w26=0xe00002d1` (`kIOReturnBadMedia`). So virt SetLayer +
sibling connect + `0x700` is a successful empty/cancel submit path, not
a mapped-surface path. Contrast GATE129: kern SetLayer reaches
GENERIC_MAP with a real surface and returns `0xe00002bd` before
PRIMARY_MAP.

### GATE132 result (2026-09-25) — cache `0x700` OK; `0xa0b910c` returns NoMemory

Sibling poke + kern SetLayer + `0x700` request. Empty Ends still A407 /
`GENERIC_MAP w0=0`. Post-handoff:

- `IOMFB_PRIMARY_MAP_CALL` at corrected **`0xa0c40b0`** hits
- `IOMFB_CACHE_MAP_ENTRY` (`0xa0b910c`) with IOSurface
  `x1=0xffffffdfc7870400`, **`cache+0x9c=0x700`**
- return **`w0=0xe00002bd`** (`kIOReturnNoMemory`) → GENERIC_MAP same
- no `SURFACE_MAP` / A408

So the allocation-side `0x700` poke stuck; BadMedia cache mismatch is
ruled out. NoMemory is raised inside `0xa0b910c` after entry (next:
early `bl 0xa0dcd30` at `0xa0b9168` vs post-cache `bl 0xa0dcdb0` at
`0xa0b919c`). `finish_x22` still shows userspace `0xe00002d1`.

### GATE133 result (2026-09-25) — wire returns 0 → NoResources−1 = NoMemory

Post-handoff path through `0xa0b910c`:

1. `cache_map_after_prep w0=0 take_cache_path=True` (prep stub OK)
2. `CACHE_PATH` hit (passed `cmp #0x700`; surface still `+0x9c=0x700`)
3. `AFTER_WIRE w0=0` — GOT stub `0xa0dcdb0` → `0xfffffff00a29590c` returned **0**
4. `NORESOURCES` hit (`mov w23,#0xe00002be`)
5. Fallback `bl 0xa0dcec0` (`→ 0xfffffff00a2943dc`) returns null →
   **`sub w23,w23,#1`** at `0xa0b9374` yields **`0xe00002bd`** (NoMemory)

So NoMemory is NoResources demoted after both the primary wire helper and
its fallback return nothing. Next: why `0xa29590c` returns 0 for this
1179×2556 `0x700` IOSurface (memory descriptor at surface `+0x30`, flags
`+0x3d0`/`+0x3d1`, plane wiring).

### GATE134 result (2026-09-25) — wire always returns 0; fallback is the miss

IOSurface `0xffffffdf022c8400`: `+0x30` memdesc **non-null**
(`0xffffffde1bb6c090`), `+0x3d0=0x804890d`, cache still `0x700`. Wire
`0xa29590c` ran MEMDESC → AFTER_LOCK (`w0=0` fall-through) → RET0 with
`w21=1` and **`mov w0,#0`** — so the helper’s normal exit is 0, which
the caller treats as “no primary resource” and enters NoResources /
fallback. The NoMemory demotion is therefore from **fallback
`0xa0dcec0` → `0xa2943dc` returning null**, not from a failed cache
check or a missing memdesc.

### GATE135 result (2026-09-25) — memdesc OK; prepare 0; map path → NoMemory

Fallback `0xa2943dc` returns the live memdesc (`+0x30` non-null,
`0xffffffe22a04b350`). `NOMEMORY_DEMOTE` (`sub w23,#1`) does **not** hit.
`AFTER_MEMDESC_VT` sees **`w0=0`** from memdesc vtable `+0xd8` with
`w1=0` — IOReturn success for prepare-style, which takes the `937c`
alternate map path. Final `0xa0b910c` / GENERIC_MAP / SwapEnd still
`0xe00002bd`. So the miss is inside the post-prepare mapping at
`0xa0b937c`… (DMA / `IOMemoryMap` creation), not cache mode, not a null
memdesc, and not the NoResources−1 demote.

### GATE136 result (2026-09-25) — memdesc vtable `+0x88` returns NoMemory

Post-prepare `937c` path: `0xa0d5cd8` returns `0`, then memdesc vtable
**`+0x88`** returns **`w0=0xe00002bd`** (`AFTER_VT88`). That value is
copied into `x23` and becomes the `0xa0b910c` / GENERIC_MAP / SwapEnd
status. `AFTER_DC540` / `MAPFAIL_JOIN` do not hit. Root failure for the
kern-SetLayer real-surface map is this IOMemoryDescriptor method on the
surface’s `+0x30` memdesc (`0xffffffe0ec1cf9a8` this boot), not the
cache-mode gate.

### GATE137 result (2026-09-25) — `+0x88` is mapper bind; nested memdesc `+0x90` NoMemorys

`vt88_call`: `obj=0xffffffe0a969cfb8` (not the memdesc), `memdesc=…c8b8`,
`w2=1`, live slot **`0xfffffff02b249740`** (static `0xfffffff00b249740`,
kernel slide `0x20000000`). That function compares `x1` to `[obj+0x48]`,
then on the install path calls **memdesc vtable `+0x90`** at
`0xb24983c` with `w1∈{0x2000000,0x2000001}`, `x2=[obj+0x70]+0x40`,
`w3=0x20`; **`cbnz w0 → return that status`**. Outer `AFTER_VT88` is
`0xe00002bd`, so the nested memdesc `+0x90` (physical-segment / map
query) is the NoMemory source. (A Busy path at `0xb249778` =
`0xe00002d5` exists but was not taken.)

### GATE138 result (2026-09-25) — inconclusive: PSEG BP exhausted by launchd

`IOMFB_MAPPER_MEMDESC_PSEG` hit **16/16** on `launchd` (all
`memdesc_pseg_ret w0=0`) long before backboardd’s surface map. The
surface `vt88_call` still returned `0xe00002bd`, but the nested `+0x90`
site was already disabled. Slot target for `+0x90` from early hits:
`0xfffffff02b25713c` (static `0xfffffff00b25713c`).

### GATE139 result (2026-09-25) — NoMemory is mapper vtable `+0xa0`

PSEG hit budget (256) was again burned by non-backboardd before the
surface map (filter only suppressed logs). On the surface frame,
`vt88_call` → **`mapper_vt_a0_call`** (`0xb249964`) with
`x1=0 x2=0 x3=1 x4=1` → **`mapper_vt_a0_ret w0=0xe00002bd`** →
`AFTER_VT88` / GENERIC_MAP same. So for this IOSurface the mapper
bind’s post-setup method at **vtable `+0xa0`** is the NoMemory source
(not the nested memdesc `+0x90` query, which is either skipped or
succeeds before `+0xa0`).

### GATE140 result (2026-09-25) — `+0xa0` is IODMACommand; target `0xb248f94`

PSEG budget fix worked for the surface frame. Live dump:

- `vt88_call`: `slot+0xa0=0xfffffff02b248f94` (static `0xb248f94`),
  `+0x48=0 +0x64=0 +0x68=0 +0x70` present
- `mapper_vt_a0_*` → **`w0=0xe00002bd`**

Static (kstrs at `0xb2495b0`): **`IODMACommand.cpp` /
`dmaCommandOperation`**. Inside `0xb248f94`, null return from the
segment-array alloc at `0xb2493b0` (`bl 0xaaf4604`) does
`sub w20, w24, #0x1b` with `w24=NotReady (0xe00002d8)` → **NoMemory**.
So the map dies creating/populating an IODMACommand for the surface
memdesc, not in the earlier PSEG query (which returned 0).

### GATE141 result (2026-09-25) — NoMemory from memdesc `+0x90` options `0x5000000`

Surface-frame dump at `+0xa0` entry:

- `+0x30=-1 +0x40` (IOMMU parent) non-null `+0x50=0xb24aae8`
- `+0x48` already the surface memdesc; `seg+0x40=0xbdc000`
  `seg+0x68=0 seg+0x75=0 seg+0xb0=0`
- Prior bind PSEG (`w1=0x2000001`) on that memdesc returned **0**
- **`iodma_pseg5_ret w0=0xe00002bd`** — IODMACommand path’s
  memdesc vtable `+0x90` with **`w1=0x5000000`**, out size `0x58`,
  fails; `sp+0x78` stays 0
- Helper / kalloc / NotReady−0x1b demote never run

So NoMemory is not “out of kernel heap”; it is this DMA prepare /
`dmaCommandOperation` query on the wired IOSurface memdesc.

### GATE142 result (2026-09-25) — `[md+0x90]` present; fail is deeper

Surface op `0x5000000` frame:

- `memdesc_dma90_op4`: **`+0x90=0xffffffe025ecdae0` (non-null)**
- prep: `prep0`=IOMMU parent, `prep8`=mapper obj, `prep+0x10=1`,
  `prep+0x40=0xbdc000`
- **no** `null90` / `nomem` demote hits
- `iodma_pseg5_ret w0=0xe00002bd`

So the CannotWire−0x2a path is not this failure. After the op4 preamble,
`0xb25713c` calls **`0xb257c54`** (`bl` at `0xb257540`); that return is
the remaining NoMemory candidate.

### GATE143 result (2026-09-25) — map helper returns NoMemory via fast-path IOMMU

```
iodma_map_helper md=… iommu=… x5=0 x6=0xbdc000
  md+0x20=0x70510513 md+0x50=0xbdc000
iodma_map_helper_ret w0=0xe00002bd
```

`md+0x50 == x6` skips the `d1c` IOMMU call (`d7c` never hits).
The equal-length fast path still does **`blraa` IOMMU vtable `+0x550`
at `0xb257e54`**; epilogue `d9c` returns that status as NoMemory.

### GATE144 result (2026-09-25) — inconclusive (SpringBoard reboot)

Guest hit `rebooting due to critical process crashes: SpringBoard`
before the surface `vt88` frame. GENERIC_MAP ×4 with sibling poke
ok; no `iodma_iommu_map`. Retry as GATE144B.

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
  `/tmp/dvm/UI_RENDER_GATE72.lldb.log`, `/tmp/dvm/UI_RENDER_GATE98.lldb.log`
- Serial and model logs: `/tmp/dvm/probe/UI_RENDER_GATE{66,69,70,72}.serial.log`
  and matching `.stderr.log` files
- Pixel event and raw data: `/tmp/dvm/UI_RENDER_GATE69.events/`;
  GATE98 handoff: `/tmp/dvm/UI_RENDER_GATE98.events/surface.FB_SWAP_HANDOFF.*.json`
  (1179×2556 ARGB; capture failed)
- GATE154 forced-path trace: `/tmp/dvm/UI_RENDER_GATE154.lldb.log`,
  `/tmp/dvm/probe/UI_RENDER_GATE154.serial.log`,
  `/tmp/dvm/probe/UI_RENDER_GATE154.stderr.log`, and
  `/tmp/dvm/UI_RENDER_GATE154.events/` (two raw 1179×2556 IOSurface captures)
- GATE155 natural control: `/tmp/dvm/UI_RENDER_GATE155.lldb.log`,
  `/tmp/dvm/probe/UI_RENDER_GATE155.serial.log`,
  `/tmp/dvm/probe/UI_RENDER_GATE155.stderr.log`, and
  `/tmp/dvm/UI_RENDER_GATE155.events/`
- GATE162 sender/receiver full-body correlation:
  `/tmp/dvm/UI_RENDER_GATE162.lldb.log`,
  `/tmp/dvm/probe/UI_RENDER_GATE162.serial.log`,
  `/tmp/dvm/probe/UI_RENDER_GATE162.stderr.log`, and
  `/tmp/dvm/UI_RENDER_GATE162.events/`
- GATE163 encoder queue/body match:
  `/tmp/dvm/UI_RENDER_GATE163.lldb.log`,
  `/tmp/dvm/UI_RENDER_GATE163.probe.out`,
  `/tmp/dvm/probe/UI_RENDER_GATE163.serial.log`,
  `/tmp/dvm/probe/UI_RENDER_GATE163.stderr.log`, and
  `/tmp/dvm/UI_RENDER_GATE163.events/`; static transaction disassembly:
  `/tmp/dvm/gate163-commit-transaction.txt`
- GATE164 invalid callback attempt (Python `UnboundLocalError`; no inference):
  `/tmp/dvm/UI_RENDER_GATE164.lldb.log`, `/tmp/dvm/UI_RENDER_GATE164.probe.out`
- GATE165 timing-tail trace and static object path:
  `/tmp/dvm/UI_RENDER_GATE165.lldb.log`,
  `/tmp/dvm/UI_RENDER_GATE165.probe.out`,
  `/tmp/dvm/probe/UI_RENDER_GATE165.serial.log`,
  `/tmp/dvm/probe/UI_RENDER_GATE165.stderr.log`,
  `/tmp/dvm/UI_RENDER_GATE165.events/`, and
  `/tmp/dvm/gate165-commit-transaction.txt`,
  `/tmp/dvm/gate165-layer-commit-callback.txt`,
  `/tmp/dvm/gate165-commit-subhelper-a.txt`,
  `/tmp/dvm/gate165-commit-subhelper-b.txt`
- GATE168 content-copy and mode-gate trace: `/tmp/dvm/UI_RENDER_GATE168.lldb.log`,
  `/tmp/dvm/UI_RENDER_GATE168.probe.out`,
  `/tmp/dvm/probe/UI_RENDER_GATE168.serial.log`,
  `/tmp/dvm/probe/UI_RENDER_GATE168.stderr.log`; extracted QuartzCore:
  `/tmp/dvm/gate168-extract/QuartzCore`
- GATE169 mode-fallback trace: `/tmp/dvm/UI_RENDER_GATE169.lldb.log`,
  `/tmp/dvm/UI_RENDER_GATE169.probe.out`,
  `/tmp/dvm/probe/UI_RENDER_GATE169.serial.log`, and
  `/tmp/dvm/probe/UI_RENDER_GATE169.stderr.log`
- GATE170 mode-query trace: `/tmp/dvm/UI_RENDER_GATE170.lldb.log`,
  `/tmp/dvm/UI_RENDER_GATE170.probe.out`,
  `/tmp/dvm/probe/UI_RENDER_GATE170.serial.log`, and
  `/tmp/dvm/probe/UI_RENDER_GATE170.stderr.log`
- GATE182–186 natural IOMFB mode path: `/tmp/dvm/UI_RENDER_GATE186.lldb.log`,
  `/tmp/dvm/UI_RENDER_GATE186.probe.out`,
  `/tmp/dvm/probe/UI_RENDER_GATE186.serial.log`, and
  `/tmp/dvm/probe/UI_RENDER_GATE186.stderr.log`; extracted IOAVFamily and
  display KEXTs under `/tmp/dvm/gate186-kexts/`, IOAV disassembly at
  `/tmp/dvm/gate185-ioav.disasm.txt`, and parsed DeviceTree at
  `/tmp/dvm/gate186-dtree.json`
- GATE171 non-reproducing startup: `/tmp/dvm/UI_RENDER_GATE171.lldb.log`,
  `/tmp/dvm/UI_RENDER_GATE171.probe.out`,
  `/tmp/dvm/probe/UI_RENDER_GATE171.serial.log`, and
  `/tmp/dvm/probe/UI_RENDER_GATE171.stderr.log`
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

### PLUGIN147 result (2026-10-10) — full-path renderer exits at V170

P147 used the durable replay2b-class display lineage, accepted merged DVMI2
trust cache, CLASS16, and the existing D572/D582/ECHO/A407 hypothesis profile;
A408 stayed off. The shell ladder returned and SpringBoard produced a
non-null home entity after Home input. Two bottom-center swipes were ACKed and
dispatched without errors, but the captured lock transition had
`forUserGesture=0`; the boot does not establish a successful dismissal. HMP
remained uniformly black
(`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`), with
zero DVMI2 presents/input-to-present samples and no IOSurface event through
the 900-second post-input window.

Backboardd's sparse scheduler counters reached at least 2,048 at
`RFT_FULL_PATH` and `RFT_AFTER_V170`. V178/V248 returned 0; VF8 and V170
returned 1. No post-V170 continuation, Server render, OGL, swap, or surface
callback was recorded. The post-call observer treated x19 as the display
receiver, but x19 was stack-like there; those receiver fields are invalid and
must not be used to assign a mode or state. After P147 closed, the observer
source gained a read-only `RFT_BEFORE_V170` hook that samples the call-site x0
display, x1 Update, and the predicate inputs before the virtual call. The
source passed syntax checks, but this hook was not loaded in P147 and still
needs a fresh-boot validation. P147 therefore reconfirms the V170 exit while
leaving its actual predicate inputs unmeasured on this boot. This does not
establish a missing DCP report or D572/D582 correctness.

The durable child is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin147-render-gate1.qcow2`
(SHA-256 `e932a63396e2ad65c7d9ec8d319befed4562ef24acb63cda1775d35591cb985e`);
the evidence bundle is in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN147_RENDER_GATE1/`.

### PLUGIN171 result — the natural active-display predicate still rejects updates

P171's pre-V170 observer is a runtime validation of the read-only call-site sample on a post-shell/Home-entity boot. Across 128 sampled full-path `render_for_time` iterations, the active display receiver had mode byte `+0x2bb=0xff`, state bytes 4/5=`0/1`, and each natural `Update+0x140` was `0x80010`; V170 returned `ignore=1` every time. Pending/ignore bytes remained zero. By the GATE236 predicate decode, the sentinel mode selects the branch combining the update mask with state byte 5, and the absent `0xa` mask bits explain these observed ignores. No later Server render/surface path is covered after the LLDB session lost its QEMU connection.

A subsequent bottom-edge input on P171 was correctly delivered (37 ACKs, 33 dispatches, no errors or coalescing), but it occurred after debugger loss and did not change the uniform-black HMP hash. Therefore it does not show whether the post-dismissal update had the same predicate values. The next cut should correlate one active-display `Update*` through `prepare_layer`/`add_context` and V170, using a reduced breakpoint set that remains attached through the gesture. Do not synthesize bits or surface pixels. Durable P171 data is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN171_P134_RENDER_DECISION1/`.


P172 captured a SpringBoard dismissal transition with `x3=1`, followed by ongoing CA commits, but no post-transition Server/OGL output. Later swap handoffs passed null IOSurface arguments; all sampled HMP frames stayed uniformly black. The pre-transition IOSurfaces show only sparse lock chrome, so the run does not establish that a Home surface was generated or scanned out. P172's Update/V170 samples were not tied to the accepted transition. P173 adds a marker at that exact transition and gates Update/V170 and CA message observations on the marker, to determine whether the new state generates a display Update and what predicate it reaches.

#### PLUGIN173 — accepted transition reaches V170, but downstream branch and surface are open

P173 recorded an accepted SpringBoard lock dismissal (`x3=1`, `1791652814.948`) on a fresh P134-lineage child. Three marker-gated active-display `render_for_time` samples about 2,063–2,066 seconds later had Update flags `0x01000018`, display mode byte `+0x2bb=0xff`, and state bytes 4/5=`0/1`; V170 returned zero each time. This shows the sampled post-transition updates pass `ignore_update_p`.

The runtime probe in P173 did not include the later compare/branch successors, so it cannot say whether the updates reach the outer render call. HMP stayed uniformly black through the final 1179×2556 capture (zero pixels above RGB 8; hash `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`); no post-transition nonblack IOSurface was established. Reattachment reset the present observer's Home-return flag, and six early logger errors preceded a runtime fix; neither quiet observer rows nor those initial errors are downstream render verdicts. The trigger helper exited 4, so the accepted transition is not attributed to its automated swipe without a matched record. Durable evidence is under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN173_P134_CA_AFTER_ACCEPT1/`. P174 arms the branch successors from boot and preserves the accepted-transition gate.

### P174: post-shell OGL/IOSurface activity is lock content, not Home

P174's first Home/swipe input preceded the real BKS shell-service return by about 1,132 seconds, so it does not classify a post-shell unlock attempt. After shell return, workspace and poster-scene activity continued, and two OGL render entries were captured. Several full-size IOSurface samples still contained only sparse lock chrome on black (9,884, 7,108, and 10,734 nonblack pixels); HMP stayed uniformly black. This establishes later compositor activity without a visible Home frame or scanout.

The follow-up trigger now requires the explicit `SHELL_SERVICE_RETURN` marker before input. Read-only lock-manager/coordinator entry and dynamic return probes were added to identify delayed relock; these hooks and the corrected input gate still need boot validation. P174's durable record is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN174_P134_POSTV170_PATH1/`.

### PLUGIN181 follow-up — accepted dismissal still returns `ignore=1`

P181 recorded a same-boot accepted user dismissal (`x3=1`) and 65 paired post-transition V170 returns. Every sampled `IOMFBDisplay::ignore_update_p` result was 1 with `Update+0x140=0x80010` (`flags & 0xa == 0`); HMP stayed uniformly black through the completed 3,900-second sampler. The pre-V170 call-site breakpoint did not emit, so P181 does not contain its active-display receiver/state values. LLDB stopped at the optional skip-target breakpoint `0x190315c48`; that extra breakpoint has been removed from the next probe. A direct implementation-entry observer at static `0x1844561a0` now samples x0 display, x1 Update, display state bytes, and Update flags. Its source passes `py_compile` and `git diff --check`, but it has not yet been boot-validated. No update flags or display state are injected.


#### PLUGIN186: post-dismissal updates pass V170 and enter OGL; surface remains unmeasured

P186's accepted lock-dismiss transition was followed by three marker-gated active-display updates. Each used flags `0x18`, returned `w0=0` from V170, and reached OGL with the same `Update*`. This extends the QuartzCore result beyond the ignore predicate, but does not establish a prepared non-black layer, IOSurface allocation, framebuffer swap, or visible Home frame. HMP remained uniformly black through 2,645 seconds. The durable P186 child and records are documented in `iomfb-dseries.md`; P187 installs the downstream server/display/surface/swap observers only after the accepted transition. D572/D582 remain hypotheses.

#### P188: accepted dismissal did not alter natural Update or visible surface

On P188, an accepted same-boot lock dismissal was followed by natural `render_for_time` samples with `ignore_update_p=1`, flags `0x80010`, and `flags & 0xa == 0`, including samples through +2,694 seconds. Three later observed BackBoard framebuffer swap handoffs had null surface arguments; the 1179×2556 HMP samples remained uniform black. The run does not show a prepared Home layer or a non-null display surface. Its mode-source and post-gesture mutator-flags traces were not enabled, so the next diagnostic captures those read-only sources before considering any change to eligibility state. P188 child and records are documented in `iomfb-dseries.md` and stored under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN188_P182_SWIPE_SEQUENCE_PRESENT1/`.


#### P190 mode source — display object 1 remains at sentinel mode

P190's read-only mode trace captured display object ID 1 receiving mode-array error `0xe00002bc`, falling back to `-1`, and retaining `-1` after the mode-set method returned success. The later power-mode lookup returned `-1`; other display objects accepted mode 0. This supports a mode-source issue that should be correlated against an accepted lock transition and a same-Update renderer/surface chain. P190 did not capture that transition: the Home entity was absent, its trigger was stopped before its timeout, and subsequent manual input was interrupted. Do not infer that the mode error caused the black frame from this boot alone.

#### P192: accepted dismissal still exits before OGL on the active display

P192 used the durable P182 lineage. After shell return, SpringBoard produced a non-null Home entity, and the input helper acknowledged a Home key and two bottom-edge swipes without dispatch errors. The follow-up swipe returned `HOME_GESTURE_PAN_SHOULD_BEGIN_RETURN` accepted=true, began Cover Sheet dismissal, and reached `LOCKSCREEN_GESTURE_TRANSITION` with `x3=1` / `for_user_gesture=1` at `1791700297.323`. Its stricter auxiliary pan-sequence confirmation was false, so only the direct accepted transition is counted. An automatic Home key after the accepted transition was acknowledged as well. The HMP frames before/after Home and both swipes, and the final frame, were uniform black with SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`.

The marker-gated observer recorded 94 paired active-display V170 samples through +1,625 seconds: display ID 1 stayed at `+0x2b8=-1` (`+0x2bb=0xff`), state bytes 4/5=`0/1`, and natural `Update+0x140=0x80010` with `flags & 0xa == 0`. Every V170 call returned `ignore=1`; `OGL_RENDER_ENTRY` and `CALL_OUTER_A` had zero hits, with no IOSurface or DVMI2 present event. P192 therefore proves Home-entity creation and accepted lock-transition input without a visible Home frame. It correlates the persistent mode sentinel and missing Update bits with the pre-OGL rejection, but does not establish an upstream firmware/model error. No fields were poked; D572/D582 remain hypotheses. Durable child and records: `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN192_P182_MODE_SOURCE_TIMEOUT_SWIPE1/`.

#### PLUGIN195 — mode provider confirms null ColorElements on the primary display

P195 used a fresh child of durable P182, retained the corrected read-only mode
provider observer, and reached the shell-service return and a non-null Home
entity. The Home key was acknowledged. The mode query for primary display ID
1 read service `0x13303` through connection `0x11803`; `ColorElements` was
null, the API returned `0xe00002bc`, and both mode/timing outputs were null.
BackBoard chose and persisted mode `-1`; the power-mode lookup also returned
`-1`. Other display objects accepted mode 0. Active display updates had mode
byte `0xff`, state bytes 4/5=`0/1`, and flags `0x80010`; sampled V170 calls
rejected them before the renderer. HMP stayed uniformly black and no
IOSurface/present event was recorded.

The P195 swipe was fully delivered (37 ACKs, 33 dispatches, no errors or
coalescing), but the captured lock transition had `x3=0`; no accepted user
dismissal was recorded. Later attempts were interrupted and left a held
contact, so they are incomplete. This run therefore advances the measured
display-mode boundary but not unlock. It does not prove what DCP response or
mode payload should be supplied; no display field or scanout data was
modified. The durable child and recovery bundle are listed in the P195 entry
of `iomfb-dseries.md`.

#### PLUGIN206: accepted dismissal has CA commits but no render/surface path

P206 recorded an accepted same-boot lock dismissal (`for_user_gesture=1`),
after shell return and Home-entity creation. The Home key and upward swipe
were ACKed/dispatched; their frames stayed uniform black. SpringBoard made
321 CA commits after the accepted transition. The post-transition BackBoard
observer saw six framebuffer swap set-layer/handoff pairs with null IOSurface
arguments and no Server `render_update`, display render, OGL, finish-update,
or IOSurface-allocation event. This is consistent with the earlier
pre-render gap, but P206 did not arm the scheduler or CA-message observer, so
it cannot distinguish missing CA handoff from scheduling/mode rejection.
P207's primary mode trace ran before input, but a callback exception at
`IOMFB_MODE_VIRT_RETURN` (`KeyError: 49`) detached LLDB. The generic UI module
selected for that boot did not install the accepted-gesture observer, so its
input ACKs and black HMP frame do not establish a dismissal or a
post-transition render result. The run was stopped and archived as coverage
loss. The orphan-breakpoint guard is fixed; the next run must load
`dm_post_shell_home_transition_present_callbacks` and verify its ready marker
before sending Home/swipe input. P207's child and records are stored under
the durable P207 paths recorded in `iomfb-dseries.md`.

#### PLUGIN208/209: accepted-transition render remains unmeasured

P208's mode query repeated the primary-display `ColorElements` null / mode
`-1` result, but it used the repository-default ramdisk; the input helper
never became ready and no gesture was sent. P209 supplied the V12 ramdisk,
yet the guest started `DVM_INPUT` v6 (`protocol=1`) rather than the DVMI2 V12
helper. The host saw no ACKs to 41 readiness pings and sent no key or touch
event. P209 again measured ID-1 mode `-1` / `0xe00002bc` and ID-6 mode 0,
before any accepted transition. It also ended before BKS/check-in and
shell-service returns. Thus neither run scores the post-dismissal CA or
render path. Use a disk state verified to run the V12 helper, then require
its DVMI2 `R` marker before sending input. Child paths and records are in
`iomfb-dseries.md`; D572/D582 remain hypotheses and no display/model fields
were changed.

#### P220 — accepted dismissal; the active display still fails V170

P220 reached shell return and a non-null Home entity, then recorded an
accepted user-gesture lock dismissal at 1791739372.679. The 1179x2556 HMP
frames stayed uniformly black through +602 seconds. In the debugger-covered
post-gesture interval, 65 active-display V170 pairs had mode `-1`, byte
`+0x2bb=0xff`, state bytes `01 01 00 00 00 01 00 00`, and Update flags
`0x80010` (`flags & 0xa == 0`); every `ignore_update_p` result was 1. Those
samples branch before OGL. The read-only mode/state writer watch was armed
only for a few seconds before LLDB detached, so later HMP frames are not a
full-window no-writer or renderer result. All six saved dev1–dev6 trees are
byte-identical, so there is no alternate saved MIPI tree for an A/B. The
DCP firmware image was recovered for static analysis, but QEMU does not
execute its RTKit task. Do not infer a correct mode/property payload or alter
the model from this result alone. Durable child and evidence are documented
in `iomfb-dseries.md`.

#### P227 — accepted dismissal; no active-display render chain

P227 completed shell return, created SpringBoard Home state, and accepted
the user Recap dismissal at `1791749422.812`. The final frame after a full
240-second post-transition observation was uniformly black (nonblack
fraction 0; SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The
natural ID-1 mode query before input read null `ColorElements` from service
`0x13603` / connection `0x11c03` and returned `0xe00002bc`; the display
remained at mode `-1` / byte `0xff` after dismissal.

In 64 post-transition active-display scheduler samples, V170
`ignore_update_p` returned 1 with mode `-1`, state bytes
`01 01 00 00 00 01 00 00`, and flags `0x80010`. The trace logged 65
`render_for_time` decisions (34 five-entry lists with bit17 clear and 31
single-entry lists with bit17 set). The transition-gated present observer
armed, then recorded one CA commit and no Server render-update, display
render, surface, or swap callback in its covered interval. The hotplug state
watch armed on the active display and saw no state write. This is consistent
with the already observed pre-render V170 rejection; it still does not name
the property publisher or prove an incorrect DCP response. See P227 in
`iomfb-dseries.md` for the full artifacts.

#### PLUGIN230 — post-slide AFK hooks confirm the bounded interface-16 gap

PLUGIN230 re-armed the AFK parser/decode/handoff/helper breakpoints after
slide resolution, alongside DCP-MIPI proxy and IOAV/internal-display
consumers. The reset-time session again saw interface-16
`dcpmipi-controller-epic` PUBLISH, but none of those downstream sites ran
before BackBoard's natural ID-1 mode query. `ColorElements` was null and the
query returned `0xe00002bc`. The run ended at that query, so this is a bounded
report-to-parser/handoff miss in the full external-endpoint announcement
profile.

This does not contradict GATE221/224: suppressing external DPTX/HDCP
announcements previously allowed the queued MIPI PUBLISH to decode and the
interface-12 proxy to start/open, but still did not establish `ColorElements`.
No property, mode, report payload, DCP answer, or scanout state was
fabricated. The next discriminating work is static kernelcache
matching/personality and provider-flow analysis for creation of the IOAV
video classes, followed by a same-boot trace of the first MIPI command/report
after proxy open. P230's durable child and capture are listed in
`docs/re/iomfb-dseries.md`.

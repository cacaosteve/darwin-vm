# WindowServer first IOSurface and swap boundary

## Source metadata

- Guest: iOS 27 beta 8, build 24A5430a; target iPhone17,3 / H17P (t8140).
- Executable: `/Users/jdolbe1/dvm-artifacts/extract/bin/backboardd`, arm64e PIE, `__TEXT` static VM address `0x100000000`.
- Framework: QuartzCore from `/Users/jdolbe1/dvm-artifacts/extract/dyld/dyld_shared_cache_arm64e` (symbols from the adjacent `.a2s`), `__TEXT` static VM address `0x1843e1000`.
- Kernel-side comparison: `docs/re/iomfb-d575-external-cancel.md`; its kernel static addresses are for the same guest build and must be re-slid for each boot.

`backboardd` enters `CAWindowServer` at static `0x1000257cc`, chooses a display-backed path only when both its main display and server-specific display objects are non-null, then reaches the normal setup-complete path.  QuartzCore's IOMFB display implementation allocates a real IOSurface at static `0x18471bf38` and hands it to `IOMFBDisplay::fb_swap_set_layer` at static `0x18447238c`; this is the narrowest userspace point at which a non-null surface can be demonstrated.  The existing H17P kernel trace begins at `swap_submit` and can distinguish a real submission reaching the display driver from a userspace-only render or a D575-related external-cancel path.

## Static path and evidence

| Phase | Static address | Observed decision or handoff | Evidence |
| --- | ---: | --- | --- |
| WindowServer start | `backboardd:0x1000257cc` | `StartWindowServer` creates `CAWindowServer` through `[CAWindowServer serverWithOptions:]` at `0x100025918`. | Direct `bl` in `StartWindowServer`; prior disassembly in `docs/re/ca-software-path.md`. |
| Display availability | `backboardd:0x10002593c`, `0x100025954` | Gets `[CADisplay mainDisplay]`, then asks the server for `displayWithDisplayId:`. | Direct calls in the same function; `x21` holds main display and `x22` holds the server display. |
| Headless gate | `backboardd:0x1000259fc`, `0x100025a00` | `cbz x21` or `cbz x22` reaches the same headless branch at `0x100025df0`; otherwise execution remains display-backed. | Disassembly and string `StartWindowServer: headless (display:%{BOOL}u/server:%{BOOL}u)` at `0x100083db4`; setup-complete site is `0x100025f74` (see `docs/re/ca-software-path.md`). |
| Display discovery | `QuartzCore:0x1843e2e58`, `0x1843e3318` | `+[CADisplay mainDisplay]` calls `ensure_displays()` and returns the cached main display. | QuartzCore symbols/disassembly; cached display data at `0x1e7059188`. |
| Internal display/server | `QuartzCore:0x1845f7cb0`, `0x1845f7f84`, `0x1845f77f8` | `AppleInternalDisplay::open(unsigned long)` builds the Apple display (`AppleDisplay` constructor at `0x1845f36b4`); `AppleDisplay::new_server()` builds an `AccelServer` (constructor `0x184629180`). | QuartzCore symbol map and direct call sites. |
| Render route | `QuartzCore:0x184451598`, `0x184776174`, `0x1844510f0` | `AccelServer::render_update` can fall through to `Server::render_update`, which calls `Display::render_display`. | Direct calls; the earlier software-renderer route is documented in `docs/re/ca-software-path.md`. |
| IOSurface creation | `QuartzCore:0x1847ae2cc`, `0x1847ae310`, `0x18471bf38` | `IOMFBDisplay::create_surface` directly calls `CA::WindowServer::IOSurface::allocate_iosurface`. | `bl` at `0x1847ae310`; allocation is reached through `Display::allocate_surface` at `0x1846820e4`, and `IOMFBDisplay::update_surface` at `0x18445b330`. |
| First swap boundary | `QuartzCore:0x18446bb14`, `0x184478b48`, `0x18447238c` | `IOMFBDisplay::finish_update` calls `swap_set_layer`, which reaches `fb_swap_set_layer`; its entry has `x3 = __IOSurface *` (implicit `this` is `x0`, framebuffer reference `x1`, layer `w2`). | `finish_update` calls `swap_set_layer` at `0x18446c734` and `0x18446c768`; `fb_swap_set_layer` calls `IOSurfaceGetID(x3)` at `0x1844723d8` / `0x1844723fc` before its framebuffer handoff. |
| Kernel correlation | `kernel:0xfffffff00918e3c4` | H17P `swap_submit` is the first known kernel-side submit point; its normal map path reaches generic mapping and then A408 or A407 construction. | `docs/re/iomfb-d575-external-cancel.md`: generic map `0xfffffff00a0c366c`, map calls `...40d4` / `...41cc`, A408 `...4664`, A407 `...477c`. |

The two `finish_update` calls do not alone establish a populated layer: they also occur for layer-state updates.  A non-null `x3` at `fb_swap_set_layer`, followed by `IOSurfaceGetID`, is the required userspace proof that an actual IOSurface reached the handoff.

## Runtime address calculation

Do not use these static addresses as runtime breakpoint addresses without resolving each image's loaded `__TEXT` base:

```
backboardd_runtime(address) = B + (static_address - 0x100000000)
QuartzCore_runtime(address) = Q + (static_address - 0x1843e1000)
```

Here `B` and `Q` are the runtime `__TEXT` starts reported by the guest debugger/image list for `backboardd` and QuartzCore respectively.  For the previously observed kernel boot, the slide was `+0x20000000`, making H17P `swap_submit` live at `0xfffffff02918e3c4`; re-measure that slide rather than assuming it persists across boots.

## Ranked runtime test plan

| Rank | Breakpoint or observation | What to record | Interpretation |
| ---: | --- | --- | --- |
| 1 | Run the established no-D575 control first. | Whether the normal display path reaches the breakpoints below. | Prevents a known D575 external-cancel condition from being mistaken for a general display failure. |
| 2 | `QuartzCore:0x18447238c` (`IOMFBDisplay::fb_swap_set_layer`). | `x3`, layer `w2`, and the `IOSurfaceGetID` result. | Non-null `x3` plus the ID call proves the first real IOSurface has reached the IOMFB swap boundary. This is the primary userspace breakpoint. |
| 3 | Kernel H17P `swap_submit` (`0xfffffff00918e3c4` static). | Hit/no-hit and the existing submission fields described in `iomfb-d575-external-cancel.md`. | A hit after rank 2 proves the submission crossed from QuartzCore into the kernel display path. |
| 4 | Kernel generic map `0xfffffff00a0c366c`, then A408 `...0a0c4664` and A407 `...0a0c477c`. | Which mapping variant executes and returned status/handle. | Separates valid imported-surface mapping from a later DCP/driver rejection. |
| 5 | `QuartzCore:0x1847ae310` or callee `0x18471bf38`. | Allocation return and the returned surface pointer. | If rank 2 is absent, distinguishes no surface allocation from a later rendering/swap omission. |
| 6 | `backboardd:0x1000259fc`, `0x100025a00`, and `0x100025f74`. | `x21`/`x22` at the gates and setup-complete log. | Distinguishes failure to obtain a CADisplay from a display-backed WindowServer that never paints. |

## Open questions

| Question | Observation that settles it |
| --- | --- |
| Does the present guest allocate a display surface at all? | A hit on `allocate_iosurface` with a non-null returned surface, followed by rank-2 `x3` non-null. |
| Does an IOSurface submission cross into H17P? | A rank-2 hit followed in order by `swap_submit` in the same boot. |
| Does the failure occur during mapping or after a valid mapped surface? | Capture the generic-map and A407/A408 hits, including their returned status, after `swap_submit`. |
| Is the failure before WindowServer obtains a display? | Capture either headless gate with null `x21`/`x22`, or setup-complete plus the later surface breakpoints. |

## Offline IOSurface metadata cross-check

The iOS 27 beta 8 IOSurface image extracted from the saved dyld cache has UUID
`6838EB44-218E-33CC-BF2D-9F0A323C34F1`. Its accessors confirm the decoder's
runtime layout: `_impl` comes from the signed `_OBJC_IVAR_$_IOSurface._impl`
offset at `0x1e5c16b44`; allocation size, width, height, bytes-per-row,
pixel-format, base component/offset, and plane count are at `_impl+0x90`,
`+0x98`, `+0xa0`, `+0xa8`, `+0xb8`, `+0xb0/+0x70`, and `+0xc8`. For a
zero-plane surface, `IOSurfaceGetBaseAddress` returns the sum of the two base
fields. This validates the offsets but does not guarantee that the backing
address is mapped/readable in a particular process. P178's geometry decoded
but its read failed at byte zero; the failure remains at memory access, not
the field-offset decode. The reproduction and disassembly are preserved in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN279_IOSURFACE_ACCESSORS_STATIC/`.

### P278 live capture note

P278 reached the accepted lock-dismissal transition and then logged 32
`FB_SWAP_SET_LAYER_ENTRY` events, including five non-null `x3` IOSurface
pointers. The live pixel sampler did not emit an IOSurface sample: the loaded
sampler did not recognize the marker after the follow-up helper moved it to
`.first.json`. The corrected source gate accepts both marker names, but a
fresh LLDB boot must validate the correction. P278's HMP frames remained
uniformly black; surface contents are therefore still unclassified. The
durable run record and child hash are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN278_TWO_SWIPE_SURFACE1/`.

### P280 — post-acceptance swap calls supplied no IOSurface

P280 boot-validated the active accepted-transition sampler gate. Twelve
`FB_SWAP_SET_LAYER_ENTRY` calls occurred after the gesture-confirmed lock
dismissal, but every one had `x3=0`; all non-null `x3` values and all four
IOSurface allocation returns were earlier. The corrected sampler therefore
had no surface bytes to read in the post-dismissal interval, rather than
missing a non-null surface. No server `render_update`, display render, or OGL
render callback recurred after the accepted marker. HMP remained uniformly
black through the 900-second post-transition sample. SpringBoard continued
CA commits and pre-syscall `0x9c42`-family message calls, but P280 did not
capture a server-side receive or kernel-resolved destination. The next
discriminator is a bounded full-body CA send/server receive comparison; it
must not equate task-local Mach names. P280 does not classify scanout or a
DCP/model cause. Its durable record is under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN280_ACCEPTED_MARKER_SURFACE_SAMPLE1/`.


### P282 — Lock Screen pixels reach the `fb_swap_set_layer` IOSurface boundary

P282 used a fresh child of the preserved P276 recap-diagnostic checkpoint, with the existing `system.tc` and CLASS16/D572/D582 hypothesis profile. It reached shell-service return, created Home entities, and recorded a gesture-confirmed Lock Screen dismissal at `1791803519.1124082`. A second ordinary swipe was also accepted at `1791804162.5634959`, followed by a Home-key relay ACK. No passcode was entered; these events establish input and SpringBoard lifecycle progress, not authenticated unlock or a visible Home screen.

The key result is that BackBoard `FB_SWAP_SET_LAYER_ENTRY` samples 2 and 3 had non-null `x3` IOSurface pointers `0x7687190520` and `0x7687191300`. The zero-plane IOSurfaces were 1179×2556, 4864-byte stride, pixel format `0x42475241`; stride-aware decoding shows Lock Screen time/status and charging battery, information control, and home indicator. Sample 2 was captured 256.398 seconds after the first accepted transition; sample 3 at 261.923 seconds. Each had 6,879 nonblack pixels, 6,634 pixels with RGB above 32, and peak RGB 255. Their difference is localized to 888 pixels in a narrow strip around the home indicator. This proves recognizable Lock Screen content was present in a BackBoard IOSurface passed at the `fb_swap_set_layer` boundary. It does not prove submission to the kernel, mapping for scanout, or display by the HMP framebuffer.

HMP remained uniformly black at every sampled point from shell return through the 900-second post-transition frame: 1179×2556, zero nonblack pixels, with repeated SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. The follow-up swipe and Home-key ACK did not change that HMP result. The current measured gap is downstream of the demonstrated IOSurface content at the userspace swap boundary, or in the relationship between that surface and the separately sampled HMP framebuffer.

P282 also records eight BackBoard `0x9c42` receives whose first 232 body bytes each have one preceding SpringBoard send candidate with matching payload prefix and 99.5–139.8 ms timing. Full sender bodies were not captured in this boot. This is a unique payload-prefix/time correlation only; it does not establish transaction identity, cross-task Mach-right identity, or surface visibility. The pixel summary, raw events, decoded images, HMP frames, and launch records are preserved in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN282_CA_9C42_POST_ACCEPT_RECEIVE1/`.

The next run gates the kernel trace on the first accepted gesture and checks H17P `swap_submit`, generic mapping, and A407/A408 dispatch boundaries. The decisive result is whether this measured non-null IOSurface is followed by a same-boot kernel submission and mapping event; an unresolved dispatch breakpoint or unchanged HMP frame alone does not name the cause.

### P283 — Lock Screen content at a submitted BackBoard surface

P283 used a fresh P276 recap-diagnostic child. SpringBoard accepted the
Cover Sheet dismissal at `1791805251.8935392`; its gesture recognizer also
recorded the accepted pan's should-begin, began, and ended events. No
passcode was entered. Afterward, two full-size IOSurfaces were both captured
as pixels and observed as non-null `x3` arguments to
`FB_SWAP_SET_LAYER_ENTRY`: `0x75a8de1530` (1179×2556, 4864-byte stride,
BGRA) and `0x75a8de17d0` (same geometry). The second decodes to the Lock
Screen time, charging indicator, Chinese swipe-up prompt, information
control, and home indicator. Its raw SHA-256 is
`58153f94a0fff764a7d0265de9d353de8f48547d9f0c9aecdea21b73498695a2`; the
next full-size sample has SHA-256
`a942a5efff6451a30f52af8f9fca5e2e1b73a1e93677c8240824305c53263f4c`.
This closes the user-space surface-content-to-`fb_swap_set_layer` edge for
these samples; it does not establish kernel submission, successful mapping,
scanout, or an authenticated unlock.

The separate HMP screendumps stayed uniformly black. P283 recorded an
unresolved A408 indirect-dispatch boundary, but no interpreted A407 result
or H17P `swap_submit` hit that proves a scanout submission. The guest
therefore produced and submitted recognizable lock pixels to the BackBoard
swap API while the QEMU HMP framebuffer still showed black. The full-body
`0x9c42` analysis found 19 exact payload-and-size prior-send/BackBoard-receive
matches (6.705–89.574 ms); that is not a transaction ID or shared-right
join. The second-swipe helper was started after the guest monitor had gone
away and sent no input, so this boot does not test a second dismissal. The
durable captures and exact-match report are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN283_CA_9C42_FULLBODY_SWAP_SUBMIT2/`.


#### P290 — no post-dismissal render or non-null layer surface at the sampled swap boundary

P290's accepted CoverSheet dismissal was confirmed at guest time
`1791809738.4652102`. Three observed BackBoard server-render/update, surface
creation/allocation, and OGL sequences all preceded that event. Afterward,
the swap observer saw six `FB_SWAP_SET_LAYER_ENTRY` calls, all with `x3=0`,
in two bursts at about +76 and +663 seconds; no further
`SERVER_RENDER_UPDATE_ENTRY` or `SERVER_OGL_RENDER` was captured. This is a
bounded absence at the armed boundary, not proof that no other render path
exists. SpringBoard CA commit entry/return callbacks continued. HMP captures
remained black; no post-dismissal IOSurface bytes were available at this
boundary to decode or compare to HMP.

The later burst coincided with one `IOMFB_GENERIC_MAP` entry, followed about
8.6 ms later by the `IOMFB_A408_BUILD` dispatch boundary and a same-TPIDR hit
at the `x8` address candidate about 4 ms after that. The run did not record a
generic-map return, descriptor contents, A407 entry, or H17P swap-submit.
This raises a better next observation point but does not show a successful
map, builder completion, or scanout.

P290's child is durable at
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin290-delayed-prompt-swipe1.qcow2`
(SHA-256 `62e7151b79cb2ba828790c05cfa23f1cad57d9e960d47d2e5f114d6a66e350c3`;
`qemu-img check -U` passed). The generic-map descriptor fields and strict
entry-thread/restored-SP return matcher were added after this boot; P290
contains no descriptor sample and does not validate those revised hooks. The
probe also no longer treats LR sampled at the A408-named dispatch instruction
as a return address; any return candidate is armed from the later same-TPIDR
dynamic-target hit, where LR/SP can be paired to that execution.

The first HMP/frame sampler hit occurred before the accepted gesture and
therefore did not itself sample the post-dismissal content. P290's delayed
swipe helper also overwrote the retained first marker with a later
unconfirmed marker. The positive marker was recovered and the accepted gate
restored while the guest remained live, but no subsequent non-null `x3`
surface appeared in the observed interval. The corrected helper now uses the
append-only gesture event log and is unit-tested, not boot-validated. P290's
event and frame records are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN290_DELAYED_PROMPT_SWIPE1/`.

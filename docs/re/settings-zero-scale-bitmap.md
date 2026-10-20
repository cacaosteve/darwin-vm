# Settings zero-scale bitmap path

## Observed failure

A fresh native `Preferences` launch reached the `_IconServices_SwiftUI`
bitmap factory at static address `0x1d41e3db0` and later the nil-CGImage
`BRK #1` at static `0x1d41e3ea0`. The runtime cache slide was
`0x150e0000`.

The factory stop recorded logical width and height of 28, source CGImage in
`x2`, and raw scale `d2 == 0`. The target held in `x21` was
`ISImageDescriptor` (runtime ISA class address `0x1fc978e88`, static
`0x1e7898e88`). This is direct runtime evidence for the failing Settings
path; it is not inferred from the earlier historical crash record.

The relevant caller sequence is:

```asm
0x1d41e3d84  mov x0, x21
0x1d41e3d88  bl  0x1d8019320          ; size
0x1d41e3d8c  mov v8.16b, v0
0x1d41e3d90  mov v9.16b, v1
0x1d41e3d94  mov x0, x21
0x1d41e3d98  bl  0x1d801a380          ; scale
0x1d41e3d9c  mov v2.16b, v0
0x1d41e3da0  mov x0, x20
0x1d41e3da4  mov v0.16b, v8
0x1d41e3da8  mov v1.16b, v9
0x1d41e3dac  mov x2, x19
0x1d41e3db0  bl  0x1d8083180          ; IFGraphicsContext factory
```

`ISImageDescriptor` stores `_size` at `+0x8/+0x10` and raw `_scale` at
`+0x18`. Its `scale` getter is a direct `ldr d0, [x0, #0x18]`.

## Native normalization contract

`-[ISImageDescriptor sanitizedScale]` is static `0x1c13c2ca4`. It reads the
same raw field, applies `frintp`, then `fmaxnm` with 1 and `fminnm` with 3.
Thus raw zero normalizes to 1. In contrast,
`+[IFGraphicsContext bitmapContextWithSize:scale:pixelFormat:]` multiplies
its requested dimensions and CTM by raw `d2`; zero is not an automatic
default in that factory.

The direct sanitizer is `-0x12e210f4` bytes from the call site, outside the
AArch64 `BL` range. A scan of the complete reachable ±128 MiB executable
window found no existing `objc_msgSend$sanitizedScale` selector stub. Do not
rewrite the shared raw `scale` stub: it has other receivers.

## Scoped snapshot validation

`SETTINGS_V6_SANITIZE3` restored the native Home checkpoint and installed an
LLDB callback which changed only a factory invocation meeting all of these
conditions:

- guest program name `Preferences`;
- target ISA class `ISImageDescriptor`;
- raw `d2 == 0`.

The callback wrote `d2 = 1.0`, then allowed the original factory and nil-image
trap to execute unchanged. A native P tap was acknowledged in 8.075 seconds.
After roughly 63 seconds, the captured screen showed the populated Settings
root and no nil-CGImage stop occurred. The screenshot and callback source are
preserved outside the repository at:

```text
/tmp/dvm/checkpoints/INPUT_V6_NATIVE_HOME1/restores/SETTINGS_V6_SANITIZE3/
```

The LLDB transcript was inadvertently directed to `/dev/null`; therefore the
result shows a populated Settings root under scoped normalization and did not
hit the trap during this interval, but it does **not** retain a per-factory
callback record. It is a snapshot diagnostic only, not persistent-patch
validation.

## Guarded fallback encoding

No safe local 12-byte trampoline cave was identified. Nearby consecutive
`BRK` words are condition-branch error targets and must not be repurposed.
If a staged exact accessor veneer is unavailable, this in-place, lower-bound
fallback is confined to the proven factory boundary:

```asm
ldr    d0, [x21, #0x18]
fmov   d1, #1.0
fmaxnm d2, d0, d1
```

It replaces static `0x1d41e3d94..0x1d41e3d9f` with:

```text
a0 0e 40 fd 01 10 6e 1e 02 68 61 1e
```

The required original-byte guard is:

```text
e0 03 15 aa 7a d9 f8 94 02 1c a0 4e
```

The affected bytes live in
`/Users/jdolbe1/dvm-artifacts/extract/dyld/dyld_shared_cache_arm64e.21`,
whose mapping base is static `0x1d0400000`; the subcache file offset is
`0x3de3d94`. In the extracted `_IconServices_SwiftUI` Mach-O, the equivalent
file offset is `0x5d94`.

This fallback preserves raw scales 1, 2, and 3 and maps zero to 1. It is not
identical to `sanitizedScale`: it does not ceil fractional values and does
not cap values above 3 (including positive infinity). It should be used only
if an exact isolated accessor call cannot be staged, and must retain the
original-byte and page-identity guards.

The original screenshot has diagonal row wrapping because QEMU’s PPM writer
emits a 3540-byte RGB stride for a width of 1179 (3537 packed bytes). The
three padding bytes per row violate the P6 format. Removing only that padding
produces `after-63s-tight.png`, with upright Settings content. Native scanout
correctly uses source stride 4864 and a tight 4716-byte BGRA destination
(`darwin_fb.c:91-110`); `ui-qmp-cmds.c:292-319` is the export defect. Use
`screendump <path>.png -f png` for subsequent captures. This is not evidence
of a device-model rendering fault or complete Settings stability.

## Persistent staging candidate

`tools/input/settings_scale_patch_24A5430a.json` holds the exact reviewed
preimage and replacement. `prepare_guarded_cache_patch.py` verifies the original
SHA-256 code-page signature, creates equal-length code/hash payloads, derives a
new CodeDirectory hash, and merges the new cache hash into the existing trust
cache. The original extracted cache is read only. The small restore ramdisk is
attached only through `safe_attach.sh`; the full System disk is patched inside
the restore guest on a new qcow2 child.

For this patch the signature slot is subcache offset `0x7cbafe2`, changing
`1381dd80ab9dd8efc08b5b263569513b7a9b4d9ba83595ba1eac5545a83e4279` to
`1467bdf00d1c0f777ef1811c8bd3682d43d8acb1df29bda927c7cc661adb0451`.
The resulting CodeDirectory hash is `1ccff4078c4256677773f1670933ac2d024c94b0`.
The installer checks every code/hash preimage before its first write and checks
all replacements afterward. Optional helper replacement also guards the exact
old binary and verifies membership of the new signed binary in its trust cache.

`WARM_STABILITY_INSTALL2` emitted `DVM_CACHE_PATCH_INSTALLED` and sealed its
new child. It combines this scale fix with `POWER_PV_RECOVERY_BUILD6`, retaining
input v6, the display allocation policy, nanosecond RTC, native development
activation, and the pinned QEMU3 binary. Artifact inputs and code/hash payloads
are under `/tmp/dvm/WARM_STABILITY_PATCH2`; the immutable installed disk is
`/tmp/dvm/WARM_STABILITY_INSTALL2/disk.qcow2`.

The first installer attempt stopped before writing because the restore
ramdisk has no usable `/tmp`; the corrected installer compares streamed bytes
without creating a scratch file. That failed child was quit and was not reused.
Fresh boot validation is recorded separately as `WARM_STABILITY_FRESH1`.

## P544 cache-state check

P544 did not apply another cache patch. Its P540-derived parent
(`ui-class16-refresh60-dm-plugin544-settings-scale-base-from540.qcow2`, SHA-256
`673e7e7358305a82a77e4f5cb71ca8edf49c134f795cae467e2382d73f32a577`) already
contains the reviewed 12-byte replacement at subcache offset `0x3de3d94` and
the matching CodeDirectory slot at `0x7cbafe2`. Read-only guest reads matched
both staged postimages exactly. The installer rejected the parent at its
whole-CodeDirectory preimage guard before writing; this was the intended safe
outcome because the parent contains other CodeDirectory changes relative to
the extracted source. No guard was bypassed and no replacement was written.

This establishes byte presence on the P540 lineage, not that the patch alone
caused any particular Settings UI behavior or that every cache/trust-cache
combination is valid. Do not reapply this patch to that lineage. Test UI effects
on a fresh child of P540 with the matching `system.tc`; keep the Home icon-label
issue separate from this Settings image-scale fix. The archived read-only
fingerprints and the two clean image checks are in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/P544_SETTINGS_SCALE_PATCH1/`.

## P549 Settings UI validation

P549 is a fresh child of P548 with the same accepted TC, natural A408 scanout,
and already-present Settings scale cache patch. A single native tap opened
`Preferences.app`: launchd's `posix_spawn` returned success (`x0=0`, child PID
353), then Preferences reached FBS scene-create/workspace-request/client-request
and scene-update sites. HMP showed Home at 5 seconds after the tap and a stable
Settings page at 20 and 55 seconds. Text including the title and major Settings
rows is legible, so the scale path does not prevent Settings text rendering.

At the IconServices bitmap-factory entry, the observer recorded 28x28 logical
dimensions, raw scale 1.0, and non-null input CGImage pointers. Its nil-image
trap did not fire. This only describes the factory inputs observed in those
calls; the run did not decode the resulting row-icon images. Several Settings
rows visibly use blue question-mark glyphs or have no leading icon, while the
row text renders normally. Keep that asset/content issue separate from native
scanout and from the Home app-name labels, which remain absent. P549 did not
modify the cache, trust cache, DCP, A408, or display geometry.

The relay correctly coalesced 34 host pointer states into 14 completed Recap
packets, including an ACKed endpoint and release. The original driver waited
for an impossible one-ACK-per-state count and was stopped; the tap and HMP
observations were completed manually and recorded. The gate probe exited 0,
panic scan was empty, and the child passed `qemu-img check -U`. Full trace and
frame hashes are in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN549_SETTINGS_APP_TRACE_FROM548/`.

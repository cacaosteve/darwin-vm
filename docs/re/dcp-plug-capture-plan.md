# P281 — real-device DCP Plug capture plan

This is a capture specification, not a new runtime finding. It consolidates
the saved Type-4 T8140 DCP Mach-O disassembly in P265, P237, P255, P257 and
P266–P270 into the smallest useful trace for a real DCP firmware execution.

## Pinned static inputs

```text
Maintainer-side DCP Mach-O artifact: `UI_CLASS16_REFRESH60_DM_PLUGIN233_DCP_PLUG_TRACE_STATIC/t8140dcp.macho`
DCP Mach-O SHA-256: 8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8
DCP Type-4 bundle: maintainer-side artifact `dcp-fw-24A5430a/dcpfw`
DCP bundle SHA-256: 7dedb7972369e7ac71b0959161fde720dbdff1d443e3e0fe91faf141a0a6d0ca
```

The exact DCP binaries and raw supporting disassembly are not checked into
this repository. The Mach-O hash is the same verified reconstruction from
the pinned bundle. Addresses below are static VAs; rebase them using the
loaded DCP image map.

## Capture sequence

1. At type-0 Plug Sync callback `0x42884c8`, record `this` in `x19`, the
   second callback argument in `x20`, `this+0x5ee0`, its vptr, the runtime
   target and return for slot `+0x850`, and the 32-bit field `this+0x42bc`.
   This records the early branch that can return `0x80000007` before builder
   entry. Also preserve the independent `this+0x5cd0` provider pointer.
2. On the continuing path, record the slot `+0x1f0` target/return on
   `this+0x5cd0` (`x1=x20`), then correlate wrapper `0x428865c` and builder
   `constructColorElements` `0x4288a60` on the same callback.
3. At the builder, capture the `+0x70` return (bit 0 is a gate). If it passes,
   capture the targets and returned object/count for slots `+0x80`, `+0x88`,
   `+0x78`, `+0x140`, and `+0x150`. Their static publication roles are
   `TimingElements`, `PreferredTimingElements`, `ColorElements`,
   `DisplayAttributes`, and `Transport`, respectively. Keep callback-receiver
   `this+0x5ee0` separate from property-provider `this+0x5cd0`.
   Static transfer sites, in call order, are `ColorElements` `0x42893c0`,
   `TimingElements` `0x42893d8`, `PreferredTimingElements` `0x42893f0`,
   `DisplayAttributes` `0x4289408`, and `Transport` `0x4289420`. At each site,
   capture whether the value is null, its object/class when present, and the
   transfer result. This separates a failed gate, a missing property, and a
   serialization/transfer failure.
4. At transfer helper `0x4289694`, save the `ColorElements` property's dynamic
   class/vtable, serializer slot `+0x30`, output bytes/count, property key,
   and the child objects referenced by the array. The known AFK metadata
   serializer `0x41c8ed4` writes child IDs, not full nested element
   dictionaries (P236). Do not infer missing child payloads from metadata.
5. Correlate the DCP transfer with AP-side DeviceTree/IOService properties,
   mode-query results, and the display IOSurface/swap/present event from the
   same device and build. The known transfer framing has a four-byte total
   length of serialized count plus one, D127 chunks up to `0x1000` bytes, and
   a 64-byte property key; snapshot bytes rather than synthesizing them.

## Access limitation

The AP-side tree and service dumps can establish device/build identity,
provider publication, and properties. They do not expose the DCP callback's
registers, provider method returns, or serialized in-memory object. Those
fields require an environment that can inspect the loaded DCP/RTKit execution.
The ordinary USB/Xcode workflow has not been shown to expose that internal
trace. No such runtime values are available in this static record.

## Static transfer-site cross-check

`transfer-call-order-disassembly.txt` preserves the 31 arm64e instructions
from static VA `0x42893ac` through `0x4289424`, disassembled from the pinned
Mach-O above with `llvm-objdump --macho --arch=arm64e --disassemble
--section=__TEXT,__text`. Its SHA-256 is
`9970a53fd57120a686b89e6fc99707d995917a1caacb92a70fdc3e96e58054d5`.
The sequence loads each previously built property, skips transfer when its
pointer is null, prepares the property-key address, and calls the shared
helper at `0x4289694`. This confirms the listed transfer sites and null
guards in the saved image; it provides no live property values or panel data.
The `x1` constant addresses resolve in `__TEXT,__const` to the exact NUL-
terminated keys: `0x43cd04b` `ColorElements`, `0x43cd03c` `TimingElements`,
`0x43cd148` `PreferredTimingElements`, `0x43cd160` `DisplayAttributes`, and
`0x43cd184` `Transport`. Their saved-Mach-O file offsets are `0x3ce04b`,
`0x3ce03c`, `0x3ce148`, `0x3ce160`, and `0x3ce184`, respectively.

No device-tree property, firmware, QEMU model, response, display mode, or
pixel data was changed. D572/D582 remain hypotheses.

## P285/P286 static additions

When capturing the P281 builder, also record its first property gate's log
signature: slot `+0x70` at `0x4288aa8`; bit-0-clear takes `0x4288aac` to the
`UnifiedPipeline.cpp:0x2779` diagnostic `HPD not present, skipping mode
export` at `0x4288c70`, before the property getters. Its `x0=0x40000001`
value is a logging argument passed to helper `0x40311bc`, not a demonstrated
builder status return. Exact disassembly and strings are in
`UI_CLASS16_REFRESH60_DM_PLUGIN285_BUILDER_GATE_LOG_STATIC/`.

Static selection code at `0x428fd5c` also assigns the active provider word at
`this+0x5cd0` from `this+0x5ce0` when `w1==0`, otherwise from `this+0x5cd8`.
The nonzero path can clear `this+0x5641`; if `this+0x674a` bit 0 was clear it
logs `found stale fTimingsUpdatePowerState, pipe may not power on properly.
Cleaning up` (`UnifiedPipeline.cpp:0x3bb8`) and sets that bit. Add `w1`, both
candidate pointers, selected `+0x5cd0`, and these byte fields to the capture.
The selector's API meaning, dynamic class, runtime values, and linkage to
P259's factory object remain unknown. See
`UI_CLASS16_REFRESH60_DM_PLUGIN286_PROVIDER_SELECTION_STATIC/` for exact
instruction/string excerpts and limits.

## P292 fallback-to-builder identity additions

The static audit found that helper `0x41df3a4` invokes slots `+0x78/+0x80`
with PAC diversity values `0x8c10/0x37f7` on the interface returned from
input `+0x198`. `constructColorElements` uses the same slots and diversity
values on its property provider; its published roles are `ColorElements`
(`+0x78`) and `TimingElements` (`+0x80`). This structural overlap makes a
shared provider plausible but does not establish receiver or array identity.
On the same hardware Plug event, compare input `+0x198` with
`UnifiedPipeline this+0x5cd0`, helper and builder slot-return range
pointers/counts, and helper x2 fallback-array addresses with builder
materialized child arrays. Do not decode the 24-byte schema from slot overlap
alone. Static audit record: `../UI_CLASS16_REFRESH60_DM_PLUGIN292_FALLBACK_BUILDER_XREF_STATIC/fallback-to-elements-reference-audit.txt`, SHA-256
`93fe8bc9b9284f765754a47c26ef79e869ea13d4dd4673313ab2137f199c8bde`.

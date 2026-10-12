# DCP firmware EPIC service surface

Source: iOS 27.0 beta (24A5430a), iPhone17,3 (iPhone 16, t8140/H17P). Firmware
`t8140dcp.im4p` (recovered by HTTP byte ranges from the matching IPSW on
2026-09-26 to `/tmp/dvm/gate232-firmware/t8140dcp.im4p`, IM4P type `dcpf`,
LZFSE payload 2,565,778 bytes, confirmed via `ipsw img4 im4p info`),
decompressed to the raw 16,695,296-byte bundle
`/tmp/dvm/gate232-firmware/dcpfw`; the larger-task Mach-O is
`/tmp/dvm/gate232-firmware/machos/dcpfw.macho`. Cross-checked
against the AP-side kexts `com.apple.driver.DCPAVFamilyProxy`,
`com.apple.driver.DCPDPFamilyProxy`, `com.apple.driver.AppleDCPDPTXProxy`,
`com.apple.iokit.IOMIPIFamily`, `com.apple.driver.AppleDCP`,
`com.apple.driver.AppleMobileDispH17P-DCP` and the kernelcache
`__PRELINK_INFO` at `/tmp/dvm/prelink/info.plist`. Initially extracted
2026-09-01 and re-recovered by IPSW byte ranges on 2026-09-26.

All firmware citations are `dcpfw+0xOFFSET`, a **file offset into the
16.6 MB decompressed bundle**, found by exhaustive string/byte scanning
(see "Bundle layout" for why this project gave up on resolving these offsets
to firmware virtual addresses). AP-side citations are the kext bundle
identifier and the `IOKitPersonalities` key, read directly out of the
kernelcache's own prelink plist with `plistlib` — no disassembly needed for
those, they are literal property-list entries.

## Summary

The DCP firmware is a custom multi-image "BUND" container, not a bare
Mach-O; inside it are (at least) two full RTKit task images that split into
an "untrusted"-looking client-side task and a smaller "trusted"-looking
server-side task, connected by an internal RPC layer Apple calls
**Tightbeam** (`tb_message.c`, `tb_connection.c`, `tb_codec.c`, `dcpfw+0x497080`
region) — a different, previously-undocumented-in-this-repo mechanism from
the AP-facing AFK/EPIC transport. The AP-facing surface itself is generic
AFK/EPIC exactly as `docs/re/afk-epic-references.md` describes from the
M1-era Linux driver, confirmed directly in this firmware's own class name and
log strings (`AFKEPInterfaceV2`, `pktCat`/`pktType`/`commandID`, `sendOpenReport`
/`sendPublishReport`/`sendCloseReport`/`sendTerminate`). Combining the
firmware's own `-epic` name strings with the kernelcache's `IOKitPersonalities`
gives a **definitive, kext-sourced list of 16 `EPICName → IOClass` pairs**,
plus roughly a dozen more `-epic` names the firmware announces that have no
matching AP-side `IOPropertyMatch` on `EPICName` found in this kernelcache — a
useful signal for what to skip in a minimal model.

## Bundle layout

`dcpfw` opens with a `DNUB` ("BUND" reversed) container header at file offset
`0x8`. A table of 11 named pools follows at offset `0x220`-ish, each record
24 bytes: `u32 id`, `char tag[4]` (ASCII, not reversed — `"txtk"` etc. appear
as literal bytes), `u64 offset`, `u64 size`. Read with a small Python script
(no existing decoder in this repo covers this format):

| tag | id | file offset | size | contents |
|---|---|---|---|---|
| `txtk` | 0xa | 0x010000 | 0x022000 | kernel-privileged boot stub, a complete standalone `MH_EXECUTE` arm64e Mach-O (`__TEXT` vmaddr `0x08000000`, `LC_UNIXTHREAD` entry, no symtab) |
| `txtr` | 0xa | 0x032000 | 0x586000 | **all `__TEXT`-segment bytes** (code+rodata+cstrings) for the two big RTKit task images, back to back |
| `txtu` | 0xe | 0x5b8000 | 0x008000 | small "user text" pool, not investigated |
| `tadk` | 0x11 | 0x5c0000 | 0x004000 | `txtk`'s `__DATA` |
| `tadr` | 0x11 | 0x5c4000 | 0x9d0000 | packed task data segments (`__DATA`, `__DATA_CONST`, `__DATA_SHARED_RW`, `APPCONFIG`, `METADATA`) for both images; `__OS_LOG` is separate |
| `tadu` | 0x15 | 0xf94000 | 0 | empty on this build |
| `orsd` | 0x15 | 0xf94000 | 0x008000 | "ro shared data" |
| `wrsd` | 0x15 | 0xf9c000 | 0 | empty on this build |
| `dlon` | 0x114 | 0xf9c000 | 0x028000 | **Mach-O headers only** — see below |
| `ldbu` | 0x15 | 0xfc4000 | 0x028000 | larger-task `__OS_LOG` string pool; its four code-referenced report strings match at the same segment offsets |
| `glso` | 0x104 | 0 | 0 | empty on this build (name suggests "global oslog", unused here) |

Evidence: table entries read at `dcpfw+0x284` (`txtk`) through `dcpfw+0x374`
(`glso`); every pool's `offset+size` equals the next pool's `offset` exactly,
confirming the table is self-consistent.

**`dlon` holds three complete Mach-O header+load-command blobs, tightly
packed** (each one's header+load-commands ends exactly where the next one's
magic begins — `dcpfw+0xf9c000`, `+0xf9c608`, `+0xf9d3c0`, verified
`0xf9c000+32+0x5e8(sizeofcmds)==0xf9c608` and
`0xf9c608+32+0xd98==0xf9d3c0` exactly). None of the three has segment
content physically adjacent to its header; its declared Mach-O `fileoff`
does not point into the small `dlon` header pool. The payload is repacked in
`txtr`/`tadr`, but the data pool is **not** described by one fixed per-image
`vmaddr − bias`: it packs whole segment blocks from both tasks. Verified
locations include larger-task `__TEXT` at `dcpfw+0x32000`, smaller-task
`__TEXT` at `+0x4d6000`, larger-task `__DATA` at `+0x5c4000`, smaller-task
`__DATA` at `+0xae9000`, smaller-task `__DATA_CONST` at `+0xf20000`, and
larger-task `__DATA_CONST` at `+0xf31000`. The task2 `__DATA`, task3
`__DATA`, and task3 `__DATA_CONST` mappings match their reconstructed
Mach-O segment bytes exactly; the larger task's table at `+0xf31000` also
decodes to targets within its `__TEXT`. The `ldbu` pool's contents match the
four report strings at the offsets referenced from code in the larger task's
`__OS_LOG`. In particular,
`+0xb11000` is inside the smaller task's `__DATA` span, not the larger
task's `__DATA_CONST`. `txtr`'s first byte (`dcpfw+0x32000`) disassembles as
a valid ARM64 function prologue (`adrp`/`add`/`bl`, a stack-guard check) when
treated as vmaddr `0x04000000` of the larger `dlon` image. This project did
not attempt to derive `LC_SYMTAB`'s
`symoff`/`stroff` (`0x9b3000`/`0xb84850` for the big image) into a working
file offset — a `strings`/regex scan for `_Z`-mangled C++ symbols anywhere in
`dcpfw` found **zero** hits, so the shipped bundle most likely has its symbol
table stripped and these fields are vestigial. Findings therefore combine
string mining, raw segment comparisons, and disassembly at numeric VM
addresses; useful C++ symbol names were not recovered.

The two big Mach-O images inside `dlon`:

| # | header @ | filetype | `__TEXT` vmaddr/size | `__DATA...` vmaddr range | `nsyms` (vestigial) | role (inferred, see below) |
|---|---|---|---|---|---|---|
| `dlon#1` | `0xf9c000` | `MH_PRELOAD` | `0xffffffff00000000` / `0x10000` | — | 0 | tiny; sections `__head`/`__tunables`/`__kernel_stack` — looks like the earliest-boot monitor/reset code, not investigated further |
| `dlon#2` | `0xf9c608` | `MH_PRELOAD` | `0x04000000` / `0x4a4000` | `0x044a4000`–`0x04a52000` | 119173 | **larger task** — hosts almost every `*EPClient`/`*Proxy`-facing "Client" class (`DCPExpertSecureInterfaceClient.cpp` @ `0x497d01`, `DCPTrustedInterfaceClient.cpp` @ `0x497d99`, `DCPUntrustedMIPIControllerEPClient.cpp` @ `0x4aa08f`) — i.e. the side that *calls into* the trusted domain |
| `dlon#3` | `0xf9d3c0` | `MH_PRELOAD` | `0x04000000` / `0xe0000` | `0x040e0000`–`0x0452c000` | 38090 | **smaller task** — hosts the "Server" counterparts (`DCPExpertSecureInterfaceServer.cpp` @ `0x5aa516`, `DCPTrustedInterfaceServer.cpp` @ `0x5aa6ac`) and, critically, `AppleDCPSynopsysMIPIDSIControllerTrusted.cpp` @ `0x5b4251` — the actual MIPI-DSI hardware driver lives here |

Both images embed **their own separate copy** of a shared platform-config
string table (every `dcpav-*-epic` name below appears twice, once in each
image's `__TEXT,__cstring`, at a fixed offset apart — e.g.
`dcpav-controller-epic` at both `0x3ea572` and `0x59abda`). This, plus the
client/server split above, is the strongest evidence for a **trusted/untrusted
security domain split inside the DCP firmware itself**: `dlon#2` is the
general-purpose "untrusted" RTKit OS that hosts AFK/EPIC and most driver
logic, `dlon#3` is a small "trusted" task holding the panel-identity/HDCP/DSI
-critical code, and the two talk to each other over Tightbeam, not AFK. This
is inferred from string placement, not from disassembling the IPC — flagged
as an interpretation, not a certainty.

## The AP-facing AFK/EPIC transport, confirmed in this firmware

The firmware's own class is `AFKEPInterfaceV2` (`dcpfw+0x497fef`,
`AFKEPInterfaceV2.cpp`), with log strings that map directly onto
`docs/re/afk-epic-references.md`'s framing model:

```
fc4284  [AFK][AFKEPInterfaceV2:%p]handleMessage (pktLen %zu pktCat 0x%x pktType 0x%x internal:%i)
fc4391  [AFK][AFKEPInterfaceV2:%p] Enqueue Command (packetType 0x%x commandID 0x%x) state %#x
fc446a  [AFK][AFKEPInterfaceV2:%p]Enqueue Response (pktType 0x%x commandID 0x%x result 0x%x) state %#x
fc44c9  [AFK][AFKEPInterfaceV2] Send error Response (pktType 0x%x, commandID 0x%x)
fc42de  [AFK][AFKEPInterfaceV2] sendOpenReport:0x%x
fc430a  [AFK][AFKEPInterfaceV2] sendPublishReport:0x%x
fc4339  [AFK][AFKEPInterfaceV2] sendTerminate:0x%x
fc4364  [AFK][AFKEPInterfaceV2] sendCloseReport:0x%x
```

`pktCat`/`pktType` are this firmware's names for packet category/type;
`commandID` is the doc's `epic_cmd` group/command RPC tag. The older Linux
AFK reference calls service announcement `REPORT/ANNOUNCE` (`0x30`), but that
is not iOS 27's wire value: the live-kernelcache receiver analysis recorded in
`qemu-sptm/hw/arm/darwin_epic.c` identifies iOS 27 `REPORT/PUBLISH` as `0x11`.
Thus `sendPublishReport` → `REPORT/PUBLISH 0x11` is the leading name-based
mapping, not a proven decode of this DCP callsite.
Disassembly of the larger task (`/tmp/dvm/gate232-firmware/machos/dcpfw.macho`)
ties all four log strings to distinct gated code paths: `__OS_LOG+0x2de`
(open), `+0x30a` (publish), `+0x339` (terminate), and `+0x364` (close), in
code around `0x4046e2c`–`0x4047034`. Each path makes an indirect call and logs
its result. In the callback at `0x4046e2c`, the open and publish paths test
bits 1 and 2 of the state halfword at `self+0x18`, then dispatch through vtable
slots `+0x20` and `+0x58`; the close path tests bit 6 and uses slot `+0x28`.
The terminate path tests bit 5 and calls helper `0x40470a4`. These are useful
breakpoint locations. The packed `__DATA_CONST` table is resolved below, but
these callsites alone do not reveal packet construction. That confirms
separate firmware operations, but not their wire-level packet subtypes or
payloads. The Linux AFK sources only document
`ANNOUNCE`/`TEARDOWN`/`STD_SERVICE`, so keep those numeric subtype questions
open below.

### Report-dispatch follow-up (2026-09-26)

Further static tracing places the callback at `0x4046e2c` behind event/workloop
registration, rather than showing it as a packet serializer. The same callback
address is passed at `0x4046b98` and `0x404e080` through an indirect slot
`+0x58` on the event-source object, together with a context pointer. When the
callback runs, its open/publish/close results come from indirect methods on
its object at vtable slots `+0x20`, `+0x58`, and `+0x28`; the terminate
result comes from `0x40470a4`. The OS_LOG calls record those result values.
For each operation, a zero return takes the local state-completion path
(clearing its pending bit, with operation-specific state updates); a nonzero
return goes to the result logger when that log category is enabled. This
looks like status handling around delegated operations, not evidence of a
particular packet ID.
This narrows where the send behavior is delegated. The segment-map correction
below resolves the concrete slot targets, while their bytes-on-wire
construction remains unknown.

The `0x404e080` registration site is inside the routine at `0x404dfd4`. It
treats the first qword of its input's 16-byte field at `+0x20` as `object`,
loads an event-source object from `object+0x68`, builds a registration record,
and invokes that source's `+0x58` method with callback `0x4046e2c` and context
`object+0x58`. Next, helper `0x439ff3c` compares the byte at local `+0x18`
with 1; this is the low byte of the second qword copied from input `+0x20`
(input `+0x28`). Only when it equals 1 does the routine test `object+0x70`
against mask `0x108`. If either masked bit is set, it calls `object`'s
`+0x140` method with argument 1, places byte value `6` in a local work-item
state, and passes that state with `object+0x58` to `0x4046cb8`. That helper
packages callback `0x4046f6c` and dispatches through an event object's `+0x48`
method. The raw gates, virtual methods, and state value are established by
the disassembly; their semantic names and the relationship between this
queued work item and a wire-level report are not.
This is a candidate open/publish setup path, not proof that it ran for the
internal panel. The firmware task is not executed by the current QEMU model,
and no runtime trace of these DCP-side conditions is available from the AP
guest.

The matching AP-side kext does not resolve that gap by name alone. Its
`AFKEPInterfaceKextV2::handleOpen(IOService *, IOOptionBits, void *)` and
`handleClose(IOService *, IOOptionBits)` routines are IOKit service lifecycle
callbacks: they operate on the supplied `IOService` and update local state.
They are not evidence that DCP's `sendOpenReport`/`sendCloseReport` names are
wire packet names. A separate AP callback whose log label is `handleReport`
compares its third argument with `0xc1`, but its callers load that value from
an event-context object at `+0x48`; the function does not parse an EPIC header
before the comparison. It therefore cannot be used to equate `0xc1` with an
EPIC subtype (or with the RTKit `RBEP_SHUTDOWN_ACK`) without tracing the
context producer. The AP kext passes its `P2SPacketTypes`/`S2PPacketTypes`
strings alongside array fields to a virtual helper; these names do not provide
a recovered numeric packet table. Its `sendOpenReport` and
`sendPublishReport` callsites (`0xfffffff008b901d0` and
`0xfffffff008b90224`) also dispatch through the object's vtable and log the
returned status; they are wrappers, not direct evidence of the DCP firmware's
wire serializer.

The strongest current conclusion remains: the firmware has distinct,
state-gated open/publish/terminate/close dispatch paths, but the wire subtype
and payload are still unresolved. A raw BUND segment-map correction removes
the earlier claim that the larger task's `__DATA_CONST` is absent. The
reconstructed Mach-O at `/tmp/dvm/gate232-firmware/machos/dcpfw.macho` has
zeros at fileoff `0x952000` because that local reconstruction omitted this
packed segment; a working copy with the recovered `__DATA_CONST` and
`__OS_LOG` bytes is
`/tmp/dvm/gate232-firmware/machos/dcpfw-task2-recovered-segments.macho`.
The former raw offset `dcpfw+0xb11000` was also wrong and falls inside the
smaller task's `__DATA`. The larger task's `__DATA_CONST` starts at
`dcpfw+0xf31000`. Its `_rtk_mtab` (`0x49f1000..0x49f16b0`) has
208 authenticated function pointers whose decoded targets fall in the
larger task's `__TEXT`, plus two negative offset-to-top values and four zero
slots. This agrees with code that installs vptr `0x49f11e0` at `0x4027b28`.
The report callback's indirect loads at `0x4046e94`, `0x4046ef4`, and
`0x4047000` now resolve through that vptr: slots `+0x20`, `+0x58`, and
`+0x28` target `0x40fc76c`, `0x41036a8`, and `0x40fc5e0`, respectively.
The dispatch loop at `0x40096d4` also indexes a populated table in this
segment. Thus the bundle contains the static method targets; a loader trace
is no longer needed just to recover them. A post-load snapshot could still
confirm the final in-memory values and actual dispatch. QEMU in this setup
does not execute the DCP RTKit task, so its AP-side LLDB hooks cannot provide
that runtime confirmation. `sendPublishReport` still most likely maps to
iOS 27 `REPORT/PUBLISH 0x11` based on the AP receiver and matching name, but
its encoded payload remains unproven.

One level below the report callback, its `+0x58` target at `0x41036a8` is a
thunk: it loads the child object from `self+0x428` and tail-branches through
that object's vtable slot `+0x900`. The setup path at `0x41040d4` fills
`self+0x428` from an interface query at `0x40f76dc`, passing the descriptor
at `0x49281c0`. That 16-byte `_afk_sys_objt` record points to the string
`DCPAVService`; the next record at `0x49281d0` names
`DCPAVServiceEPClient`. Nearby records name `DCPAVInterface`,
`DCPAVVideoInterface`, `DCPAVVideoInterfaceEPClient`,
`DCPAVAbstractVideoInterface`, and `DCPAVSimpleVideoInterface`.

The query hierarchy is partly visible. `0x40d8ef8` matches the
`DCPAVService` descriptor and returns the object on a match; otherwise it
delegates to `0x41c9284`, which recognizes `AFKService` and then falls back to
the `AFKObject` query. The adjacent type-query methods at `0x410411c` and
`0x4104164` recognize/return `DCPAVSimpleVideoInterface` alongside its video
interface bases. This confirms the service/interface layering around the
wrapper, but does not prove that `0x40d8ef8` is the runtime `+0x18` target
for the particular object passed at `0x41040d4`.

There is a concrete internal-MIPI candidate for that provider object. The
vtable at `0x49f7510` has its `+0x18` query method at `0x4144704`; that method
accepts `AppleDCPMIPIPanel` and `AppleDCPMIPICitra` descriptors, then delegates
other requests to the `DCPAVService` matcher at `0x40d8ef8`. Its vtable slot
`+0x900` resolves to `0x40c31ac`. That routine behaves like a state predicate:
it requires state `3`, calls another virtual predicate at `+0x208`, and then
returns bit 0 from object offset `+0x2b9`. If the `x1` supplied to
`0x41040d4` is this MIPI object, the nested query returns that object and the
report thunk's `+0x900` dispatch target is statically known. The boot-time
argument identity is not established, and other DCPAVService-capable classes
have their own vtables (some share this `+0x900` target, some do not).

The candidate predicate's gate inputs now have a static path through the same
MIPI class table. Its `+0x78` entry targets `0x40d24b8`, called on the startup
receiver at `0x417c304`; that method seeds `self+0x40d` to 1, then applies the
global `0x497d0c8` override: zero clears the field, one sets it, and other
values preserve the seed. The global's default is 2, written by config setup
`0x40c80bc`; its only property override is the booleanized value of
`hdmi21_features_enabled` at `0x40c85f0`–`0x40c8604`. Thus the missing-property
default leaves `self+0x40d` enabled. If the third argument's byte at `+0xf0`
does not already have bit 0 set, the method first runs that config setup and
sets the bit. The table's `+0x208` entry (`0x40ce9ec`) reads `self+0x40d`
directly. Its `+0x188` entry targets message-dispatch method `0x40da14c`; the
branch at `0x40da994` passes the same receiver and packet to `0x40dd490`. That
handler passes its receiver through to positional-record decoder `0x40e4838`
at `0x40de76c`, with the record body at `x28+4` and the record size in `w24`
(`(record[0] & 0x1f) - 3` on this path). The dispatcher reaches this call
only after matching record bytes 1–3 to `d8 5d c4` (`0x40de2e4`–`0x40de2f8`,
loaded at `0x40ddaf8`, `0x40ddafc`, and `0x40ddb04`); the record-header kind has
already selected the surrounding handler branch. Thus the candidate flag is
not set by an arbitrary sufficiently long record: the runtime signature to
capture is this prefix plus a body of at least five bytes.
The decoder reads its body byte 4 (`record[8]`, since the body starts at
`record+4`) only after its size checks: sizes below four return immediately,
size four takes the common-field path and returns before that byte, and sizes
of at least five load it as signed and store `(body[4] >> 1) & 1` to receiver
offset `+0x2b9` (`0x40e53a0`–`0x40e53a4`). A separate fixed
four-byte call at `0x40def40` does not set this bit. This message route can
set the predicate bit only when the three-byte prefix matches and
`record[0] & 0x1f` is at least eight. The
static path maps `+0x40d` to the named `hdmi21_features_enabled` option and
`+0x2b9` to a specific record bit, but the latter's semantic name remains
unknown. It still does not prove these methods ran for the stalled boot or
that the report callback's live `self+0x428` was this particular MIPI
instance; the QEMU setup does not execute the DCP RTKit task.

#### Runtime capture recipe

The matching record must be captured inside `dlon#2`, not inferred from the
AP-facing AFK log. In the task's `0x04000000` VM map (adjust for any runtime
slide), stop at `0x40dd490` and save the receiver (`x0`), packet pointer
(`x1`), and packet bytes. At `0x40de2e4`, `x28` is the current record and
`w22`/`w20`/`w19` hold bytes 1/2/3; the matching values are `d8`/`5d`/`c4`.
At `0x40de76c`, capture the decoder arguments: `x0` receiver, `x1 = x28+4`
body, and `w2` body length. If the decoder reaches `0x40e53a4`, capture
`w25` (signed `body[4]`), `w8` (the extracted bit), and `x19+0x2b9` before the
store. A hit there would confirm both the message signature and the state
update; a hit only at the dispatch entry would show that this dispatch did not
take the candidate prefix-matched parse route.

This cannot be collected by the current QEMU DCP model: its AFK callback sees
AP-to-DCP frames, while its DCP-to-AP traffic is model-generated PUBLISH and
opt-in synthetic replies; it does not execute either recovered RTKit task.
Searching the available `/tmp/dvm` probe logs found no `d8 5d c4` record, which
is expected for that model. A valid capture therefore requires a runtime that
actually executes `dlon#2` and can expose its memory/registers.

The AP-to-DCP origin assumed by a proposed AFK-mailbox hook is not established.
Static disassembly shows `0x40da14c` obtains its candidate packet from the
object-owned byte buffer at `self+0x60` (`0x40da538`), reads 0x80-byte slices
from that buffer (`0x40da574`–`0x40da580`), and dispatches type-2 slices to
`0x40dd490` (`0x40da804`–`0x40da814`). The buffer's length and data pointer
are at `buffer+0x10` and `buffer+0x18`; this identifies the immediate parser
input, but not the producer that fills the buffer or whether it is fed by
AFK, an internal DCP channel, or a hardware-facing callback. GATE226 and the
newer natural GATE245 boot both show no AP command on interface 12 after the
MIPI proxy opens (the observed interface-12 traffic is PUBLISH/OPEN only), so
those runs do not support the claim that an AP command supplies these records.
A raw AP-side AFK trace would still be a useful negative/correlation check,
but this interface correlation does not prove that the decoder's receiver is
the object behind interface 12, and it cannot replace tracing the buffer's
writer. First resolve the producer or callback that writes `self+0x60`, then
instrument that boundary in a runtime that executes the task.

The static backtrace has narrowed the missing writer. In the candidate
implementation range, the initializer at `0x40d83ec` calls `0x41c8a54` and
initializes other members (including `self+0x68`), but does not assign
`self+0x60`; the only direct stores to that field in the `0x40d8...`–
`0x40da...` methods inspected so far are zeroing stores in cleanup paths
`0x40d8d48` and `0x40d9ec0`. In the known `0x49f7510` table, slot `+0x180`
targets `0x40d9e9c`, which releases the current `self+0x60` object and clears
the field at `0x40d9eb0`–`0x40d9ec0`; the `+0x188` dispatcher `0x40da14c`
calls that slot at `0x40da1a8`, then reads `self+0x60` at `0x40da524` and
`0x40da538`. Six other `__DATA_CONST` copies contain the same adjacent
cleanup/dispatcher pair. If the live receiver uses one of these tables, this
dispatcher cannot consume a buffer that was present before its `+0x180` call;
it would need to be repopulated afterward. The copies strengthen the shared
method-path identification, but they do not establish the boot-time receiver
identity or reveal such a repopulation path. The next static/runtime boundary
to resolve is therefore any write or callback between the cleanup call and
the `self+0x60` read. The in-line candidate at `0x40da1e4` is the property
registration path `0x40d8f10` → `0x40d8f80`; it adds the named object to maps at
`self+0x3a8`/`self+0x3b0` and calls virtual slot `+0xc0` (target `0x41c6e00`
in the known table), but none of those inspected bodies directly stores to
`self+0x60`. A conditional call through slot `+0x620` targets
`0x40d9570` (`copyEDID`); it also has no direct `+0x60` store. These calls
remain possible indirect side-effect boundaries, not identified writers. A
write watchpoint on `self+0x60`, paired with breakpoints at `0x40d9e9c` and
`0x40da524`, is the decisive next measurement in a runtime that executes
`dlon#2`.

The report callback object's own table further rules out a tempting slot
association. Its installed vptr is `0x49f11e0`; the authenticated entries at
`+0x58`, `+0xe0`, and `+0xe8` decode to `0x41036a8`, `0x41034a0`, and
`0x4103478`. The `+0xe0` thunk at `0x41034a0` loads the child at
`self+0x428` and dispatches through that child's `+0x5a8` slot. It is not the
provider-setup method `0x41040d4`. This is negative ownership evidence for
this particular callback object; it does not identify the receiver table
needed by the separate `0x411e370` adapter.

### Provider-argument follow-up (2026-09-26–27)

The setup method itself is now more precisely identified. A signed
function-pointer array begins at `dcpfw+0xf30e78`: slot `+0x18` points to
`0x410411c`, which recognizes `DCPAVSimpleVideoInterface` plus its video
interface bases; slot `+0x10` points to `0x4104164`, which returns the
`DCPAVSimpleVideoInterface` descriptor. Slot `+0xe0` is the authenticated
pointer at `dcpfw+0xf30f58`, targeting `0x41040d4`. The adjacent entries make
this resemble a SimpleVideo-interface method table at raw-byte level. There is
a segment-boundary caveat: the array bytes map to the smaller task's
`__DATA_CONST`, while these function targets lie in the larger task's
`__TEXT`, so this alone does not prove the table is installed as a runtime
vptr. There is also an address-ownership mismatch in the smaller task itself:
its `__text` ends at `0x40c2140`, while all three table targets
(`0x4104164`, `0x410411c`, and `0x41040d4`) fall in that task's `__DATA` heap
reservation. The reconstructed image has zero bytes at their file offsets
`0x105164`, `0x10511c`, and `0x1050d4`; those same addresses contain executable
code in the larger task. Unless a cross-image mapping explains that overlap,
the smaller-task array cannot establish a live vptr. At the same VM address
(`0x4527e78`) in the larger-task image's `__DATA`, the bytes are also zero; the
method array is present only in the smaller-task image. A scan of that image's
`__TEXT` found no ADRP/ADD reference to `0x4527e78` or its `+0xe0` slot, and its
chained-fixup list has no pointer target to the table's image-relative offset
`0x127e78`. This leaves the candidate table's use unresolved even within the
smaller image. In that image's load commands, the table also falls in the
unnamed tail of `__DATA_CONST`: `__const` ends at `0x4527248`,
`__mod_init_func` ends at `0x45272a0`, and the table starts at `0x4527e78`.
Startup's pointer-initialization helper at `0x4005ef4` is called for
`[0x4527248, 0x45272a0)`, which does not cover the table. At entry,
`0x41040d4` preserves `self` in `x19`, moves its incoming
`x1` to `x0`, and calls `0x40f76dc`; that helper queries the `x1` object for
`DCPAVService`. The returned interface is stored at `self+0x428`, then
initialization continues through `0x40f96ac`. Thus the argument in question
is a provider object whose service interface is being selected, not the
geometry or display-list state itself.

There is a plausible generic caller for this setup slot. The adapter at
`0x411e370` loads a provider pointer from `*[x2]` into `x19`, loads its
receiver/client object from `[x0+8]` into `x20`, and calls `0x40f76dc` on the
provider. If that `DCPAVService` query succeeds, it invokes the receiver's
virtual slot `+0xe0` with `x0=x20` and `x1=x19`. Therefore, if that receiver has
the candidate method array above installed, this adapter calls
`0x41040d4` and passes the provider from `*[x2]` as the setup argument. The
nearby adapter at `0x411e268` uses the same provider check but dispatches slot
`+0xe8`. Among direct `BL 0x40f76dc` callers, this is the only successful path
found that subsequently dispatches slot `+0xe0` with the original provider in
`x1`. This is a concrete static candidate for the upstream dispatch path,
though the receiver's runtime vptr and the adapter's actual boot invocation
are not established by the image. The authenticated code pointers are present
in the larger task's data-const pool at `dcpfw+0xf337a0` (`0x411e370`) and
`dcpfw+0xf337e8` (`0x411e268`). A four-byte-aligned scan of both task
`__TEXT` sections found no direct branch to either adapter target. It did find
a use of the `+0xe8` cell:
the type-`0xb` arm of `0x403e3c4` forms address `0x49f37e8` at `0x403e438`
and stores that cell address in an object at `+0x60`. Later readers use that
stored base with offsets `+8` or `+0x10`, so this ties the cell to a constructed
dispatch record but does not show the adapter itself runs. The bytes starting
at `0x49f37e8` associate this `+0xe8` record with SAC remote methods: its first
entry is `0x411e268`, followed by a `0x4129764` handler, a `0x20` size, and a
pointer to `ClearEventLog`; later records pair `0x411e790` with
`SelectSACAggressorIndex` and `0x411e738` with `BootComplete`. The initializer
at `0x411e01c` independently registers `dcpav-sac-epic` and `dcpav-sac-ep`
through `0x4129f4c`. A generic reader at `0x403e8b8` can load the `+8` handler
from the stored table and invoke it. This makes the `+0xe8` dispatch record
SAC-related, but does not connect it to the SimpleVideo setup method at
`0x41040d4`. The object allocated by that SAC initializer is assigned vptr
address point `0x4a048c0`; its own `+0xe0` and `+0xe8` slots resolve to
`0x40f5580` and `0x40f1c24`, respectively. That particular object is not the
receiver represented by the candidate SimpleVideo table. The analogous
`+0xe0` adapter cell at `0x49f37a0` is also entry `0x1a` in
the table based at `0x49f36d0`; the only direct callers of the indexed helper
at `0x4034390` pass indices `1` and `2`. No source of index `0x1a` was found.
The earlier apparent copy of these pointers in the smaller task's
`APPCONFIG` was a reconstruction error: `dcpfw-task3.macho`'s bytes at its
`APPCONFIG` file offset `0x513000` exactly duplicate the first `0x2c70` bytes
of the larger task's `__DATA_CONST` from `dcpfw+0xf31000`. The original bundle
has distinct `0x3000`-byte APPCONFIG blocks at `dcpfw+0xf88000` and
`dcpfw+0xf8b000`. Their headers carry version `0x1d` and payload length
`0x2c70`; the smaller task checks the version at `0x4009054` and reports the
payload length from `+0xc` on its mismatch path at `0x40090d4`. The low word
at `+0x1320` resolves to the corresponding `__extra` address: the first block
maps to the larger task's `__extra` at `0x4a50c70`, and the second to the
smaller task's `__extra` at `0x452ac70`. The smaller task's real APPCONFIG
block is therefore
the one at `dcpfw+0xf8b000`; its entries at `+0x27a0` and `+0x27e8` are zero,
not the adapter pointers. A corrected analysis copy is
`/tmp/dvm/gate232-firmware/machos/dcpfw-task3-appconfig-corrected.macho`.
The larger-task adapter table's consumer is still unresolved beyond that
`+0xe8` registration site. The bundle contains no pointer-valued data word
targeting its `0x49f3680`–`0x49f37ff` range. The separate candidate method
array at `0x4527e78` in the smaller task's `__DATA_CONST` is also unattached:
an aligned scan found references to the `0x4527000` page at other offsets, but
none to `+0xe78` or its `+0xf58` slot, and no bundle data pointer targets
either address. The receiver's candidate method array is absent from
the larger-task image at its mapped VM address. So this adapter is a promising
call-shape match, but its link to `0x41040d4` is weaker than the matching slot
alone suggests. For a DCP-side trace, break at `0x411e370` and capture `*[x2]`,
`[x0+8]`, and the receiver's `+0xe0` target; that would confirm or rule out
this route before inspecting `0x41040d4`.

The SimpleVideo creation log adds a constraint. The string
`"creating SimpleVideoInterface (0x%llx)"` at `0x4474ebd` is referenced from
`0x40cec1c`, a factory that selects between `0x408`- and `0x430`-byte
allocations based on a virtual predicate at `+0x130`. The logging branch for
the `0x430` allocation uses that SimpleVideo string; the `0x408` branch uses
the SliceVideo string at `0x4474ef8`. Although `0x430` is large enough for the
field at `self+0x428`, this factory installs vptr `0x4a01e78`, whose `+0xe0`
entry at `0x4a01f58` resolves to `0x4128d14`, not `0x41040d4`. So this
particular SimpleVideo allocation does not identify the receiver in
`0x411e370`; the matching object size is only a clue, and the candidate
cross-task table remains unresolved.

`DCPAVService` is a shared capability, not a unique MIPI identity. At least
fourteen concrete query wrappers in the larger task fall back to the common
`DCPAVService` matcher. They cover `AppleDCPMIPIPanel`/`AppleDCPMIPICitra`,
external and Agile DP displays, several DP service variants, and DP adapter
classes such as `AppleDCPPS190` and `AppleDCPPS186`. The MIPI object remains the
strongest candidate for this iPhone's built-in panel path, but the successful
`DCPAVService` query by itself cannot distinguish it from those other
providers. The static image does not show which one is passed as `x1` to the
SimpleVideo setup method at boot.

The decisive runtime observation is now narrowly scoped: at entry to
`0x41040d4`, capture `x1` and its vptr; in `0x40f76dc`, record the concrete
`+0x18` query target and returned pointer; then record the stored object's
vptr and its `+0x900` target. That distinguishes the provider class and the
actual callback implementation in one run. This remains a DCP-task-side
observation; AP-side QEMU/LLDB cannot supply it in the current setup because
that configuration does not execute the DCP RTKit task.

Even with this array, the returned object's concrete class and the observed
`+0x900` implementation remain unresolved. A raw scan found no aligned
function-pointer entry for `0x40d8ef8`; the only aligned raw-bundle pointer to
`0x41040d4` is at `dcpfw+0xf30f58`, in the smaller task's data-const pool, so
it does not establish the larger task's runtime vptr.

The ring transport itself is named `iop_ringbuffer.h` (`dcpfw+0x497f01`),
consistent with the AFK doc's `QE_MAGIC` = `"IOP "`. Generic AFK object
classes present and matching the doc's model one-for-one: `AFKDictionary`,
`AFKArray`, `AFKNumber`, `AFKString`, `AFKSerialize`, `AFKObject`,
`AFKNotification`, `AFKMemoryDescriptor`/`AFKBufferMemoryDescriptor`,
`AFKMailboxEventSource`, `AFKEventSource`, `AFKWorkloop` — all as `.cpp` file
strings in the `0x4b4xxx`-`0x4b5xxx` region.

`IOMobileFramebuffer_RemoteCalls.cpp` (`dcpfw+0x4c0dc3`) itself logs
`"IOMFB: failed to serialize AFKArray"` (`dcpfw+0x4c0d9e`) — i.e. **some**
IOMFB-adjacent traffic (at minimum the `IOMFBAOPDisplayManagerEPICClient`
path to the AOP coprocessor, see below) is carried as AFK-serialized objects,
even though the core endpoint `0x37` link itself is not AFK-framed (per
`docs/re/afk-epic-references.md`'s exclusion, which this investigation did
not overturn — see "Endpoint 0x37" below).

## EPIC service table

### Confirmed both sides: firmware announces the name, a real kext matches on it

This is the authoritative list: every row is a literal `EPICName` string
found **both** in `dcpfw`'s `-epic`-suffixed string set and as an
`IOPropertyMatch = {"EPICName": ...}` key inside a real `IOKitPersonalities`
entry in the kernelcache `__PRELINK_INFO` — i.e. XNU really will try to bind
this class if our model announces this name.

| EPICName | firmware evidence | AP-side IOClass | kext bundle |
|---|---|---|---|
| `dcpav-controller-epic` | `dcpfw+0x3ea572`, `+0x59abda` | `DCPAVControllerProxy` | `com.apple.driver.DCPAVFamilyProxy` |
| `dcpav-device-epic` | `dcpfw+0x3ea5b9`, `+0x59ac2a` | `DCPAVDeviceProxy` | `com.apple.driver.DCPAVFamilyProxy` |
| `dcpav-service-epic` | `dcpfw+0x3ea7ae`, `+0x59ae2f` | `DCPAVServiceProxy` | `com.apple.driver.DCPAVFamilyProxy` |
| `dcpav-video-interface-epic` | `dcpfw+0x3f6f61`, `+0x59bb21` | `DCPAVVideoInterfaceProxy` | `com.apple.driver.DCPAVFamilyProxy` |
| `dcpav-power-epic` | `dcpfw+0x3ebfee`, `+0x59c79b` | `DCPAVPowerControllerProxy` | `com.apple.driver.DCPAVFamilyProxy` |
| `dcpav-audio-interface-epic` | `dcpfw+0x3eb492`, `+0x59bbbd` | `DCPAVAudioInterfaceProxy` | `com.apple.driver.DCPAVFamilyProxy` |
| `dcpav-cec-interface-epic` | `dcpfw+0x3eb4be` | `DCPAVCECInterfaceProxy` | `com.apple.driver.DCPAVFamilyProxy` |
| `dcpav-sac-epic` | `dcpfw+0x3ebfdf`, `+0x59c77f` | `DCPAVRemoteSACControllerProxy` | `com.apple.driver.DCPAVFamilyProxy` |
| `dcpdp-controller-epic` | `dcpfw+0x3f7a98` | `DCPDPControllerProxy` | `com.apple.driver.DCPDPFamilyProxy` |
| `dcpdp-device-epic` | `dcpfw+0x3f7b54` | `DCPDPDeviceProxy` | `com.apple.driver.DCPDPFamilyProxy` |
| `dcpdp-service-epic` | `dcpfw+0x3f7eb9` | `DCPDPServiceProxy` | `com.apple.driver.DCPDPFamilyProxy` |
| `dcpdptx-port-epic` | `dcpfw+0x3e9669` | `AppleDCPDPTXRemotePortProxy` | `com.apple.driver.AppleDCPDPTXProxy` |
| `dcp-lpdptx-port-epic` | `dcpfw+0x3e95dc` | `AppleDCPLPDPTXPortProxy` | `com.apple.driver.AppleDCPDPTXProxy` |
| `dcpdptx-hdcp-interface` | `dcpfw+0x3e99d0` | `AppleDCPDPTXRemoteHDCPInterfaceProxy` | `com.apple.driver.AppleDCPDPTXProxy` |
| `dcpdptx-hdcp-auth-session` | `dcpfw+0x3e9b42` | `AppleDCPDPTXRemoteHDCPAuthSessionProxy` | `com.apple.driver.AppleDCPDPTXProxy` |
| `dcpmipi-controller-epic` | `dcpfw+0x3f707f`, `+0x5a819f` | `DCPMIPIControllerProxy` | `com.apple.iokit.IOMIPIFamily` |

`dcpmipi-controller-epic` was **not** in the task's candidate list and has no
counterpart in either of `docs/re/afk-epic-references.md`'s M1-era sources —
this is the one new confirmed-both-sides service this investigation found.
Method: `python3 -c` script over `plistlib.load(open('prelink/info.plist','rb'))`,
walking every `_PrelinkInfoDictionary` entry's `IOKitPersonalities` for
`IOPropertyMatch` containing `EPICName` or `EPICProviderClass` — 16 hits
total, all listed above.

### Firmware announces the name; no matching `IOPropertyMatch(EPICName=...)` found in this kernelcache

These are real `-epic`-suffixed strings in `dcpfw`, at the same string-table
locality as the confirmed set above (immediately adjacent to
`EPICName`/`EPICProviderClass`/`EPICUnit`/`EPICLocation` property-key
strings, `dcpfw+0x3f6ef6`-`0x3f6f15` and `0x5a7689`-`0x5a76a8`), but this
kernelcache's `IOKitPersonalities` has no entry whose `IOPropertyMatch`
mentions them. Either XNU matches these some other way (raw 32-byte name,
`IONameMatch`, or a class this project didn't grep for), or they are
internal-only channels never exposed past `AFKEndpointInterface` to a
concrete IOKit driver.

| name | firmware evidence | notes |
|---|---|---|
| `dcpexpert-epic` | `dcpfw+0x3e7d60` | `DCPExpertEPClient.cpp` (`+0x4b3d6a`) implements it — see "dcpexpert-epic" below. `AppleDCPExpert` (the only kext-side class with "expert" in its name) binds by `IONameMatch dcp-expert-v1` on `AppleARMIODevice`, **not** by `EPICName` — plausibly opens this channel itself, internally, once already instantiated, rather than through a separate `IOKitPersonalities` match. |
| `disp0-epic` | `dcpfw+0x3e7928` | Paired DT node names nearby: `dispext-service` (external variant), `power-gate-dbe-disp0`/`-dispext0`. `AppleCLCD2`'s `IONameMatch` is `['disp0,t8140', 'dispext0,t8140']` (device-tree name, unrelated match path) — no `EPICName` match for `disp0-epic` found. |
| `system-epic` | `dcpfw+0x3e77e7` | Raw name `"system"` sits immediately before it (`+0x3e77b0`) with property-key-looking neighbors `AFKDiagnostics`/`HeapUsage`/`ThreadInfo`/`compartment` — matches Linux's `systemep.c` `"system"` service closely; likely the RTKit-standard first-endpoint diagnostic/setProperty channel the AFK doc describes, just not matched via `EPICName` on this kernelcache (Linux's own driver doesn't match this one by `EPICName` either — see the AFK doc's §2.2 note that Linux prefers `EPICProviderClass`). |
| `test-epic` | `dcpfw+0x3e7800` | Paired with `test-service`/`test-ep`. `AFKEchoTestEPIC` (`com.apple.driver.AppleFirmwareKit`) exists but matches `IONameMatch=['ap_echo-test']`, a different string — probably a different, generic AFK self-test channel, not this one. |
| `static-epic` | `dcpfw+0x4a9caf`, `+0x5b1ee6` | Sits inside `DCPAVEPClient.cpp`'s string cluster, next to `dcpav-epic-response`, `dispext`/`disp` unit-selector strings, and an `ASSERT!%s:%d getResponseQueue`. Purpose unclear — possibly a fallback/default `EPICName` `DCPAVEPClient` uses before a real per-instance name is assigned, not a real standalone service. |
| `powerlog-epic` | `dcpfw+0x3e7eae` | Paired with `powerlog-service` and `afksmc-service`. Matches Linux's `powerlog-service` almost exactly by name (`docs/re/afk-epic-references.md`'s exclusion list already named this one from the Linux source). |
| `dcpav-audio-arc-epic` | `dcpfw+0x3f882c` | Paired with short name `dcpav-arc-ep`; near HDMI-ARC-sounding neighbors (`DPRXEQ`, `cHDMIb`, `FilterThresh`) — HDMI eARC audio return channel, external-display-only. |
| `dcpav-epic-response` | `dcpfw+0x4a9d03`, `+0x5b1f3a` | Sits right next to `DCPAVEPClient.cpp`'s `getResponseQueue` assert — this is very likely a queue/object *name* used internally by `DCPAVEPClient` (e.g. the name of its response-tracking dictionary), not a channel `EPICName` at all. Flagged as probably-not-a-service. |
| `aop-test-control-epic` | `dcpfw+0x3e7ee3` | Neighbors `aop-route-embedded`/`aop-test` — AOP (audio coprocessor) test/diagnostic control, cross-coprocessor AFK usage (see `common_afk.cpp` notes below). Not display-critical. |
| `cb-ap-to-dcp-service-epic` | `dcpfw+0x3e7a7d` | Paired short name `cb-ap-to-dcp-service-ep`/raw node `cb-ap-to-dcp-service-nub`. `CBAPServiceEPClientiOS.cpp`/`CBAPToDCPService.cpp` implement it — see "CB subsystem" below. |
| `cbrootservice-epic` | `dcpfw+0x3e79a7` | Paired with `cbroot-service`/`cbauto-service`/`cbcolor-service`/`cbxtalk-service` (ambient-light/color-balance sub-nodes). `CBRootServiceiOS.cpp` implements it. |

### Not AFK/EPIC at all: `iomfb-link`

A tightly-packed, NUL-separated string table at `dcpfw+0x3e7f98`–`0x3e8158`
(26 entries, immediately followed by unrelated boot data `"RTKSTACK"`/
`"Power"`/a base36 charset table) lists short endpoint/channel names in this
exact order:

```
dcpexpert-tb-ep, dptx-monitor-tb-ep, mipi-controller-tb-ep, aon-hpd-tb-down-ep,
aon-hpd-tb-up-ep, dcptrustedclient-tb-ep, disp-health-mon-tb-ep,
management-ep, syslog-ep, kdebug-ep, tracekit-ep, oslog-ep, report-ep,
iomfb-link, system-ep, md-ep, test-ep, dcpexpert-ep, disp0-ep,
dcpav-controller-ep, dcpav-power-ep, dcpav-sac-ep, dcpav-device-ep,
dcpav-service-ep, dcpav-interface-ep, dcpdptx-port-ep, dcpdptx-hdcp-ep,
cbservice-ep, cb-ap-to-dcp-service-ep, aop-comms-ep
```

`iomfb-link` (`dcpfw+0x3e8063`) is this firmware's own literal name for the
endpoint XNU's kernelcache calls `DCPEndpoint24`/binds
`AppleDCPLinkServiceSoC` to (confirmed from `IOKitPersonalities`:
`com.apple.driver.AppleMobileDispH17P-DCP`'s `AppleDCPLinkServiceSoC`
personality, `IONameMatch = ['DCPEndpoint24', 'DCPEXTEndpoint24']`). This is
new, direct firmware-side confirmation that the name exists and that this
firmware groups it with the RTKit-standard endpoints
(`management`/`syslog`/`kdebug`/`tracekit`/`oslog`/`report`) rather than with
the `dcpav-*`/`dcpdptx-*` AFK sub-services that follow it in the same list —
consistent with `docs/re/afk-epic-references.md`'s exclusion of it from the
AFK/EPIC framing. **Caution**: this list's order is evidence only that these
26 names are declared together in some array in this order; it was not
disassembled, so it should not be read as proof of ascending numeric mailbox
endpoint order (RTKit standard endpoints are known to be low numbers `0x01`-
`0x0a` per `docs/re/dcp-bringup.md`, but `iomfb-link`/`DCPEndpoint24` is
known to be `0x37`, the *last* AFK-range endpoint — the two facts are not
reconcilable under a "list order = endpoint number order" reading, so that
reading is rejected here even though the adjacency itself is real).

## Command/argument surface found

No numeric group/command opcode table was recovered — this needs
disassembly with a resolved vmaddr↔fileoff mapping (see "Bundle layout"),
which this investigation could not build cheaply, and the shipped firmware
has no retained C++ symbol table to shortcut it with. What **was** found by
string mining, all inside `dlon#2`'s `__TEXT,__const`/`__cstring` in the
region `0x49dxxx`-`0x4bxxx` (duplicated in `dlon#3` around `0x5axxx`-
`0x5bxxx`): a set of Objective-C-style runtime type-encoding strings
(`"v64@?0{mipi_displaytiming_s=IIIIIIIIIIIIII}8"` etc.) that describe
**argument/result struct layouts** for a set of named Tightbeam RPC methods.
These are cross-domain (dlon#2 ↔ dlon#3) calls, not necessarily the AP-facing
EPIC wire format, but they are the best evidence available for what data
several `-epic` services actually carry, since the same class names
(`DCPExpertEPClient`, `AppleDCPSynopsysMIPIDSIController`) sit right next to
them:

| struct / method | type encoding | fields (as decoded) | evidence |
|---|---|---|---|
| `dcpexpert_setinterruptrouteresult_s` | `Q` | one `uint64` | `dcpfw+0x4b4400`; paired with `dcpexpert_dcpinterruptroute_s=Q` at `+0x5aa539` — `DCPExpertEPClient`'s interrupt-routing setup call, see below |
| `dcptrusted_interface_notifyrunmode__result_s` | `C(?=B)` | `char` status + block returning `bool` | `dcpfw+0x4b4463` |
| `dcptrusted_interface_notifydisplaypower__result_s` | `C(?=B)` | same shape | `dcpfw+0x4b44b5` |
| `dptx_dptxtberror_s` | `Q` | one `uint64` error code | `dcpfw+0x49de75` |
| `dptx_coverglassserialnumber_s` | `{dptx_dptxtberror_s=Q}{...lengthtype_s=Q}[44C]` | error + length + 44-char serial string | `dcpfw+0x49dee1` |
| `displayhealthmonitor_displayhealthstats_s` | 136 bytes: error(`Q`) + 10×`Q` + 5×`I` + 2×`Q` | health-stat counters | `dcpfw+0x4a9579` |
| `displayhealthmonitor_failsafestats_s` | 256 bytes: error(`Q`) + `[20I]` + `[20Q]` | 20-entry failsafe counter table | `dcpfw+0x4a9600` |
| `mipi_displaytiming_s` | `IIIIIIIIIIIIII` | 14×`uint32` — DSI mode timing (pixel clock/h/v active/porch/sync, exact field order not decoded) | `dcpfw+0x4a9d3c` |
| `mipi_rawpanelid_s` | `[15C]B` | 15-byte panel ID + 1-byte flag | `dcpfw+0x4a9d69` |
| `mipi_cgsn_s` | `[44C]B` | 44-char string + flag — plausibly a coverglass/chip serial number | `dcpfw+0x4a9d8b` |
| `mipi_extendedid_s` | `[25C]B` | 25-byte extended panel ID + flag | `dcpfw+0x4a9da7` |
| `mipi_sacaggressortableentries_s` | `BCC[9{mipi_agileclockingdata_s=IQ}]` | 9-entry table of `{uint32,uint64}`, "SAC" = spread-spectrum/aggressor coordination between the display clock and other RF-sensitive radios (inferred from neighboring strings `"eDP SAC frequency = %llu"`, `"SAC enable: DP=%u"`, `"failed allocate aggressor table index"` — not an official Apple expansion of the acronym) | `dcpfw+0x4a9dd2` |
| `mipitool_interface_{send,receive}{short,long}command__result_s` | `C(?=II)` / `C(?=I{...[256C]I})` | raw MIPI-DCS command pass-through result shapes | `dcpfw+0x4a9eec`-`0x4a9fe4` |

The last row is backed by concrete method signatures on the class that
actually owns the DSI link:

```
4aa71b IOReturn AppleDCPSynopsysMIPIDSIController::_sendSWMPRShortCommandGated(uint8_t, uint8_t, uint8_t)
4aa7ff IOReturn AppleDCPSynopsysMIPIDSIController::_sendShortCommandGated(uint8_t, uint8_t, uint8_t)
4aa8f5 IOReturn AppleDCPSynopsysMIPIDSIController::_sendSWMPRLongCommandGated(uint8_t, const uint8_t *, size_t)
4aa95e IOReturn AppleDCPSynopsysMIPIDSIController::_sendLongCommandGated(uint8_t, const uint8_t *, size_t)
4aa9f7 IOReturn AppleDCPSynopsysMIPIDSIController::_receiveShortCommandGated(uint8_t, uint8_t *)
4aab1f IOReturn AppleDCPSynopsysMIPIDSIController::_receiveLongCommandGated(uint8_t, uint8_t *, size_t *)
```

and confirmed to ride over AFK specifically (not just Tightbeam) by
`[AFK]`-tagged log lines using the same verb names:
`fdabbc [AFK]receiveShortCommand status=0x%x`, `fdac05 [AFK]sendShortCommand
status=0x%x`, etc. — i.e. **raw MIPI-DCS short/long command send/receive is
itself exposed as (part of) an EPIC service**, most plausibly
`dcpmipi-controller-epic` given `DCPMIPIControllerClient` (`dcpfw+0x4a9d17`)
sits in the same string cluster as these `mipitool_*` type encodings.
`DCPMIPIToolClient`/`MIPIToolInterfaceTBClient`/workloop name `mipitool-ep-wl`
(`dcpfw+0x599948`) suggest there may be a **separate, tooling-only** channel
for raw command passthrough distinct from the driver's normal operation —
not disambiguated here.

`DCPExpertEPClient`'s two log lines are the clearest evidence for what that
channel actually does day to day:

```
fea38e [AFK]DCPExpertEPClient::handleCommand: service=0x%llx, count=%zu
fea3ff [AFK]DCPExpertEPClient::handleCommand: service=0x%llx retrieving health stats
```

— combined with `dcpexpert_dcpinterruptroute_s`/`setinterruptrouteresult_s`
above, `dcpexpert-epic` is most likely used for **interrupt-route
configuration and health-stat retrieval** between AP and firmware, run after
the coprocessor is already up (this is consistent with
`docs/re/dcp0-expert.md`'s finding that `AppleDCPExpert` on the AP side
never gets far enough in this project's current emulation to reach any AFK
traffic at all — the single observed MMIO write is upstream of all of this).

## Endpoint 0x37 (`iomfb-link` / `DCPEndpoint24`)

`docs/re/afk-epic-references.md` explicitly excludes this endpoint as "a
distinct RPC-with-callbacks framing... layered directly on RTKit, not on
AFK/EPIC." This investigation did not overturn that, but did find more
detail from the firmware side:

- The literal name is `iomfb-link` (`dcpfw+0x3e8063` — see the endpoint-name
  table above), Apple's own name for what XNU calls `DCPEndpoint24`.
- The classic macOS-era public reverse-engineering convention for this
  protocol's RPC method IDs is 4-character tags like `A029`/`D001`
  (AP→firmware / firmware→AP). This firmware retains a handful of literal
  `[AD]\d{3}_callback__`-suffixed strings — `A119_callback__`,
  `A120_callback__`, `A122_callback__`, `A355_callback__`, `A374_callback__`,
  `A375_callback__`, `A441_callback__`, `A442_callback__`, `A444_callback__`,
  `A450_callback__` (`dcpfw+0x4c67ed`-`0x4c0e9d`, all inside `UPPipe`/
  `UnifiedPipeline`/pixel-pipeline `.cpp` regions) — but this is at most 10
  of what is publicly known (from macOS-era reverse engineering, not cited
  here as this project's own finding) to be a much larger table; most method
  dispatch in this firmware is evidently **not** done through fully-spelled
  literal strings, so a `strings` pass cannot recover the full table.
- `IOMobileFramebuffer`'s own class carries readable `virtual` method
  signatures at assert/log sites (not a full symbol table — see "Bundle
  layout"): `swap_submit_dcp(const IOMFBSwap *, ...)`,
  `swap_submit_dcp_main(...)`, `get_color_remap_mode(...)`,
  `hotPlug_notify(uint64_t)`, `getDebugInfoBufferJob(uint32_t*, uint32_t*)`,
  `init()`, `start_ios_gated()` (`dcpfw+0x4bab38`-`0x4bb819`). A firmware→AP
  notification path is confirmed by the literal string
  `batched_swap_complete_ap_gated` (`dcpfw+0x4c0d6e`, in
  `IOMobileFramebuffer_LocalCalls.cpp`) — the `_ap` suffix is this
  codebase's own convention for "goes to the AP", consistent with the AFK
  doc's direction-inference method (§1.1) but for a different transport.
  In the reconstructed task-2 Mach-O, that string resolves to VM
  `0x0448ed6e`. Its code reference at `0x04218ecc..0x04218ef4` is an
  over-256-entry diagnostic in the routine at `0x04218e38`. The normal
  branch copies a count and per-entry swap IDs/flags into a `0x2708`-byte
  input buffer, then builds FourCC `D595` (`0x44353935`) at
  `0x04218f8c..0x04218fa8` for the AP RPC. The current AP kext's D595
  handler (`0xfffffff00a0db1a8`) accepts that batch shape and calls
  `0xa0c5c08`, which iterates the IDs into the known full-retirement helper
  `0xa0c58bc`. This establishes **D595 as an additional batched swap
  completion path**, but not which AP request triggers it. The two visible
  registrations of the firmware routine (`0x04218cf4` and `0x0428b964`)
  do not themselves expose an A407/A408 discriminator. No A407-to-D595
  assignment follows from this static slice.
  The batch has a visible producer at `0x0420daf0..0x0420de34`: it tests
  an internal swap-object state at `+0x3ec`, branches through an additional
  eligibility check, copies the active object's swap ID from `+0x3d4` into
  the framebuffer's ID array at `+0x48`, writes the per-entry flag/metadata
  at `+0x448/+0x548`, and increments the count at `+0x2748`. The D595
  sender at `0x04218eb4` reads that same count and arrays. This connects
  **state-selected firmware swap work → D595 batch**, but the state test
  does not identify the incoming AP FourCC. The A407-vs-A408 lineage is
  still open.

  A current-build task-2 static pass narrows that edge. The AP's A407 input
  size (`0x1d30`) matches firmware receive handler `0x04222d8c` (length
  check `0x04222db0..0x04222db8`); its authenticated virtual call at
  `0x04223060` uses slot `+0x208` / discriminator `0xa428`, resolving to
  `0x04215c50` in the `0x04a0f990` vtable. A408's `0xff4` input matches
  handler `0x04222af8` (check `0x04222b1c`); call `0x04222d00` uses slot
  `+0x210` / `0x7317`, resolving to `0x042147e4`. These are size/vtable
  mappings; the handlers have no literal A407/A408 tag. A second vtable at
  `0x04a32e70` has the same `+0x208/+0x210` implementations but different
  virtual gate targets, so the concrete framebuffer class still matters.

  Correction to the first field-lineage pass: neither handler creates that
  object. A407 `0x04215d80..0x04215db8` and A408
  `0x04214914..0x04214950` read the request ID at `+0x98`, search the
  *existing* framebuffer list at `+0x3e30`, and receive the matching object
  through local `-0x70`. The shared search callback `0x04215bcc` walks
  `object+0xbc0`, compares `object+0x3d4` with the requested ID at
  `0x04215bf4..0x04215bfc`, rejects an object with `+0x62b` bit 0 set,
  and returns the retained pointer at `0x04215c40`. A miss leaves that
  pointer null and takes the handler's error branch. Only after a hit do
  the implementations copy request word `+0x17c` into the *found* object's
  `+0x3ec` (A407 `0x04215e3c/0x04215e40`, A408
  `0x042149c0/0x042149c4`). The stuck A407 trace's sparse input rows make
  both `+0x98` and `+0x17c` zero. This proves a conditional field write
  for a matching firmware object, not that the model's success reply
  actually found one.

  The preceding `0x10`-input/`0x08`-output receive path at `0x04223134`
  maps through vtable `+0x1f0` (`0x04223214`) to `0x0421738c`, then
  `+0x1f8` to `0x04217080`. On its successful allocation path,
  `0x042170ec..0x04217128` creates an object and runs callback
  `0x04217144`: it increments framebuffer `+0x3e9c`, clamps the initial
  or wrapped ID to **1**, writes that ID to `object+0x3d4` and the first
  output word (`0x04217158..0x0421717c`), and links the object into
  `fb+0x3e30` via `object+0xbc0/+0xbc8` (`0x04217180..0x04217198`).
  The size and ordering match the two A406 RPCs immediately before A407
  in `UI_CLASS16_REFRESH60_DM_A407_RETIRE1`; the current model replied
  status zero with an all-zero eight-byte A406 output both times. The
  following A407 request carried ID 0, whereas this firmware
  allocation path returns a nonzero ID. This is an **earlier ID-lineage
  mismatch candidate**, not proof of what the guest would do with real
  firmware: the QEMU model does not execute the allocation/search code,
  and an exhaustive alias-based inventory of possible ID-0 insertions has
  not been established.

  A direct task-2 text scan sharpens that limit. The only direct stores
  to the swap-object-shaped `+0x3d4` are `0x04217178` (A406 allocation),
  `0x0420dc40` (reassign the active object's ID), and `0x04292a5c`
  (fresh internal state-15 object); all three take an incremented
  `fb+0x3e9c` counter with the same clamp-to-1 pattern. A fourth
  `+0x3d4` store at `0x042775b4` is on a different configuration/NVRAM
  path and has no observed `fb+0x3e30` list insertion. The counter can
  also be restored from `[x1]` at `0x0429fcbc`, but every identified
  swap-ID allocator still clamps zero/wrap to 1. The only direct
  `fb+0x3e38` insertion-tail write is `0x04217198`; `0x04284af0`
  initializes that tail to `fb+0x3e30`, and `0x0420c910` updates it
  while unlinking an object. The sole direct `fb+0x3e30` store is its
  zero initialization at `0x04284b28`; the A406 insertion writes through
  the tail pointer at `0x04217190`. This is an inventory of direct
  fixed-offset stores and their local alias flow, not a proof that no
  indirect or copied ID-0 object can ever exist.

  The firmware lookup miss has a concrete response: A407 branches from
  `0x04215db4/0x04215dbc` to `0x0421689c`, sets `w20=0x8000000a`
  at `0x0421691c..0x04216920`, and returns `w20` at
  `0x04216f30..0x04216f38`. A408 has the parallel miss branch at
  `0x04214948/0x04214950` to `0x042153b4`, sets the same status at
  `0x04215434..0x04215438`, and returns it at `0x0421596c..0x04215970`.
  Therefore **if ID 0 is absent from `fb+0x3e30`**, this firmware would
  reject the stuck A407 request before the `+0x3ec` write or any
  promotion. QEMU's status-zero A407 reply bypasses that firmware check.

  **A406 reply ABI and AP consumer (current bootkc).** The 16-byte A406
  input is parsed at `0x042231b8..0x042231f4`: input `+0x0` is a
  32-bit in/out word copied to output word 0 when input byte `+0xc`
  bit 0 is clear; input `+0x4` is loaded as a 64-bit argument. The AP
  wrapper `0xfffffff00a0c8c50` builds that shape at
  `0xa0c8c68..0xa0c8ca4`, issues FourCC A406 with input length
  `0x10` and output length `8` at `0xa0c8ccc..0xa0c8ce0`, copies
  output word 0 to the caller's nonnull `x1` at `0xa0c8ce8..0xa0c8cec`,
  and returns the mapped output word 1 (`0xa0c8cf0..0xa0c8cf4`).
  Firmware's `0x04217080` allocation invokes callback `0x04217144`
  *synchronously* through `0x04033b20` (`blraaz x25` at `0x04033b60`);
  that callback increments `fb+0x3e9c`, clamps zero/wrap to 1, and
  writes the resulting 32-bit ID to both swap object `+0x3d4` and
  output word 0 (`0x04217158..0x0421717c`). On callback success it
  returns zero at `0x042171e4`, which the receive wrapper serializes
  at output `+0x4` (`0x0422321c`). Thus a successful *firmware*
  A406 allocation returns `{id>=1, status=0}`, not eight zero bytes.

  The AP's two record-creation paths pass `record+0x98` as `x1` to
  authenticated virtual slot `+0x598` (`0xfffffff00a0bfbcc..0xa0bfbe0`
  and `0xa0bfd34..0xa0bfd48`), with `x2=0`. Both branch on the
  returned status (`0xa0bfbe4`, `0xa0bfd4c`), **not** on whether
  `record+0x98` is nonzero; after status zero, the first path passes
  that record to slot `+0x928` (`0xa0bfc08..0xa0bfc1c`) and reads
  `record+0x98` again at `0xa0bfc7c`; the second passes it to slot
  `+0x5a0` and rereads `+0x98` at `0xa0bfd88`. The call signature
  matches the A406 wrapper, but neither candidate creation site fired
  in the matched `UI_CLASS16_REFRESH60_DM_A406_USER2` replay. They are
  not the observed A406 caller. The live wrapper returns through base
  IOMobileGraphicsFamily `0xfffffff00a0e5cdc`: `0xa0e5cb8..0xa0e5ce8`
  calls virtual slot `+0x598`, reads its local 32-bit ID, and stores it
  through `[descriptor+0x48]` at `0xa0e5ce4`. That destination is a
  kernel userclient output buffer, not the later kernel SwapEnd record.

  The intervening userland bridge was measured in the same replay, with
  all 21 breakpoint commands verified armed. `_kern_SwapBegin`
  `0x22a3911cc` entered twice with the same IOMFB object
  `0x73bb0d8000`; each nested A406 returned `{id=0,status=0}`.
  Selector-4 `IOConnectCallScalarMethod` returned success at
  `0x22a391224`, and `0x22a391254..0x22a391258` copied output word 0
  into object `+0xb0`, which is its embedded swap record
  `(object+0x18)+0x98`. `_kern_SwapEnd` `0x22a391334` then entered on
  **that same object** with record ID 0 and sent its embedded record
  through selector 5 (`0x22a391350..0x22a391364`). The matched kernel
  SwapEnd invocation read record `+0x98=0`, copied it to pending node
  `+0x3d4`, and sent A407 with request `+0x98=0`. Five earlier
  `_virt_SwapBegin`/`_virt_SwapEnd` pairs belonged to other objects and
  carried ID 2 at End; they do not explain this kernel-path ID 0.
  This closes the AP-side A406-to-A407 ID lineage for the observed
  transaction without equating the distinct userland and kernel record
  pointers.

  The current model's generic class-2 reply in
  `qemu-sptm/hw/arm/darwin_iomfb.c` allocates a zeroed output buffer
  and returns status zero (`iomfb_class2`, output block before the
  class-2 completion). Neither A406 call in
  `UI_CLASS16_REFRESH60_DM_A407_RETIRE1` had an output override:
  RPCs 38/39 were `in=16, out=8`, followed by A407 RPC 40 with
  input ID `+0x98=0`. The opt-in QEMU experiment
  `DARWIN_DCP_IOMFB_A406_IDS=1` now returns a monotonically assigned
  nonzero 32-bit ID at A406 output `+0x0`, zero status at `+0x4`, and
  retains an ID marker for A407/A408 identity logging. On the same
  saved parent, `UI_CLASS16_REFRESH60_DM_A406_IDS1` carried ID 1 from
  A406 through the same `_kern_SwapBegin` object, `_kern_SwapEnd`,
  pending node, and A407 request. Its `SwapWait(fence=1,options=1)`
  remained open through the 300-second bound; no A408 or completion
  callback appeared. This proves the AP ID lineage is repaired in the
  opt-in path, **not** that the firmware `fb+0x3e30` object/list/lookup
  has been emulated. It does not establish any D595/D582/D594
  completion policy. The default model and CLASS16 fixture are unchanged.

  The short `UI_CLASS16_REFRESH60_DM_A407_POST1` stop then sampled the
  exact ID-1 AP node immediately before/after A407's status-zero return.
  It remained the sole `fb+0x4138` entry with ID 1 and unchanged
  `+0x3d8=0x80000007`, `+0x3ec=0`, and valid single-node links. The AP
  D582 HOL remover (`0xa0c5728`) and D594/D595 full remover
  (`0xa0c58bc`) search that list by `node+0x3d4`; their unlink paths
  are reachable for a matching callback ID without a further AP
  `+0x3d8` transition. Therefore the first AP-visible missing event in
  the bounded QEMU replay is a matching inbound retirement callback,
  **not** a missing post-A407 AP node mutation. This is an AP-side
  readiness statement only. QEMU's retained-ID marker is not firmware
  `fb+0x3e30` lookup, and none of the firmware queue-to-active/D595
  gates below has been observed executing on this VM. No D-series ID is
  assigned to A407 from this comparison.

  The batch producer reads the *active* swap from framebuffer `+0x3e18`
  at `0x0420daec`, then state `+0x3ec` at `0x0420daf0`. For states 0 and
  8, the `0x80c0` mask check at `0x0420db0c` reaches `0x0420dd78`, where
  `tst (1 << state), 0x101` permits the batch arm. Further gates are the
  virtual `+0x378` check (`0x0420dda4`), count/`+0x830` check
  (`0x0420ddac..0x0420ddd4`), and capacity check (`0x0420dddc`). The arm
  copies active swap ID `+0x3d4` into the batch and increments framebuffer
  `+0x2748` at `0x0420ddec..0x0420de34`. States 6, 7, and 15 take an
  alternate branch from the first mask; this slice does not assign them
  to this batch arm.

  Other *direct fixed-offset* stores to the same swap-object layout found
  so far: `0x042170fc` copies a separate routine's argument to a new
  object's `+0x3ec`; `0x04292a64` creates one with state `0x0f`; and
  `0x0420dc54` sets the active object to `0x19` during a later
  producer-side transition (also writing ID `+0x3d4` and word `+0x3d8`).
  This is not an exhaustive alias/memcpy inventory. Other `+0x3ec` stores
  in different object layouts are not swap-state evidence. A path at
  `0x0420c934..0x0420c944` promotes a queued `+0x3e20` object to active
  `+0x3e18` when active is null. A407 has a possible, but gated, route for
  the *found* object: `0x04216d7c..0x04216d98` calls `0x04213980`
  only when the `w25` bit-0 condition and `w21` result permit it. That
  routine passes the same object through virtual `+0x5b0` and `+0x5b8`
  before queuing callback `0x04213e64` at `0x04213c98..0x04213cc4`.
  The callback rechecks the ID in `fb+0x3e30` and, after its own tests
  and virtual `+0x7b8`, calls `0x04214338` with source `fb+0x3e30`,
  destination `fb+0x3e20`, and that same object (`0x04214298..0x042142b0`).
  `0x04214338` unlinks it from the source and inserts it into the
  destination at `0x04214380..0x04214404`, choosing head/tail using
  `+0x3ec` and `+0x634`. The first framebuffer vtable's `+0x5b0` target
  `0x04211494` returns zero, so that class does not pass this gate;
  the second's `0x042964ac` delegates to the outer `+0x5ee0` object.
  Thus `0x0420c934..0x0420c944` can promote this object **if** these
  earlier gates queued it as the head, but the A407-to-queue-to-active
  sequence is not unconditional or observed for the stuck ID-0 request.

  A Capstone scan of all direct fixed-offset `str`/`stp` writes to
  `+0x3e18` in this task-2 text found 24 sites. Nonzero writers beyond
  the promotion above include
  `0x04294ad8` (head of `fb+0x3e20`), `0x04293028` (current `+0x3e20`
  pointer), `0x04297874` (an already-held `x20` object in a separate
  class-specific path), `0x04292a90` (fresh internal state-15 object),
  and `0x0420fc4c` (object passed as helper argument `x1` and unlinked
  from a list). `0x0420def4` re-stores the existing active object, while
  `0x042128c0`, `0x04212a78`, and `0x04212aac` temporarily replace
  `+0x3e18` around helper calls and restore the prior pointer. None of
  these stores alone establishes that the A407-found object is active.
  The remaining direct sites store zero (`0x0420df2c`, `0x0420e058`,
  `0x0420fc7c`, `0x04212c54`, `0x04212d40`, `0x04212f10`,
  `0x04217bb8`, `0x04284c38`, `0x04294618`, `0x04294a88`,
  `0x042972c8`); aliasing or indirect stores are not excluded by this
  fixed-offset scan.

  Producer eligibility is also class-dependent after the state tests.
  At `0x0420dda4..0x0420ddd4`, virtual `+0x378` returning one permits
  append; otherwise a nonzero `fb+0x2748` count permits append, and with
  count zero virtual `+0x830` must return nonzero. Capacity is `<0x100`
  (`0x0420dddc`). In the first vtable, `+0x378` (`0x042130e4`)
  and `+0x830` (`0x0420c840`) both return zero; a state-0 active object
  cannot *start* an empty batch through those gates. In the second,
  `+0x378` is the stateful `0x0429971c` and `+0x830` is
  `0x042912f8`, which reads framebuffer byte `+0x42fa`; their runtime
  results remain unknown. These gates, the earlier ID lookup, and the
  conditional queue transfer prevent an A407→D595 assignment. No
  D595/D582/D594 emission follows merely from the `+0x3ec` copy.

  **Completion-producer caller inventory (same task-2 image, static only).**
  The AP remover backward scan in `iomfb-dseries.md` leaves D582, D594,
  and D595 as inbound ID-carrying retirement paths. D582 is not merely an
  unrelated AP handler: this firmware builds it in the same swap-processing
  routine as the D595 batch. At `0x0420ce24..0x0420ce6c`, an active
  framebuffer with byte `fb+0x274c == 1` copies `fb+0x2764` IDs from
  `fb+0x2750` into a stack array. At `0x0420de3c..0x0420de48`, both that
  saved byte and nonzero count gate an outbound D582 loop. Each iteration
  loads one array ID at `0x0420de58..0x0420de64`, puts it in payload word
  `+0`, and sends a `0x0c`-byte D582 at `0x0420de74..0x0420de88` via
  `0x041d1a2c`. This is a **HOL-ID producer**, distinct from the active
  swap `+0x3d4` → `fb+0x48`/`+0x2748` → D595 batch arm at
  `0x0420ddec..0x0420de34` / `0x04218f8c..0x04218fa8`.

  A direct writer of that HOL input state is `0x042131cc..0x042131f8`:
  it sets `fb+0x274c=1`, appends its `w1` to `fb+0x2750` at index
  `fb+0x2764`, increments the count, and stores `w2` at `fb+0x2760`.
  Chained-fixup slots `0x0496fcd8` and `0x049931b8` resolve virtual
  `+0x348` to this writer for two framebuffer vtables. One callsite at
  `0x043068e8..0x04306914` supplies `w1=[owner+0x1cd8]` and
  `w2=[owner+0x1cdc]` to that slot on `[owner+0x90]`. It has **not**
  been connected to the A407-found swap object; neither a shared ID nor
  an A407-triggered call is established. Another D582 construction at
  `0x0429769c..0x042976d8` reads a queued object's `+0x3d4` if its
  local `sp+0x5c` flag is one. These are real emit sites, not proof that
  either fires for the CLASS16 A407 transaction.

  D594 is also built in firmware by `0x0421c20c..0x0421c2d8`:
  its caller supplies the 32-bit ID as `w0`, serialized at payload `+0`
  (`0x0421c228`), and the routine sends `0x730` bytes as D594 at
  `0x0421c2c0..0x0421c2d8`. The active swap's `+0x3d4` feeds calls
  at `0x0420d798..0x0420d7d4`, `0x0420da7c..0x0420dab0`, and
  `0x0420e360..0x0420e378`, but each call is reached through its own
  branch. The AP-side measured D594 completion remains A408-shaped;
  the existence of these firmware callsites does not assign D594 to A407.

  **First unresolved A407 edge.** On a *matched* A407 lookup, the found
  object receives request `+0x17c` at `object+0x3ec`
  (`0x04215db8..0x04215e40`). That alone does not make the object active
  or eligible for any of the three producers. After the handler's virtual
  `+0x7a0` call, `0x04216d7c..0x04216d9c` computes
  `w8 = w21 | ~w25` (`orn`), tests bit 0, and calls `0x04213980`
  for this same object only when **`w25` bit 0 is one and `w21`
  bit 0 is zero**. `w21` has several earlier assignments, including
  virtual `+0x820` result at `0x04216a14..0x04216a18` and forced
  one at `0x04216b4c`; QEMU does not execute those decisions. This is the
  earliest **unproven transfer edge** for a valid ID-1 A407 object: the
  code admits the later `fb+0x3e30 → fb+0x3e20 → fb+0x3e18`
  route, but the relevant runtime condition and the subsequent
  producer choice have not been observed on real firmware. The QEMU
  device model does not execute any of these firmware instructions.

  **A407 transfer-gate backward slice (current task-2 firmware,
  static).** The gate bits are *computed locals*, not two A407 request
  flag fields:

  | Gate input | Proven origin and transformations | Meaning supported by nearby code |
  | --- | --- | --- |
  | `w25 & 1` | `w24=1` at `0x04216510`; four-layer loop `0x04216518..0x04216894` visits layers selected by found-object `+0x3d8` (`lsr/tbz` at `0x04216518..0x04216520`). A407 request `+0x14c` was stored into that word at `0x04215dc0..0x04215dcc`. Per-layer request words at `+0x9c+4*i` are read at `0x04216690`; a nonzero word with a null resolved `object+0xaf0+8*i` surface clears `w24` at `0x04216990`. Rectangle/surface checks can clear it via `0x0421682c..0x0421683c` or `0x04216928..0x04216990`. `w25=w24` at `0x042169ac`; no later write precedes the gate. | All visited layer checks remained acceptable. The diagnostics at `0x04216784..0x0421683c` say negative `srcRect` coordinate or read region outside buffer; `0x04216928..0x04216990` diagnoses passthrough layers not fullscreen or having destination offsets. `0x042166ec..0x042166f0` redundantly reasserts 1 only when bit 0 was already set; a prior clear is not repaired. An empty selection can leave 1, so this is **not** proof of a presented surface. |
  | `w21 & 1` | `x24=fb+0x3e94` (`0x04215e0c/0x04215e30/0x042169e4`). If `fb+0x42dc==2`, virtual `+0x820` supplies the initial `w21` at `0x042169ec..0x04216a18`; otherwise it starts at 0 (`0x04216a20`). The decision also reads `fb+0x42bc`, `+0x4305`, `+0x42c4` at `0x04216a28..0x04216a48`; virtual `+0x520` can clear it at `0x04216a4c..0x04216a74`. The ordinary continuation through `0x04216b14` forces `w21=1` at `0x04216b4c` and writes 1 to optional output byte `x22`. Later `fb+0x42fd==1` with request `+0x34f==1` can force 1 at `0x04216b64..0x04216bb8`; a separate state-`0x15`/virtual-call path can force 1 at `0x04216bbc..0x04216c58`. No further write precedes `0x04216d7c`. | A **swallow/skip-transfer disposition**: strings referenced at `0x04216a8c..0x04216b48` explicitly say the swap is swallowed for external-display controller power 0, disabled timings, or second-display timing; `0x04216b88..0x04216bb8` says a fast-update swap is swallowed because the main display is unplugged. This names the branches, not every framebuffer field or virtual result. |

  In particular, a zero initial `w21` is not by itself enough. When
  `fb+0x42bc != 0`, `fb+0x4305 != 1`, and the initial `w21` is zero,
  `0x04216a48` jumps straight to `0x04216b58`, preserving zero.
  Alternatively, virtual `+0x520` returning bit 0 set clears `w21`
  at `0x04216a70` and also jumps there. Those paths still lose the
  transfer if either later override sets it to one. The other path
  through `0x04216a78..0x04216b4c` forces one. This explains why
  `0x04216d7c` requires **valid layer processing (`w25=1`) and no
  swallow decision (`w21=0`)**; it does not establish which way a
  real-firmware CLASS16 ID-1 transaction would go. The sparse QEMU
  A407 input shows request `+0x14c=0x80000007` and ID `+0x98=1`
  (`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A406_IDS1.stderr.log`),
  but QEMU has no firmware `fb+0x42bc/+0x4305/+0x42c4` state to score
  `w21` or the resolved per-layer surfaces to score `w25`. The A407
  body has no direct store to those `w21` source offsets in this
  slice; it does call virtual `+0x220` immediately before reading them
  (`0x042169c8..0x042169e0`), so an indirect state change is not
  excluded. One separate direct reset writer clears both
  `fb+0x42bc/+0x42c4` at `0x0429c298..0x0429c2a8`; that is not a
  measurement of their value at the A407 gate.

  **Measured CLASS16 ID-1 request score (`UI_CLASS16_REFRESH60_DM_A406_IDS1`,
  no new boot).** The RPC #39 sparse input at
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A406_IDS1.stderr.log:1397`
  has `request+0x14c=0x80000007`, selecting layer slots 0, 1, and 2
  in `0x04216518..0x04216520`. The row at `+0x90` contains ID 1 at
  `+0x98` and zero at `+0x9c`; the unlisted rows through `+0xa8`
  establish `request+0x9c+4*i=0` for **all four** slots. Consequently,
  the specific `0x04216694..0x04216698` clear for a *nonzero* request
  word with a null resolved surface cannot fire for this request.
  At `0x042165c8..0x04216604`, the handler derives
  `object+0xaf0+8*i` from its separate `x2[i]` surface arguments;
  a null result skips further surface checks at `0x042166a0 →
  0x04216840` without clearing `w24`. The A407 wrapper builds those
  four `x2[i]` pointers from serialized per-layer blobs at
  `request+0x6e0+0x22c*i`, gated by bytes at
  `request+0x1d23+i` (`0x04222e70..0x04222ef0`). The saved trace
  prints only the first 64 nonzero rows and omits the later flag bytes
  and most blobs. It therefore cannot prove the resolved pointers or
  the surface subtype/geometry checks at `0x042166a4..0x04216990`.
  This is **not** an empty-selection pass: three layer bits are set.
  It rules out one concrete `w25` failure arm, but does not prove
  `w25=1`.

  **Full matched A407 input (`UI_CLASS16_REFRESH60_DM_A407_FULL1`).** A
  bounded replay from the same saved parent used the unchanged CLASS16
  60 Hz fixture, `DARWIN_DCP_IOMFB_A406_IDS=1`, and the existing
  read-only ID-1 SwapEnd/A407 return hook. All four breakpoint commands
  armed; the matched return fired. `DARWIN_DCP_IOMFB_DEBUG=1` printed
  every one of the `0x1d30` A407 input bytes for RPC #39. The complete
  rows are in `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_FULL1.stderr.log`
  (from the `RPC #39 'A407'` line); the exactly reconstructed body is
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_FULL1.input.bin` (all `0x1d30`
  offsets covered). It contains ID `1` at `+0x98`, zero words at
  `+0x9c,+0xa0,+0xa4,+0xa8`, and `0x80000007` at `+0x14c`.
  Crucially, bytes `+0x1d23..+0x1d26` are **`01 01 01 01`**. Each
  `0x22c`-byte per-layer serialization blob at `+0x6e0+0x22c*i`
  (`i=0..3`) is entirely `0xaa`. These are poison/fill bytes, not
  serialized surfaces used by this request: the AP A407 builder tests
  its surface-array entry at `0xa0c8e10..0xa0c8e14`, writes flag `1`
  for null at `0xa0c8e38`, and only takes the blob-serialization calls
  `0xa0c8e24..0xa0c8e30` for a nonnull entry. The selected A407 arm
  passes that array as `x2=x27` at `0xa0c4768`; the wrapper retains it
  as `x25=x2` at `0xa0c8d5c`. This measures **no serialized surface
  on any slot**, including selected slots 0–2. It does not assert that
  this is the semantic purpose of every A407 or that A408 has the same
  shape.

  On current firmware, each flag bit 0 set branches at
  `0x04222e88..0x04222eec` around deserialization and stores a null
  `x2[i]` at `0x04222ef0`. For each selected slot 0–2, the A407
  handler therefore stores null at `object+0xaf0+8*i`
  (`0x042165c8..0x04216604`). Its `request+0x9c+4*i` word is zero,
  so `0x04216694` reaches `0x042166a0`; the null pointer then jumps to
  `0x04216840`, bypassing *both* the source-rectangle/read-region clear
  at `0x0421683c` and the passthrough/fullscreen/destination-offset
  clear at `0x04216990`. Slot 3 is not selected. Since `w24` begins at
  one (`0x04216510`) and no visited clear is reachable, the request
  would yield **`w25=1`** at `0x042169ac` **if** real firmware first
  finds the ID-1 swap object and reaches this loop. This is a
  request-side conditional, not a QEMU observation of firmware lookup,
  transfer, or completion. The next discriminator is the real-firmware
  `w21` swallow disposition at `0x04216d7c` on that same matched object;
  no AP surface-fill gap remains for this measured A407 body.

  The `+0x220` call just before `w21` is the same vtable target
  `0x04214428` in both recovered framebuffer classes (fixup slots
  `0x0496fbb0/0x04993090`). It traverses selected layers and may
  populate a cache under `fb+0x27b8` (`0x04214470..0x04214618`);
  no direct store to `fb+0x42dc/+0x42bc/+0x4305/+0x42c4/+0x42fd`
  occurs in this routine. Nested virtual calls mean indirect effects
  are still possible. For the second class, virtual `+0x520` is
  `0x042983e8`, which returns byte `fb+0x5cb1`; virtual `+0x820`
  is `0x04291338`, delegating to `[fb+0x5ee0]` virtual `+0x858`
  (fixups `0x04993390/0x04993690`). Those are framebuffer/driver
  policy state, not fields copied from A407's `+0x98` ID. The direct
  reset at `0x0429c2a8` is one established direct writer for
  `+0x42bc/+0x42c4`; writers or live values for the other fields,
  and the relevant class-specific virtual returns, have **not** been
  connected to a particular earlier AP RPC. The preceding QEMU
  A414/A442/A385 traffic cannot be named as a missing answer merely
  from ordering. No specific QEMU reply change is justified.

  **Classification ceiling after full input.** For this request the
  firmware `w25` loop is pass-valued conditional on lookup; the AP
  artifacts still cannot rank transfer against `w21=1` swallow. QEMU
  has no firmware
  framebuffer policy object or `+0x220/+0x520/+0x820` execution.
  The smallest real-firmware capture should therefore stop after a
  matched lookup at `0x042169b0` (`w25`, after the move) and
  `0x04216d7c` (`w21`,
  `w25`, object ID), adding only the actual `0x0421683c/0x04216990`
  clear site if `w25=0`, or the branch at `0x04216a48`, force-one
  `0x04216b4c`, and later overrides if `w21=1`. That distinguishes
  layer failure, swallow, and transfer on **one** real-firmware swap;
  a QEMU status-zero A407 cannot serve as any of those observations.

  **`w21` path for the full ID-1 request (static, not a QEMU gate hit).**
  The complete input also has `request+0x34f=0` and `+0x17c=0`
  (`UI_CLASS16_REFRESH60_DM_A407_FULL1.input.bin`). The latter is
  copied to the found object's `+0x3ec` at `0x04215e3c..0x04215e40`;
  there is no later direct store to that field before the `+0x3ec`
  test at `0x04216be4`. The fast-update override at
  `0x04216b64..0x04216bb8` requires `fb+0x42fd==1` **and**
  `request+0x34f==1`, so this request cannot take it. The later
  `0x04216bbc..0x04216c58` override needs virtual `+0x368` true,
  object `+0x3ec==0x15`, virtual `+0x7f0` true, and object
  `+0x844==1`; the copied zero excludes it on the visible direct path.
  An indirect mutation by an intervening virtual is not established.

  The remaining ordinary policy fork is exact, with
  `x24=fb+0x3e94`. If `fb+0x42dc != 2`, `w21` starts at zero
  (`0x042169ec..0x04216a20`); otherwise virtual `+0x820` supplies it.
  At `0x04216a28..0x04216b58`:

  | Condition after initial `w21` | Result before late overrides |
  | --- | --- |
  | `fb+0x42bc==0` | Goes through `0x04216a78..0x04216b4c`, sets `w21=1` (swallow). |
  | `+0x42bc!=0`, `+0x4305!=1`, initial `w21==0` | Branches at `0x04216a48` to `0x04216b58`, preserving zero (transfer-eligible). |
  | `+0x42bc!=0`, `+0x4305!=1`, initial `w21!=0` | Virtual `+0x520` must return bit 0 set to clear `w21` at `0x04216a70`; otherwise sets one at `0x04216b4c`. |
  | `+0x42bc!=0`, `+0x4305==1`, `+0x42c4==0` | Goes to `0x04216a78`, sets one. |
  | `+0x42bc!=0`, `+0x4305==1`, `+0x42c4!=0` | Calls virtual `+0x520` at `0x04216a4c`; bit 0 set clears `w21`, bit 0 clear sets one. This arm does **not** test the initial `w21` at `0x04216a48`. |

  The `0x04216a78..0x04216b4c` diagnostics distinguish external
  controller power zero (`0x04489182`), external timings disabled
  (`0x044891e6`), and second external display timing disabled
  (`0x04489237`); they are conditional logs, while all these paths
  converge on `w21=1`. For a powered, timed **main internal** display,
  those external-display diagnostics are not evidence that this swap
  was swallowed. The *branch condition* still depends on firmware
  framebuffer state, especially `+0x42bc` and the display-class
  `+0x4305`/virtual results. `+0x42fd` is immaterial to this request
  because its paired request byte is zero.

  **State provenance ceiling.** In the recovered framebuffer setup at
  `0x04286240..0x042862ec`, `x23=fb+0x42bc` and
  `0x042862e4..0x042862e8` writes four 32-bit ones beginning there,
  including `+0x42bc` and `+0x42c4`. That is an initialization path,
  not their measured values at the matched A407. The separate path
  `0x0429c298..0x0429c2a8` clears both words before a display-state
  call, proving they can also be zero. `+0x4305` is initialized from
  another virtual result at `0x0428626c..0x04286290`; its value for
  this CLASS16 transaction is not captured. For one recovered class,
  virtual `+0x520` returns byte `fb+0x5cb1`
  (`0x042983e8`), while `+0x820` delegates through
  `[fb+0x5ee0]` virtual `+0x858` (`0x04291338`); the other class
  maps both slots to `0x04033efc`. The `+0x220` call
  (`0x04214428`) immediately before the reads has no direct store to
  these policy fields. No writer here is tied to a particular earlier
  A/D RPC or to a demonstrably wrong QEMU reply. They remain
  firmware-side display/power/timing policy, not AP A407 input bytes.

  | Input to swallow decision | Writer/source classified so far | QEMU-visible link |
  | --- | --- | --- |
  | `fb+0x42bc`, `+0x42c4` | Firmware setup writes four ones starting at `+0x42bc` (`0x042862e4..e8`); another firmware display-state path clears these two (`0x0429c298..a8`). Power/timing-policy state, with the exact intervening transition open. | No specific earlier A/D RPC or stub reply proven to set/clear either on this CLASS16 swap. |
  | `fb+0x4305` | Firmware setup writes a bool derived from a separate virtual `+0x130` result (`0x0428626c..90`); read again in the A407 external-display diagnostic branch. | No measured AP field or RPC answer fixes that bool here. |
  | `fb+0x42dc`, virtual `+0x820` | `+0x42dc==2` selects the class-specific virtual result as initial `w21`; the writer/value at this A407 is unresolved. | No QEMU reply is identified as its source. |
  | virtual `+0x520` | Class-specific firmware method; one recovered target returns `fb+0x5cb1`, the other remains `0x04033efc`. | Not a direct A407 request bit; no proven upstream stub. |
  | `fb+0x42fd`, request `+0x34f` | Firmware unplug/fast-update check, but the measured request byte is zero. | Excluded for this transaction regardless of `+0x42fd`. |
  | virtual `+0x220` | Internal pre-read layer/cache call `0x04214428`; no direct policy-field write. | An indirect effect through nested virtual calls is unproven, not a license to change a QEMU reply. |

  Consequently, **`w25=1` is conditional-proven, but `w21=0` versus
  swallow is not scoreable from this QEMU replay**. The first
  unproved edge is the ordinary policy fork, before any completion
  producer. The minimal read-only stop on a target executing this
  firmware is `0x04216d7c` keyed to the ID-1 object from the A407
  lookup: record `w25`, `w21`, object pointer/ID, and branch to
  `0x04216d90`/`0x04213980` or the cleanup arm. If `w21=1`, one
  conditional stop at `0x04216a28` records
  `fb+0x42dc/+0x42bc/+0x4305/+0x42c4` and the reached
  `0x04216a70` or `0x04216b4c` edge. A QEMU status-zero reply or
  retained-ID HIT cannot replace that observation; no completion
  callback or model policy change follows from this static result.

  **AP connection checked; static/QEMU `w21` analysis ends here.** A
  bounded search of the extracted IOMobileGraphicsFamily-DCP and
  IOMobileFramebuffer strings plus the existing AP cross-reference notes
  found no proven mirror or IORegistry property for firmware
  `fb+0x42bc/+0x4305/+0x42c4/+0x42dc`, nor an earlier QEMU RPC answer
  that fixes their value for this swap. `iomfb-d575-external-cancel.md`
  proves a different AP object field, `self+0x430`, can encode external
  display state (`0xfffffff009188860..0x918888`,
  `0xfffffff00a0bcb88..0xa0bcb90`); neither its object identity nor its
  offset connects it to these firmware framebuffer fields. The CA power
  timeline in `setup-launch-runtime.md` likewise does not give their
  firmware values. This is a bounded negative search, not proof that no
  bridge exists anywhere. With the current QEMU model, the real firmware
  handler never executes, so this CLASS16 run cannot yield a `w21` value.

  **Ready real-firmware gate probe:**
  `tools/re/dcp_a407_w21_gate_callbacks.py` arms only task-2 A407 lookup,
  the policy fork, `0x04216d7c`, and `0x04213980`. It requires a debugger
  connected to a target *executing this recovered DCP firmware* with
  task-2 registers/memory visible. It cannot run against today's QEMU
  IOMFB stub. Load it with LLDB
  `command script import /absolute/path/to/tools/re/dcp_a407_w21_gate_callbacks.py`,
  then `script dcp_a407_w21_gate_callbacks.install(lldb.debugger, slide=0)`;
  supply the actual task-2 load slide if nonzero. Optional environment
  `DVM_DCP_A407_SWAP_ID` defaults to 1, and `DVM_DCP_A407_FB` pins a
  known framebuffer pointer. Count a run only after all nine
  `COMMAND_LIST_PROOF` lines and `DCP_A407_GATE_READY` appear. The
  `LOOKUP_OBJECT` event at `0x04215dbc` requires
  `request+0x98==object+0x3d4==1` and records the framebuffer/object
  identity. The gate event reads `w25/w21` for that same object at
  `0x04216d7c`; if `w21=1`, its buffered policy events contain only
  `0x04216a28` fields and any `+0x520` return or force-one site reached.
  A same-object `TRANSFER_SCHEDULED` hit at `0x04213980` is the
  **transfer-scheduled** outcome. `CLEANUP_ARM` at `0x04216da0`
  without that hit, with `w25&1=1` and `w21&1=1`, is **swallow**.
  The callback stops the target at either verdict. Neither outcome says
  the object became active or completed; no D-series producer is hooked.

  **First QEMU edge now opt-in.** `DARWIN_DCP_IOMFB_A406_IDS=1` remains
  the ID-only lineage probe: A406 returns a nonzero ID and logs a
  model-local ID-set HIT/MISS on later A407/A408. A separate
  `DARWIN_DCP_IOMFB_A406_OBJECTS=1` implies that ID probe and retains
  an allocated per-ID record after A406 (`out>=8`, `{u32 id,u32 status=0}`).
  For an A407 body of exactly `0x1d30` bytes, it looks up request
  `+0x98` and logs `PROBE A407 object lookup HIT/MISS` plus the ID.
  The only firmware behavior mirrored is A406 ID/list membership
  (`0x04217080`, `0x04217144..0x04217198`) followed by A407's
  lookup key; the record is **not** a firmware object at `fb+0x3e30`.
  On either HIT or MISS, A407 still receives the model's status-zero
  reply. Firmware's `0x8000000a` lookup-miss status is deliberately
  not returned because the AP reaction to that error is not established
  for this experiment. `DARWIN_DCP_IOMFB_OUT` overrides still win:
  canned A406 skips allocation/retention, and canned A407 skips this
  object lookup. Default behavior stays off. The opt-in record is
  transient probe state, not a migrated firmware framebuffer.

  A model-local object HIT is the **first QEMU-modelable edge before
  `w21`**, not proof of firmware lookup, layer acceptance, swallow,
  `0x04213980` scheduling, `fb+0x3e20` transfer, active install, or
  retirement. The current QEMU model still never executes `w21`.
  No D582/D594/D595 is emitted, no completion knob is enabled, and
  CLASS16/SpringBoard are unchanged. Pure helper checks in
  `test-darwin-iomfb-swap.c` cover A406 packing and object retain,
  duplicate/zero rejection, A407 HIT/MISS, invalid input, and clear.

  **Bounded object-lookup replay (`UI_CLASS16_REFRESH60_DM_A406_OBJECTS1`).**
  Relinked project QEMU with `ninja -C qemu-sptm/build qemu-system-aarch64`,
  then ran `tools/re/setup_gate_probe.sh` for 420 s maximum with
  `DARWIN_DCP_IOMFB_A406_OBJECTS=1` (and `A406_IDS=1`), stopping on the
  first A407 object-lookup marker. The child used the same
  `ui-class16-refresh60-dm-plugin4-launchfrozen1.qcow2` parent, patched
  dev6 BOOTKC/device tree/`system.tc`, and unchanged CLASS16 60 Hz callback
  file as the ID-only replay. `DARWIN_DCP_IOMFB_COMPLETE=0` and
  `DARWIN_DCP_IOMFB_SCANOUT=0`; the existing fixture's six setup callbacks
  remained in place. All 23 reused AP lineage hooks armed. RPC #38 A406
  (`in=16/out=8`) allocated and **RETAINed ID 1**; RPC #39 A407
  (`in=0x1d30/out=12`) carried request `+0x98=1` and logged
  `PROBE A407 object lookup HIT id=1`. The AP trace also showed
  `_kern_SwapBegin` storing ID 1, the same object reaching `_kern_SwapEnd`,
  pending-node ID 1, and `SwapWait(fence=1,options=1)` entering. No matched
  Wait return or A408/D582/D594/D595 appeared before the watcher stopped
  and exited QEMU. This short stop verifies **only the model-local A406
  retain → A407 lookup brick**; it is not a long Wait-retirement bound or
  a firmware `fb+0x3e30` lookup. Evidence:
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A406_OBJECTS1.stderr.log`,
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A406_OBJECTS1.lldb.log`,
  `.probe.out`, and `.stop` under the same `/tmp/dvm/` tag.

  **Next opt-in QEMU brick (no retirement).**
  `DARWIN_DCP_IOMFB_A406_OBJECTS=1` now copies A407 request word
  `+0x17c` into the retained ID-matched record's minimal state field
  after a HIT, mirroring the conditional firmware store to
  `object+0x3ec` at `0x04215e3c..0x04215e40`. With only objects mode,
  it logs the copied value and leaves gate/transfer unmodeled. New
  `DARWIN_DCP_IOMFB_A407_TRANSFER=1` implies objects mode (and nonzero
  A406 IDs); after the same HIT/state copy it marks that model record
  queued and logs `TRANSFER_ASSUMED`. That mark explicitly **assumes
  `w21=0`**; QEMU cannot measure the task-2 policy gate at
  `0x04216d7c` and does not execute `0x04213980` or install a firmware
  `fb+0x3e20` list node. The CLASS16 request measured in
  `UI_CLASS16_REFRESH60_DM_A407_FULL1` has `+0x17c=0`, so zero is an
  expected copied value, not evidence of a missing object. MISS/invalid
  requests do not update or queue an object; A407's status-zero reply
  remains unchanged on HIT and MISS. Canned A406/A407
  `DARWIN_DCP_IOMFB_OUT` overrides still take precedence. No D-series
  callback, Wait wake, completion producer, or CLASS16 fixture change
  follows from this assumed-transfer mark. The pure helper and all six
  IOMFB swap unit cases passed after building the affected QEMU target.

  **Bounded transfer-opt-in replay (`UI_CLASS16_REFRESH60_DM_A407_TRANSFER1`).**
  One child of the same saved parent/CLASS16 60 Hz fixture ran with
  `DARWIN_DCP_IOMFB_A407_TRANSFER=1`, `COMPLETE=0`, and `SCANOUT=0`;
  it stopped on `TRANSFER_ASSUMED`, then exited QEMU. All 23 reused
  AP lineage hooks armed. Model RPC #38 A406 allocated and retained
  ID 1; RPC #39 A407 had request `+0x98=1`, logged object lookup HIT,
  copied request `+0x17c=0` to the retained state, and logged
  `TRANSFER_ASSUMED id=1 queued=1`. `_kern_SwapWait(fence=1,options=1)`
  entered on the same AP object; no matched return or A408/D582/D594/D595
  appeared before the immediate marker stop. This confirms the **opt-in
  model mark**, not real-firmware `w21=0`, list transfer, or completion.
  Evidence: `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_TRANSFER1.stderr.log`,
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_TRANSFER1.lldb.log`,
  `.probe.out`, and `.stop` under the same `/tmp/dvm/` tag.

  **Queued A407 → completion-producer ceiling (current task-2 image).**
  The `TRANSFER_ASSUMED` mark does not identify one D-series callback.
  In `/tmp/dvm/gate232-firmware/machos/dcpfw.macho`, the queue walker
  takes `x20=[fb+0x3e20]` at `0x04296d34` (and reloads it at
  `0x042975b4`). On a path that unlinks that queued item,
  `0x04297694..0x042976a0` tests a local byte at `sp+0x5c`; when it
  equals one, `0x042976a4..0x042976d8` copies **that queued object's**
  `+0x3d4` into a 12-byte D582 payload and sends it. The local flag's
  value for the measured A407 state-0 object is unknown. A separate
  D582 HOL producer reads `fb+0x2750` into a saved ID array at
  `0x0420ce38..0x0420ce74` and serializes its entries at
  `0x0420de3c..0x0420de88`; no A407-to-HOL-array identity edge is
  established.

  If the queued object instead reaches active `fb+0x3e18` via
  `0x0420c934..0x0420c944`, the active object's ID has at least two
  further conditional routes. D595 appends `[active+0x3d4]` to the batch
  at `0x0420ddec..0x0420de34`, subject to the state/virtual/count gates
  at `0x0420daf0..0x0420dddc`, then the sender at
  `0x04218f8c..0x04218fa8` emits the batch. D594 calls at
  `0x0420da94..0x0420dab0` and `0x0420e360..0x0420e378` likewise pass
  `[active+0x3d4]` to the ID-bearing `0x0421c20c` builder, behind
  their own branches. State 0 in the CLASS16 A407 request can pass the
  early D595 mask test, but the later gates and framebuffer class are
  unmeasured; it does not exclude D582 or D594. Thus **queued-ID D582
  versus active-ID D595/D594 remains a real producer fork**. QEMU's
  queue mark is not proof that any of those firmware branches ran, and
  the transfer-only model emits no completion FourCC.

  Smallest follow-up on a target actually executing this task-2 image:
  for one framebuffer and ID 1, stop at the first *producer-side* ID
  serialization, without re-probing the A407 gate. At
  `0x042976a4`, require `x20+0x3d4==1` and record `x19=fb` plus the
  preceding `sp+0x5c` flag (queued D582). At `0x0420de58`, require the
  loaded saved HOL ID 1 and the same framebuffer (other D582 path).
  At `0x0420ddec`, require `[fb+0x3e18]+0x3d4==1` and record state
  `+0x3ec` and the virtual/count gate results (D595 batch). At
  `0x0420da94` and `0x0420e36c`, require the same active pointer/ID
  and record the caller branch before `0x0421c20c` (D594). The first
  matching producer, or the absence of all of them after the queued
  work is serviced, would decide the next branch. None is observable
  inside the present QEMU stub; the synthetic A/B below is not a
  substitute for identifying a faithful firmware callback.

  **Default-off A407 completion A/B (`2026-10-02`, QEMU-only).** The
  current-build producer fork above remains unresolved. For an AP-handler
  experiment, `DARWIN_DCP_IOMFB_A407_COMPLETE=d582|d595|d594` now implies
  objects and assumed transfer, and sends exactly one labeled `HYPOTHESIS`
  callback for each A407 HIT/queued ID. It is off when unset or invalid.
  D582 uses the queued ID in a 12-byte packet: ID at `+0`, byte `+4=1`,
  auxiliary word `+5=0` **by hypothesis**, byte `+9=0`
  (`0x042976a4..0x042976d8`; AP `0xa0da3ec`). D595 and D594 first log a
  QEMU-only `ACTIVE_ASSUMED` mark. D595 uses this build's `0x2708`-byte
  input with ID at `+0`, count 1 at `+0x400`, zeroed optional fields, and
  `0x400` output (`0x04218f8c..0x04218fa8`; AP `0xa0db1a8`). D594 reuses
  the `0x730`-byte ID-bearing A408 remover packet (`0xa0db034`), solely as
  an A407 control. None is assigned as A407's real completion. Each callback
  runs nested on the still-open A407 caller's ack-0 slot; A407 replies
  status 0 afterward. This avoids the previously measured separate-workloop
  D594/SwapEnd lock inversion (`display-completion-deadlock.md`). The old
  `DARWIN_DCP_IOMFB_COMPLETE` A408 switch remained off.

  Three independent bounded children used the same
  `ui-class16-refresh60-dm-plugin4-launchfrozen1.qcow2` parent, patched dev6
  BOOTKC/DTREE/`system.tc`, unchanged CLASS16 60 Hz callback file, and one
  selected variant per boot. Each stopped on the matched ID-1
  `_kern_SwapWait(fence=1,options=1)` return, after its AP remover entry:

  | Tag suffix / variant | Model send and AP callback | AP remover before Wait | ID-1 Wait return |
  | --- | --- | --- | --- |
  | `AB_D5821` / D582 | nested D582(ID 1), AP `D582_ENTRY` input `0x0c` | `D582_REMOVER`: head ID 1 | `0`, 6.0 ms |
  | `AB_D5951` / D595 | nested D595(ID 1,count 1), AP `D595_ENTRY` input `0x2708` | `FULL_REMOVER`: head ID 1 | `0`, 8.2 ms |
  | `AB_D5941` / D594 | nested D594(ID 1), AP `D594_ENTRY` input `0x730` | `FULL_REMOVER`: head ID 1 | `0`, 4.0 ms |

  Each full tag is `UI_CLASS16_REFRESH60_DM_A407_<suffix>`; its device
  model log is `/tmp/dvm/probe/<tag>.stderr.log`, its keyed AP hook log is
  `/tmp/dvm/<tag>.lldb.log`, and its verdict is `/tmp/dvm/<tag>.probe.out`.
  All 28 callback sites armed in each run; the model logs show A406 retain
  ID 1, A407 lookup HIT, `TRANSFER_ASSUMED`, then the one chosen callback.
  The AP hook recorded the matching ID at the remover with that ID still at
  the pending head; the Wait return is the observed retirement result. There
  was no separate post-unlink head snapshot. These are **three QEMU-positive
  hypotheses**, not a firmware-producer ranking: the AP accepts all three
  synthetic packets. No boot ran more than one variant, and none establishes
  real `w21=0`, active promotion, D-series producer choice, migration return,
  or pixels. The next correctness discriminator remains the producer-side
  real-firmware stop above.

  **Longer post-Wait D582 hypothesis (`2026-10-02`).** One 480-second
  CLASS16 child, `UI_CLASS16_REFRESH60_DM_A407_POSTWAIT_D5822`, reused the
  same saved parent, patched dev6 BOOTKC/DTREE/`system.tc`, and 60 Hz
  callback fixture. It set `A406_OBJECTS=1`, `A407_TRANSFER=1`,
  `A407_COMPLETE=d582`, `COMPLETE=0`, and `SCANOUT=0`; the last two are the
  existing A408-completion and host-scanout switches. All 33 read-only
  swap/unwind sites armed. Model stderr recorded exactly four labeled
  `HYPOTHESIS` D582 sends/replies, one for each A407 HIT/queued ID 1–4.
  AP hooks saw four corresponding `D582_ENTRY`/`D582_REMOVER` hits, each
  with the same ID at the pending head. The matched
  `_kern_SwapWait(fence=1,options=1)` returned `0` in 6.9 ms. **After**
  that return, backboardd performed A406/SwapEnd/A407 transactions for IDs
  2–4, and a backboardd `DM_XPC_SYNC_RETURN` marker fired about 1.24 s
  later. That sync return was not paired to the original migration call,
  so it does not prove `DMPerformMigration` unwound. No A408, D594, D595,
  `DM_PERFORM_RETURN`, SpringBoard check-in return, shell pre-return, or
  panic was observed before the bound. This proves post-Wait execution
  advanced through additional swaps under **one QEMU hypothesis**; it
  neither selects D582 as firmware truth nor establishes migration or
  display completion. Logs: `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_POSTWAIT_D5822.stderr.log`,
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_POSTWAIT_D5822.lldb.log`, and
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_POSTWAIT_D5822.probe.out`.
  The earlier `POSTWAIT_D5821` child had zero breakpoint proofs because
  the composite LLDB callback import failed; it is an instrumentation
  miss, not guest evidence.

  **Next bounded migration edge (`UI_CLASS16_REFRESH60_DM_A407_MIGTAIL_D5821`,
  `2026-10-02`).** A separate 480-second child kept that same parent,
  patched dev6 boot inputs, CLASS16 60 Hz fixture, and *only* the D582
  completion hypothesis (`A406_OBJECTS=1`, `A407_TRANSFER=1`,
  `A407_COMPLETE=d582`, `COMPLETE=0`, `SCANOUT=0`). The read-only swap,
  rolling DataMigration, plugin-progress, and unwind hooks armed; no model
  or fixture code changed. The ID-1 `_kern_SwapWait(1,1)` returned `0` in
  4.8 ms, and model/AP logs recorded exactly five D582-hypothesis
  send/remover pairs for IDs 1–5. The backboardd `DM_XPC_SYNC_RETURN` at
  `t=1790990106.126` **preceded** its measured `DM_PERFORM_ENTRY` at
  `t=1790990106.167`; it therefore cannot be the reply for that call.
  On the same backboardd owner thread (`0xffffffe0d94db830`), the rolling
  ladder entered `DM_MIGRATE_CALL` at `t=1790990106.410`, then
  `DM_XPC_SYNC_CALL` (`DataMigration` static `0x25e8f5798`, runtime
  `0x26f8f5798`) at `t=1790990106.421`. Its paired return at static
  `0x25e8f579c` and the enclosing migration/check-in/shell returns were
  absent through the bound (about 389 s after the call); the frozen owner
  stack still contained the sync-reply wait.

  The frozen DataMigrator thread `0xffffffe0d967d8f0` had
  `-[DMMigrationPluginWrapperConnection runPluginAtPath:withIdentifier:parameters:completion:]`
  → `-[DMXPCConnection sendMessageSync:]` → Mach reply wait. Its saved
  request dictionary (`0x74e10404e0`) contained the exact identifier
  `com.apple.-0LaunchServicesMigrator`; preserved path object
  `0x74e0c35a40` decoded to
  `/System/Library/DataClassMigrators/00LaunchServicesMigrator.migrator`.
  A live migration-plugin wrapper (PID 192, thread
  `0xffffffe0d93bf060`) was inside `__LSCopyServerStore` →
  `___LAUNCH_SERVICES_IS_RESEEDING_ITS_DATABASE_AND_MAY_BLOCK__` →
  synchronous NSXPC reply wait. `lsd` was alive (PID 117); a saved worker
  stack reached `LSCreateRegistrationData` and the
  `MIFilesystemScanner`/`MILaunchServicesDatabaseGatherer` bundle scan,
  with another thread in bundle validation. Thus the next **bounded**
  dependency after swap retirement is LaunchServices database reseeding
  needed by `00LaunchServicesMigrator`, not another open SwapWait. One
  endpoint does not prove that the LS scan stopped advancing or that the
  sync wait is permanent; `DM_PERFORM_RETURN` remains unobserved. The D582
  callback remains a QEMU hypothesis, not a firmware assignment.
  Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_MIGTAIL_D5821.lldb.log`,
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_MIGTAIL_D5821.stderr.log`,
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_MIGTAIL_D5821.mem/migration-dependency.json`,
  `...mem/lsd-sparse.json` under the same tag, and the saved
  `...wrapper-symbols.json` / `...lsd-symbols.json` address lookups.

  **Bounded LS reseed follow-up (`UI_CLASS16_REFRESH60_DM_LS_RESEED_D5822`,
  `2026-10-02`): outcome B, active scan.** The same parent, patched dev6
  boot inputs, CLASS16 60 Hz fixture, and D582-hypothesis environment were
  held fixed for a 650-second read-only run. `_kern_SwapWait(1,1)` returned
  `0` in 8.8 ms. Backboardd then entered `DM_PERFORM_ENTRY` at
  `t=1790991711.392` and its matched `DM_XPC_SYNC_CALL` (static
  `0x25e8f5798`) at `t=1790991713.025`; neither the sync return nor
  `DM_PERFORM_RETURN` fired by cutoff. The migration wrapper's same thread
  entered `__LSCopyServerStore` (static `0x186df21c8`) at
  `t=1790991749.199`, called the blocking selector (`0x186df2688`), then
  entered its sync send (`0x186ef62b8`) at `t=1790991749.387`; none of
  their paired returns fired. On `lsd`, system scan (`0x186e75f0c`) and
  scan driver (`0x186f81090`) entered at `t=1790991746.173/1746.192` and
  remained open, but nested registration entry/return pairs kept advancing:
  50, 100, 150, 200, 250, 300, 350, 400, 450, 500, 550, then **600
  completed registrations** at `t=1790992256.055`, about 18 seconds
  before freeze. The first unfinished server stage is the scan-driver call,
  not one fixed registration item. This run does not show a missing or
  failed prerequisite, and it does not establish when reseeding finishes.
  An `lsd` blocking-handler/seed-wait hit at `t=1790991746.139/1746.147`
  preceded the wrapper send, so those hits are *not* paired to that client
  request. D582 remains a QEMU completion hypothesis. Probe:
  `tools/re/dm_ls_reseed_boundary_callbacks.py`; evidence:
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_RESEED_D5822.lldb.log`,
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_RESEED_D5822.probe.out`, and
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LS_RESEED_D5822.stderr.log`.
  Earlier `...LS_RESEED_D5821` had the process-name slide omitted from
  the probe and is an instrumentation miss for LS, not a separate guest
  verdict.

  **Why this parent reseeds (`UI_CLASS16_REFRESH60_DM_LS_REASON_D5823`,
  `2026-10-02`): recovery-file rebuild, outcome 2.** A short condition-stop
  replay held the same parent, CLASS16 fixture, and D582-hypothesis env.
  ID-1 `_kern_SwapWait(1,1)` returned `0` in 6.95 ms. On one `lsd` thread,
  the database-builder block carried reason flags `0x5` at
  `t=1790995500.277`, then entered system scan (`0x186e75f0c`) and its
  driver (`0x186f81090`) at `t=1790995506.190/5506.206`. Static
  `0x186ec1ea0` loads those flags from block `+0x28`; the priority tests
  at `0x186ec1eb4..0x186ec1ec0` select bit 2 at `0x186ec1ef8..0x186ec1efc`,
  calling `__LAUNCH_SERVICES_IS_REBUILDING_THE_DATABASE_BECAUSE_A_RECOVERY_FILE_EXISTED__`.
  Bit 0 is also set (fresh creation), but bit 3 (database did not exist)
  is clear. `__LSDatabaseCreateFromPersistentStore` tests
  `__LSDatabaseRecoveryFileExists` at `0x186eb45b0` and sets the bit-2
  reason at `0x186eb462c..0x186eb4630`. Thus this full system scan is
  recovery-file driven on this boot, not merely inferred from the long
  wrapper wait. The exact recovery-file pathname and why it persisted
  were later measured on the same saved parent. Its backing chain reaches
  `ui-class16-refresh60-ccs-ls-seedprogress1.qcow2` through
  `ui-class16-refresh60-dm-plugin4-launchfrozen1.qcow2`; that older run saw
  scan-driver return and a later `syncWrite:` *call*, but not its return.
  An interrupted earlier sync is consistent with this lineage, not proved.
  This condition-stop does not measure whether the current scan driver or
  migrator eventually returns. The first `lsd` blocking handler had
  peer PID 125 before the wrapper's blocking send (wrapper PID 193),
  so it is not paired with that wrapper request. D582 remains
  a QEMU hypothesis. Probe: `tools/re/dm_ls_reseed_boundary_callbacks.py`;
  evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REASON_D5823.lldb.log`
  and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REASON_D5823.probe.out`.

  **Recovery marker identity (`...LS_PARENT_SEED_D5825`).** The lsd
  `__LSDatabaseRecoveryFileExists` path builder (`0x186eb4d50`, checked at
  `0x186eb4d98`) resolved
  `/private/var/mobile/Containers/Data/InternalDaemon/D7D6FEE4-CBF0-469A-B81A-1071B12A6174/Library/Caches/com.apple.LaunchServices.error`
  with `exists=1`. The sibling `com.apple.LaunchServices.syncInterrupted`
  and `com.apple.launchservices.recreate` checks both returned `exists=0`.
  `-[_LSDefaults dbRecoveryFileURL]` (`0x186ec8a98`) selects the `.error`
  name; `__LSDatabaseCreateRecoveryFile` uses `open` at
  `0x186eb4e00..0x186eb4e18`, while
  `__LSDatabaseDeleteRecoveryFile` uses `unlink` at
  `0x186eb53e4..0x186eb53eec`. This establishes which marker drives the
  rebuild, but not why it remained after the prior incomplete seed. In
  particular, `syncInterrupted` being absent means the old missing
  `syncWrite:` return is **not** proof of an interrupted-sync marker. The
  store filename builder has the format `%s-%03llu-v2.csstore` (default
  prefix `com.apple.LaunchServices`), but this run did not capture the
  exact numbered `.csstore` pathname. The recovery marker alone is
  sufficient to explain the observed reason bit; no DB file was edited.
  The persistent Data volume is FileVault-locked when attached read-only on the
  host, so the marker was read from guest lsd; no LS files were removed.

  **Natural recovery seed on a new overlay (`UI_CLASS16_REFRESH60_DM_LS_PARENT_SEED_D5825`).**
  The same CLASS16 60 Hz fixture and D582-hypothesis env
  (`A406_OBJECTS=1`, `A407_TRANSFER=1`, `A407_COMPLETE=d582`, A408
  `COMPLETE=0`, `SCANOUT=0`) ran from
  `ui-class16-refresh60-dm-plugin4-launchfrozen1.qcow2`. Reason flags
  were again `0x5`, and lsd entered the scan driver at
  `t=1790997661.504`. Completed registrations advanced through at
  least 1,050; the driver returned (`x0=1`) at `t=1790998517.621`,
  and system scan returned at `t=1790998521.767`. The exact builder
  thread then called `syncWrite:` at `t=1790998609.013`, observed its
  return at `t=1790998609.228`, and reached builder sync return at
  `t=1790998609.237`. The `00LaunchServicesMigrator` wrapper's
  `__LSCopyServerStore` blocking send returned at `t=1790998609.573`;
  the condition stop preserved overlay
  `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825.qcow2`
  as a candidate repaired parent. This closes the prior **missing
  `syncWrite:` return** and shows the recovery scan is finite on this
  branch. Whether lsd removed `.error` and whether a fresh child skips
  reason `0x5` was left to the child check; a returned write alone does not
  prove durable seed reuse. Probe/log:
  `tools/re/dm_ls_reseed_boundary_callbacks.py`,
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_PARENT_SEED_D5825.lldb.log`.
  An earlier `...LS_PARENT_SEED_D5824` used a callback fixture whose
  timing rates were zero, causing a pre-LS reboot; it has no seed verdict
  and was not promoted.

  **Fresh-child check (`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_CHILD_D5826`).**
  Booting from the saved `...LS_PARENT_SEED_D5825.qcow2` overlay with
  identical CLASS16/D582-hypothesis settings, lsd checked the same
  `.error` path and returned `exists=0` at `t=1790998735.041`.
  `DMPerformMigration` entered at `t=1790998762.385`. Two migration
  wrapper `__LSCopyServerStore` calls returned successfully in about
  0.19 s and 0.03 s (`t=1790998783.958` and `1790998844.728`),
  with no blocking-store send, flags-`0x5` reason, or scan-driver entry.
  Forty-seven wrapper LS-store returns accumulated while other plugins
  ran. Finally the **matched original** backboardd
  `DM_XPC_SYNC_RETURN` fired at `t=1790999956.738`, followed on the same
  thread by `DM_PERFORM_RETURN` at `t=1790999956.755` (`x0=0`), about
  1,194 s after its entry. The condition stop was the migration return;
  check-in and `_startSystemShellService` were not measured. This is a
  durable parent/child recovery fix for the LS gate, not merely a
  faster LS query in the seed run. D582 remains a QEMU hypothesis,
  independent of this LS result. Evidence:
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_CHILD_D5826.lldb.log`.

  **Check-in/shell continuation probe (`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_SHELL_D5827`).**
  This is another fresh child of the repaired `...LS_PARENT_SEED_D5825.qcow2`
  overlay with the same CLASS16 60 Hz and D582-hypothesis settings.
  `tools/re/dm_repaired_ls_shell_callbacks.py` reuses the migration
  unwind, BackBoardServices check-in, and FB shell tail brackets. All
  52 sites armed before the guest continued. ID-1 `_kern_SwapWait(1,1)`
  returned `0` after about 5 ms, and backboardd entered
  `DMPerformMigration`. On SpringBoard's shell-service thread,
  endpoint/connection/configure/activate/remote-target calls returned,
  then `BKS_MIGRATION_CHECKIN_CALL` fired at `t=1791000436.809` with
  `wait_for_migration=1`. That call's return, the shell-service return,
  and ADFL entry were the condition-bound outcomes of this run. The
  matched backboardd `DM_PERFORM_RETURN` fired on its entry thread at
  `t=1791001551.963` (`x0=0`); SpringBoard's
  `BKS_MIGRATION_CHECKIN_RETURN` (`0x18a1476a4`) followed at
  `t=1791001551.993` (`x0=0`), then `BKS_CHECKIN_RETURN`
  (`0x18a147774`) at `t=1791001552.001` and
  `SHELL_SERVICE_RETURN` (`0x1c4a3c05c`) at `t=1791001552.005`,
  all on the original
  SpringBoard shell-service thread. Thus the migration return released
  the BKS check-in in about 30 ms, and the shell-service call returned
  roughly 42 ms after migration. No flags-`0x5` rebuild marker or panic
  appeared. The probe stopped at the shell-service return, before
  `_FBSystemShellInitialize` tail/return or ADFL; those are **not**
  claimed. This is QEMU progress under the labeled D582 hypothesis,
  not proof of firmware callback identity. Evidence:
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_SHELL_D5827.lldb.log`.

  **Shell-tail / ADFL continuation (`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_TAIL_D5828`).**
  A fresh child of
  `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825.qcow2`
  reused the CLASS16 60 Hz fixture and the same opt-in A406 objects,
  A407 transfer, and `A407_COMPLETE=d582` hypothesis; A408 completion
  and scanout remained off. The 52 sites in
  `tools/re/dm_repaired_ls_shell_callbacks.py` armed before boot. ID-1
  `_kern_SwapWait(1,1)` returned `0` in 6.2 ms. The matched backboardd
  `DM_PERFORM_RETURN` fired with `x0=0` at `t=1791005038.213` (about
  1,228 s after entry); no lsd flags-`0x5` rebuild marker fired.
  SpringBoard's migration check-in returned `x0=0` at
  `t=1791005038.260`, followed by `BKS_CHECKIN_RETURN` at `.269`
  and `SHELL_SERVICE_RETURN` at `.272` on the same shell thread.
  That thread then reached every armed post-service tail milestone
  (`C08C`, `C094`, `C0B0`, `C140`, `C1B0`, `C240`, `C2D8`),
  `SHELL_PRE_RETURN` at `.679`, and the caller-side
  `SB_SYSTEM_APP_AFTER_SHELL` at `.685`. The latter proves
  `_FBSystemShellInitialize` returned. `SB_ADFL_ENTRY` fired on that
  same thread at `t=1791005039.355`, about 0.67 s after the shell
  return. The condition stop was ADFL **entry**, so ADFL return and
  later workspace/display milestones are unmeasured. This is
  hypothesis-enabled QEMU progress, not D582 firmware identification.
  Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_TAIL_D5828.lldb.log`
  and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_TAIL_D5828.watch.log`.

  **ADFL / Main-home continuation (`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_ADFL_D5829`).**
  Another fresh child of the repaired D5825 parent used the same
  CLASS16 60 Hz fixture and D582-hypothesis settings. The 58 initial
  read-only sites armed before boot. ID-1 `SwapWait(1,1)` returned `0`
  in 7.9 ms; matched `DMPerformMigration` returned `x0=0` at
  `t=1791007923.185`, and the caller-side shell-init return fired at
  `t=1791007924.905`. `applicationDidFinishLaunching:` entered on the
  SpringBoard shell thread at `t=1791007927.630`; its live caller return
  address was armed on that thread. After its setup-mode update return,
  `SB_WORKSPACE_CREATE_REQUEST` fired at `t=1791008010.662`.
  `-[SBHomeScreenEntity init]` entered 11 ms later and returned a
  **non-null object** (`x0=0x7a0de8a1c0`) at `t=1791008010.681`.
  After that home-entity return, SpringBoard hit `CAContext setLayer:`
  eight times and `SB_WORKSPACE_EXECUTE_TRANSITION` twice. Finally the
  paired ADFL caller return fired on the original thread at
  `t=1791008028.444`, about 101 s after entry. The condition stop
  required both the ADFL return and non-null home init return. This
  establishes Main-home/workspace and CA layer-installation activity,
  **not** a CA commit, presented frame, or pixels. No flags-`0x5`
  rebuild marker or XNU panic occurred. D582 remains a labeled QEMU
  hypothesis, not a firmware callback identification. Probe:
  `tools/re/dm_repaired_ls_adfl_home_callbacks.py`; evidence:
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_ADFL_D5829.lldb.log`
  and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_ADFL_D5829.watch.log`.

  **Post-home commit / first-surface handoff (`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_PRESENT_D5830`).**
  A fresh child of repaired parent
  `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825.qcow2`
  kept the CLASS16 60 Hz fixture, `DARWIN_DCP_IOMFB_A406_OBJECTS=1`,
  `DARWIN_DCP_IOMFB_A407_TRANSFER=1`, and the labeled
  `DARWIN_DCP_IOMFB_A407_COMPLETE=d582` hypothesis. A408 completion and
  scanout remained off. All 65 initial read-only sites armed. ID-1
  `_kern_SwapWait(1,1)` returned `0` in ~6.2 ms; `DMPerformMigration`
  returned `x0=0`; shell initialization, non-null Main-home entity init,
  and ADFL caller return repeated. After home, SpringBoard entered
  `CA::Context::commit_transaction` at `t=1791010063.172`; the first
  call returned to its caller at `t=1791010086.322` (~23.15 s).
  `backboardd` then entered `CA::WindowServer::Server::render_update`
  at `t=1791010125.417`, returning at `.510`. Its framebuffer swap
  handoff initially carried null `x2`; hit #6 at `t=1791010127.741`
  carried non-null `x2=0x7c2ad303d0`, `x3=512`, and later capped
  handoff samples carried the same pointer. This is a natural post-home
  surface handoff, not a proven presentation. The first commit and render
  update have ordered timestamps but no transaction/message identity
  linking them; the dynamic return hooks paired only their first calls.

  No post-home kernel `IOMFB_A408_BUILD` hit or outbound model A408 RPC
  appeared through the ~10-minute post-home condition bound; the model
  continued A406/A407 and hypothesis-labeled D582 traffic. Those A407s
  are same-boot IOMFB activity, not paired to the non-null handoff.
  `IOMFBDisplay::create_surface` / `ALLOCATE_IOSURFACE_RETURN` did not
  hit after home in the capped probe; the handed-off surface may have
  existed earlier. There was no XNU panic. The first unproven render
  edge is **non-null framebuffer handoff → kernel A408 build/outbound
  A408** (or a measured alternative present route). Neither a presented
  frame nor pixels are established, and D582 remains a QEMU hypothesis.
  Probe: `tools/re/dm_posthome_present_callbacks.py`; evidence:
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_PRESENT_D5830.lldb.log`,
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_PRESENT_D5830.stderr.log`,
  and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_PRESENT_D5830.probe.out`.

  **Natural post-home handoff → pre-submit map failure
  (`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_BRANCH_D5831`).** A fresh child
  of the same repaired D5825 parent retained CLASS16 60 Hz,
  `A406_OBJECTS=1`, `A407_TRANSFER=1`, and the D582 completion
  hypothesis; A408 completion and scanout stayed off. All 73 read-only
  sites armed, and ID-1 Wait, `DMPerformMigration` (`x0=0`), and non-null
  Main-home entity return repeated. The first post-home non-null
  `FB_SWAP_HANDOFF` at `t=1791013680.850` passed surface
  `0x75f11443d0` on IOMFB object `0x75f1061400` (thread
  `0xffffffe230ebf060`). On that same thread and object,
  `+0xb38` dispatched to `_kern_SwapSetLayer` at static
  `0x22a3918f4`, then public SwapEnd `+0xc28` dispatched to
  `_kern_SwapEnd` at `0x22a391334`. The paired End return at
  `t=1791013680.889` was `x0=0xe00002bd` (`kIOReturnNoMemory`);
  the containing `finish_update` returned immediately afterward.
  The return hook is the caller address `0x19a3e45cc`; its raw
  `object_match=false` field in this run compared the **return status**
  in `x0` with the object pointer and is a logging artifact. The
  entry and return were paired by the saved same-thread object; the
  probe code now logs that object on returns.

  No matched pending-node link, `0xa0c4564`/`0xa0c4608` submit gate,
  A407 build, or `A408_BUILD` at `0xa0c4664` fired for that End; the
  model had no outbound A408. The serial log begins a repeated
  `mapper-disp0` sequence at line 1722: `IODARTMapper::_iovmAlloc`
  rejected a 759-page allocation for **inactive DART**;
  `iovmMapMemory` returned `0xe00002bd` for length `0xbdc000`, and
  IOMFB reported failure to set the DMA-command memory descriptor.
  This is same-boot evidence that the surface mapping path is failing
  before A407/A408 selection, consistent with the paired End status.
  The serial lines do not print the exact IOSurface pointer, so the
  particular mapper call is not independently object-paired to the
  selected handoff. The next read-only cut is the `dart-disp0` active
  state at the matched map attempt, versus the known active-DART
  `GATE154` comparison; no DCP completion or scanout behavior was
  changed. D582 remains a QEMU hypothesis. Probe:
  `tools/re/dm_posthome_swap_branch_callbacks.py`; evidence:
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_BRANCH_D5831.lldb.log`,
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_BRANCH_D5831.serial.log`,
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_BRANCH_D5831.stderr.log`,
  and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_BRANCH_D5831.probe.out`.

  **Matched display-DART map (`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5832B`).**
  The same D5825/CLASS16 profile reached Main home, then paired the first
  non-null handoff (`surface=0x752ad18480`, IOMFB object
  `0x752b0bc000`) to `_kern_SwapEnd` and its IOMMU `+0x550` map on
  thread `0xffffffe4e355c180`. Map length was `0xbdc000`; mapper
  `0xffffffe3fdf8c000` pointed to provider `dart-disp0`
  (`0xffffffe231e0c000`). On this same map the mapper active byte
  `+0x1e0=0`, provider active byte `+0x99a=0`, and provider flags
  `+0x988=0xc0000065`. The guest requested active state (`x1=1`) at
  `0x9f1ee98`; its provider `+0x598` query at `0x9f1eef4` returned
  zero at `0x9f1eef8`. It joined at `0x9f1f0a8` without executing
  DART power call `0x9f1f074` or mapper active-state write
  `0x9f1f07c`. `_iovmAlloc` rejected 759 pages as inactive, the
  IODMA return and matched End both reported `0xe00002bd`. This is
  a **guest inactive-state / skipped-enable** boundary before submit,
  not evidence about A407/A408 completion. Read-only model MMIO checks
  found `dart-disp0` SID-0 enable/TCR/TTBR all zero while the separate
  `dart-dcp` enable register was nonzero. The matched mapper's SID
  `+0x110` was not captured. Which branch skips the DART power call,
  and whether an earlier pre-attach power transition occurred, remain
  unproven; no model change is justified. Probe:
  `tools/re/dm_posthome_dart_active_callbacks.py`; logs:
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5832B.lldb.log`,
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5832B.serial.log`,
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5832B.probe.out`.

  **Inactive-DART guard resolved (`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5833`).**
  Another unchanged D5825 child paired the first post-home handoff,
  `_kern_SwapEnd`, and the `dart-disp0` map. The mapper's SID field
  `+0x110=0`; config word `[[mapper+0x120]+8]=0xe0000065` has bit 31
  set. The provider has `+0x988=0xc0000065`, `+0x99a=0` and returned
  0 at its `+0x598` active query. The mapper requested active state
  `w20=1`; the prerequisite returned 1 at `0x9f1ef08`. The matched
  call hit config gate `0x9f1effc`, request/query compare
  `0x9f1f000` (`w20=1`, `w25=0`), and **skip-power branch
  `0x9f1f008 → 0x9f1f0a8`**. It did not hit DART power method
  `0x9f1f074` or mapper active-state write `0x9f1f07c`. Serial then
  reported the inactive 759-page reject; paired End again returned
  `0xe00002bd`. An earlier unpaired state request after migration
  returned took the same branch on this mapper, so no intervening
  activation preceded the home map. The first failed edge is therefore
  the guest's bit-31/mismatched-state power skip, not a wrong DART
  instance or an A407/A408 submit gate. Whether an external display
  power event should have activated `dart-disp0` before this request
  remains unresolved. The bit-31 policy is compatible with prior
  manual-availability observations, not a license to force guest state
  or alter QEMU's DART model. D582 remains a labeled completion
  hypothesis. Probe: `tools/re/dm_posthome_dart_active_callbacks.py`;
  evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5833.lldb.log`,
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5833.serial.log`,
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5833.probe.out`.

  **Display-DART activation bound (`UI_CLASS16_REFRESH60_DM_DART_ENABLE_D5834`).**
  A fresh child with the same D5825 parent, CLASS16 fixture, and D582
  hypothesis stopped on the matched post-home `_kern_SwapEnd` return.
  Its non-null handoff mapped `0xbdc000` through `dart-disp0` SID 0;
  provider `+0x99a=0`, active query 0, requested state 1, and config
  `0xe0000065` again took `0x9f1f008` (skip power). The map failed as
  inactive and End returned `0xe00002bd`. An earlier state request on
  the same mapper had also skipped power. Read-only hooks at both
  direct active-byte writers were armed, but neither ran for
  `dart-disp0` before the failed map. QEMU's DART debug trace contained
  no `dart-disp0` MMIO access or `ENABLE_STREAMS` write; the separate
  `dart-dcp` instance did receive two `0xc00` enable writes.

  BOOTKC static paths are now concrete: `AppleT8110DART` vtable
  `+0x580` (`0x9707c3c`) can store the requested state at
  `+0x99a` (`0x9707ce4`) after successful power; its manual-availability
  vtable `+0x648` (`0x9708a98`) stores `x1` there at `0x9708ad8`.
  The IODART platform-function branch `0x9f1b958..0x9f1b988`
  forwards a nonzero input word to `+0x648`. IOMFB's `powerUpDART`
  path `0xa0ba4b0` → vtable `+0x8c0` → `0xa0ba678` → mapper
  vtable `+0x3a8` (`0x9f26474`) can request the mapper state
  transition at `0x9f1ee98`; the two observed transitions skipped
  provider power because of the already measured bit-31 guard.
  The exact caller/event that should issue the manual-availability
  activation on this child is **not identified**. GATE154 reached
  `+0x99a=1` with the same flag class and then mapped/A408, so the
  flag alone is not a permanent prohibition. This is an absent guest
  activation before map, with no evidence of a model-ignored enable.
  GATE154's LLDB/serial logs were unavailable locally for a direct
  activating-caller comparison; only its documented outcome and surface
  events remain.
  The next read-only edge is the caller into the manual-availability
  dispatcher `0x9f1b944..0x9f1b988`, paired with IOMFB
  `powerUpDART` entry `0xa0ba4b0` on the same display/mapper.
  No DART force-enable or completion change was made. Evidence:
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_DART_ENABLE_D5834.lldb.log`,
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_DART_ENABLE_D5834.stderr.log`,
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_DART_ENABLE_D5834.serial.log`.

  **DART caller identities (static D5835 slice).** This build has only
  two direct callers of IOMFB `powerUpDART` (`0xa0ba4b0`):
  `DCPPowerManager::fn_0xa0`/`set_power_state` at `0xa0d667c`, and D572
  callback `0xa0d9aa4` at `0xa0d9b74`.
  The power-manager call has `enable=0`, `dcp_power_off=1`
  (`0xa0d6670..0xa0d667c`), so it is a power-down path.
  D572 accepts a four-byte input,
  resolves an IOMFB instance through its callback index, and passes
  bits 0 of bytes 0/1 to `powerUpDART`. The IOMFB implementation calls
  mapper `+0x3a8` with its `setActive` selector; the matching mapper
  branch calls `0x9f1ee98`, which skipped provider power on D5834.
  Separately, DCPPowerManager vtable `+0x80` at `0xa0d5db0` calls
  IOMFB `powerUpDART_v2_assertion` (`0xa0ba37c`) at
  `0xa0d5e18..0xa0d5e24` with enable derived from `x3 != 0`.
  Its refcount transition calls IOMFB vtable `+0x8c0` at
  `0xa0ba460..0xa0ba488` (`0xa0ba658`/`0xa0ba678`), an
  activation-capable path that does not directly call `0xa0ba4b0`.
  D5835 hooks the implementation, not v2 assertion entry; a quiet
  implementation hook only excludes an activation transition there.
  The separate mapper `registerErrorHandler` selector at `+0xa70`
  can forward through `IODART::fn_0x3a8` (`0x9f1b560`) and, under its
  additional argument checks, call the provider `+0x648` active-byte
  writer at `0x9f1b988`. These are code paths, not evidence either
  activation event occurred on the natural CLASS16 boot.

  The first D5835 replay armed all DART caller/writer sites and saw
  ID-1 `SwapWait` return and migration entry, but never reached the
  post-home handoff. `SpringBoard.migrator` hit the native reboot
  deadline and the guest panicked at 12:37 PDT. All DART hooks were
  quiet before that event; this is an unexercised-map coverage result,
  not a natural-path activation verdict. Logs:
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_DART_CALLER_D5835.lldb.log` and
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_DART_CALLER_D5835.serial.log`.
  An unchanged second replay is tagged
  `UI_CLASS16_REFRESH60_DM_DART_CALLER_D5835B`.

  **D5835B caller verdict:** the repeat hit `DM_PERFORM_RETURN` with
  `x0=0`, check-in and shell-service return, then ADFL and the matched
  post-home non-null surface, then mapped `0xbdc000` through
  `dart-disp0` SID 0. Its provider `+0x99a` remained 0. The earlier
  and matched-map requests on the same mapper both asked for state 1,
  queried 0, and took `0x9f1effc → 0x9f1f000 → 0x9f1f008`
  (config `0xe0000065`), bypassing provider power. The map failed
  `0xe00002bd`, paired `_kern_SwapEnd` returned the same value, and
  there was no display-DART MMIO. No D572 callback, direct
  `powerUpDART`, v2 `+0x8c0` implementation, manual-availability
  dispatcher, or provider active-byte writer hit between arm and map.
  Thus the instrumented upstream activation call/transition is
  **A: absent** on this boot;
  the later map-driven state request is **C: correct mapper, bit-31
  skip leaves inactive**. The CLASS16 callback script has no D572 and
  the QEMU model does not emit it, making D572 one concrete missing
  event candidate; the v2 assertion route remains another, with its
  entry unhooked but its activation implementation covered. The trace
  does not prove which event real firmware uses, and GATE154's
  documented active result has no surviving caller log. No model or
  completion change follows. Evidence:
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_DART_CALLER_D5835B.lldb.log`,
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_DART_CALLER_D5835B.serial.log`,
  and `.stderr.log`.

  **D572 QEMU activation hypothesis (`UI_CLASS16_REFRESH60_DM_D572_HYP1`).**
  New default-off `DARWIN_DCP_IOMFB_D572=1` appends a four-byte-in,
  four-byte-out D572 callback after the six unchanged CLASS16 setup items
  (which begin after A353). Input `01 00 00 00` asks the current-build
  `0xa0d9aa4` handler to call `powerUpDART(enable=1,power_off=0)` via
  `0xa0d9b64..0xa0d9b74`. It is explicitly a **hypothesis**, not a
  current-firmware scheduling claim. QEMU does not force DART registers.
  Callback #7 was sent and completed with status 0/output `00000000`;
  during that exchange, the guest programmed `dart-disp0` TCR/TTBR and
  `ENABLE_STREAMS` `0xc00=0x18` (streams 3/4, not SID 0), whereas the
  unchanged D5835B baseline had no display-DART MMIO. The direct D572
  LLDB hook armed after this early callback and thus cannot prove handler
  entry independently; the callback/reply interval and guest MMIO are the
  evidence for the induced path. Provider `+0x99a`, the paired home map,
  and `_kern_SwapEnd` return are scored at the later condition stop.
  D582, CLASS16, A408 completion, and scanout remain unchanged. Logs:
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_D572_HYP1.stderr.log` and
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_D572_HYP1.lldb.log`.

  That condition stop reached the post-home non-null surface on the same
  thread and IOMFB object as `_kern_SwapEnd`. `DMPerformMigration`
  returned `x0=0`; shell-service and ADFL ran; then the matched handoff
  carried `x2=0x7789140490`, `x3=512`. Unlike D5835B, the display
  provider was already active (`dart-disp0+0x99a=1`) at the first
  post-migration state requests and at the matched SID-0 map. The two
  DART precondition returns and `IODMA_MAP_RETURN` were 0; paired
  `_kern_SwapEnd` returned 0 instead of `0xe00002bd`. Later in the
  boot, the **guest** also wrote `dart-disp0` `ENABLE_STREAMS`
  `0xc00=0x1` before A407 #61 (SID 0); the early D572 window wrote
  `0xc00=0x18` (streams 3/4). No QEMU register force-enable was used.

  | D572 hypothesis score | D5721 result | Evidence limit |
  |---|---|---|
  | Callback | #7 sent, four-byte in/out, reply status 0 | Early direct handler hook missed. |
  | Guest power work | `dart-disp0` MMIO inside callback window | Direct `powerUpDART` entry was not hooked that early. |
  | Provider / map | `+0x99a=1`, matched SID-0 map returns 0, End returns 0 | Activation writer time was not captured directly. |
  | Submit/pixels | No A408 build or model A408 before the End-return stop | No presentation or scanout conclusion. |

  Although that probe stopped at the matched End return, its LLDB log
  includes the earlier submit sites on the same handoff object/thread:
  `0xa0c4518` linked node ID 12 with `+0x3d8=1`;
  `0xa0c4564` read `fb+0x5870=0`;
  `0xa0c4608` read `fb+0x5df0=0`;
  `0xa0c477c` built A407; model RPC #434 carried A407 request
  `+0x98=12` and logged the QEMU-local object HIT. `0xa0c4664`
  A408 build was absent before End returned 0. The zero bit 0 at
  `fb+0x5df0` selects the A407 arm for this mapped swap.

  **Fresh D572 submit repeat (`UI_CLASS16_REFRESH60_DM_D572_SUBMIT1`).**
  With the same D5825 parent, CLASS16 file, D572 opt-in, and D582
  retirement hypothesis, the guest reached `DM_PERFORM_RETURN x0=0`,
  ADFL, and the post-home non-null surface handoff. That handoff and
  `_kern_SwapEnd` shared the same backboardd thread and object
  `0x76eac41400`; `dart-disp0` SID 0 had `+0x99a=1`, both DART
  precondition returns and `IODMA_MAP_RETURN` were 0. The matched
  End linked node ID 12 (`+0x3d8=1`) at `0xa0c4518`; gate
  `0xa0c4564` read `fb+0x5870=0`, and `0xa0c4608` read
  `fb+0x5df0=0` and took the A407 arm. `A407_BUILD` at
  `0xa0c477c` fired for that node/thread; model RPC #830 was A407
  with request `+0x98=12` and QEMU-local lookup HIT. The paired
  End returned 0; neither matched A408 build nor model A408
  appeared before the End-return stop. This independently repeats
  D5721's **mapped End → A407** decision. The first observed
  selector is clear bit 0 at `fb+0x5df0`; its writer/meaning is
  not established by these runs. Evidence:
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_D572_SUBMIT1.lldb.log`,
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_D572_SUBMIT1.stderr.log`,
  and `.serial.log`. The bounded child stopped at End return with
  zero XNU panics, without an A408 completion or scanout claim.

  Thus the D572 opt-in is **QEMU-positive for clearing this boot's
  inactive-DART/NoMemory map gate**, while its real-firmware scheduling
  remains unproven. D582 remains a separate completion hypothesis. The
  repeated post-map choice is A407; these runs intentionally stopped
  at the mapped End return.

  **AP `fb+0x5df0` selector bound (`UI_CLASS16_REFRESH60_DM_FB5DF0_WRITER1`
  / `UI_CLASS16_REFRESH60_DM_FB5DF0_EARLY1`).** The current BOOTKC
  `_kern_SwapEnd` saves `fb+0x5000` at `0xa0c388c` and reloads it as
  `x26` at `0xa0c41ec`. Its `ldrb` at `0xa0c45fc` reads
  `x26+0xdf0 = fb+0x5df0`; the bit-0 branch at `0xa0c4608` takes
  A407 (`+0x990`, call `0xa0c477c`) when clear and A408 (`+0x998`,
  call `0xa0c4664`) when set. This proves only a submit-method
  selector. A direct-offset inventory of the DCP and base IOMFB
  kexts found no direct writer for `+0x5df0`; alias/bulk writes are
  not excluded. Constructor clears adjacent `+0x5de0..+0x5def`
  (`0xa0b52c8`) and `+0x5e00..+0x5e1f` (`0xa0b537c`), leaving this
  byte outside those explicit stores. Adjacent `+0x5df2` and the
  `iomfb_disable_async_swap` writer to `+0x5868` are not this field.

  The early child armed a write watch at construction
  (`0xa0b52c4`, `fb=0xffffffe4e02d0000`), observed byte zero and
  no writes before the first pending ID-1 node. A separate longer
  child armed at its first ID-1 node (`fb=0xffffffe10a468000`), saw
  no writes through the mapped post-home End on that same fb, then
  recorded `+0x5870=0`, `+0x5df0=0`, pending ID 14, A407 build,
  no A408 build, and End `x0=0`. This rules out a missed ordinary
  setter in the measured natural path, while it does not identify a
  field name or prove no indirect writer exists elsewhere. GATE154
  reached A408 on an older forced-color run, but its raw artifacts
  are absent, so it gives no `+0x5df0` value/writer comparison.
  Classification: the natural mapped home swap selects **A407 from
  a byte that stayed clear**; A408 is not established as its required
  present route. The next firmware edge for this route is the A407
  lookup/transfer path toward `0x04213980` and conditional queue/active
  lists. Current QEMU `TRANSFER_ASSUMED`/D582 remains a hypothesis,
  and no presentation or pixels follow from this result. No model or
  fixture change. Logs:
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_FB5DF0_WRITER1.lldb.log`,
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_FB5DF0_WRITER1.stderr.log`,
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_FB5DF0_EARLY1.early-kernel-lldb.log`,
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_FB5DF0_EARLY1.lldb.log`.

  **Mapped A407 is surface-bearing (`UI_CLASS16_REFRESH60_DM_A407_MAPPED_FULL2`).**
  One fresh D5825/CLASS16 child kept the D572+D582 hypotheses and
  A408 completion/scanout off. After `DM_PERFORM_RETURN x0=0` and
  home initialization, a non-null backboardd surface handoff
  (`x2=0x766cd203d0`) paired to the same object's `_kern_SwapEnd`:
  pending ID 11, `fb+0x5870=0`, `fb+0x5df0=0`, A407 build, End
  `x0=0`, no A408 build. `DARWIN_DCP_IOMFB_DEBUG=1` recorded the
  entire `0x1d30` A407 request for RPC #574; the reconstructed bytes
  are `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_MAPPED_FULL2.input.bin`
  (SHA-256
  `f432a24da2ce0b7d7f43915ff2af72de204c74c9f3d169cd6733d5e35cdab756`),
  with selected fields in `.input.json`. An attempted `MAPPED_FULL1`
  child used the wrong trust cache and failed AMFI before LLDB slide
  resolution; it contributes no A407 evidence.

  Request `+0x98=11`, `+0x14c=1`, `+0x9c..+0xa8={1,0,0,0}`,
  `+0x17c=0`, `+0x34f=0`, and `+0x1d23..+0x1d26={0,1,1,1}`.
  Slot 0's `+0x6e0` blob is serialized rather than poison and
  contains `ARGB`, width 1179, height 2556, stride 4864, allocation
  `0xbdc000`; the other three blobs remain `0xaa` poison. The raw
  word at `+0xf90` is `0x10000000000`, not yet independently paired
  with the SID-0 mapped DVA. This is **not** the earlier `A407_FULL1`
  null-surface request: FULL1 had `0x80000007`, zero per-layer words,
  four null flags, and four poison blobs. With a real ID-11 lookup
  hit, the current firmware would deserialize slot 0 and reach its
  surface/rectangle checks at `0x042166a4..0x04216990`; `w25` can
  now be cleared there rather than passing by the old null-pointer
  shortcut. The request alone does not measure the firmware-resolved
  surface or `w25`. Its zero `+0x34f/+0x17c` still excludes the two
  previously identified late swallow overrides, but `w21` display
  policy remains unmeasured. Thus the first **real-firmware** gap on
  this surface-bearing A407 is ID lookup followed by slot-0 `w25`,
  then `w21`/transfer; QEMU status zero proves none of those gates.

  QEMU logged its local ID-11 lookup HIT, `TRANSFER_ASSUMED queued=1`,
  and one labeled D582 nested callback/reply for that ID. The LLDB
  stop was at the paired End return, before an ID-11 Wait or AP
  remover could be scored. The first **QEMU-local present** gap is
  separate: `darwin_iomfb.c` calls `iomfb_scanout()` only when the RPC
  is A408, and `darwin_iomfb_swap_surface()` currently accepts only
  the A408 `0xff4` input shape. Enabling the existing scanout knob
  would not process this A407 `0x1d30` body. No A407-specific scanout
  was implemented: its candidate DVA still needs SID-0 correlation,
  and the transfer/retirement path remains explicitly hypothetical.
  Logs: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_MAPPED_FULL2.lldb.log`
  and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_MAPPED_FULL2.stderr.log`.
  There is no buffer-update or pixel claim.

  **A407 scanout model seam (opt-in, QEMU-local).** The measured
  `MAPPED_FULL2` request is the unit-test fixture for
  `darwin_iomfb_a407_surface()`. `DARWIN_DCP_IOMFB_A407_SCANOUT=1` accepts
  only the retained-ID HIT and the measured one-primary A407 shape
  (`+0x14c=1`, slot-0 word nonzero, null flags `{0,1,1,1}`, bounded
  ARGB geometry). It uses request `+0xf90` as a **candidate** SID-0 DVA,
  translates through `dart-disp0`, and copies before the labeled D582
  hypothesis runs. A successful DMA/present would prove a QEMU host-buffer
  update, not firmware `w25/w21`, D572/D582 identity, or physical display
  presentation. The existing scanout flag remains A408-only; this sibling
  opt-in leaves A408 completion off.

  **A407 scanout hypothesis live (`UI_CLASS16_REFRESH60_DM_A407_SCANOUT_HYP3`).**
  The repaired D5825/CLASS16 child kept D572+D582, A408 completion off,
  and A408 scanout off; it added only `A407_SCANOUT=1` while retaining
  `DARWIN_DCP_IOMFB_ECHO_MODE=1`. HYP1/HYP2 had mistakenly used
  `...MODE_ECHO`, lacked the A413→A442 echo control, and failed before
  A407; they are invalid scanout comparisons. HYP3 returned
  `DMPerformMigration x0=0`. The matched non-null handoff reached an
  ID-12 pending node, clear `fb+0x5df0` A407 build, and `_kern_SwapEnd
  x0=0`. Model RPC #434 logged local A407 HIT and
  `TRANSFER_ASSUMED id=12`; the measured single-primary parser produced
  1179×2556 BGRA, stride 4864, and DVA `0x10000000000`.
  `dart-disp0` SID-0 DMA and `darwin_fb_present_bgra()` succeeded,
  producing the `HYPOTHESIS A407 presented ... id=12` log **before**
  hypothetical D582 send/reply. Null-layer IDs 1–4 were rejected without
  scanout. The probe stopped on paired End return with zero panics;
  ID-12 Wait return and screendump were not sampled. This proves a
  QEMU host-buffer update only. Firmware lookup, slot-0 `w25`, policy
  `w21`, and D572/D582 identity remain unmeasured. Evidence:
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_SCANOUT_HYP3.lldb.log`,
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_SCANOUT_HYP3.stderr.log`,
  and `.probe.out`.

  **A407 visible-content check (`UI_CLASS16_REFRESH60_DM_A407_VISIBLE1`).**
  A fresh D5825/CLASS16 child used the exact HYP3 D572+D582+A407-scanout
  environment (`DARWIN_DCP_IOMFB_ECHO_MODE=1`, A408 completion/scanout off).
  `DMPerformMigration` returned `x0=0`, with no panic. The first surface-bearing
  present was ID 6 just after ADFL entry; its frozen HMP dump
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_VISIBLE1.png` was uniformly black
  (1179×2556, SHA-256
  `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).
  The same child was resumed, then stopped at a later A407 ID-13 present.
  Its HMP dump `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_VISIBLE1.id13.png`
  (SHA-256
  `a2c2646a5f567a5070d62a027d605929fd39c55c50f957f36e90278e8df1c451`)
  contains 41 RGB colors and 6,436 pixels above channel value 8 out of
  3,013,524. It visibly contains dim status/time (`9:09`), an information
  glyph, and a home indicator on an otherwise black screen. That proves
  nonzero UI-like pixels reached the **QEMU host framebuffer** through the
  A407/SID-0 scanout hypothesis; it does not prove a full home screen,
  hardware presentation, `w21`, or real-firmware D572/D582 identities.
  The model logged ID-13 HIT/present before the labeled D582 reply. A paired
  guest ID-13 `_kern_SwapWait` return remains unmeasured because LLDB detached
  at the earlier ID-6 stop. Evidence: the tag's `.lldb.log` and
  `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_VISIBLE1.stderr.log`.

  **Post-home A407 frames and Wait closure (`UI_CLASS16_REFRESH60_DM_A407_RICH_WAIT1`).**
  The same D5825/CLASS16 hypothesis stack ran with LLDB attached past
  `DM_ADFL_HOME_COMPLETE`. Six live HMP dumps for A407 IDs 14–19 are in
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_RICH_WAIT1.frames/`, with SHA-256
  and pixel counts in the tag's `.frames.jsonl`. IDs 15–17 and 19 were
  status-bar-sparse (~0.23% of pixels above RGB threshold 8); ID 14 was
  black. ID 18 rose to **0.711%**, visibly showing a Charging / 100%
  banner and swipe-up lock prompt. A frozen final dump after ID 20 showed
  status/time plus swipe-up text (0.308%). No captured frame showed
  wallpaper or home icons. Matched backboardd `_kern_SwapWait` for the
  same object/fences 14–19 returned `x0=0` at both `0x80000001` and
  `0x80000000` options; ID 20's `0x80000001` Wait also returned `0`.
  The logged D582 remains a completion **hypothesis**, not a firmware
  identification. Every successful A407 present passed the one-primary
  parser (`darwin_iomfb_swap.c:219`), so these accepted wires had only
  selected slot 0 and null slots 1–3. Eleven SpringBoard post-home
  `CA_CONTEXT_SET_LAYER` hits do not establish a paired commit or explain
  absent wallpaper/icons. The remaining richness question lies upstream
  of the sparse primary surface, with no evidence requiring A408 or a
  DART change. The tag's `.lldb.log`, `probe/*.stderr.log`,
  `.visibility-watch.log`, `.frames.jsonl`, and PNGs contain the score.

  **Lock-content source check (`UI_CLASS16_REFRESH60_DM_LOCK_RICH1`).**
  A bounded run with the same D5825/CLASS16/ECHO_MODE/D572/D582/A407-scanout
  stack captured eight post-home A407 dumps (IDs 12–19) and zero panics.
  ID 13 showed the Charging/100% banner with 0.590% pixels above RGB
  threshold 8; ID 19 showed sparse status time and swipe-up text at 0.380%.
  The matched A407 and both backboardd `_kern_SwapWait(13, ...)` calls
  returned `x0=0` within 7.15 ms. SpringBoard created and retained a
  `CSProminentTimeView` and native `_UIAnimatingLabel`, updated its time
  string, and entered CA commits before ID 13. Its time view wanted
  glass material and a `CSGlassContentStyleRenderer` was present; the
  separate warm-boot native-label/glass failure in
  `docs/re/lockscreen-poster-time.md` is a comparison, not proof of the
  same failure here. Four `wallpaperImageForVariant:wallpaperMode:` calls
  returned nil, but the PosterBoard controller retained a
  background scene/context ID `0xb25ca86a`; nil static images do not
  exclude a dynamic poster. Thus neither prominent-time view creation nor
  background-host attachment is the missing edge. Whether the scene and
  time styling produced non-black CA layers, or those layers were lost
  before the one-slot A407 primary, remains open. A small next read-only
  bracket is poster host `setScene:` `0x201ee0ee8`, CA layer/commit for
  context `0xb25ca86a`, and its first server render update before the
  matched A407. See `iomfb-dseries.md` LOCK_RICH1 and the tag's
  `.lldb.log`, `.frames.jsonl`, `lock-state-final.json`, and
  `frames/id13.png` / `frames/id19.png`. These are hypothesis-enabled QEMU
  presents; they do not establish real-firmware D572/D582 identity or
  `w21` transfer.

  **Poster context to first sparse present (`UI_CLASS16_REFRESH60_DM_LOCK_CA1`).**
  The same fixed D5825/CLASS16/ECHO_MODE stack, with A407 scanout and the
  labeled D572/D582 hypotheses, now has a cross-process context-ID join:
  `MercuryPosterExtension` installed a non-null CA root layer through
  `-[CAContext setLayer:]` (`0x18451a138`) for `0xb641f540` at epoch
  `1791094867.882`; SpringBoard's background poster host attached a
  non-null scene with that same context ID at `setScene:` `0x201ee0ee8`
  at `1791094870.460`. In the continuation of this one child,
  backboardd `CA::Render::Server::render_update` (`0x184776174`) returned
  before the non-null handoff and A407 ID 12. A407 ID 14's HMP image
  remains Charging/status/swipe chrome on black, 0.762% pixels above
  channel threshold 8; `_kern_SwapWait(14,0x80000001)` returned `x0=0`
  in 4.2 ms. The 63 captured images (IDs 12–74) never exceeded 0.762%
  in that bound. A later native time-label sample had a non-hidden CA
  layer with non-null `contents`, but that pointer is not a non-black
  rasterization verdict. The CA commit hook was global, not paired to
  context `0xb641f540`; the context-filtered server updater hook gave no
  positive poster hit and does not cover every render/cache path.

  Therefore source **existence** is proven, while blank source versus
  lost-before-A407 versus a clear remains unresolved. The first missing
  measurement is a context-ID-keyed backboardd resolve/render result
  (non-black surface/draw bounds) immediately before one ID-keyed A407;
  pair that with the prominent-time label's rendered texture/bounds.
  `iomfb-dseries.md` LOCK_CA1 records the exact artifacts and limits.
  This is still QEMU-hypothesis-enabled present, not real-firmware
  D572/D582 identity or `w21` transfer.

  **Offscreen inclusion is not display contribution (`UI_CLASS16_REFRESH60_DM_LOCK_CTX1`).**
  A continuous child on the same hypothesis-enabled stack observed this
  boot's MercuryPosterExtension root/context `0x6462aa96` at epoch
  `1791097861.850`, then SpringBoard attached a scene with that same ID.
  Backboardd's `CA::Render::Update::add_context` (`0x18443ffb8`) received
  the matching context/root twice; both inclusion checks returned 1 and
  `add_included_context` ran. The static caller at `0x1845a60d4` is
  `Server::render_display_context_list_to_target`, however, and built a
  separate offscreen `Update` (`0x108501ec0`, then `0x108501e10`). The
  display `render_update` before A407 IDs 12–14 used `0x108cffe48`.
  Consequently the positive context-inclusion hits do **not** establish
  poster participation in the display update or non-black pixels; no
  matching natural `render_for_time` `add_context` call was seen in this
  bound. ID 14's Wait returned 0 and its HMP image was still status/swipe
  chrome on black (0.342% pixels above channel threshold 8). The native
  time-label `contents` pointer was non-null but its backboardd rendered
  texture/bounds were not captured. The next read-only cut is the
  context-ID-keyed `render_for_time` inclusion sites `0x184410b50` /
  `0x1844131f8`, paired by `Update*` with display `render_update` and
  A407, followed by prepared-layer bounds/output and the time label's
  server-side layer identity. Blank source, loss before A407, and clear
  remain unclassified. See `iomfb-dseries.md` LOCK_CTX1 and
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CTX1.{lldb.log,frames.jsonl}`;
  D572/D582/A407 scanout remain QEMU hypotheses.

  **Natural-display follow-up was a poster coverage miss (`LOCK_DISPLAY1`).**
  On one D5825/CLASS16 child, `render_update` x1 matched the `Update*`
  passed to `Display::render_display` and `OGL::Renderer::render`
  (`0x107cffe48` and `0x107cfc990`). A same-guest bounded continuation
  captured one uniformly black A407 host present (ID 6). Neither armed
  interval produced a MercuryPosterExtension `setLayer:` or SpringBoard
  poster `setScene:` event, so no context ID was available to join to
  those display updates. The ID-filtered direct and nested inclusion
  hooks being quiet is therefore **not** proof of display exclusion;
  LOCK_CTX1's offscreen context ID is from a different boot. The native
  time-label CALayer had non-null contents, but no backboardd server layer
  or prepared bounds/texture was identified. No blank/lost/cleared
  classification follows. Artifacts and the black-frame hash are in
  `iomfb-dseries.md` LOCK_DISPLAY1.

  Static narrowing for the next positive-context run: natural
  `render_for_time` can add a context directly at `0x184410b50` /
  `0x1844131f8`, or `Updater::prepare_layer0` can resolve a nested
  LayerHost. The latter calls `LayerHost::retain_hosted_context` at
  `0x18441ec84` using host context ID at `Render::Layer+0x22c`
  (embedded host `+0x1c`); its result at `0x18441ec88` precedes
  `add_included_context` at `0x18441ec9c` and the secure/policy tests
  `0x18441eccc` / `0x18441ecdc`. The probe now logs this lookup before
  ID filtering. A matching non-null Context on the same display
  `Update*` is the first missing live edge; prepared output and an
  A407 pairing remain subsequent measurements. This static CA path
  adds no DCP completion claim and leaves the D572/D582/scanout
  hypotheses unchanged.

  **LOCK_JOIN2 closes the display-join coverage gap, not the pixel gap.**
  One continuous D5825/CLASS16 child with the same labeled
  D572/D582/A407-scanout stack observed MercuryPosterExtension install a
  non-null root for this boot's context `0x35fc28af` at
  `1791104318.303`; SpringBoard's `setScene:` retained that same ID at
  `1791104321.581`. Backboardd's nested LayerHost path passed that
  Context to `add_included_context` (`0x18441ec9c`) on
  **`Update* 0x1058ffe48`**, with secure and inclusion checks both
  returning 1 (`0x18441eccc` / `0x18441ecdc`) and the accept arm
  `0x18441ece4` running at `1791104322.046`. The same `Update*` was
  later passed to display `render_update`, `render_display`, and OGL
  immediately before the mapped framebuffer handoff and A407 ID 13.
  Unlike LOCK_CTX1's offscreen context-list `Update*`, this is positive
  poster **display-update attribution**. The ID-13 host dump was
  uniformly black; a later held frame had only sparse time/status/swipe
  chrome (0.321% of pixels above channel threshold 8). An accepted
  context on a reused `Update*` is not a sampled non-black draw or proof
  of participation in that particular A407's compositor command list.
  Blank poster source, loss after preparation, and a later clear remain
  unseparated. The client time CALayer again had non-null contents but
  has no paired server `Render::Layer`/texture sample. The next narrow
  read is the accepted `Updater::prepare_layer0` branch at
  `0x18441ece4` (x21 `Render::Layer`, x19 `LocalState0`, x23
  `Update*`) through its actual draw result, plus the clock's
  `-[CALayer _copyRenderLayer:layerFlags:commitFlags:]` source→render
  layer identity at `0x1844c0268`. Artifacts, exact times, image paths,
  hashes, and the static-return-hook limitation are recorded in
  `iomfb-dseries.md` LOCK_JOIN2 and
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_JOIN2.poster-join.json`.
  This changes no DCP A407 `w21`, transfer, or D582/D572 callback claim.

  **LOCK_DRAW2 is a bounded host-attachment coverage miss.** A new
  continuous D5825/CLASS16 child under the same hypothesis stack crossed
  migration/home and MercuryPosterExtension installed root `0x7869129d00`
  for context `0xbc7d5c3f`. SpringBoard CA commits and time-layer copies
  continued, but `_updatePosterLayers` (`0x201edef20`) and the background
  host's `setScene:` (`0x201ee0ee8`) did not fire before the post-home
  watcher bound. Therefore this child has no scene ID/display-accepted
  poster node to sample; it does **not** classify LOCK_JOIN2's accepted
  poster as blank, lost, or cleared. The time label's client
  `_copyRenderLayer:` path returned non-null source contents at
  `0x1844c061c`, but no server `Render::Layer` or texture identity was
  established. The read-only nested `prepare_layer0` bracket now covers
  `0x18441ece4` through `0x18441fe80`/`0x18441ff28`, with OGL keyed by
  `Render::LayerNode*` (`OGL::Layer+0x10` written at `0x184466304`).
  Exact logs and the watcher result are in `iomfb-dseries.md` LOCK_DRAW2
  and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_DRAW2.poster-join.json`.
  No firmware `w21` or DCP completion inference follows from this miss.

  **LOCK_DRAW4 validates the corrected probe but again misses poster
  attachment.** DRAW3's thin-wrapper callback module was not registered via
  LLDB `command script import`, so its first breakpoint ended that partial
  capture; its later reattach is not scored. DRAW4 fixed that before a fresh
  D5825/CLASS16 boot, with the same labeled D572/D582/A407-scanout stack.
  Migration returned, home completed, and eight unrelated backboardd
  `LayerHost::retain_hosted_context` call/return pairs established that the
  server-side hook was live. The time label again had non-null **client**
  source contents during `_copyRenderLayer:`, with no server-layer identity.
  Over the 1,000-second post-home bound, neither a Mercury non-null CA root
  nor SpringBoard background `setScene:` appeared. The watcher recorded
  empty Mercury/scene ID sets and
  `poster-join-no-positive-context-posthome-bound` in
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_DRAW4.poster-join.json`. Later A407
  presents cannot be attributed to a poster on this child. This is coverage,
  not a blank/prepared/lost/cleared draw verdict; JOIN2's same-boot accepted
  display `Update*` remains the positive join. The exact callback/control
  details and artifacts are in `iomfb-dseries.md` LOCK_DRAW4. No DCP gate,
  callback, or firmware identity is inferred.

  **LOCK_ATTACH1 names an earlier SpringBoard scene-null skip on another
  attachment-miss child.** The D5825/CLASS16/ECHO child kept D572, D582,
  and A407 scanout as labeled QEMU hypotheses. Migration and home completed;
  the 1,000-second post-home bound again had no Mercury root, hosted
  `setScene:`, or positive poster context, without a panic. The newly armed
  controller bracket saw one `PBUIPosterViewController` call
  `_updatePosterScenesForReasons:updater:completion:` at `0x201ed190c`
  19 times before home. All 19 calls loaded `_scene=0` and took the
  `0x201ed1964 → 0x201ed1a0c` null branch; none took the scene-present
  `0x201ed1968` arm. `setActivelyRequired:` requested 1 and later 0 on
  the same controller without changing those observed null decisions.
  Static PaperBoardUI code locates the next input at
  `updateConfiguration:`'s block (`0x201f37e68`): the rendering-role scene
  lookup returns in `x20` at `0x201f37ed8`, then `x20` is passed with
  `_scene` field address `x22` to the store helper at
  `0x201f37fdc..0x201f37fe4`. These writer sites were added to the
  read-only callback module **after** the freeze and have no live verdict
  yet. This run does not establish Mercury extension liveness or poster
  pixels. See `iomfb-dseries.md` LOCK_ATTACH1 and
  `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_ATTACH1.poster-join.json`; it
  changes no DCP completion or transfer inference.

  The gate is not equivalent to the A407 reply status. At
  `0x04216d80..0x04216d88`, `w25&1=1` presets `w20=0` even when
  `w21&1=1` skips the transfer. That skipped arm reaches
  `0x04216db0..0x04216e0c`, which schedules `0x04213de4` with
  `fb+0x3e30` and the object's ID; that callback searches/unlinks the
  ID and releases the object (`0x04213e00..0x04213e4c`). The handler
  then releases its copied surfaces at `0x04216e14..0x04216ea8`
  and returns `w20` at `0x04216f34`. A `w25` failure instead presets
  `0x80000001` at `0x04216d84..0x04216d88`. Therefore even on real
  firmware, **status-zero A407 can be a swallowed/cleanup transaction,
  not evidence of `fb+0x3e20` transfer**. QEMU status zero tells us
  still less, because it bypasses this handler altogether.

  **Passing the gate queues work; it does not itself move the object or
  emit completion.** At `0x04216d90..0x04216d98`, `x0=fb` and
  `x1=[x29-0x70]` (the matched object) enter `0x04213980`. It uses
  that object and schedules callback `0x04213e64` via `0x04033b20`
  at `0x04213c98..0x04213cc4`. The callback rechecks `object+0x3d4`
  against the `fb+0x3e30` list at `0x04213eac..0x04213ed0` and has
  additional per-layer and virtual `+0x7b8` tests before
  `0x04214298..0x042142b0` calls `0x04214338` with source
  `fb+0x3e30`, destination `fb+0x3e20`, and that same object. That
  helper unlinks through `object+0xbc0/+0xbc8` at
  `0x04214380..0x042143a0` and inserts into the destination at
  `0x042143d0..0x04214404`; state `+0x3ec` and byte `+0x634`
  choose head or tail. A later `0x0420c934..0x0420c944` can install
  the queued head at `fb+0x3e18` if no active object exists. Both
  the callback checks and that promotion remain conditional. A direct
  branch scan of the separate A408 handler `0x042147e4..0x04215c50`
  found no call to `0x04213980`; it does not justify importing this
  A407 gate or a D-series assignment into A408.

  The *first unresolved execution edge* on a real-firmware ID-1 swap
  is now the **computed** `w25/w21` outcome, especially the
  framebuffer-policy path through `0x04216a28..0x04216b58`. A tiny
  read-only probe on a target running this firmware can refine the
  existing object-keyed plan below: capture `x19=fb`, `x20=request`,
  `[x29-0x70]=object`, request `+0x98`, and object `+0x3d4` at
  `0x04215db8`; record `w24` and loop index `x26` only at clear sites
  `0x0421683c/0x04216990`, then `w25` at `0x042169b0` (after
  `0x042169ac` copies `w24`). At
  `0x042169ec..0x04216a24`, record `fb+0x42dc` and virtual
  `+0x820` result if called; at `0x04216a28..0x04216a74`, record
  `fb+0x42bc/+0x4305/+0x42c4`, virtual `+0x520` result if called,
  and the branch to `0x04216b58` or force-one `0x04216b4c`.
  Record later overrides at `0x04216bb8/0x04216c50` only if hit,
  then `w25/w21` at `0x04216d7c` and the same-object entry at
  `0x04213980`. This separates **A: gate failed** from a passed gate;
  the existing `0x04214338` and `0x0420c944` pointer checks separate
  **B: queued/active transition failed**, **C: active without a
  matching completion producer**, and **D: matching producer fired**.
  The probe cannot run in the present QEMU IOMFB model because it
  does not execute these firmware instructions.

  Minimal read-only real-firmware discriminator: key one transaction by
  framebuffer pointer, request `+0x98 == 1`, and the lookup output object
  at `0x04215db8`; confirm `object+0x3d4 == 1` and the
  `+0x3ec` write at `0x04215e40`. At `0x04216d7c..0x04216d9c`
  record `w21`, `w25`, branch outcome, and whether `0x04213980` receives
  that exact pointer. A miss there localizes the first gap to the
  matched-object transfer gate. If it passes, follow that pointer at
  `0x04214338` (list transfer), `0x0420c944` (active install), then
  only the ID-1 producer sites: `0x0420de58` (D582 HOL ID),
  `0x0420ddec` plus `0x04218f8c` (D595 batch ID/send), and the
  `0x0421c228` D594 ID serializer if reached. Correlate the emitted
  FourCC/payload ID with the AP handler/remover and the same
  `fb+0x4138` node; a producer miss after active install localizes
  eligibility, whereas a sent matching callback with no AP unlink
  localizes transport/retirement. This is a probe plan for a target
  running this firmware, **not** a claim about the QEMU replay.
  Score only the first failing edge for this object/ID: null A407 lookup
  output means lookup failed; lookup hit but no `0x04213980` means its
  transfer gate failed; transfer without same-pointer active install
  means queue/promotion failed; active install without an ID-1 D582,
  D594, or D595 producer means producer eligibility failed; an emitted
  ID-1 callback without an AP remover hit means callback delivery or
  dispatch failed; a remover hit without unlink/wake means AP retirement
  failed. A successful unlink/wake closes this chain. Do not infer any
  of these outcomes from QEMU's model-local retained-ID HIT.
- `IOMobileFramebuffer_RemoteCalls.cpp` logs `"IOMFB: failed to serialize
  AFKArray"` (`dcpfw+0x4c0d9e`) and validates argument-array bounds
  (`"%s: indexes: in_args->idx_count (%lu) > in_args->indexes[] count
  (%lu)"`, `+0x4c0de7`) — evidence the call ABI is a generically-typed,
  variable-length in/out argument array (consistent with the publicly
  understood shape of this protocol), not evidence this specific project
  independently reverse-engineered the wire header.
- A **separate**, AFK-framed channel exists for AOP (audio coprocessor)
  integration with the framebuffer: `IOMFBAOPDisplayManagerEPICClient.cpp`
  (`dcpfw+0x4d4a5a`) with log strings `"%s: cannot find AFK endpoint"`,
  `"%s Connected AOP Display-Manager Interface: %p %p"`
  (`dcpfw+0x4d4a7f`-`0x4d4b2e`) — this is Always-On-Display integration
  (AOP renders the always-on clock/widgets and needs to coordinate with the
  main display pipeline), not needed for basic panel bring-up.

**Bottom line for endpoint 0x37**: still not modelled beyond confirming its
firmware-side name and that it is a distinct, generically-typed RPC-array
protocol layered directly on the RTKit mailbox (no AFK ring, no `epic_hdr`).
Building a working impersonation of it needs either (a) full disassembly of
`IOMobileFramebuffer_RemoteCalls.cpp`'s message-header parsing with a solved
vmaddr mapping, or (b) reusing the publicly-documented macOS-era `A000`-style
tag scheme as a starting hypothesis and validating it against this firmware's
`10` surviving literal tags — neither was attempted here.

## Module map (298 `.cpp` files, by rough subsystem)

Full list mined via `grep -io '[A-Za-z0-9_]*\.cpp' dcpfw_strings.txt | sort -u`
against a `strings -a -t x -n 4 dcpfw` dump. Grouped for the minimal-set
recommendation below (a file appearing in a group is not proof every
function in it runs at boot, only that the subsystem exists):

| group | representative files | relevance |
|---|---|---|
| AFK/EPIC transport core | `AFKEP*.cpp`, `AFKEndpointInterface*.cpp`, `AFKDictionary/Array/Number/String/Serialize/Object.cpp`, `afk_interface_transport.cpp`, `afk_messenger_{aop,common,standard}.c`, `common_afk.cpp`, `AFKPlatformExpert.cpp` | **required**, generic to every service |
| RTKit/Tightbeam plumbing | `AFKWorkloop*.cpp`, `AFKEventSource*.cpp`, `AFKMemoryDescriptor*.cpp`, `afkmem_rtkit.cpp`, `afktp_rtkit.cpp`, `pmc.cpp`, `pmgr.cpp`, `pmgr_t8140.cpp`, `rtkpmc_t8140.cpp` | **required** |
| `dcpexpert-epic` | `DCPExpert*.cpp` (8 files incl. `DCPExpertEPClient.cpp`, `DCPExpertSecureInterface{Client,Server}.cpp`, `DCPExpertT8140.cpp`) | likely required (interrupt routing) |
| generic AV stack (`dcpav-*-epic`) | `DCPAV*.cpp` (23 files), `AppleDCPAVPowerController.cpp` | **required** for any panel, internal or external |
| MIPI/DSI internal panel | `AppleDCPSynopsysMIPIDSIController{,Trusted}.cpp`, `AppleDCPMIPIPanel.cpp`, `AppleDCPMIPIPowerController.cpp`, `AppleDCPMIPIT8140UntrustedPowerController.cpp`, `AppleDCPMIPICitra.cpp`, `AppleDCPMIPIDisplaySACController{,Serializer}.cpp`, `DCPMIPIControllerServer.cpp`, `DCPMIPIUntrustedVideoInterface.cpp`, `DCPMIPIVideoInterfaceCommandSerializer.cpp`, `MIPIControllerClient.cpp`/`Server.cpp`, `DCPUntrustedMIPIControllerEPClient.cpp` | **required**, this is the internal-panel path |
| brightness/backlight | `Brightness{EDRHandler,LCD,LCDConversion,OLED,Util}.cpp`, `ControllerIW7042.cpp`, `ControllerLP8549.cpp` | likely required for a usable (non-black) panel; `*OLED*` is the relevant variant on iPhone 16 |
| external DisplayPort/HDMI/USB-C-alt | `AppleDCPDPTX*.cpp` (20 files), `AppleDCPT8112/8120/8122/8132/8140*.cpp`, `AppleDCPDP2HDMI.cpp`, `AppleDCPBaobabExternalDisplay.cpp`, `AppleDCPExternalDisplay.cpp`, `DCPDP*.cpp` (7 files), `AppleDCPMCDP2900/29XX.cpp` (redriver chips), `AppleDCPPS186/190.cpp` | **excludable** for an internal-panel-only bring-up |
| HDCP | `AppleDCPDPTXHDCP{1,2}Controller.cpp`, `AppleDCPDPTXHDCPController.cpp` | **excludable** (external-display content-protection only — no HDCP on the internal panel path) |
| audio (incl. ARC/eARC, AOP) | `AONHPD{Client,Server}.cpp`, `AUC.cpp`, `AOPStatsHandler.cpp`, `CB*.cpp` (11 files — color-balance/ambient tied to AOP), `IOMFBAOPDisplayManagerEPICClient.cpp` | **excludable** |
| CEC | (implied by `dcpav-cec-interface-epic` only — no standalone `.cpp` filename found for it) | **excludable** |
| pixel pipeline / UnifiedPipeline (`UP*.cpp`, ~40 files) | `UnifiedPipeline*.cpp`, `UPPipe*.cpp`, `UPBlock_*.cpp` (24 blocks: gamma, dither, PCC/PCC2D color correction, WPC white-point, VFTG timing generator, GenPipe, etc.), `UPPipeDCP_H17P{,_ext,_int}.cpp` | **required** — this is the actual display-pipe hardware programming behind `iomfb-link`; note the explicit `_int`(ernal)/`_ext`(ernal) split confirms which half matters for a built-in panel |
| MIPI raw-command tooling | `MIPIControllerClient/Server.cpp` overlap with the driver path above; `DCPMIPIToolClient` region only reachable via a separate `mipitool-ep-wl` workloop | tooling, **excludable** |
| color/gamut science | `GainMapHandler.cpp`, `GainTable.cpp`, `GamutMapperHandler.cpp`, `ICCHandler.cpp`, `FxMatrix.cpp`, `UniformityCompensator.cpp`, `TableCompensator.cpp` | likely required for correct-looking output, not for getting *a* picture up |
| display health monitoring | `DCPDisplayHealthMonitor{,Client,Server}.cpp`, `DisplayHealthMonitorClient/Server.cpp` | optional telemetry, **excludable** for first light |
| M3/mailbox/diagnostics | `M3DiagsHandler.cpp`, `CtrlMailboxHandler.cpp`, `MailboxHandler.cpp`, `MailboxChannel.cpp`, `PCC2DMailboxHandler.cpp`, `VideoModeMailboxHandler.cpp` | internal to the M3 pixel-pipeline microcontroller inside DCP, not the AP link — required for the pipeline to run, irrelevant to what we announce over AFK |

### GATE219: DPTX is waiting for an external AV-interface match

The paused DPTX `start()` frame is blocked in
`IOService::waitForMatchingServiceWithToken` with an infinite timeout. Its
captured matching dictionary is:

```text
Location = "External"
IOPropertyMatch = { IOAVControllerUserInterfaceSupported = true }
```

The Boolean was confirmed from the live `OSBoolean` object (`value` byte at
`+0x0c` is `1`). The initial existing-services lookup returned no match, so
the wait is for a later registry notification. This is consistent with the
external DisplayPort/HDMI/USB-C-alt branch classified above, and does not show
that the internal `disp0` MIPI panel path is blocked. Do not treat this wait as
the cause of the missing internal-panel pixels. The more relevant open path is
endpoint `0x37`/`iomfb-link`: after `A353`, the AP has no IOMobileFramebuffer
client to trigger first-client-open, and firmware-originated `D` callbacks
are not modelled yet (see `docs/re/iomfb-link.md`, “Where this leaves the pixel
path”).

### GATE221/224: the internal MIPI AFK service does bind

With `dcpdptx-port-epic`, `dcp-lpdptx-port-epic`, and the two DPTX HDCP services
omitted from the QEMU announce list, the queued `dcpmipi-controller-epic`
PUBLISH is decoded on interface 12. `DCPMIPIControllerProxy::start` returns
success, calls `registerService`, and returns success; QEMU observes the AP's
`REPORT OPEN` for interface 12. So GATE211/212's no-open result was caused by
the announce queue being held by the external DPTX/HDCP path. This closes the
AFK MIPI-proxy-start question, but does not prove MIPI command traffic or
populate BackBoard's missing `ColorElements` registry property; see
`docs/re/quartzcore-render-bounds-followup.md` for GATE223–225.

GATE226 confirmed that after `REPORT OPEN`, no further AFK report arrived on
interface 12 before BackBoard's natural mode query. GATE227 also found no
`IOAVHandlerInternalDisplay::plugEvent_async_gated` call. The current QEMU
EPIC implementation only announces the service and handles AP-originated
commands with a synthetic probe response; it does not generate spontaneous
MIPI reports. Whether the AP requires such a report, and its real payload, are
unresolved. The no-report observation is a limit of this emulator run, not
proof that the physical DCP never sends one.

GATE228 found no entry into the base `IOAVHandler::IOAVEvent` cast/dispatch.
GATE229 found that the internal-display handler's class-specific allocator,
base constructor, setup method, and event-registration helper were also not
entered before BackBoard's query. This moves the AP-side gap earlier than the
event callback: the display handler is not instantiated in this boot. The
GATE230 resolved the five IOAV video-class allocator targets and found no
`IOAVInterface::start` entry. Its allocator breakpoints were installed too
late to exclude earlier allocation. GATE231 moved those targets to the reset-
time breakpoint set; none of the five allocators or the start hook ran before
the natural mode query. The IOAV video-interface instances therefore were not
created in the observed boot window. Next identify the IOKit personality or
provider event that should request one of them; the missing spontaneous
firmware report remains a candidate input, with no payload schema established.

## Minimal set for first light on the internal MIPI-DSI panel

Based on the module map and the confirmed EPICName table:

**Required AFK services to announce:**
- `system-epic` (RTKit-standard first service — bring this up regardless, matches Linux's mandatory `systemep_init()`)
- `dcpexpert-epic` (interrupt routing / health stats — `AppleDCPExpert` already blocks earlier in this project's boot per `docs/re/dcp0-expert.md`, but once past that, this is likely next)
- `disp0-epic` (internal-panel `disp0` node, as opposed to `dispext-service`)
- `dcpav-controller-epic`, `dcpav-device-epic`, `dcpav-service-epic`, `dcpav-video-interface-epic`, `dcpav-power-epic` (the five confirmed-both-sides generic AV services — every `EPICName` here has a real `DCPAVFamilyProxy` class waiting to bind)
- `dcpmipi-controller-epic` (the MIPI-DSI-specific control surface, confirmed both-sides, `IOMIPIFamily`/`DCPMIPIControllerProxy`)

**Plausibly required, unconfirmed on the AP side:**
- `dcpav-sac-epic` (spread-spectrum/backlight coordination — unclear if the panel stays dark without it)
- Endpoint `0x37`/`iomfb-link` itself, which is not AFK at all and is not modelled by this document (see above) — **this is very likely the actual hard blocker for getting pixels out**, independent of which AFK services are announced

**Excludable for internal-panel-only bring-up** (all evidence above): every
`dcpdptx-*`/`dcpdp-*`/`dcpdptx-hdcp-*` service (external DP/HDMI/USB-C-alt +
HDCP), `dcpav-audio-interface-epic`, `dcpav-cec-interface-epic`,
`dcpav-audio-arc-epic` (audio/CEC/eARC), `aop-test-control-epic`,
`cb-ap-to-dcp-service-epic`, `cbrootservice-epic` (AOP/ambient-color,
cross-coprocessor features), `powerlog-epic` (telemetry), `test-epic`/
`static-epic` (unclear purpose, low risk to omit first).

## Open questions

- **No numeric group/command opcode was recovered for any service.** This
  needs either a solved vmaddr↔fileoff mapping for `dlon#2`/`dlon#3` (this
  project fitted per-segment biases empirically for `__TEXT`/`__DATA` of one
  image only, and did not attempt `LC_SYMTAB`/`LC_DYSYMTAB` resolution — see
  "Bundle layout") or a from-scratch structural parse of the `dlon`
  Mach-O load commands against a byte-exact model of Apple's firmware
  packer, which is out of scope for a strings-first pass.
- **`dcpexpert-epic`, `disp0-epic`, `system-epic`, `test-epic`,
  `static-epic`, `powerlog-epic`, and the `cb-*`/`aop-*` services have no
  confirmed `EPICName`-matching `IOKitPersonalities` entry** in this
  kernelcache. Either they match some other way (raw 32-byte announce name,
  a class this project's plist grep missed, `IONameMatch` instead of
  `IOPropertyMatch`) or XNU genuinely never surfaces them past
  `AFKEndpointInterface`. Settling this needs disassembling
  `AFKEndpointInterface::probe`/`start` in `com.apple.driver.AppleFirmwareKit`
  to see every matching strategy it tries, not just the `IOPropertyMatch`
  case this investigation grepped for.
- **Endpoint 0x37's actual header/opcode format** is still unknown beyond
  "a generically-typed in/out argument array, not AFK-framed" — see
  "Endpoint 0x37" above for what would settle it.
- **The wire-level subtype and payload for `sendOpenReport`/`sendCloseReport`**
  remain unresolved. `sendPublishReport` most likely maps to iOS 27
  `REPORT/PUBLISH 0x11` based on the AP receiver and matching name, but the DCP
  indirect target and serialized bytes have not been traced. Do not apply the
  older Linux `ANNOUNCE 0x30` value to this build.
- **Whether Tightbeam (the `dlon#2`↔`dlon#3` trusted/untrusted IPC this
  investigation found) has any AP-visible surface at all**, or is purely
  internal to the coprocessor. If purely internal, our model never needs to
  implement it — but this was not confirmed either way.
- **`mipi_displaytiming_s`'s exact 14-field order** (pixel clock vs. h/v
  active/porch/sync placement) was not decoded — the type encoding gives
  count and primitive type (`I` = `uint32`) only, not field names.
- The `dlon#1` image (tiny, `vmaddr 0xffffffff00000000`, no symbols) was not
  investigated at all — plausibly the earliest-stage boot/monitor code, but
  this is a guess from section names (`__head`, `__tunables`,
  `__kernel_stack`) only.

### Asahi Linux cross-check (2026-09-29)

The local `/Users/steve/github/linux` checkout was read-only inspected at
`77cb8f24c`. Its DCP driver makes the AP-visible transport boundary explicit:
RTKit endpoints `0x28` (DPAVSERV), `0x29` (AV), and `0x37` (IOMFB) are distinct
(`drivers/gpu/drm/apple/dcp-internal.h:36-46`); `dcp_recv_msg` routes them to
different handlers (`dcp.c:222-245`). AFK then reads a DCP-produced shared
receive ring whose entries carry a separate channel field
(`afk.c:587-617,655-676`). An AFK “interface 12” should therefore not be
equated with RTKit endpoint 12, and an absent report on that AFK channel says
nothing by itself about IOMFB endpoint `0x37` or an internal DCP-task buffer.

The comparison gives a concrete, separate lead for the missing
`ColorElements` publication. Asahi's IOMFB callback tables map the property
transfer handlers to IDs 122/123/124 on 12.3 and 126/127/128 on 13.5
(`iomfb_v12_3.c:57-60`, `iomfb_v13_3.c:62-65`). For this iOS 27.0 H17P build,
the symbolicated `AppleMobileDispH17P-DCP` kext independently identifies
D126/D127/D128 as `UnifiedPipeline::setDCPAVPropStart/Chunk/End`; disassembly
confirms the layouts: a `u32` size, chunks of up to 0x1000 bytes with offset
and length in a 0x1008-byte request, then a 64-byte key. Each callback also
requires a four-byte status output. See `docs/re/iomfb-dseries.md` for handler
addresses and the input checks.

Asahi's AP handlers parse `TimingElements` and `DisplayAttributes`. They also
retain raw blobs for four recognized debugfs keys—`ColorElements`,
`TimingElements`, `DisplayAttributes`, and `Transport`—under each DRM
connector (`connector.c:24-75,101-114,134-151`). They do **not** preserve every
unknown dictionary key; the pasted notes' broader claim is not supported by
this code. On an Asahi machine the raw `ColorElements` blob can be read from
the connector's DRM debugfs directory, if that DCP publishes it. The Linux
parser uses its older binary dictionary format, while the H17P D128 path has
an `OSUnserializeXML failed` error path, so an M1/M2 blob is a useful key and
shape reference but not a valid iOS 27 payload to copy blindly.

The local `/Users/steve/github/m1n1` source supplies a second, live trace
route. `DCPClient` starts endpoint `0x37`; `DCPManager.handle_cb` dispatches
the property tags to `setDCPAVPropStart/Chunk/End` (`fw/dcp/client.py`,
`fw/dcp/dcpep.py`, `fw/dcp/manager.py:190-210`). These are DCP-originated
callbacks into the AP manager, not AP commands that request a named property.
The manager accumulates chunks and parses them with `OSSerialize`, so it is
not a suitable decoder for the H17P XML path. `proxyclient/hv/trace_dcp.py`
can trace endpoint `0x37` traffic, but this local checkout has no
`TRACE_DCP_IOMFB_PROP_LOG` raw JSONL hook or `parse_prop_capture.py`.
Earlier notes claiming those tools were installed are stale. A matching
hardware capture would need raw D126-D128 callback inputs and ACKs before
the semantic parser, plus D120 and D130 ordering markers.

The local H17P DCP task also contains the producer itself. In the larger-task
Mach-O at `/tmp/dvm/gate232-firmware/machos/dcpfw.macho`, code at `0x4288a60`
collects five per-display property objects, and the block at `0x42893ac`
publishes each non-null object through the transfer helper at `0x4289694`.
The property order is `ColorElements`, `TimingElements`,
`PreferredTimingElements`, `DisplayAttributes`, then `Transport`. The helper
constructs D126/D127/D128 with the AP-verified layouts. A full direct-call
scan found two callers: an unnamed path that may send D123 and conditionally
D130, and `UnifiedPipeline::start_ios_gated()`, which sends D107-D112 then
D120 and publishes only after the D120 reply bit and a later state check pass.
D130 maps to `UnifiedPipeline::update_ext_pipe_count(bool, bool, uint32_t)`;
its flag meanings remain unknown, and D130 is not a prerequisite on the
`start_ios_gated()` path. See `docs/re/iomfb-dseries.md` for exact gates, wire
arguments, and the next QEMU-only ordered callback experiment.
This path identifies the DCP producer without linking it to the separate
`self+0x60` buffer.

The local `asahi-fairydust-display` repository is a Fedora Asahi kernel build
and external DisplayPort resume patch; it contains no IOMFB property producer
or callback implementation. It could help obtain raw connector debugfs data
on supported M1/M2 hardware, but it does not execute the target iPhone17,3
H17P firmware.

The matching callback names and the static producer do not prove the producer
runs during the observed QEMU boot. The QEMU baseline records zero naturally
generated endpoint-0x37 callbacks, its explicit callback harness is synthetic,
and the model does not run the DCP RTKit task. With the known-good dev6 boot
inputs, the ordered synthetic D107-D112-then-D120 sequence returned
`1,1,0,1,0,1,0`; an LLDB trace identified the AP-side A475 query as the direct
reason D120 was zero. Setting the model's A475 output to `01` flipped only the
D120 result to `01000000`, a diagnostic positive control rather than a
hardware-derived response. The next barrier is the DCP-side provider vtable
check at `+0x130` and the actual population and transfer of all five property
objects. P197 identifies that receiver as the provider retained by
`AppleDCPDisplayService`; its dynamic class and slot implementation are still
unknown. A real H17P/iOS 27 callback capture is the shortest route to
valid D126-D128 payloads and ACKs; a hardware trace is needed to confirm which
producer path runs. This remains separate from the `self+0x60` watchpoint;
current evidence does not connect those paths.

### Offline MIPI timing input trace (2026-09-29)

The target H17P DCP firmware identifies the MIPI setup method as
`AppleDCPSynopsysMIPIDSIController::handleStart(AFKService *)` (name string at
VA `0x44782c5`; method entry at `0x4132adc`). This gives a concrete input path
for the internal panel's timing record:

- `handleStart` formats property names through helper `0x4133ee8`, whose
  format string at VA `0x4478386` is `"%s-%u"`. The base keys include
  `display-timing`, `mipi-config`, `display-timing-lpm`, and
  `mipi-config-lpm` (literals at `0x43c4f9c`, `0x43c4fab`, `0x43c4fb7`, and
  `0x43c4fca`). A zero selector uses the unsuffixed base key; a nonzero
  selector formats names such as `display-timing-N` and `mipi-config-N`.
  The selector's semantic source is not yet identified.
- The normal timing lookup begins at `0x41335c8`; its record pointer is saved
  at `self+0x7e8` (`0x4133680`). The low-power lookup begins at `0x4133894`
  when the controller's low-power flag is set, and saves its record pointer at
  `self+0x7f0` (`0x4133944`). The corresponding MIPI-config values are stored
  separately at `self+0x7f8` and `self+0x800` (`0x41337d0`, `0x4133be8`).
- `getDisplayTiming` is at VA `0x413a190` (signature string at `0x4478be5`).
  Option bit 0 selects `self+0x7e8` or `self+0x7f0` (`0x413a230`–`0x413a244`),
  then the method copies 56 bytes to its output (`0x413a244`–`0x413a25c`).
  This matches the 14-`uint32` `mipi_displaytiming_s` encoding at
  `0x4477d3c`; field order remains unknown.

The target `/arm-io/disp0` ADT is not empty of timing data. It contains scalar
panel properties including `frame-duration-nclks=0x5e5a6`,
`frame-duration-lpm-nclks=0x44f10`, `subframe-duration-nclks=0x61b4d`,
`subframe-interrupt-time-lines=0x9be`, `display-lead-time-nclks=0x6ac5`,
`start-t-nclks=0x4b0`, `swmpr-t-nclks=0x12c0`, and `piodma-t-nclks=0x8ca0`.
Neither target system nor restore ADT has `display-timing`,
`display-timing-lpm`, or `mipi-config` properties under `disp0`; the only
`display-timing*` match is the separate external-display `display-timing-info`
under `dispext0`. That 32-byte value and its framebuffer dimensions are a
separate external-display path. The reconstructed
larger-task Mach-O's `APPCONFIG` segment is zero-filled, so it supplies no
missing internal-panel record either.

The AP-side `AppleSynopsysMIPIDSIController::start(IOService *)` kext also
looks up `display-timing`, `display-timing-lpm`, and `mipi-config-lpm` on its
provider (code at `0xfffffff0096f1bd4`, `0xfffffff0096f1c34`, and
`0xfffffff0096f1c90` in `/tmp/dvm/gate186-kexts/com.apple.driver.AppleSynopsysMIPIDSI`).
This is a useful lead toward the registry object that supplies the firmware's
indexed records, but no direct code or trace yet connects that AP-side provider
to the `AFKService` property object consumed by DCP `handleStart`.

This static path can be traced without an iOS 27 physical device. The remaining
offline target is to identify who constructs the indexed property dictionary
and what `N` denotes, then determine whether the ordinary ADT scalars feed that
construction. A physical H17P/iOS 27 trace is still needed to confirm runtime
population and capture the actual D126-D128 payloads; copying the external
display timing blob or inventing a 14-word record would not establish those
values. This MIPI setup path is not yet linked to the separate
`UnifiedPipeline::start_ios_gated()` property-publication path or to the
missing `ColorElements` observation.

### Poster `_scene` writer coverage: CONFIG3 succeeds; CONFIG5 stopped pre-home (2026-10-04)

On continuous child `UI_CLASS16_REFRESH60_DM_LOCK_CONFIG3`, the
`updateConfiguration:` block (`0x201f37e68`) ran with a non-null controller
at `0x201f37e98`. Scene lookup returned candidate `0x794222bc00` in `x20` at
`0x201f37edc`; the strong-store call at `0x201f37fdc` received `_scene` field
`0x7940dbd618` in `x22`, whose prior value was nil. Six later
`_updatePosterScenes` visits took the non-null arm `0x201ed1968`; the 19 null
arm hits preceded the store. Thus the first nil writer edge is closed for
this boot: there was no nil controller, scene lookup, or skipped store.

MercuryPosterExtension then supplied a non-null CA root for context
`0xe6e42442`, matching the context ID and non-null scene stored by SpringBoard
`setScene:`. The first A407 sampled after that join was ID 12 and its host
frame was uniformly black. The display-side accept/prepared/draw result was
not scored, so the screenshot does not classify poster content.

The CONFIG3 hosted-resolve log cap used the LayerHost's local `+0x1c` value
(118) as if it were the resolved CAContext ID. It suppressed every resolver
row after eight unrelated controls, before examining the resolved Context*.
The callback now filters later resolver **returns** by the actual resolved
context ID, and the watcher can require poster-context display acceptance
before capturing A407. The follow-up `UI_CLASS16_REFRESH60_DM_LOCK_CONFIG5`
used the same repaired parent and exact patched boot bundle, but was stopped
after 600 seconds with the matched SpringBoard migration check-in call
`0x19a6a36a0` still open and no return at `0x19a6a36a4`. It had no panic and
never reached home or the poster sites; its silence is not an attachment
miss. The post-join draw question remains open pending a child that reaches
the post-home window with the corrected resolver capture.

Artifacts: CONFIG3 `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG3.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG3.poster-join.json`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG3.stderr.log`; CONFIG5
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG5.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG5.probe.out`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG5.serial.log`. D572,
D582, and A407 scanout remain QEMU hypotheses; no model, fixture, or guest
state changed.

### LOCK_CONFIG6: corrected resolver capture missed the post-home window (2026-10-04)

On fresh child `UI_CLASS16_REFRESH60_DM_LOCK_CONFIG6` from the same D5825
parent and unchanged CLASS16/ECHO/D572/D582/A407-scanout stack, the matched
SpringBoard migration check-in `0x19a6a36a0` remained open with no return at
`0x19a6a36a4` for 600 seconds. There was no `DMPerformMigration` return,
home, config-writer hit, Mercury root, scene, or display accept; no XNU panic
was recorded. The stop and LLDB detach therefore represent pre-home coverage
loss, not a poster attachment miss. This duplicates CONFIG5's bounded
pre-home outcome and does not change CONFIG3's positive `_scene` store and
same-context scene attachment result.

The corrected resolved-context filter and display-accept watcher have not
yet been exercised on a successful post-home child: CONFIG3's black ID-12
frame remains unclassified at the server prepare/draw stage, while CONFIG5
and CONFIG6 did not reach that stage. Artifacts:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG6.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG6.poster-join.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG6.stop`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG6.probe.out`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG6.serial.log` /
`stderr.log`. No model, parent, fixture, or guest state changed.

### LOCK_CHECKIN2: BKS handoff queued behind an unchanged lane head (2026-10-04)

`UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN2` is a fresh child of the repaired
D5825 parent with the same CLASS16/ECHO/D572/D582/A407-scanout hypothesis
stack and A408 completion/scanout off. BKS receive/handoff, the entry-triggered
DataMigrator ladder, corrected poster callbacks, and hidden-cull exclusion
were armed before continuing the guest. ID-1 `SwapWait(1,1)` returned zero in
4.9 ms. SpringBoard then entered migration check-in at runtime `0x19acc76a0`
(static `0x18a1476a0`); return `0x19acc76a4` was not observed.

The initial server path did run: backboardd's migration listener at
`0x102aa64d4` and `configureConnection` at `0x102aa6610` returned on the same
thread in about 33 ms and 17 ms. The server took the child-activation
immediate branch, enqueued block `0x105586268`, and called
`xpc_dictionary_handoff_reply`. The resulting continuation `0x74f2dd12c0`
was pushed onto lane `0x74f31a6c80`. At +3.37 s, its queue chain was
`0x103a01280 → 0x103a012c0 → 0x74f2dd12c0`; the same order appeared at
+12.57 s and +54.01 s. No matched lane wake/invoke/serial-drain, BKS child
block invoke, concrete check-in handler, or SpringBoard check-in return fired
before the bounded stop. Thus the first open edge is **dispatch-lane service
of the queued BKS continuation**, behind two older items. Why those older
items do not drain remains unproved; this does not establish permanent queue
starvation or a missing server reply.

`DMPerformMigration` also entered on backboardd thread pointer
`0xffffffdfa8733830` and reached environment/device-mode returns; the BKS
server handoff ran on `0xffffffdfa8735120`, so this DM entry is not proven to
serve that exact check-in. Its entry-triggered ladder did not observe a
`DM_PERFORM_RETURN`. An earlier unpaired XPC-return marker is not counted as
migration completion.
No home, poster scene, display accept, or post-home A407 was scored; there was
no panic. This is a pre-home handoff result, not a poster/draw classification.
The probe reported a frozen guest, but the HMP socket refused a connection
after the driver exited; the qcow2 child and logs remain available, while a
live paused guest is not confirmed.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN2.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN2.stop`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN2.slide.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN2.probe.out`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN2.serial.log` /
`stderr.log`. `LOCK_CHECKIN1` was an unscored callback-registration failure
stopped in early boot and contributes no guest-path evidence. D572, D582, and
A407 scanout remain QEMU hypotheses; model, parent, and fixture were unchanged.

`UI_CLASS16_REFRESH60_DM_LOCK_CONFIG7` repeated the corrected-resolver
capture on another fresh D5825 child. The matched BKS migration check-in
entered at `0x19f6c76a0` and remained open without a return at
`0x19f6c76a4` for 600 seconds. `DMPerformMigration`, home, config writer,
Mercury root, scene join, and display accept were not observed; there was no
XNU panic. The bounded stop detached LLDB, so the poster watcher's detach
label is not a poster result. This is a third pre-home coverage miss after
CONFIG5/6, not a draw classification and not a contradiction of CONFIG3's
positive `_scene` store and matching context attachment. The corrected
display-accept → prepare/OGL question remains open until a child reaches the
post-home window.

Artifacts:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG7.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG7.poster-join.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG7.stop`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG7.probe.out`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG7.serial.log` /
`stderr.log`. No model, parent, fixture, or guest state changed.

### LOCK_CHECKIN3: fresh child failed before EL0 probe attach (2026-10-04)

The attempted fresh D5825 child, `UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN3`,
did not reach SpringBoard or the BKS instrumentation. At ignition level 1,
AMFI rejected `/System/Library/Caches/com.apple.dyld/dyld_shared_cache_arm64e.01`
with `unsuitable CT policy 0`; launchd then panicked because
`/usr/lib/libSystem.B.dylib` was unavailable without a loaded dyld cache.
The live-slide resolver consequently found no cache-backed EL0 PC and the
check-in/drain callbacks never armed. This is a boot-coverage failure, not a
new lane or migration result. The child was based on
`/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825.qcow2`; no
parent, fixture, model, or guest state was changed.

The retained `LOCK_CHECKIN2` queue bytes do identify the older work. At the
matched handoff, lane `0x74f31a6c80` had head `0x103a01280`, then
`0x103a012c0`, followed by migration continuation `0x74f2dd12c0`. Both old
items carried `flags=0x114` and `_dispatch_call_block_and_release`; their
block invoke PCs resolve to `0x18f8d818c`
(`BSXPCServiceConnection _lock_activateNowOrWhenReady:` pending-events
block) and `0x18f8d68dc` (its `connectionActivated:` block). The target lane
was `0x74f31a5d00`; its sampled state `0x6000a60001cb25` had the enqueued
bit set, dirty bit set, and drain-owner bits `0x1cb24`. The continuation was
therefore appended behind two activation blocks on an already-owned/enqueued
target lane. This points away from a missing wake as the first explanation.

The reason the target owner remains the leading blocker comes from the
separate, earlier `UI_CLASS16_REFRESH60_BKS_OWNER_FROZEN1` frozen snapshot,
not from a same-boot thread match in CHECKIN2. There, owner bits `0x17918`
matched live backboardd pthread port `0x1791b`; its saved stack was in
`_dispatch_lane_serial_drain` / `_dispatch_lane_invoke`, running
`__DMPerformMigration` and blocked in
`DMXPCConnection sendMessageSync` → `_xpc_connection_send_message_with_reply_sync`
→ `_mach_msg2_trap`. The associated DataMigrator worker waited synchronously
on its main queue, whose main thread was at a semaphore wait. Together these
artifacts support a **head-of-line blocked active drainer in the migration
XPC path**, which prevents the queued activation blocks and check-in
continuation from being invoked. The owner-port value in CHECKIN2 differs;
the current boot's exact owner-to-thread/XPC stack pairing remains unmeasured.

The aggregate probe now prints `DM_LOCK_CHECKIN_QUEUE_IDENTITY` with each
continuation's invoke PC and target lane state. Its flags decoder uses the
low 32 bits of the item header. Syntax check passed. The next valid capture
needs a cache-accepted boot, then a frozen same-boot owner-port-to-thread
match; poster accept/draw remains out of scope until check-in returns and
home is reached.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN3.probe.out`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN3.slide.json`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN3.serial.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN3.stderr.log`, and
`/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-checkin3.qcow2`. No BKS
callback fired in CHECKIN3.

### LOCK_CHECKIN5: same-boot lane owner matches the DM/XPC stack (2026-10-04)

`UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN5` was a fresh child of the repaired
D5825 parent. CHECKIN3/4's generic merged trust-cache selection failed AMFI;
this run explicitly used the known cache-accepted
`/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`. Serial confirmed
`dyld cache mapped system-wide`, and the LLDB hooks resolved/armed.

At the matched BKS migration handoff, the target lane state was
`0x6000a600002a21` (owner bits `0x2a20`, enqueued and dirty). The frozen
same-boot `backboardd` snapshot maps those bits to pthread Mach port
`0x2a23` (`pthread+0xf8`, masked to `0x2a20`) on XNU thread
`0xffffffec684958f0`. Its saved user stack is in dispatch's serial-drain
path and includes the DataMigration XPC synchronous-send chain:
`DM_XPC_SYNC_RETURN` return site `0x25e8f579c` →
`DMConnection` send return `0x25e8f4bac` → migration-client return
`0x25e8f25b8` → `__DMPerformMigration` controller path `0x25e8f03fc`,
with the lower frames returning through the lane drain. This is same-boot
owner→thread→stack evidence for the HOL mechanism; it is not CHECKIN2's
owner port transplanted to this run.

The handoff's outer queue was observed empty by +82 ms, while the target
lane still carried the matched owner state in the frozen snapshot. This
does not show that the two activation callbacks ran: no
`SERVER_BS_CHILD_BLOCK_INVOKE`, `BKS_MIGRATION_CHECKIN_RETURN`, or
`DM_PERFORM_RETURN` hit was recorded before the condition stop. One
`DM_XPC_SYNC_RETURN` hit at `t=1791164536.900` precedes the observed
`DM_PERFORM_ENTRY` at `t=1791164536.961`; do not pair that return with this
later entry or treat it as the check-in reply.

The same frozen snapshot found DataMigrator PID 120's main-thread port
`0x103` at a semaphore wait (wait event `0xfffffff027011654`, saved frame
`0x100ee55cc`) and worker port `0x2703` in a dispatch-sync wait with
DataMigrator/XPC handler frames. This matches the main-queue/semaphore
dependency seen in `BKS_OWNER_FROZEN1` and is the leading explanation for
the missing DM reply, but this capture does not pair that exact semaphore
to the particular BKS request. Check-in return and home were not reached;
poster accept/draw remains unscored.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN5.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN5.mem/launch.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN5.mem/process-stacks.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN5.mem/ram/`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN5.slide.json`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN5.serial.log` /
`stderr.log`. No model, fixture, parent, or guest state was changed.

### LOCK_CHECKIN6: matched request remains inside DataMigrator migration work (2026-10-04)

`UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6` used a fresh D5825 child and the
known cache-accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`.
The fixed D582-hypothesis / CLASS16 environment was unchanged. All composed
LLDB modules imported and armed; the run stopped frozen at its 900-second
bound with no panic or shell progress.

This capture closes the request-to-worker gap. Backboardd's matched
`DM_CLIENT_SYNC_CALL` at shared-cache site `0x269af5798` occurred at
`t=1791165886.189` on thread `0xffffffec9228b830`, with connection
`0x7772c76d00` and message `0x77731a07e0`. DataMigrator then entered
`DM_SERVER_HANDLE_ENTRY` at `t=1791165886.263` on thread
`0xffffffec92220950`; the server's `getInt64("msgID")` reached the msgID-0
arm at `t=1791165886.276`. That same thread entered the migration call,
delegate, and `DM_START_MIGRATION_CALL` at `t=1791165886.294`. At the frozen
snapshot, thread `0xffffffec92220950` was still asleep in the dispatch-sync
path whose queue argument resolves to the label
`com.apple.datamigrator.migration`. Its saved stack still contained the
DataMigrator migration/server frames. This is a same-request handler and
worker match, rather than a temporal match to an unrelated earlier XPC
return.

The synchronous response edge never appeared: no matched
`DM_START_MIGRATION_RETURN` (`0x102b4bfcc`), `DM_MIGRATION_RETURN`
(`0x102b3ea90`), reply-block call (`0x102b3eb40`), server fallback return,
backboardd `DM_CLIENT_SYNC_RETURN` (`0x269af579c`),
`DM_PERFORM_RETURN`, or SpringBoard BKS check-in return was recorded.
Thus the first missing reply edge is after DataMigrator accepts msgID 0 and
enters `startMigrationAndOrBlockIfNecessary...`, before that migration
routine returns and the XPC reply is delivered. The handler was scheduled;
this is not a missing server receive.

The frozen backboardd thread is the same 0xffffffec9228b830 that made
the captured send. Its saved frames include the sync-send return chain
0x269af579c -> 0x269af4bac -> 0x269af25b8 -> 0x269af03fc, confirming
that this client call remained open at the snapshot.

The same frozen DataMigrator main thread `0xffffffec92687060` was waiting
at the known semaphore site in `xpc_connection_send_barrier`, with saved
`x20=0x7c0b0663a0` and `x22=0x7c0b04ca00`. Barrier 9 on that connection
captured the same semaphore, queued dispatch-Mach barrier item
`0x103901340` on channel `0x7c0b03e3a0`, and entered an infinite semaphore
wait at `t=1791166521.726`. Unlike the preceding barrier on the same
semaphore, this final barrier had no observed lane invoke, barrier invoke,
signal-block invoke, or semaphore signal before freeze. This pairs the main
thread's semaphore with a concrete outstanding DataMigrator barrier while
the matched request worker is blocked; it does **not** by itself prove the
barrier is waiting on this request or that the two waits form a deadlock.
The worker's observed sync target is the named migration queue, not a
proven main-queue target. The exact dependency between the outstanding
barrier and the request's migration block remains unresolved.

The decisive classification is therefore: **request handler ran; matched
migration work is still in dispatch-sync; the main thread is parked on an
unsignaled XPC barrier; no migration reply was sent.** This is consistent
with a DataMigrator cross-queue dependency, but the present evidence does
not justify calling it a proven cycle. Check-in return, home, poster, and
draw remain unmeasured on this child. D572/D582 remain QEMU hypotheses;
the display fixture and model were unchanged.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6.mem/launch.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6.mem/process-stacks-datamigrator.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6.mem/process-stacks.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6.mem/ram/`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6.slide.json`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6.serial.log` /
`stderr.log`. The composed read-only probe is
`tools/re/dm_lock_checkin_reply_callbacks.py`; no model, fixture, parent, or
guest state was changed.

#### LOCK_CHECKIN6 offline migration-queue owner decode

The frozen RAM image resolves the matched handler's saved queue argument
`x0=0x10348e2e0` to `com.apple.datamigrator.migration`. At the 900-second
freeze the queue had head `0x16d461ff0`, tail `0x16d605fd0`, state
`0x60002500000e01`, and chain
`0x16d461ff0 → 0x16d691ff0 → 0x16d4edf90 → 0x16d605fd0`. The head
`0x16d461ff0` is stack-resident in the blocked handler's stack range; its
`+0x40` word is runtime `0x102b450d8`, executable offset `0x90d8` from the
recorded image base, matching the no-op block invoke passed to the sync.
That strongly identifies the head as the waiting sync item itself, so no
older queued item is ahead of it in this list. The three following items
are queue contents, not assigned as the blocker.

The queue's low owner token `0xe00` matches the `0xe03` thread-port-shaped
field at `cthread_self+0x18` for DataMigrator thread `0xffffffec92687830`.
DataMigrator main has `0x103` at that same field, matching its independently
identified port. The owner thread's saved frame `0x102b4f76c` minus the
recorded executable base `0x102b3c000` is offset `0x1376c`, the return site
following the call at `0x13768`. Same-build static/live RE identifies that
call as `runPluginAtPath:withIdentifier:parameters:completion:` (plugin
launch over XPC). CHECKIN6 did not capture its arguments, so no plugin name
is newly attributed from this freeze.

The migration `dispatch_sync` is therefore waiting for its serial queue's
active owner to finish; the owner remains inside that plugin-launch call at
`0x13768`, with no return past `0x1376c`. This is outcome **C**: active
plugin/XPC work is the first identified non-returning edge, rather than a
missing wake or an unscheduled handler. Earlier same-build traces followed
the same call into `DMMigrationPluginWrapperConnection`'s synchronous send
and SystemAppMigrator/installcoordination work, but those deeper helper
stacks are not paired to CHECKIN6. Barrier 9's main-thread wait remains
unlinked to this call; the evidence does not prove a deadlock cycle. The
smallest next discriminator is the plugin-launch XPC request/reply paired
with the wrapper-side request and return at `0x1376c`. No migration reply,
check-in return, or home was reached, so poster/draw remain out of scope.

This queue/owner decode used only the preserved CHECKIN6 RAM and the saved
DataMigrator process page-table root. It did not resume or modify the VM.

#### LOCK_PLUGIN4: live plugin identity after the matched msgID-0 request

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN4` used a fresh child of
`/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825.qcow2`,
the cache-accepted `bp-patched-dev6/system.tc`, and the unchanged CLASS16 /
D572+D582 hypothesis environment. The read-only plugin probe armed its
`0x13768` / `0x1376c` sites after the matched `DM_START_MIGRATION_CALL`.

The live DataMigrator owner entered `0x13768` with path
`/System/Library/DataClassMigrators/SystemAppMigrator.migrator`, identifier
`com.apple.MobileInstallation.SystemAppMigrator`, parameters pointer
`0x75270449a0`, and completion pointer `0x16f7959e0`. About 10 ms later the
same owner entered `DMMigrationPluginWrapperConnection`'s
`sendMessageSync` site (`0x25e8f1f88`); its request `0x7526c58360` contained
`pluginIdentifier=com.apple.MobileInstallation.SystemAppMigrator`. This
connects the live `runPluginAtPath:` arguments to the wrapper-side synchronous
request on the same owner/thread.

That synchronous call had no matching `CLIENT_SYNC_RETURN`, and the
`runPluginAtPath:` return site `0x1376c` was not hit during the following
~107 seconds. The wrapper process showed a connection initialization and
unclassified XPC traffic; a `CLIENT_WRAPPER_MESSAGE` arrived on the same
wrapper-connection receiver, but its fields did not identify it as the
completion for this request. No plugin-ID-matched wrapper receive/reply was
captured, so the wrapper reply edge remains open. A separate
`MergeBuddyProvisioningResponse.migrator` launch ran on a different owner and
is not attributed to the SystemApp request.

The run stopped on `DM_START_MIGRATION_RETURN` at `t=1791171088.056`, but
that hit's thread pointer (`0xffffffe1fe807830`) differs from the
`runPluginAtPath:` owner (`0xffffffe1fe743060`). It is therefore not paired
to this plugin call and is only the probe cutoff. `DM_MIGRATION_RETURN`, the
matched wrapper sync return, the `0x1376c` return, reply-block invocation,
backboardd sync return, and SpringBoard check-in return were not observed
before that cutoff. Do not infer a completed migration, a permanent plugin
hang, or a deadlock from this run; barrier 9 remains unlinked.

The first three launches on the same owner did return through both wrapper
sync and `0x1376c`: ContainerMigrator (~2.9 s), 00LaunchServicesMigrator
(~3.9 s), and MSUDataMigrator (~1.5 s). Thus the new evidence narrows the
long-running candidate to the SystemAppMigrator request in this boot, but
does not prove why its wrapper reply is outstanding. Logs are
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN4.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN4.events/`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN4.serial.log` /
`stderr.log`. The probe now writes identifier-scoped success markers for
plugin entry/return and wrapper sync/reply so later stop conditions can avoid
unrelated plugin events. Poster/draw remain out of scope; D572/D582 remain
QEMU hypotheses.

#### LOCK_PLUGIN5/6: scoped reply follow-up coverage misses

Two fresh children were started from the same D5825 parent with the known
`bp-patched-dev6/system.tc` and unchanged CLASS16/D572/D582 hypothesis setup.
The plugin probe was configured to stop only on the identifier-scoped
SystemAppMigrator wrapper-reply, wrapper-sync-return, or `0x1376c` markers;
the unpaired `DM_START_MIGRATION_RETURN` was not a stop condition.

Neither child reached the matched DataMigrator/plugin window. In PLUGIN5,
launchd reported `rebooting due to critical process crashes: SpringBoard`
before a migration/plugin callback; shutdown then ended in
`Halt/Restart Timed Out`. In PLUGIN6 the same launchd SpringBoard critical
crash occurred before any `DM_START_MIGRATION_CALL`, `0x13768`, or wrapper
marker. The supplied trust cache was accepted by launchd in both; these are
boot-coverage failures, not evidence about SystemAppMigrator or its reply.
No wrapper/installcoordinationd/installd stacks were sampled because there
was no live matched request to key them to.

Artifacts:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN5.lldb.log` and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN5.serial.log` /
`stderr.log`; likewise the `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN6` logs and
events directory. The earlier PLUGIN4 evidence remains the latest positive
identity result: SystemAppMigrator's matched synchronous send had no observed
return for ~107 seconds, but its wrapper reply is still unresolved. The
smallest useful next run is a cache-accepted child that reaches that exact
request; keep the ID-scoped markers and, at a bounded freeze, correlate its
wrapper receiver/message to the DataMigrator receiver before sampling
installcoordinationd or installd. Do not infer deadlock, permanent plugin
hang, or barrier-9 linkage from these coverage misses.

#### LOCK_PLUGIN7: bounded crash diagnosis, still no target request

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN7` repeated the repaired D5825 parent,
known cache-accepted `bp-patched-dev6/system.tc`, CLASS16, and D572/D582
hypothesis setup. It stopped on launchd's SpringBoard critical-crash line;
the plugin reply markers remained scoped to
`com.apple.MobileInstallation.SystemAppMigrator`. The cache was validated
and sent to launchd, and the guest reached `Early boot complete`, but no
matched `DM_START_MIGRATION_CALL`, `0x13768`, or plugin callback appeared
before `rebooting due to critical process crashes: SpringBoard` at
20:55:08.102627. PLUGIN7 therefore adds no evidence about the wrapper reply.

The first explicit serial error immediately before the restart was
`_process_matches_constraint: error: Requirement not satisfied: Constraint
not matched`. The captured postmortem reports zero XNU panics and contains no
SpringBoard `OS_REASON` or process-specific exit payload. The constraint line
is only the earliest visible error, not a proven cause. The separate
`docs/re/springboard-crash.md` note records a prior SpringBoard exit reason
for missing `/usr/lib/libobjc-trampolines.dylib` in the boot image; PLUGIN7
did not capture that reason, so it remains an unconfirmed candidate for this
child. No image or guest state was changed.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN7.postmortem.txt`,
`.threads.txt`, `.ramscan.txt`, `.lldb.log`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN7.serial.log` /
`stderr.log`. PLUGIN4 remains the high-water mark: its matched
SystemAppMigrator `sendMessageSync` had no observed scoped return for about
107 seconds. Further work should first restore that live request window, then
stop on its identifier-scoped wrapper reply, client sync return, or `0x1376c`;
do not sample helper stacks without the matched request.

#### LOCK_PLUGIN8: live SystemApp request restored; no scoped reply at bound

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8` used the D5825 parent,
`bp-patched-dev6/system.tc`, and the same CLASS16/D572/D582 hypothesis
environment. The launch file imported `dm_lock_checkin_plugin_callbacks.py`
from boot, correcting the callback-module mismatch present in PLUGIN5–7.
PLUGIN8 passed the earlier coverage gate and reached the matched
SystemAppMigrator call.

At `t=1791174134.8907661`, owner
`tpidr_el1=0xffffffecf3b30180` entered `runPluginAtPath:` (`0x13768`) with
path `/System/Library/DataClassMigrators/SystemAppMigrator.migrator` and
identifier `com.apple.MobileInstallation.SystemAppMigrator`. The same owner
entered `CLIENT_SYNC_CALL` at `t=1791174134.903908`; request
`0x7b3b044ba0` carried that identifier. This re-establishes a PLUGIN4-class
request window and supersedes the coverage-only status of PLUGIN5–7 for the
current investigation, without explaining their SpringBoard crashes.

The 1200-second watch expired. No identifier-scoped
`WRAPPER_XPC_REPLY`, `CLIENT_SYNC_RETURN`, or `0x1376c` return marker appeared;
at the final HMP stop the matched request had been open about 7m48s. Other
plugins produced returns, and a later `DM_START_MIGRATION_RETURN` was on
owner `0xffffffecf391d120`, not the matched SystemApp owner. Neither is
evidence of this request's reply. No SpringBoard critical-crash line appeared
and the probe summary reported zero XNU panics. The run therefore restores
request coverage but does not identify a crash cause, wrapper reply, or
permanent hang.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.events/`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.watch.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.probe.out`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.serial.log` /
`stderr.log`. The child was left frozen at the bound. Any next helper sample
must remain tied to this live plugin identifier and request. Poster/draw stay
out of scope; D572/D582 are QEMU hypotheses only.

#### LOCK_PLUGIN8 same-freeze helper-stack sample

The saved RAM snapshot of the paused PLUGIN8 child identifies DataMigrator
PID 120 (`com.apple.datami`) and its thread `0xffffffecf3b30180`, matching the
`tpidr_el1` at the SystemApp `CLIENT_SYNC_CALL`. That thread's saved stack
remains in the XPC/Mach wait path; its saved frames include the inner DM sync
return site `0x27af5179c`, `CLIENT_SYNC_RETURN` site `0x27af4df8c`, and plugin
return `0x10243776c`.
Request `0x7b3b044ba0` is still mapped in this process, and the recorded call
event identifies its plugin as
`com.apple.MobileInstallation.SystemAppMigrator`. This ties the frozen
client-side wait to the live request.

The same freeze contains XPC-waiting workers in `com.apple.migrat` (PID 169),
including owner `0xffffffecf3775120`, and active workers in `installcoordinationd`
(PID 203) and `installd` (PID 172). A nearby wrapper send from that
`com.apple.migrat` owner used connection `0x7cd1098000` and message
`0x7cd0c100c0`, but neither its decoded fields nor its saved payload carry
the SystemApp identifier; the payload includes `msgID`, `pid`, and an
Apple-account benefactor key. A `CLIENT_WRAPPER_MESSAGE` shortly after the
send used `x0=0x7b3ac74c80`, the same receiver captured at the SystemApp
`runPluginAtPath:` entry, but its fields were not decoded and no scoped sync
return followed. This is same-receiver traffic, not proof that it answered
the sync. No message or connection link ties the sampled
installcoordinationd/installd workers to the SystemApp request. These stacks
show concurrent helper work, not which peer holds this send, whether that
work is progressing for this request, or a hang/deadlock.

The first unresolved edge is wrapper-side peer identity/reply. Preserve the
matched wrapper connection and message through remote service receive/reply,
then pair it with the SystemApp-scoped `CLIENT_SYNC_RETURN` and `0x1376c`.
Do not infer the peer from process names or transplant PLUGIN4 helper IDs.
Artifacts are `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.mem/ram/`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.mem/process-stacks.json`, and
the existing PLUGIN8 LLDB/event logs. No migration/check-in return or home
was measured; the VM remains paused.

#### LOCK_PLUGIN8 wrapper-message decode: same receiver, nonterminal message

The saved PLUGIN8 event log and paused-guest RAM resolve the previously
undecoded `CLIENT_WRAPPER_MESSAGE` at `t=1791174135.516374`. Its receiver
`x0=0x7b3ac74c80` is the exact receiver captured at the matched
SystemAppMigrator `runPluginAtPath:` entry (`t=1791174134.890766`); its
selector pointer resolves to `handleMessage:`, and message `0x7b3ac2c900`
contains an inner `CPX@` payload at `+0xb8` with keys `msgID` and
`checkNecessity`. This establishes inbound protocol traffic on the same
per-call receiver, about 0.61 seconds after the SystemApp sync call. The
payload does not identify a terminal migration result or name a remote
process/transport endpoint.

The matched SystemApp request `0x7b3b044ba0` remained mapped and its owner
thread remained in the synchronous XPC/Mach wait at the freeze. There is no
SystemApp-scoped `CLIENT_SYNC_RETURN` or `0x1376c` return, and no
request-linked wrapper reply was captured. In the same trace, a completed
ContainerMigrator call provides a comparison shape: a wrapper send decoded
with `migrationResult` was followed by that plugin's client sync return.
That comparison does not identify the SystemApp peer. In particular, the
nearby send on connection `0x7cd1098000` / message `0x7cd0c100c0` remains
excluded: its payload carries an Apple-account benefactor field and no
SystemApp identifier. The concurrent `com.apple.migrat`,
installcoordinationd, and installd stacks remain unpaired.

Thus the same-receiver callback is not the observed completion of the
SystemApp synchronous call; it may be an intermediate request or protocol
message. The remote receive/reply edge and the wrapper connection field that
would identify its peer are still unresolved. A follow-up must record the
transport endpoint/message identity at the SystemApp call and carry it
through remote receive, reply, and the scoped client return. No permanent
hang or deadlock is established. Source artifacts:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.mem/ram/`, and
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.mem/process-stacks.json`.

#### LOCK_PLUGIN8 transport follow-up: endpoint still not paired

A read-only follow-up attached to the paused PLUGIN8 QEMU GDB stub and armed
the SystemApp-scoped sync/return and wrapper send/receive/reply sites. The
frozen GDB context was a vCPU/kernel context rather than the saved
DataMigrator user thread, so the request and receiver seed reads returned no
user bytes. The original call-time `x0..x8` and transport endpoint therefore
cannot be reconstructed from this attach.

The bounded resume produced generic wrapper XPC sends, including messages
whose decoded dictionary contained only an opaque `f` value. Their connection
and message identities do not link to the SystemApp request or the matched
receiver. It also produced generic migration return markers on different
`tpidr_el1` values from the matched SystemApp owner
`0xffffffecf3b30180`; those are not its `CLIENT_SYNC_RETURN` or
`0x1376c`. No remote receive/reply for request `0x7b3b044ba0` was established.
The guest was paused again after the bounded sample. This is a transport
coverage/correlation miss, not evidence that the remote endpoint failed to
receive or reply, and not a hang/deadlock finding.

The probe now preserves raw `x0` endpoint-candidate bytes at wrapper send and
receive hooks in addition to the message registers and bounded XPC object
bytes (`tools/re/dm_lock_checkin_plugin_callbacks.py`). Those bytes are not
assigned a field layout yet. The decisive next capture must start at the
SystemApp `CLIENT_SYNC_CALL`: log its `x0..x8`, request and per-call receiver;
then capture the underlying Mach send header (destination port, reply/local
port, and message ID), the wrapper-side receive header/connection, the reply
send to that reply port, and the same-thread `CLIENT_SYNC_RETURN` /
`0x1376c`. Match on port/message identity as well as the already-proven
SystemApp identifier; keep the `checkNecessity` receiver message separate
unless its own message ID is shown to be the terminal result. The paused
PLUGIN8 attach began after the send, so a fresh child with these hooks armed
before the call is required for that identity chain. Poster/draw remain out
of scope until migration check-in returns and home completes.
The bounded console excerpt is preserved at
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.followup.stdout.log`.

#### LOCK_PLUGIN11/12: fresh-child transport capture

The paused PLUGIN8 context could not recover call-time user registers, so
PLUGIN11 and PLUGIN12 were fresh D5825 children using the accepted
`bp-patched-dev6/system.tc` and unchanged CLASS16/D572/D582/A407-scanout
hypothesis stack. PLUGIN9 had stopped on an earlier plugin; PLUGIN10's
transport callback raised before the target call. PLUGIN11 reached
SystemAppMigrator but its loaded Mach2 parser treated the vector address as a
header; discard that run's parsed port fields.

PLUGIN12 captured `CLIENT_SYNC_CALL` before the trap. For
`com.apple.MobileInstallation.SystemAppMigrator`, DataMigrator owner
`0xffffffe545d6f830`, request `0x7548c2c7e0`, and receiver `0x7549070e40`,
the `mach_msg2_trap` packed arguments show the request-bearing send to
destination name `0x7803`, local reply name `0x2e03`, message ID
`0x40000000`, voucher name `0x170f`, and a 508-byte message whose payload
contains the same `pluginIdentifier`. The same owner then begins a receive
on `0x2e03` (receive size `0x4000`). The preceding 52-byte send to `0x5503`
with message ID `0x77303074` is preserved separately and is not assigned as
the plugin request.

The receiver's `handleMessage:` callback at `t=1791182950.161` is not shown
to be the terminal result. No peer receive/reply was joined to the request,
and no same-owner `CLIENT_SYNC_RETURN` / `0x1376c` appeared during the
600-second capture. Numeric port names observed in other processes collided
with these values; Mach names are task-local, so those events do not identify
the peer. This is a bounded open sync, not a hang/deadlock finding.

The next required identity is the underlying shared IPC right (for example,
the `ipc_port` reached from each task's port namespace entry) or an equivalent
XPC connection/message identity. Only then can a peer receive and reply be
paired to this request. `dm_lock_checkin_plugin_callbacks.py` now decodes the
packed Mach2 arguments and scopes numeric names per process; PLUGIN12 used
the immediately prior logger version, so do not use its cross-process
numeric matches. Evidence is under
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN12.*` and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN12.*`. Poster/draw remain
out of scope until check-in returns and home completes.

#### LOCK_PLUGIN13: exact-kernel IPC conversion boundary found; dynamic right join remains open

The PLUGIN12 socket was checked after its LLDB session; QEMU reported
`VM status: running`, so do not treat it as a frozen guest or expect its old
call-time registers to be recoverable from a later attach. The independent
`DM_CLIENT_SYNC_RETURN` event in the PLUGIN12 event directory has
`tpidr_el1=0xffffffe5459ac950`, whereas the matched SystemApp owner was
`0xffffffe545d6f830`; that return is not this sync's completion.

Static disassembly of the exact extracted boot kernel identifies the send
conversion path. In this stripped kernel, `mach_msg2_trap` at
`0xfffffff00aaa3b38` calls helper `0xfffffff00aaa3650`, which reaches
`0xfffffff00aa8bc1c`. The latter's register-save pattern matches
`ipc_kmsg_copyin_from_user(kmsg, send_uctx, space, map, priority, options)`;
it is the boundary where names from a task's send header are resolved in that
task's IPC space before the kernel message is delivered. The resolution
sequence to sample is `ipc_kmsg_copyin_header` /
`ipc_kmsg_copyin_header_rights` → `ipc_entry_lookup(space, name)` →
`ipc_right_copyin` → kernel destination `ipc_port*`. On receive, the exact
call site is `mach_msg2_trap` static `0xfffffff00aaa402c` → helper
`0xfffffff00aa8f990`. Its entry uses x0 as the current IPC space, x1 as the
receive name, and x2 as the output slot; the helper calls its lookup routine
and writes the resolved object through that slot on success. This matches the
`ipc_mqueue_copyin(space, rcv_name, &object)` role. The inner send-side lookup
instruction remains unnamed in this stripped binary; this is a static
boundary identification, not a live resolution of PLUGIN12's names.

For SystemApp's call, the names `0x7803` (destination) and `0x2e03` (reply)
must each be paired with the owning DataMigrator `ipc_space`; the peer's
task-local receive name must independently resolve to the same kernel port
object. Do not join on numeric name equality. The next fresh-child capture
must arm before `CLIENT_SYNC_CALL`, record each task/space/name, sample the
send destination object's `ipc_port*`, sample the peer receive object, and
then follow the reply object back to DataMigrator. Close only on that shared
object plus message/plugin identity and same-owner `CLIENT_SYNC_RETURN` /
`0x1376c`. No peer receive, reply, or deadlock conclusion is added here.

The static pass narrows the missing edge to the copyin/copyout IPC boundary;
it does not establish the remote service, terminal result, or why the 600s
sync remained open. Poster/draw remain parked. Exact-kernel disassembly is in
`/tmp/dvm/mach2-disasm.txt`, `/tmp/dvm/send-helper-disasm.txt`, and
`/tmp/dvm/ipc-copyin-disasm.txt`.

#### LOCK_PLUGIN14: exact name-lookup function and bounded shared-object hooks; boot unavailable

Follow-up disassembly identifies the generic port-name table lookup at static
`0xfffffff00aa82188`: `x0` is the IPC space and `w1` the task-local name; it
indexes the table via `space+0x48`, uses `name>>8` with 0x18-byte entries,
validates the entry generation/right bits at `entry+8`, and returns the
`ipc_entry*` in `x0` or zero. This is the exact name→entry site matching
`ipc_entry_lookup`; dynamic state is still needed to identify the underlying
right for this SystemApp request.

`tools/re/dm_lock_checkin_plugin_callbacks.py` now adds a request-scoped send
copyin-return hook at `0xfffffff00aaa3900` and a receive-resolution hook at
`0xfffffff00aa8fa18`. For inline messages, the send-side `ipc_kmsg` header
object fields are sampled at `kmsg+0x48` (destination) and `kmsg+0x50`
(reply/local), with message ID at `kmsg+0x60`; the callback requires ID
`0x40000000` and the SystemApp identifier bytes before using those fields.
Receive-side `x22` is compared to those resolved object candidates after
masking PAC/sign-extension bits; no peer numeric-name equality is used. The
send event is named `IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS` deliberately: it
records the kernel-resolved destination/reply object candidates plus the
DataMigrator task PID, owner token, and that task's call-time local names, but
does not claim a peer match. A receive event is the cross-task join only when
its resolved object pointer equals one of those candidates; it records the
receiving task PID/program and that task's own local receive name. The reply
send is separately matched to the resolved reply object, and closure still
requires the original owner token's `CLIENT_SYNC_RETURN` / `0x1376c`. Non-inline
signed headers and failed LLDB conditions are skipped/disabled. This is
instrumentation only; no live receive/reply has been measured with it.

The requested fresh child could not start because the backing chain is
incomplete. `qemu-img info` verifies D5825's immediate backing is
`/tmp/dvm/data-seed/ui-class16-refresh60-dm-plugin4-launchfrozen1.qcow2`; that
image in turn names
`/tmp/dvm/data-seed/ui-class16-refresh60-ccs-ls-seedprogress1.qcow2`, which is
absent. Thus D5825 is the documented PLUGIN lineage, but not a self-contained
checkpoint. Searches of `/tmp/dvm`, `~/dvm-artifacts`, and Spotlight found no
copy of the missing image. Its only recorded location is under `/tmp/dvm`, a
scratch tree documented as wipe-prone; the specific cleanup that removed this
file is not known. No unrelated parent was substituted. There is no
safe one-command rebuild from the current artifacts: the repository's
`rebuild_persistent_parent.sh` creates a fresh persistent parent, not the
guest-produced seed-progress checkpoint. Recreate that checkpoint only by
restoring its original ancestor and rerunning the recorded
`CCS_LS_SEEDPROGRESS1` seed run with
`tools/re/ccs_ls_seed_progress_callbacks.py`; without that ancestor the prior
LS data state cannot be reproduced as the same lineage. Once the verified
pre-seed parent and original boot inputs are recovered, the minimal probe
shape is:

```sh
PARENT=<verified-original-pre-seed-parent.qcow2> \
CALLBACKS=ccs_ls_seed_progress_callbacks \
PROBE_STOP_LLDB_REGEX=LSD_SEED_SYNCWRITE_CALL \
KEEP_GUEST=1 SECS=1800 STALL_SECS=1800 \
tools/re/setup_gate_probe.sh UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS1
```

This is a command template, not runnable with current artifacts: the required
ancestor and original run inputs are absent, and a same-named replacement
alone would not prove equivalence to the old backing data. No matching DVM
QEMU process was present to release; the PLUGIN12 user registers therefore
remain unavailable. The shared object join, peer receive/reply, and same-owner
`CLIENT_SYNC_RETURN` remain open. The earlier 600-second open interval does
not establish a hang or deadlock.

#### LOCK_PLUGIN15: seed checkpoint replayed; SystemApp window revalidated, IPC object hook missed

The unavailable seed image was recreated by replaying its recorded operation,
not recovered byte-for-byte. The original pre-seed ancestor was found at
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/disk.qcow2`; the boot inputs
were its sibling `bootkc`, `system.tc`, and `system.dtree`. The CLASS16
60 Hz callback was regenerated from the checked-in color/timing fixtures with
the recorded rates (horizontal 21,000 Hz and vertical 60 Hz); its ColorElements
segment matched the retained CLASS16-probe callback. The replay used
`ccs_ls_seed_progress_callbacks`, `KEEP_GUEST=1`, and a JSON-event stop at
`LSD_SEED_SYNCWRITE_CALL`.

`UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS1` reached 1,061 registration
returns, `LSD_SCAN_DRIVER_RETURN`, `LSD_SYSTEM_SCAN_RETURN`, and
`LSD_ENUM_RETURN`, then stopped at the actual `syncWrite:` call entry. It did
not measure that write's return. The new 336 MiB qcow2 has SHA-256
`004f1685acf94d2264fd6c5d4e5bc0d0bd65ac5fbde1be0d6a1b5771d0b617ab` and was
copied byte-for-byte to
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-ccs-ls-seedprogress1-replay-20261005.qcow2`
(same hash). `qemu-img check` reported no errors. The plugin4 overlay was
repointed to that durable replay, and `qemu-img info --backing-chain` then
resolved D5825 through plugin4, the replayed seed, and the original boot1
ancestor. This restores a runnable, replayed D5825 lineage; it does not claim
the regenerated seed is byte-identical to the lost file.

One fresh child, `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN15_IPC1`, used that D5825
chain, `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, the same
CLASS16/D572/D582 settings, and `dm_lock_checkin_plugin_callbacks` from boot.
It revalidated `DM_START_MIGRATION_CALL` and a live
`com.apple.MobileInstallation.SystemAppMigrator` `CLIENT_SYNC_CALL`. The
matched DataMigrator owner was `0xffffffe026249120`; request `0x78b3038d80`,
receiver `0x78b3074e00`. Its 508-byte Mach message had ID `0x40000000`,
destination name `0x6c03`, and reply name `0x200b`. The earlier destination
`0x7503` / message ID `0x77303074` was a separate send and is excluded.

The fresh-child probe logged `DM_LOCK_PLUGIN_KERNEL_IDENTITY_READY` and armed
the post-copyin hooks at runtime `0xfffffff02aaa3900` and
`0xfffffff02aa8fa18`, but neither produced a hit. Consequently there is no
`IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS`, no object-equal peer receive/reply, and
no same-owner `CLIENT_SYNC_RETURN` / `0x1376c`. A same-receiver
`handleMessage:` notification is not promoted to the terminal result. The
matched sync remained open for about 661 seconds until this bounded child was
stopped; this is not a hang or deadlock finding. The first unresolved edge is
now specifically the live hit/validation of the post-copyin kernel hook after
the captured `mach_msg2` send, before any cross-process peer claim.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS1.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS1.serial.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN15_IPC1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN15_IPC1.events/`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN15_IPC1.serial.log`.
The guest reported zero panics. Poster/draw remain parked; D572/D582 remain
QEMU hypotheses.

#### LOCK_PLUGIN29: current-build mach_msg2 post-copyin sample validated

Fresh child `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN29_IPC_REPLY` ran on the
replayed D5825 lineage with the accepted `system.tc` and same CLASS16/D572/D582
settings. The matched SystemAppMigrator call had owner
`0xffffffeaf20ee0c0`, request `0x7619038c60`, receiver `0x7619060e20`, and
Mach message ID `0x40000000` (local destination name `0x4a0f`, reply name
`0x2013`).

The live kernel sample closes the earlier hook-placement gap. On this bootkc,
`mach_msg2_trap` begins at static `0xfffffff00aaa3b38`; it calls the send
copyin helper at `0xfffffff00aaa3f50` and returns there at
`0xfffffff00aaa3f54` (runtime `0xfffffff02aaa3f54`, slide `0x20000000`). At
the return, `x21` is the kmsg. The inline kernel header begins at `kmsg+0x40`;
destination and reply object pointers are at `kmsg+0x48` / `+0x50`, and the
message ID is at `kmsg+0x5c`. The emitted
`IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS` event identified destination object
`0xffffffeaf1f83430` and reply object `0xffffffeaf1fd6470`, joined to the
call-time owner/request and ID. Its OOL body did not contain the plugin ID;
the event's correlation does not depend on inline body bytes.

Thus the previous static send breakpoint `0xfffffff00aaa3900` was not the
return PC of this live `mach_msg2` path. The PLUGIN15 receive-helper
breakpoint `0xfffffff00aa8fa18` was also not the site that produced the live
object sample; the successful
caller-side resolved-right sample is `0xfffffff00aaa4040`, after the helper
call at `0xfffffff00aaa402c`, with `x22` holding the resolved object and
`x21` the local receive name. Its observed object was the same DataMigrator
task's reply right, so this is receive-right resolution only—not peer
delivery. Do not equate task-local port names across processes.

No object-equal destination receive, reply-object send, or same-owner
`CLIENT_SYNC_RETURN` / `0x1376c` was observed during the original 1200-second
instrumented window. This is a bounded `mach_msg2` observation, not proof
that a peer is absent or that the request is hung. PLUGIN31/32 below correct
the earlier static assignment of `0xfffffff00aaa40bc`: it is a secondary
object-copyin result before the outgoing send, not a message-dequeue success
sample. A late attach to this site is not scored for peer delivery. No peer
join/reply is established.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN29_IPC_REPLY.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN29_IPC_REPLY.events/`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN29_IPC_REPLY.probe.out`, and
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN29_IPC_FOLLOWUP2.lldb.log`.
The late follow-up was stopped with the guest paused. The seed remains a
replay, not a byte-identical recovery. D572/D582 remain QEMU hypotheses.

### PLUGIN30 IPC peer-delivery follow-up (legacy DEQUEUE tag; 2026-10-05)

A fresh replay-D5825 child with the cache-accepted `system.tc` re-entered the matched SystemAppMigrator sync. The capture used callback-side object comparisons at receive-right `0xfffffff02aaa4040` and the then-candidate `0xfffffff02aaa40bc`, plus reply-object send and same-owner return; the peer receive/send sites were not filtered by DataMigrator's `tpidr_el1`. The old DEQUEUE label was incorrect: PLUGIN31/32 disassembly shows `+0x40bc` is a secondary object-copyin result before send. `py_compile` passed for both probe modules.

Matched send: owner `0xffffffe503396890`, request `0x74fb01cf60`, msgID `0x40000000`, destination candidate `0xffffffe2534d1030`, reply candidate `0xffffffe252ce86a0`. A receive-right resolve at `0xfffffff02aaa4040` matched the reply candidate in DataMigrator itself. It did not establish a peer receive. No object-equal destination dequeue, reply-object send candidate, or same-owner `CLIENT_SYNC_RETURN`/`0x1376c` was observed within the 1500-second boot bound. The 40bc callback was armed and proved in LLDB's command list, but no matched dequeue was logged. Keep the peer/reply edge unresolved; an unanswered interval is not a hang/deadlock result.

Tag/logs: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN30_IPC_DEQUEUE.{lldb.log,events,probe.out,slide.json}` and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN30_IPC_DEQUEUE.{serial,stderr}.log`. The input is the durable replay seed chain, not the byte-identical missing seed. D572/D582 remain QEMU hypotheses; no display/model changes were made.


### PLUGIN31/32: same-owner `+0x40bc` result disambiguated

Two fresh replay-D5825 captures reached the matched SystemAppMigrator request and logged the same-owner `0xfffffff02aaa40bc` breakpoint after the send-side destination/reply objects had been captured. PLUGIN31: owner `0xffffffe2298f5120`, request `0x7d34c2cd20`, destination `0xffffffe31086d0c0`, reply `0xffffffe3106fe110`; sample `x0=0`, `x22=reply`, `x23=0` (pre-instruction), `x8=destination`. PLUGIN32: owner `0xffffffdf0b2118f0`, request `0x7bcac60ea0`, destination `0xffffffdd407d4cd0`, reply `0xffffffdd4068c2b0`; sample `x0=0`, `x22=reply`, `x23=0` (pre-instruction), `x8=destination`, `x24=0x6107`.

Bootkc instructions at `0xfffffff00aaa40b0..0xfffffff00aaa40c0` show a secondary copyin helper call, output load into `x8`, status test, then `csel x23,x8,xzr,eq`. Therefore `+0x40bc` is before an outgoing send and is not a dequeue result. The previous PLUGIN32 attempt to interpret `x8+0x40` as a message header was invalid; `x8` is the copyin output. This classifies the hit as a successful same-owner secondary copyin only; peer receive, terminal reply, and same-owner sync return remain unproven. The outgoing-send helper call is at static `0xfffffff00aaa3f50` (target `0xfffffff00aa8dd58`) and its return-status branch at `+0x3f54`. `+0x4160` calls `ipc_mqueue_receive`; `+0x4164` and `+0x417c` are later result boundaries, not dequeue success sites. Static queue-path success is the unlink at `0xfffffff00aa8f3a4..0xfffffff00aa8f3c4` followed by the `ith_kmsg` store at `0xfffffff00aa8f44c`.

The callback is now labeled `KERNEL_SECONDARY_COPYIN_RESULT`; it reports the raw/status registers and does not emit a dequeue event. These runs stopped at that first hit, so they do not support a negative claim about subsequent reply activity. PLUGIN33 then followed the same-owner matched send farther: its 516-byte ID `0x40000000` kmsg reached send-helper call `0xfffffff02aaa3f50` with destination/reply object candidates `0xffffffdff6da2fb0` / `0xffffffdff6be4df0`, followed by `ipc_mqueue_receive` call `0xfffffff02aaa4160` on the reply object (`x22=0xffffffdff6be4df0`). The direct send-helper return status for that message, receive return, `ith_kmsg` handoff, peer reply, and same-owner sync return were not captured before the guest paused at an unrelated-owner breakpoint. This is a reached reply-receive call, not a dequeue or a negative peer result.

The static normal queue-dequeue success point is now identified at `0xfffffff00aa8f44c`: after unlinking the chosen queue entry (`0xfffffff00aa8f3a4..0xfffffff00aa8f3c4`), the function PAC-stores the selected kmsg to the receiving thread's `ith_kmsg` (`thread+0x3b0`). Alternate direct-delivery stores `0xfffffff00aa8e638` and `0xfffffff00aa8eca0` still need live header/object correlation. The callback now seeds a send-helper-call **candidate** so the next boot can apply object filters at these handoff sites without claiming that the candidate itself proves delivery. PLUGIN33 artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN33_IPC_MQUEUE.lldb.log`, `.events/`, and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN33_IPC_MQUEUE.{serial,stderr}.log`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

### PLUGIN34–36: request reaches wrapper `ith_kmsg`; terminal reply remains open

PLUGIN34 (`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN34_IPC_ITHKMSG`) closes the request-delivery edge for one matched SystemAppMigrator sync. At the queue handoff store `0xfffffff02aa8f44c`, the receiver was `com.apple.migrationpluginwrapper`; the stored kmsg header matched the 516-byte msgID `0x40000000` send and its remote ipc_object matched the destination candidate captured at the DataMigrator send-helper call. This is stronger than send-helper or receive-call evidence: the queued message was removed and assigned to the receiver thread's `ith_kmsg` (`thread+0x3b0`). Exact registers and pointers are recorded in `iomfb-dseries.md` under PLUGIN34–36.

PLUGIN36 repeated that matched handoff and showed the same DataMigrator owner subsequently resolving and entering `ipc_mqueue_receive` on the captured reply object (`0xffffffe4ce727b80`, owner `0xffffffe21e5c3060`, request `0x79d103c180`). Before the capture ended, no object-equal reply kmsg was stored to `ith_kmsg`, no reply-destination copyin candidate was recorded, and no same-owner SystemApp `CLIENT_SYNC_RETURN` / `0x1376c` appeared. PLUGIN35 was not scored past its debugger pause. The supported conclusion is **request delivered to migrationpluginwrapper; terminal reply delivery and sync return remain open**. The bounded absence is not a hang/deadlock finding, and the debug pause is not classified as an IPC failure.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN34_IPC_ITHKMSG.*`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN34_IPC_ITHKMSG.{serial,stderr}.log`, and matching PLUGIN35/36 `lldb.log`, `events/`, `probe.out`, and serial/stderr logs. The helper-return callback now observes cross-task sends for reply-object comparison while treating unreadable unrelated-owner candidates as non-matches. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

### PLUGIN38 wrapper-side reply follow-up

Fresh replay-D5825 child `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN38_WRAPPER_REPLY` revalidated SystemAppMigrator request delivery: owner `0xffffffdedb7da890`, request `0x73bac104e0`, 516-byte msgID `0x40000000`; the destination-object-matched kmsg was stored to `ith_kmsg` in `com.apple.migrationpluginwrapper` on thread `0xffffffdedb7920c0` at runtime `0xfffffff02aa8f44c`.

Within ~14 ms the same wrapper thread crossed libxpc's Mach-event boundary on connection candidate `0x7de2c44780`; the second event's raw payload contains `com.apple.MobileInstallation.SystemAppMigrator`. The same thread/connection reached the XPC event-handler boundary (`0x19da17a44`, LR `0x19da19f5c`). This proves dispatch of a matching plugin-ID-bearing event through the wrapper's XPC machinery, but does not identify the registered app callback or prove that plugin work started.

No matching reply-object send/copyin candidate, DataMigrator reply `ith_kmsg`, or same-owner `CLIENT_SYNC_RETURN` / `0x1376c` was captured during roughly 355 seconds of attached coverage. LLDB then stopped at the global plugin-return PC for an unrelated `TCCAccessRequest` owner and exited. The guest reached its 1200-second bound and was left frozen with zero XNU panics, but the rest of that guest interval was uninstrumented. The terminal reply remains open; this is neither proof that the wrapper never replied nor a hang/deadlock finding. The next hook should identify and follow the registered wrapper event callback into its app-level message handler, then retain the reply-object equality filter through the send and DataMigrator return. The probe now omits the global `runPluginAtPath` return breakpoint that interrupted this capture; syntax validation passed, boot validation remains. Full tag/artifact paths and event details are in `iomfb-dseries.md` under PLUGIN38. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

### PLUGIN42/43 wrapper callback and reply follow-up

The PLUGIN42 coverage check reached a SystemAppMigrator sync but saw only sibling wrapper-event traffic; it did not identify the registered callback. PLUGIN43 (`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN43_WRAPPER_HANDLER`) boot-validates the corrected probe with no global `runPluginAtPath` return stop. For matched owner `0xffffffee0c5418f0` / request `0x73ab0443c0` / msgID `0x40000000`, the 516-byte request's resolved destination and reply objects were `0xffffffee0c2bf280` and `0xffffffee0c5b6d70`. Its destination-matched kmsg reached wrapper `ith_kmsg` at `0xfffffff02aa8f44c` on TPIDR `0xffffffee0c13f830`.

On that receiver thread and connection `0x73ad038780`, the event-handler boundary `0x1878a3a44` was followed by the registered block invoke `0x265bdf44c` (block `0x73acc5a2e0`, obtained via connection `+0x20` and block `+0x10`). The same connection/thread then issued an XPC send (`0x73ad0184e0`), but decoded fields did not establish that its destination was the captured reply object. A later DataMigrator `handleMessage` entry (`0x265be20cc`, message `0x73ab048700`) carried the SystemAppMigrator identifier in its receiver context; its payload semantics are not decoded, so it is not classified as a terminal result.

No reply-object send/handoff or same-owner `CLIENT_SYNC_RETURN` / `0x1376c` was observed during the roughly 294-second attached follow-through. This bounded result leaves terminal reply open; it is not a hang/deadlock conclusion. Next is decoding the outbound callback message and client handler payload while retaining reply-object identity through send and same-owner return. D572/D582 remain QEMU hypotheses; display/poster work remains parked.

Logs: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN43_WRAPPER_HANDLER.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN43_WRAPPER_HANDLER.events/`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN43_WRAPPER_HANDLER.{serial,stderr}.log`; PLUGIN42 logs use the same tag stem.

### PLUGIN44: callback send/client message captured, terminal semantics still open

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN44_WRAPPER_DECODE` used a fresh replay-D5825 child with the accepted `bp-patched-dev6/system.tc`, CLASS16/D572/D582 unchanged, and callbacks armed before the matched SystemAppMigrator sync. The matched owner was `0xffffffde1b8b3060`, request `0x741b03c420`, msgID `0x40000000`, size 516. Its resolved destination/reply ipc_objects were `0xffffffdfe76c55d0` / `0xffffffdfe770bee0`; the destination-matched message was handed to wrapper `ith_kmsg` at `0xfffffff02aa8f44c`.

The registered wrapper callback then sent XPC message `0x7c06c30180` on connection `0x7c06c48780` and the matched receiver thread. Its decoded XPC fields were empty. A raw UTF-16 scan found `MergeBuddyProvisioningResponse.migrator`, but no `migrationResult`, `checkNecessity`, or `SystemAppMigrator` string. Important limitation: the event record carries the original request's destination/reply objects as comparison candidates; it did not resolve this outbound message's kernel destination. Do not call this either a reply-object send or a send to the original destination. The full raw message and bounded pointer graph are in `progress.MATCHED_WRAPPER_XPC_SEND_DECODE.001.json`.

At `0x272e320cc`, DataMigrator `handleMessage` received message `0x741ac3c600` in receiver context owned by the matched request and attributed to `com.apple.MobileInstallation.SystemAppMigrator`. Although the dictionary decoder returned no fields, the raw body contains the literal `msgID` and `checkNecessity` fields and no `migrationResult`. This classifies the observed message as the intermediate necessity check, not the terminal result. Its raw snapshot also includes pointer candidates at `+0x60` and `+0x70`, beyond the graph walk's current `+0x38` scan limit, so additional nested payload remains undecoded. Plugin attribution is not terminal-result proof.

The attached observation continued to a 600-second bound after the matched SystemApp sync window was established. The reply-object send candidate, reply-object `ith_kmsg` handoff, and same-owner `CLIENT_SYNC_RETURN` / `0x1376c` remained absent. This is a bounded observation, not a permanent-hang/deadlock claim. Next is a focused decode of nested message objects (especially `+0x60/+0x70`) while preserving the existing kernel object-equality send/handoff filters and same-owner return. Logs: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN44_WRAPPER_DECODE.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN44_WRAPPER_DECODE.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN44_WRAPPER_DECODE.probe.out`, and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN44_WRAPPER_DECODE.{serial,stderr}.log`. The guest was left frozen; no panic was recorded. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

### PLUGIN46: nested callback payload and outbound destination

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN46_SEND_DEST` used a fresh child of the replay-backed D5825 parent, the accepted `bp-patched-dev6/system.tc`, and unchanged CLASS16/D572/D582/A407-scanout settings. It revalidated SystemAppMigrator request `0x785303cc60` (owner `0xffffffdfe8b260c0`, msgID `0x40000000`, 516 bytes), with destination/reply objects `0xffffffde1b777790` / `0xffffffde1b7f4190`. The destination-matched request was handed to wrapper `ith_kmsg` at `0xfffffff02aa8f44c` on TPIDR `0xffffffdfe7eaf830`.

The same wrapper thread/connection `0x75f0c4c640` issued callback-associated XPC send `0x75f102c1e0`. The expanded pointer graph walked the tagged value at `+0x60` and references at `+0x68/+0x70`; nested data contains `MSUDataMigrator` and a `msgID` key, but no `MergeBuddyProvisioningResponse.migrator`, `SystemAppMigrator`, `migrationResult`, or `checkNecessity`. XPC field decoding remains empty. This names a nested payload value, not the service endpoint or terminal result.

The first send-helper call on this callback TPIDR followed about 62 ms after the XPC send. At `0xfffffff02aaa3f50`, `x0=0xffffffe0cd75f200` is a coherent 88-byte kmsg with Mach ID `0x10000000`, destination `ipc_object* 0xffffffde1b79ff70`, and no local object. The body contains the `CPX@` envelope and `pid`/`msgID` keys. The destination differs from the SystemApp request's reply object `0xffffffde1b7f4190`; this kmsg is not a terminal reply. Same-thread/time and payload-shape correlation make it the leading candidate for the callback XPC send, but the probe did not capture a transport transaction ID that directly maps XPC pointer `0x75f102c1e0` to this kmsg. Later kmsgs on this reused wrapper thread are not assigned to the callback XPC send. A compact paired record is in `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN46_SEND_DEST.events/analysis.PLUGIN46_WRAPPER_XPC_TO_MACH_SEND_CANDIDATE.json`.

At the 600-second post-request bound, no object-equal send to the original reply object, reply-object `ith_kmsg` handoff, or same-owner `CLIENT_SYNC_RETURN` / `0x1376c` appeared. This leaves terminal reply open; it is not a hang/deadlock claim. Next is a receiver-side `ith_kmsg` comparison for destination object `0xffffffde1b79ff70`, while retaining the original reply-object filter and same-owner return. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN46_SEND_DEST.{lldb.log,events,probe.out,slide.json}` and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN46_SEND_DEST.{serial,stderr}.log`. The guest was left frozen with zero XNU panics. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

### PLUGIN47: callback outbound destination received by DataMigrator; terminal reply remains open

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN47_DEST_PEER` used a fresh replay-D5825 child (backed by `ui-class16-refresh60-dm-ls-parent-seed-d5825.qcow2`), the accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, and the same CLASS16/D572/D582 stack. The probe retains callback-thread Mach destination candidates and joins their headers/body against queued/direct `ith_kmsg` stores, while preserving the original SystemApp reply-object and same-owner return filters.

The matched SystemAppMigrator request was request `0x7818c38660`, owner TPIDR `0xffffffe2b8698950`, msgID `0x40000000`, with destination/reply objects `0xffffffe482ab4c40` / `0xffffffe48327f820`. The wrapper callback on TPIDR `0xffffffe2b8624950`, connection `0x786ec60780`, emitted the leading outbound XPC-to-kmsg candidate about 22 ms after the callback send: kmsg `0xffffffe39d908a00`, size 88, Mach ID `0x10000000`, destination object `0xffffffe482aaec50`, null local object. This destination differs from the original SystemApp reply object.

At the queue-removal/`ith_kmsg` store (`0xfffffff02aa8f44c`), another thread, TPIDR `0xffffffe2b87ce0c0`, under program label `com.apple.datamigrator`, received the same kmsg. The receiver-side header matched destination object, Mach ID, size, and the complete captured 56-byte body exactly. This closes the receiver for the outbound **kmsg candidate**. The XPC-message-to-kmsg relation remains based on the same callback thread/connection, a 22 ms interval, and payload shape; no transaction ID proves that relation. The receiving process is identified, but its role in completing SystemAppMigrator is not yet established.

The kernel reply-object send/handoff filters remained active through their configured 900-second SystemApp Mach window. `SYSTEMAPP_MACH_WINDOW_CLOSED` recorded the bounded closure; no send to reply object `0xffffffe48327f820` or reply-object `ith_kmsg` handoff appeared in that window. The same-owner `CLIENT_SYNC_RETURN` / `0x1376c` marker remained armed until the manually requested stop about 935 seconds after the matched send and did not fire. This bounded open-reply result does not establish a hang or deadlock. Next, follow the receiving DataMigrator thread from this `ith_kmsg` into its userspace handler and determine whether it triggers a result sent to the original reply object. The probe reported re-freezing the guest at the bound with no XNU panic, but the monitor socket refused a post-run HMP reconnect and the QEMU process was no longer available for inspection. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN47_DEST_PEER.{lldb.log,events,probe.out,slide.json}`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN47_DEST_PEER.{serial,stderr}.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin47-dest-peer.qcow2`. Probe syntax and `git diff --check` passed.

### PLUGIN51: exact secondary DataMigrator handoff reaches intermediate `handleMessage:`

On fresh replay-D5825 child `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN51_DM_HANDLER`, the matched `com.apple.MobileInstallation.SystemAppMigrator` call was request `0x7722c50720` on TPIDR `0xffffffec9a4ba0c0`. The 508-byte msgID `0x40000000` user send recorded destination name `0x5b03` and reply receive name `0x2a13`, both task-local names. The accepted `system.tc`, CLASS16 fixture, and D572/D582 hypothesis settings were unchanged.

The registered wrapper event whose graph contained the SystemAppMigrator identifier ran on TPIDR `0xffffffec9a393830`, connection `0x7df8c78780`, invoke `0x27a5c744c`. Its 88-byte secondary send candidate (Mach ID `0x10000000`, destination object `0xffffffe9024e8220`) was subsequently stored to `ith_kmsg` at `0xfffffff02aa8f44c` on DataMigrator TPIDR `0xffffffec9a2cf060`. The destination `ipc_object*` and complete header/body matched, closing delivery of this kmsg to DataMigrator. The callback-event-to-send pairing remains a callback-thread/context candidate without a transport transaction ID.

The same receiver TPIDR entered DataMigrator `handleMessage:` at `0x27a5ca0cc` for message `0x7722c54500`; the decoded selector is `handleMessage:` and its bounded payload contains `msgID` plus `checkNecessity`, not `migrationResult`. Thus the observed handler is an intermediate necessity-check path, not a terminal SystemAppMigrator result. A separate observed `migrationResult` send was from another plugin/thread and is not linked to this request.

The same-owner `CLIENT_SYNC_RETURN` watch remained armed and no SystemAppMigrator return was recorded before a deliberate HMP quit after the handler sample. However this child did not emit `IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS`: although it logged the call-time reply name `0x2a13`, it did not resolve that name to a kernel reply object. Therefore this capture cannot establish whether a later send targets the original reply object, and its bounded no-return observation is not a hang/deadlock result. The next discriminator is a same-child kernel-object resolution for the reply right followed by object-equal send/`ith_kmsg` and same-owner return tracking. Artifacts are under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN51_DM_HANDLER.{lldb.log,events/}` and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN51_DM_HANDLER.{serial,stderr}.log`; the replay child is `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin51-dm-handler.qcow2`. D572/D582 remain QEMU hypotheses; display/poster work remains parked.

### PLUGIN52: reply right resolved in the matched child; no terminal reply within the bound

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN52_REPLY_RESOLVE` used a fresh replay-D5825 child and the accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`; CLASS16 and the D572/D582/A407-scanout hypothesis settings were unchanged. PLUGIN51's missing object event was caused by `_kernel_arm()` testing whether `KERNEL_BREAKPOINTS` was nonempty. Earlier callback-side send probes legitimately populated that dictionary, so the test skipped installing the `mach_msg2` owner prologue. The guard now tests specifically for `KERNEL_MACH2_OWNER_PROLOGUE`. In PLUGIN52 the owner-entry and post-copyin path hit, validating the correction.

For SystemAppMigrator request `0x7b0ec34060` (TPIDR `0xffffffdff06dc180`, msgID `0x40000000`, 508-byte user message), the live task-local names were destination `0x7903` and reply `0x2603`. At runtime `0xfffffff02aaa3f54`, the same-owner user-send header and 516-byte kernel kmsg `0xffffffdff01c1100` were joined; the kmsg fields resolved to destination `ipc_object* 0xffffffde2390cdf0` and reply `ipc_object* 0xffffffde2399af20`. `IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS` fired. The local name is not used as cross-task identity.

The send helper returned status zero, and the same owner resolved reply receive name `0x4000` to `0xffffffde2399af20` at `…4040`, then entered `ipc_mqueue_receive` on that object at `…4160`. Request delivery to migrationpluginwrapper was also confirmed by the destination-matched `ith_kmsg` store at `…f44c`. The subsequent 88-byte callback message to DataMigrator used a different destination object and reached `handleMessage:` as `checkNecessity`, not `migrationResult`. A later 56-byte wrapper-thread message likewise targeted a different object. These messages do not close the original synchronous call.

The attached run reached the 1,200-second cap and left the guest frozen. No object-equal send or `ith_kmsg` handoff to reply object `0xffffffde2399af20`, and no same-owner SystemApp `CLIENT_SYNC_RETURN` / `0x1376c`, was recorded. Other plugin returns—including ones sharing a TPIDR—are excluded unless the SystemApp identifier and request match. This is a bounded no-reply result with the reply object known, not proof of a permanent hang or deadlock. Logs and child: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN52_REPLY_RESOLVE.{lldb.log,events/,probe.out,slide.json}`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN52_REPLY_RESOLVE.{serial,stderr}.log`, `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin52-reply-resolve.qcow2`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN52 frozen follow-up: reply producer is service-side, internal completion edge open

A read-only HMP/GDB snapshot of the still-running frozen PLUGIN52 child read the matched DataMigrator owner thread `0xffffffdff06dc180`. Its `thread+0x28` is `0xffffffde2399af30`, the exact waitq at `reply ipc_object 0xffffffde2399af20 + 0x10`; the waitq's receiver field points back to the same owner. The caller was still associated with this reply receive queue at the freeze. This does not reveal the wrapper's current userspace stack or prove why the response was absent.

The original request was delivered to `com.apple.migrationpluginwrapper`; that service is the expected producer of the response that completes the caller's synchronous `sendMessageSync`. SystemAppMigrator completion/result is the likely trigger. The matched `DM_RUN_PLUGIN_CALL_13768` carried completion block `0x16dab19e0`, but its matching return at `0x1376c` did not occur. The observed `checkNecessity` message was a separate side request to object `0xffffffde2390cd60`; the later 56-byte wrapper send targeted `0xffffffde238d8c40`. Neither is the response to reply object `0xffffffde2399af20`. Other-plugin `migrationResult` traffic is not linked to this call. The next missing edge is therefore the SystemAppMigrator completion/result reaching the wrapper's response path and a response sent to the captured reply object. The capture does not identify the internal callback or establish that the wrapper failed to attempt it.

At the 1,200-second limit there was no object-equal reply send/handoff and no same-owner `CLIENT_SYNC_RETURN` / `0x1376c`; this is a bounded observation, not a hang/deadlock verdict. The wrapper receiver thread's snapshot waitq pointer was different and unreadable, so it is not assigned a dependency. Freeze artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN52_REPLY_RESOLVE_FREEZE.{threads.txt,ramscan.txt,postmortem.txt}`; HMP socket: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN52_REPLY_RESOLVE.sock`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.


#### PLUGIN52 same-child completion follow-up: no valid callback observation

The live PLUGIN52 child (QEMU PID 77364; HMP socket `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN52_REPLY_RESOLVE.sock`) was reattached through GDB port 1279. The matched SystemAppMigrator event already records completion block `0x16dab19e0` and its `+0x10` invoke pointer `0x102473bd8`; `0x10247376c` is the plugin-call return, `0x260e99f8c` the same-owner sync-return site, and `0xffffffde2399af20` the resolved reply object. Breakpoints for those addresses and an object-equal send check were attempted, but the reconnect stopped at kernel IPC hook PCs `0xfffffff02aaa4040` / `0xfffffff02aaa3f54`. The narrowed LLDB follow-up then crashed during interrupt/register unwinding (`GDBRemoteRegisterContext`).

No new completion invoke/return, plugin-call return, same-owner sync return, or reply-object send/handoff event was recorded. Because the debugger did not provide a continuous observation window, this is a **coverage failure**, not evidence that SystemAppMigrator did not complete or that the wrapper never attempted a reply. The previous PLUGIN52 waitq/object snapshot remains the only valid wait evidence; no hang/deadlock conclusion changes. Next, use a fresh replay-D5825 child and arm the dynamic completion invoke from the matched `DM_RUN_PLUGIN_CALL_13768` block (`block+0x10`), matching `x0` to that exact block and recording its return at LR; retain reply-object equality and same-owner return/handoff watches. The temporary LLDB helpers were under `/tmp/dm_plugin52_*_followup*` and were not committed as a probe. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN54: dynamic SystemApp completion hook stayed quiet through a covered window

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN54_COMPLETION` used a fresh child on the replay-backed D5825 lineage, the accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, and the unchanged CLASS16/D572/D582 stack. The child overlay was `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin54-completion.qcow2`; the durable replay seed is not byte-identical to the lost original seed. The backing chain was checked before launch. PLUGIN53 was only a hook-filter validation attempt and is not scored here.

The matched SystemAppMigrator call was on owner TPIDR `0xffffffe94d53d120`, request `0x7964c542a0`, with completion block `0x16f8f59e0`. The probe read this call's invoke pointer from block `+0x10` as `0x1005a3bd8` and dynamically installed a breakpoint there, filtering invocation on `x0 == 0x16f8f59e0`. The LLDB command-list proof records the address and callback. The plugin-call return site was armed at `0x1005a376c` for this SystemApp call. Invocation was not constrained to the original call owner; call and invoke owners are logged separately.

The same call's post-copyin sample resolved the request to destination object `0xffffffe5b67fe500` and reply object `0xffffffe5b6728fa0` (516-byte kernel message, msgID `0x40000000`; task-local names are labels only). The same owner entered `ipc_mqueue_receive` on that reply object. Reply-object send/handoff and same-owner SystemApp sync-return watches remained armed. The matched receive window closed at 2,100 seconds with no completion invoke, completion return, plugin `0x1376c` return, reply-object send/handoff, or SystemApp-scoped sync return. LLDB and QEMU remained present through the configured observation cap; there was no register-context crash, detach, or instrumentation-stop event. This is a covered bounded no-invoke observation: completion had not started during this window, but this does not establish that it never starts or identify the upstream reason. It is not a hang/deadlock finding.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN54_COMPLETION.{lldb.log,events/,slide.json,watch.log}`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN54_COMPLETION.{serial,stderr}.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin54-completion.qcow2`. The next cut is upstream of this exact completion block: identify what schedules/invokes it, or extend the same matched trace if late invocation is the question. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN55: wrapper sync response is the prerequisite before B can run

Static RE of the PLUGIN54 build identifies the immediate owner of completion block B. The DataMigrator caller forms B at stack `sp+0x10` (`/tmp/dvm/datamigrator-unslid.macho`, base `0x100000000`), signs invoke `0x100013bd8` into `B+0x10`, then calls `DMMigrationPluginWrapperConnection runPluginAtPath:withIdentifier:parameters:completion:` at `0x100013768` with B in `x5`. The callback body at `0x100013bd8` subsequently submits a nested block using a queue pointer captured at `B+0x20` (`0x100013c94`); the executable separately creates a serial queue named `com.apple.datamigrator.pluginCompletionSerialQueue` at `0x100013468`, but pointer identity between that named queue and `B+0x20` is unverified. This queue submission is downstream of B's invoke and does not explain PLUGIN54's pre-invoke silence.

In the matching `DataMigration.framework`, `-[DMMigrationPluginWrapperConnection runPluginAtPath:withIdentifier:parameters:completion:]` starts at `0x25e8f1a10`. Its synchronous plugin-send helper call is at `0x25e8f1f88`, with return at `0x25e8f1f8c`; after processing that response, the method reads the `migrationResult` key (cstring `0x25e8f77b7`, reference at `0x25e8f1fa8..0x25e8f1fb4`) and invokes a local result block at `0x25e8f2068` (`blraa`, local block in `x22`, result argument in `x24`). This identifies the wrapper's synchronous plugin response as the immediate prerequisite before B can run. The likely producer is SystemAppMigrator's terminal result to `migrationpluginwrapper`; exact local-block capture → B identity still needs live confirmation rather than inference from method shape alone.

The durable wrapper-reply probe now captures the matched method entry and B, the sync-return response, and the local result-block call. It records whether that block graph contains B or B's invoke pointer; it never stops the guest. The existing exact-block invoke/return and object-equal reply-port watches remain armed. PLUGIN54 proves only that B was not invoked in its healthy 2,100-second window and that the caller remained on the resolved reply object; it does not by itself show whether the wrapper sync send returned. The new boundary hooks were not live-validated because that guest/LLDB had exited. The concrete next edge is therefore the same-request wrapper sync response: determine whether it returns with `migrationResult`, whether the local callback is B (or schedules B), and then whether the resolved reply object receives the terminal response. This is not a “never completes” or hang/deadlock finding.

Static artifacts: `/tmp/dm_run_plugin_disass_current.txt`, `/tmp/dm_handle_message_disass_current.txt`, `/tmp/datamigrator-otool.asm`, and `/tmp/dm-plugin-static/DataMigration`. Next live run should preserve the request/reply ipc_object equality and same-owner `CLIENT_SYNC_RETURN` alongside the wrapper boundary hooks. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN56–57: live wrapper sync remained open before B's result callback

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN56_LIVE_RESPONSE1` was a fresh D5825 replay child with accepted `system.tc`, CLASS16, and the fixed D572/D582/A407-scanout hypothesis settings. It revalidated SystemAppMigrator at `DM_RUN_PLUGIN_CALL_13768`: owner TPIDR `0xffffffe48fee98f0`, path `/System/Library/DataClassMigrators/SystemAppMigrator.migrator`, block B `0x16b8f99e0`, invoke `0x1046b7bd8`. The wrapper-method entry on that owner carried the same receiver and block. The post-copyin event resolved reply candidate `0xffffffe3aa8fd810`, and the same owner entered `ipc_mqueue_receive` on that object. This first run's user-space wrapper/result hooks remained armed, but the kernel object-matching subwindow used its default 600-second duration; it is preliminary for reply-object coverage. It ran to the 2,400-second probe cap with LLDB healthy and zero kernel panics.

The corrected fresh child `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN57_SYNC2400` used the same parent, trust cache, fixture, and model knobs, with `DVM_PLUGIN_MACH_WINDOW_SECONDS=2700` and a 3,000-second probe cap. Its matched SystemApp request was `0x758b02cea0`, owner TPIDR `0xffffffdee58020c0`, Mach msgID `0x40000000`. DataMigrator formed B `0x16f6259e0` with `*(B+0x10)=0x100873bd8`; the wrapper `runPluginAtPath:` entry on the same owner recorded the same B and invoke. The send's kernel-resolved reply object was `0xffffffdfcb49baf0` (task-local reply name `0x2103` is only a label). The same owner entered `ipc_mqueue_receive` on that object, and the matched request was delivered to `com.apple.migrationpluginwrapper` at the queue's `ith_kmsg` store.

The kernel send/receive/object-equality hooks remained active for the full 2,700-second matched window, which closed normally with `SYSTEMAPP_MACH_WINDOW_CLOSED`. Throughout it, no reply-object send or `ith_kmsg` handoff was recorded. The non-stopping wrapper hooks were armed at runtime `0x271f3df8c` (sync-return boundary) and `0x271f3e068` (local result-block invoke); the dynamically armed exact-B invoke was `0x100873bd8` with `x0==B`. None fired. No SystemApp `0x1376c` plugin return or same-owner sync return was recorded. Earlier `CLIENT_SYNC_RETURN` hits in the same DataMigrator TPIDR belong to ContainerMigrator, LaunchServices, or MSUDataMigrator and are not this call. LLDB remained attached until the normal 3,000-second freeze; the probe reported zero kernel panics.

This is outcome **A within the covered interval**: the wrapper's synchronous plugin send had not returned, so the local `migrationResult` callback, identity comparison between that local block and B, and B's invoke were not reached. It places the first missing edge at a response to the wrapper sync / delivery to the known reply object; it does not identify the remote result producer and does not prove a deadlock, permanent hang, or that B never runs later. The local-block-equals-B question remains unscored because the callback boundary was not reached. Artifacts are under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN56_LIVE_RESPONSE1.{lldb.log,events/,watch.log}` and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN56_LIVE_RESPONSE1.{serial,stderr}.log`, and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN57_SYNC2400.{lldb.log,events/,watch.log}` plus the matching `/tmp/dvm/probe/` logs. The replay child is `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin57-sync2400.qcow2`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN59: matched wrapper thread snapshot after SystemApp delivery

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN59_PRODUCER` used a fresh replay-D5825 child, accepted `system.tc`, the same CLASS16/D572/D582 hypothesis settings, and continuous LLDB. For matched SystemAppMigrator request `0x7bef040c60` / owner TPIDR `0xffffffeaccaac950`, DataMigrator formed B `0x16bd219e0` with invoke `0x104203bd8`. The resolved destination/reply objects were `0xffffffe9e62eefb0` / `0xffffffe9e617f790`; task-local names are labels only. The request was delivered to `com.apple.migrationpluginwrapper` on TPIDR `0xffffffeacd4f18f0`, and the registered callback ran at `0x26e19344c` with the matched event handoff pointing to this request.

At the 900-second bound, the guest-RAM process snapshot tied that same wrapper TPIDR to kernel PID 162 (`p_comm` truncated as `com.apple.migrat`). Its saved PC/LR `0x247579734` / `0x247579720` were initially mistaken for a Mach-trap location. Symbolication with this boot's dyld slide (`0xf8a4000`) identifies them as `___open_nocancel + 8` / `_open$NOCANCEL + 40` in `libsystem_kernel`, not a Mach send or receive. The saved registers are `x0=0x16dd2707e`, `x1=0x01100000`, `x2=0`; translating `x0` through this process's saved 16-KiB page tables yields `/System/Library/AppPlaceholders/Tips.app`. The local SDK defines the flags as `O_DIRECTORY | O_CLOEXEC`. The wrapper callback thread was enumerating bundle metadata: the user frames include `___opendir2`, `__CFIterateDirectory`, bundle creation, `_MICreateCFBundleEnforcingInfoPlistSize`, `_MILoadInfoPlistWithError`, `MIGlobalConfiguration` bundle-ID-map enumeration, and `-[DMXPCConnection _handleMessage:]` above libxpc event handling. This identifies the operation at the snapshot, but not its return value or whether the open was slow, blocked, or later returned; no IPC peer can be assigned to this PC.

The same RAM capture contains DataMigrator PID 120's exact request-owner TPIDR on the wrapper sync/reply-receive stack (`0x26e195f8c → 0x10420376c`, with Mach wait frames above it). Over the covered interval there was no wrapper sync return, local result callback, exact-B invoke, reply-object send/`ith_kmsg`, or same-owner SystemApp return. The captured wrapper operation is a directory open, so this snapshot has no Mach endpoint or peer to match at that point. A fresh continuous capture should follow the `open$NOCANCEL` return and the subsequent bundle-scan/result path while retaining the DM reply-object and same-owner return watches. The targeted scan produced no installcoordinationd or installd process candidate in this snapshot; that remains a sampling limit, not proof those services were absent or uninvolved. Do not infer deadlock, permanent noncompletion, or close on side `checkNecessity` / other-object traffic.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN59_PRODUCER.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN59_PRODUCER/events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN59_PRODUCER/process-stacks.json`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN59_PRODUCER/ram/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN59_PRODUCER/launch.json`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN59_PRODUCER.{serial,stderr}.log`; child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin59-producer.qcow2`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN60: open-return probe boot coverage miss

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1` used a fresh child of the verified replay-D5825 parent `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825.qcow2`, with the same accepted `system.tc`, CLASS16/D572/D582 environment, and a 2,400-second bounded run. The durable wrapper probe and its call-time IPC/reply hooks were imported before guest execution; LLDB stayed attached through the bound. The new open hook is dynamically armed only after a positive, matched SystemApp wrapper callback, then filters the observed `_open$NOCANCEL` PC to that callback's TPIDR and adds a return breakpoint at the captured LR.

This child never reached that gate: no BKS check-in, DataMigrator migration entry/return, matched SystemApp `CLIENT_SYNC_CALL`, or wrapper callback was recorded. Consequently the Tips/AppPlaceholders open hook never armed and there is no open-return, subsequent-scan, or reply result from this run. The guest emitted serial output through the full bound, reached Early Boot but not shell, and had zero XNU panics. The serial log contains 51 APFS checksum-corruption reports; these are a possible lineage/boot-coverage issue, not a proved cause of the missing migration callback. The final CPU snapshot was PC `0xfffffff02ab11eb8`; it is not assigned as the cause here. This is a pre-request coverage miss, not evidence that the PLUGIN59 open failed, blocked, or returned.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1.lldb.cmd`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1/launch.json`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1.serial.log`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1.stderr.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin60-openret1.qcow2`. The next usable run must first restore the matched SystemApp callback window; only then can it score the Tips open return and retain the reply-object/same-owner watches. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN61: corrected launch still missed the pre-request gate

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN61_OPENRET2` used a newly created qcow2 child whose backing file was the same verified D5825 parent used by PLUGIN59. `qemu-img check` passed before and after the run. The accepted `bp-patched-dev6/system.tc`, CLASS16/D572/D582 environment, boot arguments, and QEMU settings were retained. Comparing launch manifests found that OPENRET1 had omitted PLUGIN59's `-S` paused start and probe-UART socket, using direct file serial instead. PLUGIN61 restored both: QEMU started paused, LLDB attached and installed the wrapper/reply/open observers, then execution continued with the serial chardev socket.

At about 11 minutes, serial had reached Early Boot but LLDB had recorded no BKS check-in, DataMigrator entry, matched `CLIENT_SYNC_CALL`, or SystemApp wrapper callback. The dynamic Tips open observer therefore never armed; open return, post-scan/result, and reply remain unscored. The serial log had 131 lines matching explicit APFS corruption/checksum-error text at the stop, versus 22 such lines in the PLUGIN59 capture. This is a concrete lineage/boot-coverage difference even though the qcow2 backing chain and launch path were corrected; it does not prove APFS caused the missing request. HMP CPU0 was stopped at PC `0xfffffff00709a610`; no causal symbol is assigned. No completion, model, or fixture changes were made.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN61_OPENRET2.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN61_OPENRET2.lldb.cmd`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN61_OPENRET2/launch.json`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN61_OPENRET2.events/`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN61_OPENRET2.serial.log`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN61_OPENRET2.stderr.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin61-openret2.qcow2`. This is a pre-request coverage miss, not an open/reply result. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN63: replayed seed restores DataMigrator coverage, but not the SystemApp callback gate

The missing seed checkpoint was replayed from the durable PLUGIN15 ancestor, `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/disk.qcow2`, with sibling inputs `/Users/steve/dvm-artifacts/bp-patched-dev6/bootkc`, `system.dtree`, and the accepted `system.tc`. `UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS2_REPLAY` used `tools/re/setup_gate_probe.sh` with `CALLBACKS=ccs_ls_seed_progress_callbacks`, `PROBE_STOP_LLDB_REGEX='CCS_LS_SEED_PROGRESS_JSON.*LSD_SEED_SYNCWRITE_CALL'`, `KEEP_GUEST=1`, and an 1,800-second bound. It reached `LSD_SEED_SYNCWRITE_CALL` after 1,061 registration items, with `LSD_SCAN_DRIVER_RETURN`, `LSD_SYSTEM_SCAN_RETURN`, and `LSD_ENUM_RETURN` already recorded. The stop is at the `syncWrite:` call entry; a `syncWrite:` return or seed-complete marker was not observed. This replay is therefore a new checkpoint, not a byte-identical recovery of the lost seed.
The replay invocation used `PARENT=/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/disk.qcow2`, `BOOTKC=/Users/steve/dvm-artifacts/bp-patched-dev6/bootkc`, `DTREE=/Users/steve/dvm-artifacts/bp-patched-dev6/system.dtree`, `TC=/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, `CALLBACKS=ccs_ls_seed_progress_callbacks`, `PROBE_STOP_LLDB_REGEX='CCS_LS_SEED_PROGRESS_JSON.*LSD_SEED_SYNCWRITE_CALL'`, `SECS=1800`, `STALL_SECS=1800`, `KEEP_GUEST=1`, `AUTO_POSTMORTEM=0`, and `GDB_PORT=1287`; model variables were `DARWIN_DCP_EPIC=all`, `DARWIN_DCP_REPLY=1`, `DARWIN_DCP_IOMFB=4`, `DARWIN_DCP_IOMFB_RPC_TRACE=1`, and `DARWIN_SMP_PV=1`.

The replay seed is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-ccs-ls-seedprogress2-replay-20261006.qcow2`, SHA-256 `421d40e1d3c082036ae661dd57c232b00a31cf1cd51e713dbb00690a3e6c3c9d`. A plugin4 overlay copy, `/tmp/dvm/data-seed/ui-class16-refresh60-dm-plugin4-launchfrozen1-replay2.qcow2`, was metadata-rebased onto that seed; the new D5825-class parent is `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2`. The prior `...dm-ls-parent-seed-d5825.qcow2` was left untouched. `qemu-img info --backing-chain` confirms replay2 → durable boot1 disk → install disk, and the copied plugin4 layer and new parent sit above replay2. `qemu-img check` passed on the seed, copied plugin4 layer, new parent, and validation child.

The first validation attempt, PLUGIN62, omitted the explicit trust-cache override and fell back to the generic merged-system trust cache; launchd failed to load `/usr/lib/libSystem.B.dylib` and live DSC resolution failed. That is a setup error, not a lineage result. Corrected child `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN63_RESEED_OPENRET1_TCFIX` used the new parent, `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, the PLUGIN59 paused-start/probe-UART launch shape, and the same CLASS16/D572/D582/A407 settings. LLDB and the wrapper/reply/open probes were installed before continuing the guest.

PLUGIN63 is a **partial coverage recovery**: the guest reached Early Boot, DataMigrator `DM_CLIENT_MIGRATE_ENTRY` and matched sync traffic, then `ContainerMigrator` and `00LaunchServicesMigrator` plugin entries; ContainerMigrator returned. The 1,800-second probe bound expired while LaunchServices remained the latest plugin entry. There was no `BKS_CHECKIN_RETURN`, `DM_PERFORM_RETURN`, SystemAppMigrator plugin call/callback, Tips `open$NOCANCEL` return, reply-object handoff, or same-owner SystemApp return. The dynamically armed Tips observer never armed. Thus this is not yet a request-capable child for the target SystemApp callback, and it supplies no open/reply verdict. LLDB stayed attached to the bound, the probe reported zero XNU panics, and the guest was re-frozen.

For an apples-to-apples literal count of serial lines containing `obj_checksum_verify:7152:`, PLUGIN63 has 880, compared with 11 in the saved PLUGIN59 serial log and 45 in PLUGIN61. The broader corruption-line counts previously recorded for PLUGIN59/61 (22/131) used a different text filter; do not mix those metrics. The much larger PLUGIN63 count is a lineage/coverage signal only, not proof that APFS errors caused the missing SystemApp callback. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS2_REPLAY.{lldb.log,events/,probe.out,launch.json}`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS2_REPLAY.{serial,stderr}.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN63_RESEED_OPENRET1_TCFIX.{lldb.log,lldb.cmd,events/,probe.out,launch.json}`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN63_RESEED_OPENRET1_TCFIX.{serial,stderr}.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin63-reseed-openret1-tcfix.qcow2`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN64: replay2 reaches SystemApp callback; no callback-scoped Tips open observed

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN64_REPLAY2_OPENRET2` used a fresh child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin64-replay2-openret2.qcow2` from the same replay2 parent `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2`; the old D5825 parent was untouched. It used the accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, PLUGIN59's paused `-S` / probe-UART launch form, and the fixed CLASS16 hypotheses (`A406_OBJECTS=1`, `A407_TRANSFER=1`, `A407_COMPLETE=d582`, `D572=1`, `A407_SCANOUT=1`; A408 completion and scanout remained off). The configured probe bound was 3,600 seconds and the guest was retained.

The migration ladder advanced past PLUGIN63: ContainerMigrator returned, then `00LaunchServicesMigrator` returned at `1791318993.389`, about 1,412.8 seconds after its matched sync call. MSUDataMigrator returned next. SystemAppMigrator entered at `1791319003.956`; the matched `CLIENT_SYNC_CALL` was request `0x7b950383c0`, owner TPIDR `0xffffffe7d5bcc180`. The registered wrapper callback ran at `1791319004.388` on receiver TPIDR `0xffffffe7d5891120`, with `com.apple.MobileInstallation.SystemAppMigrator` present in the event. The probe dynamically armed its callback-thread `_open$NOCANCEL` observer at `0x257839734`. Neither `SYSTEMAPP_APPPLACEHOLDERS_OPEN_CALL` nor `SYSTEMAPP_APPPLACEHOLDERS_OPEN_RETURN` appeared through the one-hour bound. This is a post-gate result of **no matching Tips/AppPlaceholders open observed**; it does not show an open failure, a syscall return value, or a permanent stall.

The matched SystemApp owner entered `ipc_mqueue_receive` on its reply path at `1791319004.079`; the 516-byte msgID `0x40000000` request was handed to `com.apple.migrationpluginwrapper` at `1791319004.306`. The object-equality Mach watcher accidentally retained its 600-second default and closed at `1791319603.987`. Reply-object send/handoff is therefore scored only for that first 600-second window, not the rest of this boot. The same-owner lifecycle watch remained active; no SystemApp `CLIENT_SYNC_RETURN` / `0x1376c`, `DM_PERFORM_RETURN`, or BKS check-in return appeared by the bound. The latest plugin lifecycle was MobileSlideShow's return at `1791319623.716`, not a SystemApp return. LLDB remained attached, the guest reported zero XNU panics, and it was stopped/re-frozen at the configured bound. `qemu-img check` reported no errors on the child.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN64_REPLAY2_OPENRET2.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN64_REPLAY2_OPENRET2.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN64_REPLAY2_OPENRET2.watch.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN64_REPLAY2_OPENRET2/launch.json`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN64_REPLAY2_OPENRET2.{serial,stderr}.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin64-replay2-openret2.qcow2`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN65: wrapper callback advances through Mach RPCs; reply-object equality remains unscored

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN65_NEXTEDGE1` used a fresh child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin65-nextedge1.qcow2` from replay2 parent `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2`, the accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, the PLUGIN59 paused-start/probe-UART launch form, and the unchanged CLASS16/D572/D582 hypothesis setup. It was bounded by an external stop request at about 3,660 seconds after launch; the reply/Mach observation window was set to 4,200 seconds and stayed live through the run stop. QEMU was left paused, LLDB stayed attached until the stop, serial reported zero XNU panics, and the child passed `qemu-img check -U` with no errors.

Migration reached the SystemAppMigrator call at `0x104783768` at `1791323386.443` with completion block `0x16b7a19e0` and invoke target `0x104783bd8`. The registered wrapper callback ran at `1791323540.877` on TPIDR `0xffffffeb36c5e890` with the SystemAppMigrator event marker. The first observed post-callback Mach operation on that TPIDR was an 80-byte message (Mach ID `0x10000000`) carrying `pid`/`msgID` fields to task-local port `0x4203`; its trap returned `x0=0`. Subsequent wrapper traffic included RPCs for `getMigrationServiceWithReply:`, `cleanupModelForDataMigrationForRestoreType:reply:`, and `dataMigrationWillFinishWithReply:`. Two `mach_msg2` calls on this callback TPIDR returned `x0=0` after approximately 1,117 and 1,121 seconds; one was receive-only, while the other used a send-and-receive vector. These samples show long waits followed by resumed wrapper activity, not one stable callback-thread wait through the entire bound.

At `1791324270.801`, the wrapper also sent a 68-byte Mach message with ID `0x20000000` whose body contains `migrationResult`; the trap returned `x0=0`. Its task-local destination (`0x4303`) was not resolved to an `ipc_port*` and was not compared equal to the original DataMigrator reply object. The run produced no `IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS` event, no matched SystemApp plugin return at `0x1376c`, no completion-block invoke/return, no same-owner sync return, no `DM_PERFORM_RETURN`, and no BKS check-in return. Thus the send is a migration-result candidate, not proof of a terminal reply. The reply watch ran through the full observed SystemApp interval (about 2,145 seconds from SystemApp entry to stop), fixing PLUGIN64's 600-second cutoff, but without a resolved reply object there is no object-equality verdict about a reply handoff.

The callback-scoped open hook did fire at its configured boundary, but its captured `x0` values were fd-like small integers, flags were `0`, and the path pointer could not be read. Raw return values included `3`, `4`, `5`, and `-1`; because the syscall argument layout/path was not validated, these are not a Tips/AppPlaceholders open result. Tips remains unscored. Do not infer a hang, deadlock, or permanent noncompletion from the bounded interval.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN65_NEXTEDGE1.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN65_NEXTEDGE1.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN65_NEXTEDGE1.watch.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN65_NEXTEDGE1/launch.json`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN65_NEXTEDGE1.{serial,stderr}.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin65-nextedge1.qcow2`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.


#### PLUGIN66: request reply object resolves; later reply join is invalidated by mutable probe state

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN66_REPLYJOIN1` used a fresh child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin66-replyjoin1.qcow2` backed by `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2`, accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, the PLUGIN59 paused-start/probe-UART launch shape, and the unchanged CLASS16/D572/D582 hypothesis setup. The 4,200-second probe bound was reached by an explicit stop-file condition after more than 2,600 seconds from the matched SystemApp request. The child was left frozen; LLDB had remained attached through the covered window; serial reported zero XNU panics.

The matched `SystemAppMigrator` sync was request `0x7644c54a80` on owner TPIDR `0xffffffe0d16bd120`, with Mach2 ID `0x40000000` and task-local names destination `0x2557` / reply `0x3e1b`. Post-copyin sampling resolved the 516-byte request's destination object to `0xffffffdfea9d22c0` and reply object to **`0xffffffdfeb36f3a0`**. The same destination object was then seen at the wrapper's `ith_kmsg` handoff. This closes reply-object resolution for the original request and confirms its delivery to the wrapper; it does not establish a terminal response.

The running probe did not preserve an immutable comparison target after that handoff. Later SystemApp-shaped sends from other owners could replace the global reply target; emitted `IPC_REPLY_DEST_COPYIN_CANDIDATE` “same object” labels therefore compare against a mutable candidate, not reliably against the original `0xffffffdfeb36f3a0`. Direct inspection of serialized event headers found no later recorded header containing that original object, but the mutable filter means this is not evidence that no such send occurred. In particular, the `migrationResult`-to-original-reply join is **unscored**. The source was corrected during this run to pin the original owner/object pair and retain later sends as candidates; that edit was not loaded into this already-running LLDB session and remains unvalidated on boot.

No same-owner SystemApp `CLIENT_SYNC_RETURN` / `0x1376c`, plugin return, `DM_PERFORM_RETURN`, or BKS check-in return was observed through the covered window. Other owners' return markers are excluded. This is not a hang/deadlock or permanent-noncompletion finding. Tips/open and poster/draw remain out of scope. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN66_REPLYJOIN1.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN66_REPLYJOIN1.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN66_REPLYJOIN1.watch.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN66_REPLYJOIN1/launch.json`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN66_REPLYJOIN1.{serial,stderr}.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin66-replyjoin1.qcow2`. The child passed `qemu-img check -U`; the three touched probe modules passed `py_compile` and `git diff --check`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.


#### PLUGIN67: reply-object pin validated; no object-equal send observed in the bounded window

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN67_REPLY_PIN1` used fresh child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin67-reply-pin1.qcow2` from the replay2 D5825 parent `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2`, accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, the PLUGIN59 paused-start/probe-UART launch settings, and the unchanged CLASS16/D572/D582 hypothesis stack. The current wrapper/kernel probe was imported before guest continuation. The run reached its 4,200-second bound and was explicitly stopped/frozen; it covered about 2,686 seconds after the matched SystemApp request. Serial reported zero XNU panics.

SystemAppMigrator request `0x7d10c30900` entered at `1791334991.801` on owner TPIDR `0xffffffde1b5f8180` (Mach2 ID `0x40000000`; local destination/reply names `0x2557` / `0x3e1b`). The same-owner post-copyin join resolved the 516-byte request to destination **`0xffffffde1cabc730`** and reply **`0xffffffde1c6715d0`**; the destination matched wrapper `ith_kmsg`. This child therefore revalidates original request resolution and delivery.

The immediate pin fix is live-validated. The matched wrapper callback thread was TPIDR `0xffffffde1b6b98f0`. Its 939 captured Mach sends all had `reply_candidate_match=false` against the pinned reply object. Thirteen later SystemApp-shaped kernel send candidates came from other TPIDRs; each was marked `owner_match=false`, `candidate-only; original reply pair retained`, and carried destination/reply objects distinct from `0xffffffde1c6715d0`. Thus later owners no longer replace the request's comparison key. The global reply-destination copyin watch emitted no object-equal send candidate in the 2,686-second window.

The matched callback did not emit an XPC message identified as `migrationResult`. Four `WRAPPER_XPC_SEND` markers carrying `migrationResult` appeared on other TPIDRs (including `0xffffffde1b713060`, `0xffffffde1b1fd120`, and `0xffffffde1b498180`); without connection/transaction or kernel-object linkage they are not assigned to this SystemApp request. Their task-local or XPC fields are not a substitute for the pinned Mach object. Therefore the `migrationResult`-to-original-reply equality remains **unscored**; the bounded kernel send watch found no equal destination, but this is not a claim that no terminal response can occur later.

No same-owner SystemApp `CLIENT_SYNC_RETURN` / `0x1376c`, `DM_PERFORM_RETURN`, SystemApp plugin return, or BKS check-in return was observed. The run does not establish a hang, deadlock, or permanent noncompletion. Tips/poster/draw remain parked; D572/D582 remain QEMU hypotheses. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN67_REPLY_PIN1.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN67_REPLY_PIN1.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN67_REPLY_PIN1.watch.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN67_REPLY_PIN1.lldb.cmd`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN67_REPLY_PIN1.{serial,stderr}.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin67-reply-pin1.qcow2`. The child passed `qemu-img check -U`; the three related probe modules passed `py_compile`, and `git diff --check` passed.


#### PLUGIN68: candidate-sender expansion not scored; fresh replay2 child missed the request gate

UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN68_MIGRESULT_JOIN1 used a fresh child /tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin68-migresult-join1.qcow2 backed by the PLUGIN64–67 replay2 parent /tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2, accepted system.tc, and the same CLASS16/D572/D582/A407-scanout launch settings. The corrected probe was imported before the request window and LLDB event capture was enabled. Its source now records any post-pin XPC migrationResult candidate by sender TPIDR and adds that owner to the kernel send-helper filter; the kernel event records the actual kmsg destination ipc_object*, compares it to the immutable reply pin, and labels same-thread/time association as a candidate rather than a transaction join.

This boot did not reach the prerequisite SystemApp request. No DM migration entry, SystemApp CLIENT_SYNC_CALL, request-object pin, wrapper handoff, migrationResult XPC candidate, or kernel candidate event appeared in the covered window. Early Boot completed, then serial stopped advancing through launchd/service markers while the VM remained running; the APFS obj_checksum_verify:7152 count reached 1,158 and then stayed flat. This is a pre-request coverage miss. The PLUGIN67 reply object cannot be reused in this child, and the new XPC-to-kernel-destination join remains unvalidated; no reply equality, no-reply, or sync-return conclusion follows from this run. The checksum errors are a lineage signal only, not a proven cause.

The child was left host-paused after the 33-minute observation window. qemu-img check -U reported no image errors before the pause. Artifacts: /tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN68_MIGRESULT_JOIN1.events/, /tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN68_MIGRESULT_JOIN1.serial.log, /tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN68_MIGRESULT_JOIN1.stderr.log, and child /tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin68-migresult-join1.qcow2. The probe edits are read-only; D572/D582 remain QEMU hypotheses and poster/draw remain parked.

#### PLUGIN69: replay2 retry still missed SystemApp; replay3 reseed started

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN69_MIGRESULT_JOIN2` used a fresh child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin69-migresult-join2.qcow2` backed directly by `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2`. It used the accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, PLUGIN59's `-S`/probe-UART startup shape, the CLASS16/D572/D582 settings, and explicit A408 completion/scanout off. The probe was imported before the first continue. The original PLUGIN67 launch manifest was unavailable, so this retry's settings were reconstructed from the saved replay2 launch manifest and PLUGIN67 startup record; no parent or fixture was substituted.

At 3,600 seconds, Early Boot had completed but no migration-probe event appeared: no DM migration entry, SystemApp `CLIENT_SYNC_CALL`, reply pin, wrapper delivery, or `migrationResult` sender candidate. LLDB remained attached. The literal `obj_checksum_verify:7152:` count was 1,184 versus 1,158 in PLUGIN68, then plateaued. This second consecutive pre-DM miss with a small increase in that APFS signal triggered the documented reseed path; it is not an IPC result or a causal APFS diagnosis. PLUGIN67 remains the latest request-scoped evidence: reply pin held, 939 matched-callback-thread sends were unequal to it, and `migrationResult` XPC on other TPIDRs still lacked a kernel-destination join.

The PLUGIN63 seed-progress procedure was replayed from durable `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/disk.qcow2`, using its sibling `bootkc`, `system.dtree`, and accepted `system.tc`. `UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS3_REPLAY` used the existing `ccs_ls_seed_progress_callbacks` probe, stopped on `LSD_SEED_SYNCWRITE_CALL`, and reached that call after 1,061 registration entries. Scan-driver, system-scan, and enumeration returns were recorded; `syncWrite:` return/seed-complete was not. Durable seed `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-ccs-ls-seedprogress3-replay-20261006.qcow2` SHA-256 is `7d7603a6ee5c52dc6e16568a12360a0f86625edd08d28be3cc1ce45f38e8f9d7`; its `/tmp` copy matches. Copied plugin4 and D5825-class overlays were rebased to form `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay3.qcow2`; replay2 media was preserved. `qemu-img check` passed for the seed and rebased layers.

#### PLUGIN70: replay3 validation still missed the request gate

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN70_MIGRESULT_JOIN3` booted a fresh child from the replay3 D5825-class parent with the same accepted trust cache, startup shape, and fixed hypothesis environment. The current pinned-reply/candidate-sender probe was loaded before continuing. At 3,600 seconds it had completed Early Boot but emitted no DM/SystemApp event, pin, wrapper handoff, or `migrationResult` candidate. LLDB stayed attached and the child was host-paused; `qemu-img check -U` passed. The `obj_checksum_verify:7152:` count was 1,150, below PLUGIN69's 1,184, so the user-specified rising-count condition did not repeat after reseeding. This remains a pre-request coverage miss: no reply equality, no-reply, hang, or deadlock conclusion is drawn. PLUGIN67 remains the last valid pin/unequal-send result; PLUGIN68–70 do not alter it.

Artifacts: PLUGIN69 manifest `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN69_MIGRESULT_JOIN2.launch.json`, logs `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN69_MIGRESULT_JOIN2.{lldb.log,events/}` and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN69_MIGRESULT_JOIN2.serial.log`; seed3 checkpoint and PLUGIN70 child/logs are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/` and `/tmp/dvm/`. No D572/D582 or completion hypotheses were changed; poster/draw remain parked.


#### PLUGIN71: lean replay3 gate still missed DM; broad-probe tax not supported

The lean arm, `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN71_GATE_LIGHT_A1`, used a fresh child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin71-gate-light-a1.qcow2` from replay3 D5825 parent `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay3.qcow2`, accepted `system.tc`, and the same PLUGIN70 boot args, CLASS16/D572/D582 environment, and `DARWIN_DCP_IOMFB_ECHO_MODE=1`. It installed only two auto-continue callbacks at startup: backboardd `DM_CLIENT_MIGRATE_ENTRY` (`0x25f040af8`) and DataMigrator `CLIENT_RUN_ENTRY` (`0x25f03da10`). Sync-call/return callbacks were dynamically armed only for the exact SystemAppMigrator path. The probe callback list was verified and `ready.gate-light.json` was written; no SystemApp path appeared, so the dynamic sync hooks never armed.

At the 3,600-second bound, PLUGIN71 had no DM migration entry, SystemApp run/sync entry, request pin, wrapper delivery, or `migrationResult`; LLDB remained attached, only the readiness event existed, and serial showed no panic/reboot. PLUGIN70's full probe on the same replay3 parent missed the same request gate. Both runs had 47 normalized launchd records with the same early task sequence and the same WebBookmarks/webinspectord missing-executable messages as their final launchd milestones; the remaining serial tail was IOMFB power-status traffic. PLUGIN71's `obj_checksum_verify:7152:` count was 1,216, versus PLUGIN70 1,150, PLUGIN69 1,184, PLUGIN64 1,080, and request-positive PLUGIN67 1,253. The overlap makes the APFS count a lineage signal, not a causal discriminator. This A/B weighs against the broad join probe alone suppressing DM, but does not prove image failure or name the missing prerequisite.

The PLUGIN70/71 launch manifests match in accepted TC, bootkc, dtree, ramdisk, boot args, CLASS16 fixture, and hypothesis settings; only tag-derived paths and GDB port differ. PLUGIN64 lacked the ECHO_MODE key, but PLUGIN67's saved QEMU stderr explicitly reports block-0x41 mode echo enabled on a request-positive boot. PLUGIN67's launch manifest is missing, so an exact full manifest comparison is unavailable. The remaining lineage distinction is replay3: its seedprogress3 checkpoint stopped at `LSD_SEED_SYNCWRITE_CALL` without a recorded return before the plugin4/D5825 rebase; PLUGIN67 used replay2.

No join verdict follows: `migrationResult`→pinned reply was not exercised. Next, use a coverage-light fresh child of the existing request-positive PLUGIN67 overlay; only arm the object-equality join after the matched DM/SystemApp gate. If it misses too, compare the first launch/job divergence from the known-positive window before replaying another seed. No reply/hang/deadlock conclusion is drawn from these pre-request misses.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN71_GATE_LIGHT_A1.launch.json`, `.launch.sh`, `.lldb.cmd`, `.lldb.log`, `.events/`, serial `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN71_GATE_LIGHT_A1.serial.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin71-gate-light-a1.qcow2`. The child was host-paused and passed `qemu-img check -U`. `tools/re/dm_gate_light_callbacks.py` passed `py_compile`; shell launch syntax and `git diff --check` passed. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN72: direct PLUGIN67-overlay child missed the DM gate; deferred reply join unexercised

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN72_GATE_LIGHT_JOIN1` booted fresh child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin72-gate-light-join1.qcow2`, backed directly by `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin67-reply-pin1.qcow2`. The verified backing chain continued through replay2 D5825/plugin4 and durable seedprogress2. The source and resulting child passed `qemu-img check -U`. Launch used accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, `-S` plus probe UART, unchanged CLASS16/D572/D582/ECHO settings, and A408 completion/scanout disabled. The launch was cloned from PLUGIN71 because PLUGIN67's manifest is absent; it matches the known PLUGIN67 setup facts but is not claimed byte-identical.

At reset the light probe installed only backboardd `DM_CLIENT_MIGRATE_ENTRY` (`0x25f040af8`) and DataMigrator `CLIENT_RUN_ENTRY` (`0x25f03da10`). The optional `DVM_GATE_LIGHT_ARM_REPLY_AFTER_SYSTEMAPP=1` path would import/install the pinned reply matcher only after a callback positively identifies `com.apple.MobileInstallation.SystemAppMigrator`. Through just over 3,600 seconds, only `ready.gate-light.json` existed: no DM migration entry, SystemApp path/sync entry, reply pin, wrapper delivery, or `migrationResult`. Thus the deferred matcher never armed; its on-boot behavior is unvalidated, and the reply join is unscored. No child-local reply pointer exists, and PLUGIN67's pointer was not reused.

Serial reached 47 launchd records, including both missing-executable reports for WebBookmarks and webinspectord, then continued with IOMFB power/RPC output. Relative to Early Boot complete, PLUGIN72 logged webinspectord at +169 s and WebBookmarks at +1,960 s. PLUGIN67's request-positive log had WebBookmarks at +150 s and webinspectord at +158 s; PLUGIN71 had them at +220 s and +241 s. The changed order and long WebBookmarks delay is the first concrete launchd divergence from the positive boot, although both expected jobs eventually appear. The literal APFS checksum marker count was 159 (PLUGIN67: 1,253; PLUGIN71: 1,216), which is descriptive only. A direct PLUGIN67-overlay child also missing the gate means replay3 lineage alone cannot explain the latest misses. The missing PLUGIN67 launch manifest and this boot's timing/order shift leave snapshot state versus host/runtime drift unresolved; do not launch another seed replay on this evidence alone.

The QEMU was paused with HMP at the bound; LLDB remained attached, serial showed no panic/reboot, and `qemu-img check -U` reported no errors. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN72_GATE_LIGHT_JOIN1.{launch.json,launch.sh,lldb.cmd,lldb.log,stderr.log}`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN72_GATE_LIGHT_JOIN1.events/`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN72_GATE_LIGHT_JOIN1.serial.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin72-gate-light-join1.qcow2`. The deferred join code is present in `tools/re/dm_gate_light_callbacks.py`, but no positive gate validated it. Syntax, launch script, `py_compile`, `git diff --check`, and qcow integrity checks passed. PLUGIN67 remains last request-scoped truth; D572/D582 are QEMU hypotheses, with poster/draw parked.

#### PLUGIN73: replay2 gate-light control also misses DataMigrator

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN73_REPLAY2_GATE_LIGHT_JOIN2` booted a new qcow child directly from the replay2 D5825 parent (`/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2`), not from PLUGIN67's post-request overlay and not from replay3. The verified chain reaches replay2/plugin4 and the durable seedprogress2 checkpoint. Parent and child passed `qemu-img check -U` before launch, and the child passed after the run. The launch reused PLUGIN72's recorded settings because the PLUGIN67 launch manifest is missing: accepted `system.tc`, paused start plus probe-UART, CLASS16/D572/D582/ECHO held fixed, and A408 completion/scanout off.

LLDB attached before continue with only the gate-light backboardd migration-entry and DataMigrator plugin-entry callbacks. The optional reply-object matcher was set to arm only at a matching `com.apple.MobileInstallation.SystemAppMigrator` run entry. At the HMP stop after approximately 1 h 52 min of healthy QEMU/LLDB runtime, the event directory contained only `ready.gate-light.json`; neither gate fired and the deferred reply matcher did not load. There was no request pin, wrapper delivery, `migrationResult` candidate, or reply equality to score. Serial showed no XNU panic or critical-process reboot.

This replay2 control's 47 normalized launchd records match the early prefix from PLUGIN71 and the surviving PLUGIN67 serial evidence: WebBookmarks (+147 s) then webinspectord (+149 s) after Early Boot. PLUGIN72 instead logged webinspectord at +169 s and WebBookmarks at +1,960 s. In PLUGIN73 launchd did not advance beyond the webinspectord record at guest time `12:15:17`; the remaining serial tail was IOMFB power-status traffic. PLUGIN67's event log proves that its live run later entered SystemAppMigrator, but its serial capture is truncated at this common prefix. Accordingly, the first observable split is the missing later DM/SystemApp event, not a distinct earlier launchd record. The probe does not name the first service/job dependency behind that split.

PLUGIN72's cold child of the PLUGIN67 overlay and PLUGIN73's direct replay2-parent child both miss DM, so the theory that PLUGIN67's disk epoch alone explains cold-boot misses is insufficient. PLUGIN64 reached SystemApp from replay2, making host/runtime or boot progression variability after the shared prefix plausible, but not identified. The literal APFS marker count was 1,323 and flat at stop; it is descriptive only. This run is a pre-request coverage miss, not a reply-path, hang, or deadlock result, and does not justify another seed replay.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN73_REPLAY2_GATE_LIGHT_JOIN2.{launch.json,launch.sh,lldb.cmd,qemu.out}`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN73_REPLAY2_GATE_LIGHT_JOIN2.events/`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN73_REPLAY2_GATE_LIGHT_JOIN2.serial.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin73-replay2-gate-light-join2.qcow2`. Gate-light and wrapper-reply probes passed `py_compile`; the launch script passed `bash -n`; `git diff --check` and `qemu-img check -U` passed. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN73 frozen-process cut: DataMigrator is absent at the miss snapshot

The PLUGIN73 guest was inspected while still paused after its 1 h 52 min gate-light miss. A full 12-GiB HMP RAM snapshot plus `warm_boot_postmortem.py` found SpringBoard PID 35, backboardd PID 72, launchd PID 1, installd PID 187, installcoordinationd PID 335, and `com.apple.migrationpluginwrapper` PID 198. No process-name candidate for `DataMigrator` / `com.apple.datamigrator` was present. This supports “DataMigrator was not resident at the snapshot”; it cannot distinguish never launched from an earlier exit. The guest was not resumed or modified for this inspection.

The first absent event after the common 47-record launchd prefix is therefore DataMigrator launch/activation and backboardd `DM_CLIENT_MIGRATE_ENTRY` (`0x25f040af8`); DataMigrator `CLIENT_RUN_ENTRY` (`0x25f03da10`) is also absent. This localizes the missing process/event but does not identify why it was not launched. Four launchd threads were waiting on `0xfffffff027011654` with user frames `0x1c42fef50` / `0x100f99740`, but that wait is unclassified and cannot be tied to a DataMigrator job. SpringBoard, backboardd, migrationpluginwrapper, installd, and installcoordinationd were present, so none was simply absent from this process snapshot.

The positive contrast remains PLUGIN64/67: DataMigrator entry at `1791317538.519` / `1791333533.657`, then SystemAppMigrator at `1791319003.952` / `1791334991.787`. PLUGIN73 has the same early WebBookmarks→webinspectord prefix but neither DM entry. Its reply matcher never armed; this is not an IPC result. The next targeted retry should add a launchd process-launch/exec hook filtered to the DataMigrator executable beside the two gate-light breakpoints. Do not spend another long gate-only window until that launch-attempt discriminator is implemented and validated. The launchd wait is not evidence of a hang or deadlock; no seed replay or model change was made.

#### PLUGIN74–75: replay2 reaches SystemApp; deferred object resolver needed owner seeding

Two fresh replay2-parent children with the launchd observer reached DataMigrator and SystemApp. PLUGIN74 recorded `DM_CLIENT_MIGRATE_ENTRY` at `1791409850.335`, SystemApp `CLIENT_RUN_ENTRY` at `1791410465.256`, and `CLIENT_SYNC_CALL` at `1791410465.339`; the deferred wrapper-reply callback then failed with `NameError: handoff_match is not defined` (and an adjacent undefined `connection`), ending coverage before object resolution. PLUGIN75 corrected the exception and recorded DM entry at `1791410667.866`, SystemApp entry at `1791411459.139`, and sync call at `1791411459.235`. It still did not emit `IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS`: the reply composition installed lifecycle callbacks only after the live `CLIENT_RUN_ENTRY` breakpoint had fired, leaving the inner per-owner state empty at the subsequent sync callback.

The deferred setup now copies the current gate's SystemApp owner, path, receiver, and completion block into the lifecycle/transport tables. PLUGIN76 (`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN76_REPLAY2_REPLYJOIN_SEED1`) is the fresh validation on the same replay2 parent. PLUGIN74/75 establish gate coverage only; wrapper XPC events are not terminal responses and the original reply object remains unresolved in those runs. No reply/no-reply, hang, or deadlock result follows. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN78 follow-up: replay2 gate restored; upstream DataMigrator activation remains unidentified

The targeted child `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN78_REPLAY2_REPLYJOIN_SEED3` reached backboardd `DM_CLIENT_MIGRATE_ENTRY` at `1791413668.129`, then SystemAppMigrator `CLIENT_RUN_ENTRY` at `1791414383.147` and `CLIENT_SYNC_CALL` at `1791414383.235`. This shows the PLUGIN73 gap after the shared launchd prefix is not a stable replay2-parent prerequisite: a same-lineage child passed the gate. Launchd `posix_spawn`/`posix_spawnp`/`execve` observation recorded ordinary process samples, but no DataMigrator-path spawn event. The actual activation mechanism remains unobserved; DataMigrator could have started through another API or outside the captured call path. PLUGIN73's frozen snapshot proves DataMigrator was not resident then, not that launchd never tried. The first missing observable edge remains DataMigrator activation and backboardd migration entry; no upstream service or dependency is named.

The deferred resolver pinned original reply object `0xffffffe02afabf70` for SystemApp request `0x77010487e0` (owner TPIDR `0xffffffe02aaf6890`) and matched the 516-byte msgID `0x40000000` request destination `0xffffffe02adff3a0` to wrapper delivery. The DataMigrator owner entered `ipc_mqueue_receive` on the reply object.

Three 76-byte msgID `0x20000000` kmsgs whose bodies contain `migrationResult` were linked to XPC observations by same sender TPIDR, short timing, and matching body key. Their destinations were `0xffffffe02cadbd30`, `0xffffffe02ba38cd0`, and `0xffffffe02c55afb0`; each differs from the pinned reply. These are strong candidate joins, not transaction-ID joins, and none proves the terminal response for this SystemApp request.

The child paused at a debugger stop before the configured 4,800-second bound. Through the last recorded wrapper event at `1791414528.929`, there was no reply-object handoff or same-owner `CLIENT_SYNC_RETURN`/`0x1376c`. This is not a no-reply, hang, or deadlock conclusion. PLUGIN78 completes the targeted gate retry and validates the pin/destination comparison, but does not identify the upstream PLUGIN73 activation cause. The next useful step is a narrow capture of the actual DataMigrator activation mechanism on a miss boot; no seed replay is warranted. Artifacts are under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN78_REPLAY2_REPLYJOIN_SEED3.*` and its `.events/` directory. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN79: launchd starts DataMigrator through its XPC service

A fresh child of the existing replay2 D5825 parent was run with the accepted `system.tc`, established CLASS16/D572/D582 profile, and a coverage-light launch observer. The probe captured the previously missed service label from spawn argv rather than requiring “DataMigrator” in the executable path. At `1791418712.016007`, launchd PID 1 issued `posix_spawnp("/usr/libexec/xpcproxy", argv=["xpcproxy", "com.apple.datamigrator"])`; a nested `posix_spawn` for the same request followed at `1791418712.024323`. Both launchd calls returned zero and reported child PID 122. The captured launchd stack starts `0x1027a8598 → 0x1cb86efc0 → 0x1cb859700` (nested call: `0x1027a14dc → 0x1027a8598`).

The spawned `xpcproxy` then called `posix_spawn` for `/System/Library/PrivateFrameworks/DataMigration.framework/XPCServices/com.apple.datamigrator.xpc/com.apple.datamigrator`, with `XPC_SERVICE_NAME=com.apple.datamigrator` in its environment (`1791418712.203852`; caller PCs `0x102e40cbc → 0x19d4235b8`). This is the runtime activation route on a positive child: launchd's `com.apple.datamigrator` XPC job → `/usr/libexec/xpcproxy` → the DataMigration framework XPC-service executable. The inner xpcproxy spawn return was not recorded before the probe stopped on backboardd `DM_CLIENT_MIGRATE_ENTRY` at `1791418755.717777`; DataMigrator `CLIENT_RUN_ENTRY` was not part of this short activation score. Do not promote the inner call to a confirmed successful DataMigrator exec.

This validates a hit-side route that PLUGIN78's executable-path-only launch observer missed. It does not say which stage is absent or fails on PLUGIN73: its frozen snapshot proves only that DataMigrator was not resident at the snapshot. A miss-side comparison must now watch for the `com.apple.datamigrator` xpcproxy argv label, the launchd spawn result/PID, the inner executable spawn/return, and the DM run entry. Until that comparison, the post-prefix activation miss remains unexplained; no hang/deadlock or terminal-reply conclusion follows. Artifacts are under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN79_ACTIVATION1.*` and the corresponding `.events/` and serial/stderr logs. No seed replay or model change was made. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN80: positive activation control closes through SystemApp client entry

Fresh replay2 child `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN80_ACTIVATION2` reproduced launchd's `com.apple.datamigrator` service request: `/usr/libexec/xpcproxy` was spawned with argv `xpcproxy com.apple.datamigrator` at `1791420606.823433`; the nested launchd spawn returned zero with child PID 121 at `1791420606.878711`. The xpcproxy process then spawned the DataMigration framework service executable with `XPC_SERVICE_NAME=com.apple.datamigrator` at `1791420607.005887`. This observer used argv matching, so it covers the launchd label that PLUGIN78's executable-path-only filter missed.

The inner spawn return breakpoint was dynamically installed at `0x102fad944` but produced no event. The current callback silently rejects hits whose LLDB thread ID or TPIDR differs from the call sample; therefore inner return status is unresolved. Downstream gates did fire: backboardd `DM_CLIENT_MIGRATE_ENTRY` at `1791420645.138169`, followed by DataMigrator `CLIENT_RUN_ENTRY` for `com.apple.MobileInstallation.SystemAppMigrator` at `1791421586.061417`, with completion block `0x16ff259e0`. `GATE_ACTIVATION_CHAIN_COMPLETE` confirms both entries on the same boot. This positive control took about 940.9 seconds from DM migration entry to SystemApp run entry.

PLUGIN80 did not reproduce a miss and does not classify PLUGIN73's absent activation stage. It validates the hit path through the SystemApp run entry, while the inner xpcproxy return remains a probe-coverage gap. No reply matcher was armed; this boot makes no reply/no-reply or hang claim. Artifacts are under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN80_ACTIVATION2.*` and the `.events/` directory. Image check passed; no model, fixture, seed, A408, or completion changes were made. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN81–82: full-bound activation controls did not reproduce the miss

The two fresh replay2 children, `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN81_ACTIVATION3` and `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN82_ACTIVATION4`, each ran for the 6,720-second bound with the unchanged D5825 parent, accepted `system.tc`, and the CLASS16/D572/D582/ECHO profile. Both were hits, so neither classifies PLUGIN73's miss.

PLUGIN81 captured launchd's `xpcproxy com.apple.datamigrator` request, a successful outer return (`x0=0`, PID 153), and the xpcproxy spawn of the DataMigration framework executable. It reached `DM_CLIENT_MIGRATE_ENTRY` at `1791423572.554` and SystemAppMigrator `CLIENT_RUN_ENTRY` at `1791424328.826`, then `CLIENT_SYNC_CALL`. PLUGIN82 likewise captured the label spawn at `1791430866.743`, successful outer return (`x0=0`, PID 122) despite an LLDB thread-ID change, and the inner framework-executable spawn with `XPC_SERVICE_NAME=com.apple.datamigrator` at `1791430866.932`. It reached DM migration entry at `1791430905.169` and SystemApp client entry at `1791431716.733`. P82 ended at its full wall bound with zero panics and no shell.

The dynamic inner-return breakpoint produced shared-PC candidates, but none matched the saved xpcproxy call context; observed candidates included `fairplayd.H2` and wrapper-service activity. Therefore the inner spawn return remains unscored, not failed. The diagnostic callback now reports candidates before filtering; after P82, its match key was changed to saved SP plus TPIDR, leaving thread ID and process name diagnostic-only. That final matcher revision is not yet boot-validated. Result: two full-bound hits, zero misses, and PLUGIN73's first absent stage remains unknown. The reply join was parked; no reply, hang, or deadlock conclusion follows. Artifacts are under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN81_ACTIVATION3.*` and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN82_ACTIVATION4.*`; `qemu-img check -U` found no P82 image errors. No seed or model changes were made. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN86: outer return validated; inner return unmatched; full-window reply-object comparison

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN86_ACT_RETURN_REPLY4` used a fresh replay2 D5825 child, accepted `system.tc`, and the unchanged CLASS16/D572/D582/ECHO profile. The gate-light observer captured `DM_CLIENT_MIGRATE_ENTRY`, SystemAppMigrator `CLIENT_RUN_ENTRY` at `1791472684.813025`, and matched `CLIENT_SYNC_CALL` at `1791472684.901463` (request `0x7c5ec408a0`, owner TPIDR `0xffffffe2e53f3060`). The deferred matcher armed from this live SystemApp entry.

The revised saved-SP-plus-TPIDR matcher validated on the outer launchd→xpcproxy spawn: nested outer returns for the datamigrator service matched their saved call contexts with `x0=0`, including a return despite an LLDB thread-ID change. The inner framework-executable spawn call was observed from xpcproxy at `1791472011.1233659` with `XPC_SERVICE_NAME=com.apple.datamigrator`, saved SP `0x16b1db040`, and TPIDR `0xffffffe2e56a5120`. A shared-PC candidate later appeared in configd (`1791477263.866025`, SP `0x107386460`, TPIDR `0xffffffe2e52a3060`), matching neither saved value. Three further candidates belonged to a different spawn context and also failed that context's saved SP/TPIDR key. The inner return therefore remains unobserved; the mismatches are not spawn-failure evidence.

For this request, post-copyin resolution plus the wrapper's `ith_kmsg` established destination object `0xffffffe200854220`, original reply object `0xffffffe2004fbaf0`, and delivery of the 516-byte msgID `0x40000000` request. The probe held the original reply key through the explicit `SYSTEMAPP_MACH_WINDOW_CLOSED` marker at 7,200 seconds (`1791479884.935322`). Across that interval it recorded 20 kernel-send candidates, of which five contained the `migrationResult` body key. Their resolved destinations were `0xffffffe2004a3a60`, `0xffffffe200726d70`, `0xffffffe2004a3a60`, `0xffffffe200726d70`, and `0xffffffe200842080`; all five were unequal to the pinned reply. The XPC-to-kernel sender association remains a candidate join without a transaction ID, so no send is labeled the terminal response for this request.

No same-owner `CLIENT_SYNC_RETURN`, SystemApp plugin return/`0x1376c`, `DM_PERFORM_RETURN`, or BKS check-in return occurred in the complete observed window. This bounded result does not establish a hang or permanent noncompletion, and it does not classify PLUGIN73's activation miss. The run stopped on `SYSTEMAPP_7200S_WINDOW_COMPLETE`; the guest was left frozen, there were zero panics, and `qemu-img check -U` found no image errors. Artifacts are under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN86_ACT_RETURN_REPLY4.*` and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN86_ACT_RETURN_REPLY4.{serial.log,stderr.log}`. No seed, model, fixture, A408, or completion change was made. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN87: reply-object discriminator not reached

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN87_ANY_REPLY_DEST1` used a fresh child of the replay2 D5825 parent with accepted `system.tc` and the fixed CLASS16/D572/D582 launch profile. The coverage-light probe reached backboardd `DM_CLIENT_MIGRATE_ENTRY` at `1791480772.355`, but no SystemAppMigrator `CLIENT_RUN_ENTRY` or `CLIENT_SYNC_CALL` followed. Consequently there was no request-local reply pin, no `migrationResult` sender candidate, and no complete 7,200-second reply window. The deferred reply module was never imported, so the newly added all-sender destination-equality watch and receiver joins remain syntax-checked but not boot-validated.

The guest then panicked with `Non-monotonic time: last_dispatch at 0x38d9c1ef, ctime 0x38d9a797 @sched_prim.c:815`, followed by nested panic output. The probe stopped on the nested-panic limit. Classify P87 as a boot coverage failure; it supplies no peer, reply, sync-return, no-reply, or hang evidence. `qemu-img check -U` found no errors in the fresh child. Logs and events are under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN87_ANY_REPLY_DEST1.*` and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN87_ANY_REPLY_DEST1.*`. D572/D582 remain labeled QEMU hypotheses; poster/draw remain out of scope.

#### PLUGIN88: full any-send reply-object window; migrationResult destinations resolve to other peers

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN88_ANY_REPLY_DEST2` used a fresh replay2 child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin88-any-reply-dest2.qcow2`, accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, and the unchanged CLASS16/D572/D582/ECHO profile. The coverage-light gate reached `DM_CLIENT_MIGRATE_ENTRY` at `1791482002.72402`, then SystemAppMigrator `CLIENT_RUN_ENTRY` at `1791483496.787873`. The deferred reply probe loaded at `1791483496.902039` for request `0x79a7040a20`, owner TPIDR `0xffffffe9e8b2b060`; post-copyin resolution pinned original reply object `0xffffffeaceb9ace0` (key `0xffeaceb9ace0`) and request destination `0xffffffeacebf18a0`. The request's 516-byte msgID `0x40000000` kmsg was delivered to `com.apple.migrationpluginwrapper` with destination-object equality. The owner then entered `ipc_mqueue_receive` on the pinned reply object.

The immutable any-destination matcher was ready at the global post-copyin Mach helper return `0xfffffff02aaa3f54` (`0xfffffff00aaa3f54` static). It compared resolved kernel destination objects from all observed hits at that send-helper site against the pinned reply key; no `IPC_ANY_SEND_DEST_PINNED_REPLY_OBJECT` event occurred before the explicit `SYSTEMAPP_MACH_WINDOW_CLOSED` marker at `1791490696.9259899` (`window_seconds=7200`). Thus the result is zero equal destinations on the watched `mach_msg2` post-copyin helper-return path for the full matched window. This is a bounded path-scoped negative, not a claim about unobserved IPC mechanisms or permanent noncompletion.

Four `IPC_MIGRATION_RESULT_KERNEL_SEND_CANDIDATE` records carried the `migrationResult` key; their resolved destination objects were `0xffffffeacef73b80`, `0xffffffeacee4d780` (twice), and `0xffffffeacef1fdc0`, all unequal to the pin. The XPC-to-kernel correlation for these records remains a candidate join where it relies on same-thread/time/body shape rather than a transaction ID. Receiver-side `ith_kmsg` evidence identifies the peers as follows:

* `0xffffffeacef73b80` was observed at a `tccd` receive, but that dequeued message had ID `0x46`, size 44, and a different body; this names the receiving service for the object, not delivery of the recorded `migrationResult` message.
* `0xffffffeacee4d780` received an exact 76-byte ID `0x20000000` `migrationResult` body in `com.apple.migrationpluginwrapper` (candidate 7; receiver TPIDR `0xffffffe9e8a758f0`). The object also appeared in an earlier same-shaped send candidate, so the exact receiver event is not assigned to that earlier send absent a transaction ID.
* `0xffffffeacef1fdc0` received an exact 76-byte ID `0x20000000` body in `com.apple.migrationpluginwrapper` (receiver TPIDR `0xffffffe9e8de0180`).

At the close, a paused 12-GiB RAM capture and thread scan found the matched owner thread at GPA `0x1005ba17060`, with `thread+0x28 = 0xffffffeaceb9acf0`, exactly the pinned reply object's `+0x10` wait queue. Its saved-state flavor was `0x15`, with saved PC `0x24e817cd4`, LR `0x24e81b308`, and SP `0x16b691590`; the thread continuation was `0xfffffff02aa8f658`. This confirms the request owner remained on the pinned reply wait at the sampled close. The thread scan found no waiter keyed to the three `migrationResult` destination-object wait queues. The postmortem process-name scan yielded no candidates for the requested DataMigrator/wrapper names, so no separate wrapper-process stack is claimed.

No same-owner `CLIENT_SYNC_RETURN`, SystemApp plugin return/`0x1376c`, `DM_PERFORM_RETURN`, or BKS check-in return fired in the full window. The guest was left frozen; the child passed `qemu-img check -U`. Artifacts are under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN88_ANY_REPLY_DEST2.*`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN88_ANY_REPLY_DEST2.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN88_ANY_REPLY_DEST2/ram/`, and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN88_ANY_REPLY_DEST2.{serial.log,stderr.log}`. The runtime `migrationResult`→kernel-send and all-destination watcher were exercised on this hit. This bounded open reply is not a hang/deadlock finding. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN89: receive-store coverage and post-migrationResult wrapper traffic

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN89_REPLY_PATH3` was a fresh replay2 hit with the accepted `system.tc` and unchanged CLASS16/D572/D582/ECHO settings. The live SystemApp request was `0x77f9038900`; owner TPIDR `0xffffffe1e6d3f830`; resolved destination `0xffffffde5082bd30`; pinned reply object `0xffffffde5090faf0`. Its 516-byte msgID `0x40000000` request reached `com.apple.migrationpluginwrapper` by an object-equal `ith_kmsg` handoff, while the caller entered `ipc_mqueue_receive` on the pin's waitq `0xffffffde5090fb00`.

At the 7,200-second `SYSTEMAPP_MACH_WINDOW_CLOSED` event (`1791509573.194928`), global post-copyin return `0xfffffff02aaa3f54` had 142,675 hits, including 33,367 readable kmsgs. None of those readable resolved destinations equaled the pinned reply. The three armed receive-store sites were queue store `0xfffffff02aa8f44c` (151,012 callback hits), direct store A `0xfffffff02aa8e638` (0 hits), and direct store B `0xfffffff02aa8eca0` (26,372 hits); each reported zero destination-object matches to the pin. These counts make the negative stronger than PLUGIN88's helper-return-only check while keeping it scoped to the observed sites.

Three exact 76-byte ID `0x20000000` `migrationResult` receives were joined to wrapper destination objects by matching resolved destination, header ID/size, and body. The earlier post-result send hook joined only on TPIDR/time, so its six records need process-identity correction. The first remained labeled `com.apple.migrationpluginwrapper`: ID/size `1016/44`, destination `0xffffffde505d9ed0`, about 800 seconds after the exact receive. The following five were labeled `amsengagementd` on the reused TPIDR, with IDs/sizes `1999646836/68`, `79/32`, `268435456/724`, `3240/48`, and `5403/56`. All six destination objects were unequal to the pin, but only the first is still a wrapper-send candidate; its receiver was not found in the saved events. The last destination `0xffffffde50a86080` belongs to the amsengagementd-labeled send and is not the wrapper's next edge. None is a transaction-ID-proven reply.

The frozen close sample's thread scan found `thread+0x28 = 0xffffffde5090fb00`, the reply object's `+0x10` waitq. This confirms a waiter remained on the pinned queue at close; the saved process-name scan did not recover a wrapper or DataMigrator stack (one backboardd candidate only), so no wrapper end-stack is assigned. No same-owner `CLIENT_SYNC_RETURN`, plugin return/`0x1376c`, `DM_PERFORM_RETURN`, or BKS check-in return occurred in the window. The child passed `qemu-img check -U` and remains frozen. Artifacts are under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN89_REPLY_PATH3.*` and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN89_REPLY_PATH3.events/`, with the RAM capture in `.mem/`. This is a bounded path-scoped no-match result, not evidence of a hang, deadlock, permanent noncompletion, or firmware-correct D572/D582 behavior. Poster/draw remain parked.

#### PLUGIN90: receive-store follow-up and producer boundary

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN90_POST_MR_PEER1` was a fresh replay2 child using the accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, the same CLASS16/D572/D582/ECHO launch profile, and a 7,200-second SystemApp window. Request `0x7b5b030a20` (owner TPIDR `0xffffffe97edaf060`) resolved destination `0xffffffe7b199f4c0` and pinned reply object `0xffffffe7b1b20e80`. Its 516-byte msgID `0x40000000` kmsg was delivered to `com.apple.migrationpluginwrapper`; the owner entered receive on reply waitq `0xffffffe7b1b20e90`.

P90's all-sender post-copyin hook at `0xfffffff02aaa3f54` was listed as armed, but `DVM_TRACE_ANY_PINNED_REPLY_DEST` was disabled in the deferred module. The window-close counters confirm the gap (`postcopy_return_hits=0`, `readable=0`, `bad=0`), so P90 must not be cited as a second global any-send negative; P89 is the prior valid run at that site. In contrast, the independent `ith_kmsg` store hooks were live: queue store `0xfffffff02aa8f44c` recorded 100,672 hits, direct-store A `0xfffffff02aa8e638` recorded zero, and direct-store B `0xfffffff02aa8eca0` recorded 8,725. No event at these stores matched the pinned reply object. Thus P90 extends the bounded receive-handoff coverage, not coverage of every send primitive.

Two `migrationResult` messages (76 bytes, msgID `0x20000000`) were received by `com.apple.migrationpluginwrapper` with exact destination-object, header, and body matches. Both destination objects differed from the original reply. The second exact receiver context used TPIDR `0xffffffe97ecbb830`. A later candidate send on that numeric TPIDR was 1,972.9 seconds after the receive and labeled `UserEventAgent`; its exact 112-byte event-registration kmsg was received by `nesessionmanager`. That peer is valid for the later UserEventAgent-labeled message, but not attributable to wrapper post-result work. Therefore the receiving peer for the wrapper's post-`migrationResult` outbound remains unresolved; do not transfer P89's destination pointer `0xffffffde50a86080` across boots.

At the protocol boundary, the expected responder for DataMigrator's synchronous `runPluginAtPath:` request is `com.apple.migrationpluginwrapper`, which received the request's destination-object-matched kmsg. Its reply must target the request-local pinned reply right. Static DataMigration.framework client code calls `sendMessageSync` at `0x25e8f1f88`, resumes at `0x25e8f1f8c`, reads `migrationResult`, and invokes a local result block at `0x25e8f2068`. This names the caller-side continuation and response contract; it does not provide the wrapper service's send instruction. SystemAppMigrator completion/result is the likely trigger, but the exact service-side reply producer and whether it ran remain open.

The frozen close-time thread scan found a row with `thread+0x28 = 0xffffffe7b1b20e90`, matching the reply object's `+0x10` waitq; it did not recover a guest process identity for that row. No same-owner sync/plugin return, `DM_PERFORM_RETURN`, or BKS check-in return was recorded. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN90_POST_MR_PEER1.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN90_POST_MR_PEER1.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN90_POST_MR_PEER1.threads.txt`, and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN90_POST_MR_PEER1.{serial.log,stderr.log}`; child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin90-post-mr-peer1.qcow2`. The child passed `qemu-img check -U`, with zero panics. The all-sender enable and same-process post-result filter were corrected in source after the boot; the 7,200-second candidate window preserves P89's slower wrapper-labeled send. These source changes remain unvalidated. No hang/deadlock or firmware-fidelity conclusion follows; D572/D582 remain QEMU hypotheses, and poster/draw remain parked.

#### PLUGIN91B: deferred reply-probe arm failure

The fresh replay2 child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin91b-post-mr-peer2.qcow2` reached `DM_CLIENT_MIGRATE_ENTRY` at `1791522355.230` and SystemAppMigrator `CLIENT_RUN_ENTRY` at `1791525277.890` with completion block `0x16b0219e0`. The deferred wrapper-reply module's breakpoints loaded, but the gate callback failed while reading `block+0x10` because `process` was undefined. The all-sender watch status was false because the option was set before `kernelpath.install()` reset it. LLDB then exited; no reply object, candidate send, peer, or terminal return was scored in this child. This is a probe-coverage failure after a positive activation gate, not evidence about the terminal reply. The source fix obtains the process from the live frame and sets the all-sender option after install; it requires a new boot. `qemu-img check -U` passed on the child. D572/D582 remain hypotheses; no hang, deadlock, or presentation claim follows.

#### PLUGIN92: temporary run artifacts and replay2 working parent unavailable

P92 is unavailable in the current host workspace: `/tmp/dvm` is absent, no P92 QEMU/LLDB process remains, and its logs, events, and child image cannot be read. It has no verifiable `GATE_LIGHT_REPLY_JOIN_ARMED`, `GATE_LIGHT_PINNED_REPLY_SEND_WATCH_READY enabled=true`, or nonzero `…3f54` counter evidence. P92 contributes no gate, peer, reply, or bounded-negative result.

The replay2 D5825 working parent used by P88–P92 was also stored only under `/tmp/dvm/data-seed/` and is absent. Its durable ancestor `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-ccs-ls-seedprogress2-replay-20261006.qcow2` remains intact and passes `qemu-img check -U` (SHA-256 `421d40e1d3c082036ae661dd57c232b00a31cf1cd51e713dbb00690a3e6c3c9d`). The plugin4 launch-frozen intermediate delta needed to construct the documented replay2 parent is missing too. Using seedprogress2 directly would change the lineage, so no replacement boot was launched.

The deferred-arm source now obtains `process` from the live frame and enables `TRACE_ANY_PINNED_REPLY_DEST` after `module.install()`. `py_compile` and `git diff --check` pass, but no fresh boot has validated these changes. P89 remains the only corrected post-result attribution: one wrapper-labeled send to `0xffffffde505d9ed0`, with no saved receiver; later TPIDR-reuse sends were `amsengagementd`. P90 had receive-store coverage but its global `…3f54` option was off; P91B raised `NameError` during deferred arming; P92 is unavailable. The reply question remains open. Resume only after restoring the replay2 parent, and count a window only when arm-ready/enabled evidence and nonzero helper counters are present. D572/D582 remain hypotheses; poster/draw remain parked.


#### PLUGIN93–95: durable replay2b recovery chain

The `/tmp` wipe removed the working historical plugin4/replay2 layers. Recovery used the surviving durable seedprogress2 checkpoint (SHA-256 `421d40e1d3c082036ae661dd57c232b00a31cf1cd51e713dbb00690a3e6c3c9d`) and preserved every recreated layer under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/`. PLUGIN93's plugin4-class launch-frozen layer is `ui-class16-refresh60-dm-plugin4-launchfrozen-replay2b.qcow2` (SHA-256 `822ed6a83bc94a1fe24dfdfe5a0d371893149ca540be318ec3d6c8667af60414`). PLUGIN94's initial match was a setup line, not a runtime stop. PLUGIN94B reached migration work but panicked on non-monotonic time before return; its preserved partial layer is `ui-class16-refresh60-dm-ls-partial-panic-plugin94b-replay2b.qcow2` (SHA-256 `260926a25618cf964c98e9f452c6104ed3ab0dc740f9eafbc18ac34f26ddaebd`).

PLUGIN95 continued from that named partial intermediate and observed `DM_PERFORM_ENTRY` → `DM_XPC_SYNC_CALL` → `DM_XPC_SYNC_RETURN` → same-thread `DM_PERFORM_RETURN` (`x0=0`), with zero panics. It stopped on the actual `DM_PERFORM_RETURN` callback; `LSD_BUILDER_SYNC_RETURN` itself was not observed. The resulting durable D5825-class parent is `ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2b.qcow2` (SHA-256 `7d9a1cb431c998a1cdabf47bf401385ba47a7c6f25eb20aeb62dcc15595fe3ea`). Its qemu backing chain is P95 parent → P94B partial panic → P93 plugin4 replay2b → durable seedprogress2 → boot1 disk. `qemu-img check -U` and `qemu-img info --backing-chain` passed. This replay2b chain restores a request-capable documented path but is not byte-identical to the lost historical replay2 parent; no historical IPC evidence is transplanted onto it.

PLUGIN96 ARM1 is excluded: its success-stop list included `GATE_LIGHT_REPLY_JOIN_ARMED`, so it would stop before the reply window; it was terminated before the migration gate. ARM2 used the corrected deferred arm, explicitly enabled the all-sender matcher, resolved the request's pinned reply, and stopped after the positive same-owner return around 1,850 seconds into the request. This early success close replaces the planned 7,200-second negative window; it is not a full-bound no-reply observation.

#### PLUGIN96 ARM2: wrapper migrationResult handed to pinned reply; sync returned

On the replay2b child, the deferred-arm markers were boot-validated: `GATE_LIGHT_REPLY_JOIN_ARMED` and `GATE_LIGHT_PINNED_REPLY_SEND_WATCH_READY enabled=true`. SystemAppMigrator request `0x74c50587e0` pinned reply object `0xffffffecffda9540`; its destination object `0xffffffecffc579d0` was independently confirmed by matching the original request kmsg at wrapper receipt.

The exact wrapper migrationResult XPC candidate on TPIDR `0xffffffede6f7b830` was followed by a 76-byte Mach msgID `0x20000000` response at `KERNEL_ITH_KMSG_DIRECT_STORE_B` (`0xfffffff02aa8eca0`). The kmsg's resolved remote object matched the pinned reply object, and the payload/header matched the migrationResult candidate. The original DataMigrator owner TPIDR `0xffffffede6bfc950` then reached SystemApp-scoped `CLIENT_SYNC_RETURN` at `0x27c901f8c` about 28 ms later. This is positive receive-side object-equality and same-owner-return evidence. The global post-copyin matcher did not emit a pin-equality event before the handoff, so the direct-store-B match is the decisive path; four process-scoped wrapper post-result outbound candidates were peer-joined to `cfprefsd` and `notifyd` and were all non-pin destinations.

The guest was stopped at the matched return before the 7,200-second timeout; no full-window negative is claimed. `DM_PERFORM_RETURN` and BKS check-in return were not observed before the intentional stop. The child is preserved as `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin96-reply-join-arm2-success.qcow2` (SHA-256 `99bd22d814215ffd5dccffe82f7e541a0ea5638de2ffe10211e4025911dab298`) and passed `qemu-img check -U`; copied logs/events are in `recovery-records/PLUGIN96_REPLY_JOIN_ARM2/`. The child belongs to the explicitly reconstructed replay2b lineage, not the lost byte-identical historical replay2 chain. D572/D582 remain hypotheses; poster/draw remain parked.

#### PLUGIN97: post-sync migration and shell ladder completed

Fresh replay2b recovery-lineage child `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN97_POST_SYNC_LADDER1` used the durable D5825-class parent `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2b.qcow2`, accepted `system.tc`, and established CLASS16/D572/D582/ECHO settings. The run recorded backboardd `DM_PERFORM_ENTRY` at `1791568044.209604` (TPIDR `0xffffffe9b19a8950`), SpringBoard shell-service/migration-check-in entry at `1791568047.189052` / `1791568047.214302` (TPIDR `0xffffffe9b1bbc180`), and SystemAppMigrator `CLIENT_RUN_ENTRY` at `1791568067.990362` with block `0x16d6099e0`. Its `CLIENT_SYNC_CALL` was request `0x748f0445a0` on owner TPIDR `0xffffffe9b195c180`. Deferred reply arm was ready; this boot resolved and pinned reply object `0xffffffe9b26de350`, with request destination `0xffffffe9b2a31c90` delivered to the wrapper by object match.

The first broad LLDB session exited on a kernel trace stop. A narrow return-only LLDB session then attached to the still-live same guest, seeded from saved entry events, and observed the post-sync ladder. The shared `CLIENT_SYNC_RETURN` PC emitted an owner/request-associated event at `1791569460.971211` (`x0=0x748ec35400`, request `0x748f0445a0`, TPIDR `0xffffffe9b195c180`), shortly before the perform return. Because the PC is shared and repeated, this is not an independent handoff proof. `DM_PERFORM_RETURN` followed at `1791569461.347405`, `x0=0`, after 1417.138 seconds, on the same TPIDR/thread as entry. `BKS_MIGRATION_CHECKIN_RETURN` followed at `1791569461.382015`, `x0=0`, after 1414.168 seconds and on the SpringBoard check-in caller's TPIDR. `BKS_CHECKIN_RETURN` at `1791569461.389929` and `SHELL_SERVICE_RETURN` / `POST_SYNC_SHELL_SERVICE_RETURN` at `1791569461.393836` matched that same check-in TPIDR/thread. Thus migration perform, BKS check-in, and shell-service return are closed on this boot. The exact plugin-return `0x1376c` and completion-block B invoke were not observed after reattach; they remain unscored. Shell-service return does not establish that the UI reached home.

Durable child: `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin97-post-sync-ladder1.qcow2`, SHA-256 `3f45626dcda4a853f4bc41d160da12f899c6d0b77fa81c54f4e62c3536e3e0c4`. Event/log bundle: `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/PLUGIN97_POST_SYNC_LADDER1/`. Source and durable copy passed `qemu-img check -U`, and their hashes agree. The guest was manually paused after the shell success event; automated stop did not trigger because the setup watcher lacked the configured success-label list. No model/fixture/A408 changes were made. D572/D582 remain hypotheses; poster/draw remain parked.

#### PLUGIN98: post-shell UI reached scene settings; visible home remains unproven

Fresh child `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN98_POST_SHELL_UI1` used the durable replay2b D5825-class parent and accepted `system.tc`. On this same boot, `DM_PERFORM_RETURN x0=0` occurred at `1791570567.617846`, BKS migration check-in returned `x0=0` at `1791570567.651104`, BKS check-in returned at `1791570567.659599`, and shell service returned at `1791570567.663093` on the matching SpringBoard check-in caller. This closes the post-sync ladder but does not prove that home appeared.

The initial post-shell hook arm had slide zero and is excluded. A corrected same-child reattach read slide `0x1b824000` and armed the lock/home callbacks at `1791570690.029012` (122 seconds after shell return). Workspace request/transition callbacks followed. Poster scene state initially remained null, then `updateConfiguration:` resolved scene `0x772de68e00` at `1791571214.463598` and stored it into `_scene` field `0x772d1b5c18` at `1791571215.581394`; later `_updatePosterScenes` hit the non-null present arm and `willUpdateSettings`. These are SpringBoard scene-configuration events only; no matching `POSTER_HOST_SET_CONTEXT_SCENE`, home-entity return, completed ADFL marker, or A407-present event was saved.

HMP frames at shell return, workspace transition, scene-settings, and debugger-stop samples were all black by the recorded RGB threshold (`nonblack_gt8=0`, 1179×2556; SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The configured 1,800-second post-shell watch did not complete: LLDB crashed twice in `GDBRemoteRegisterContext` while unwinding, and the later paused CPU PC `0x19fc43e88` was near the backboardd `SERVER_HOSTED_ALLOWED_UPDATE` hook without a recovered process identity. The observed hook interval is partial, not a bounded no-home or guest-stall verdict. A fallback minimal callback arm produced only its ready marker before coverage ended.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin98-post-shell-ui1.qcow2` has SHA-256 `ec67e862bd92c736be335862fb69e63ac8572420d56464b8429a14ab4e4c6d0a` and passed `qemu-img check -U`. The evidence bundle is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/PLUGIN98_POST_SHELL_UI1/`. No model, fixture, A408, or scanout change was made. D572/D582 remain hypotheses.

#### PLUGIN99: shell clear and black samples; configured UI observer failed to arm

On fresh durable replay2b child `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN99_POST_SHELL_HOME2`, the post-sync ladder completed through `DM_PERFORM_RETURN`, SpringBoard BKS migration/check-in return, and `SHELL_SERVICE_RETURN`; the matching return values were zero where recorded. SpringBoard ADFL entry fired twice. The dynamic compact observer then logged `POST_SHELL_UI_ARM_FAILED module-not-registered`: the module import used the environment-selected filename, but the lookup still requested the legacy fixed module name. Consequently P99 has no valid workspace/home-entity/poster-scene callback coverage.

The captured framebuffer was uniformly black at shell return and at 0/120/240/360 seconds afterward (`1179×2556`, zero pixels above the nonblack threshold). These are direct display samples, not proof that the UI layer was never composed. The guest was cleanly ended after the 360-second sample rather than spending the remaining uninstrumented wait. The fix explicitly loads the selected module into `sys.modules` using the same module name that its LLDB script callbacks reference. It passed `py_compile` and `git diff --check`; a new child must confirm `POST_SHELL_HOME_MIN_READY` before callback absence can be scored.

The durable child is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin99-post-shell-home2.qcow2` (SHA-256 `b820414a5de7425c97b06e270d5d456c8991dcf7e565fd64f59f05aae9e358e3`); its backing chain begins at the durable replay2b D5825-class parent and `qemu-img check -U` passed. Evidence is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/PLUGIN99_POST_SHELL_HOME2/`. No touch/unlock was attempted, and no A408 or display-model change was made. D572/D582 remain hypotheses.

#### PLUGIN100: LLDB callback registration check and post-shell output

P100 reached shell service return on the replay2b lineage and armed the selected post-shell module enough to emit a READY record. The subsequent callback silence was not valid coverage: a host LLDB reproduction confirmed that `importlib` plus `SetScriptCallbackFunction` leaves the breakpoint command list empty. P100 consequently provides no callback verdict. The main ladder recorded BKS migration/check-in and shell-service return; `DM_PERFORM_RETURN` was not observed in this child.

Screendumps at shell return and +0/+120/+240/+360/+480/+600/+721/+841/+961 seconds were all black at 1179×2556. P100 was ended cleanly after +961 seconds. The corrected dynamic arm performs `command script import` on the selected module path and resolves it by that selected module name. The compact callback installer now requires the callback function to appear in `breakpoint command list` for every site before it writes READY. `py_compile`, `bash -n`, and `git diff --check` passed; runtime validation is pending.

The child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin100-post-shell-home-armfix1.qcow2` (SHA-256 `732a710d69f140b7dfc91278c79d64f771a9debfd5d11ea8546f4dee1bd7a900`) passed `qemu-img check -U`; its backing parent is the durable replay2b D5825-class parent. Records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/PLUGIN100_POST_SHELL_HOME_ARMFIX1/`. No unlock input or model/A408 change occurred. D572/D582 remain hypotheses.

#### PLUGIN101: SpringBoard home initialization and poster attachment after shell return

P101 used a fresh replay2b D5825-class child, accepted `system.tc`, and no model or guest changes. The shell-service return was followed by two SpringBoard ADFL entries, workspace requests/transitions, and two non-null `SB_HOME_ENTITY_INIT_ENTRY_RETURN` events. `POSTER_HOST_SET_CONTEXT_SCENE` then attached scene/context `0x214d6a3ae`. This proves that the SpringBoard home lifecycle and poster-scene attachment ran; it does not prove visible home pixels or scanout.

Every HMP frame captured at shell return, home-entity return, and post-shell 0/120 seconds was uniformly black at 1179×2556 (`nonblack_gt8=0`, hash `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). P101's callback list was boot-validated, but the post-home CA/backboardd render and framebuffer-swap probes were not installed. Thus the first unresolved edge is CA/backboardd composition or presentation, not whether SpringBoard entered its home entity path. P102 adds that read-only trace. The durable P101 child passed `qemu-img check -U`, hash `370c922eb1e88b71c6c51059ad596d888f7f71a73c2eedb7af02941d602bbcdb`; all run evidence is preserved in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/PLUGIN101_POST_SHELL_HOME_CBPROOF1/`. D572/D582 remain hypotheses; no unlock input was sent.

#### PLUGIN102: SpringBoard render path reaches framebuffer handoff; IOSurface pixels still unmeasured

P102 used a fresh child of durable replay2b D5825-class parent `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2b.qcow2`, with accepted `system.tc`, CLASS16, and the established D572/D582/ECHO/A407 hypothesis profile; A408 completion/scanout remained off. SpringBoard's migration check-in returned `x0=0` at `1791575919.4722528`; `BKS_CHECKIN_RETURN` and `SHELL_SERVICE_RETURN` followed on the matching TPIDR. No matched `DM_PERFORM_RETURN` event was recorded, so this run closes the check-in/shell edge only.

The post-shell callbacks were runtime-validated (`POST_SHELL_HOME_MIN_READY`, seven sites; `POSTHOME_PRESENT_READY`, seven sites, one location each). ADFL, workspace transitions, and two non-null home-entity returns ran; poster context/scene `0x2006623ae` was attached nine times. SpringBoard CA commit ran sixteen times. Backboardd entered `Server::render_update`, created 1179×2556 surfaces, received non-null IOSurface allocation returns, and reached `IOMFBDisplay::fb_swap_set_layer` plus the framebuffer-handoff stub. The observational IOMFB A408-build callback also ran; this is not a claim of firmware correctness or visible presentation.

All eleven HMP samples through 600 seconds after shell return, including samples paired to home-entity, CA, render-update, surface-allocation, and swap markers, were uniformly black (`nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). P102's attempted surface read was wired to `x2` at the handoff stub, where the value was null or frame state. The verified `fb_swap_set_layer` entry takes the IOSurface in `x3`; the read-only callback has been corrected to capture that pointer and its backing pixels before handoff. P103 is the fresh-child test of the corrected read. P102 was intentionally stopped after the stable-black interval; this is not a guest failure or pixel-source classification. No touch/unlock input or model, fixture, A408, SpringBoard, or CLASS16 changes were made.

Durable P102 child: `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin102-post-shell-render1.qcow2` (SHA-256 `b431bbfc64f568b18cf0cfce8dcbca08803747425da1aec41599edb69f2feeff`), passed `qemu-img check -U`. Records: `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN102_POST_SHELL_RENDER1/`. D572/D582/A407 remain hypotheses; visible home and unlock remain open.


#### PLUGIN105B: shell-to-home callbacks continue, but captured framebuffer stays black

PLUGIN105B was a fresh child of the durable replay2b D5825-class parent with `system.tc`, CLASS16, and the existing D572/D582/ECHO/A407 profile; A408 stayed off. `DM_PERFORM_RETURN` was observed with `x0=1` at `1791579181.4131682`; SpringBoard's matched BKS migration/check-in and shell-service callbacks returned, with shell `x0=0x1fb729c28` at `1791579350.172986`. The nonzero DM return is preserved as observed rather than labeled success.

SpringBoard then ran ADFL, returned non-null home entities (first at `1791578840.116829`, then `1791578946.967806` and later), executed workspace transitions, and attached poster scene/context `0x20782a3ae`. CA commit and backboardd `Server::render_update` callbacks followed. Backboardd allocated a non-null 1179×2556 IOSurface at `1791579117.145526`. The ordinary legacy upward swipe and Home-button relay entries each received successful ACKs; that proves submission/acknowledgment only, not unlock or home appearance.

HMP samples through +842 seconds stayed uniformly black (`nonblack_gt8=0`, 1179×2556, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). All first 16 observed `fb_swap_set_layer` entries had null `x3`, after which the callback's cap disabled it. Thus there is no all-window claim about later swap surfaces. No A407-present marker was captured. The runtime RAM/thread scan is partial and did not name an application-side blocker; it is not evidence that D572/D582 or A407 is wrong.

The next read-only trace expands the set-layer/handoff cap to 256 and adds `Server::render_display`, OGL render, finish-update/surface/swap, and dynamic return hooks. These hooks passed syntax validation only before the next run. Durable child: `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin105b-post-shell-swipe2.qcow2`, SHA-256 `fc1dcee45e15a80481e332c60c8db7d17d90f9671222ca9612db6f864c59c61d`, `qemu-img check -U` clean. Evidence (including input and postmortem records) is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN105B_POST_SHELL_SWIPE2/`. No unlock success or visible home is claimed; no model, fixture, SpringBoard, CLASS16, A408, or scanout change was made.


#### PLUGIN105B: post-shell home callbacks ran; visible output stayed black

PLUGIN105B is a fresh child of the durable replay2b D5825-class parent with accepted `system.tc`, CLASS16, and the existing D572/D582/ECHO/A407 profile; A408 remained off. `DM_PERFORM_RETURN` was observed with `x0=1` at `1791579181.4131682`. SpringBoard's matched BKS migration/check-in and shell-service callbacks returned; shell returned `x0=0x1fb729c28` at `1791579350.172986`. The DM return is nonzero and is preserved as observed.

SpringBoard ADFL and workspace transitions ran. Home-entity initialization returned non-null objects at `1791578840.116829`, `1791578946.967806`, and later; poster host/context scene callbacks attached scene/context `0x20782a3ae`. CA committed, backboardd `Server::render_update` ran, and a 1179×2556 IOSurface allocation returned non-null. Legacy DVMINPUT1 swipe and Home-button events received successful relay acknowledgements; this does not prove unlock or visible home.

HMP captures through +842 seconds after shell remained uniformly black (`nonblack_gt8=0`, 1179×2556, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The first 16 `fb_swap_set_layer` entries had null `x3`; a fixed cap then disabled the callback, so later swaps are unmeasured. No A407 marker was captured. Partial thread/RAM postmortem data did not identify an app-level blocker. This is not a firmware or display-model verdict.

The follow-up read-only probe expands set-layer/handoff logging to 256 hits and adds render-display, OGL, finish-update/surface/swap, and return hooks. It passed syntax validation but is not yet boot-validated. Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin105b-post-shell-swipe2.qcow2` SHA-256 `fc1dcee45e15a80481e332c60c8db7d17d90f9671222ca9612db6f864c59c61d` passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN105B_POST_SHELL_SWIPE2/`. No model, fixture, SpringBoard, CLASS16, A408, or scanout change was made. Visible home and unlock remain open.

#### PLUGIN106B: compositor IOSurface contains sparse lock-screen chrome

P106B is a continuous replay2b child. The migration/check-in/shell ladder returned, SpringBoard produced non-null home entities and workspace requests, and backboardd completed a render/display/OGL/swap cycle with an IOSurface. A bounded 1500-second post-shell sampler finished through its explicit stop marker. Every HMP screendump remained black (`nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`); no A407-present marker was observed.

The sampled 1179×2556 BGRA IOSurface was not blank: 6,769/3,013,524 pixels were nonblack (0.2247%, peak RGB 255), visibly showing time/status icons, an info glyph, and the home indicator on a black field. This is lock-surface evidence only; it does not establish wallpaper, scanout, or home/unlock. The child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin106b-post-shell-surface-trace1.qcow2` (SHA-256 `ebb356479c0cd0d56494aa5ebea4c4d0d8c21a59c224f2a1b8ff876be0f1cb75`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN106B_POST_SHELL_SURFACE_TRACE1/`. The input-enabled follow-up uses the existing QMP/UART HID path with a launchd-only helper bootstrap and observes the resulting IOSurface. No A408 forcing, framebuffer painting, or display-model change was made; D572/D582 remain hypotheses.

#### PLUGIN107 / P108: post-shell path reached home-entity init; native-input protocol mismatch corrected in a sibling child

P107's replay2b child returned through `DM_PERFORM_RETURN x0=0`, BKS migration/check-in return, and shell service return on one boot. The compact SpringBoard observer armed successfully and recorded ADFL entry/update return (`x0=1`), a workspace-create request, and a non-null home-entity initializer return. These are lifecycle callbacks only; they do not prove that a visible home frame reached the compositor. HMP dumps were black, while no post-home IOSurface sample or A407 presentation hit was captured before stop.

The launchd bootstrap started helper v6 (PID 20), which remained between `DVM_INPUT_INITIALIZING` and readiness. Source and wire logs identify the cause: v6 reads `DVMINPUT1` and emits textual `DVM_INPUT_READY`/`DVM_INPUT_ACK`, but the active QEMU bridge transmits and accepts only DVMI2 records (`DVMI2R`/`DVMI2A`). P107 is therefore an input-protocol coverage failure, not a failed swipe. Its durable child and recovery bundle are `ui-class16-refresh60-dm-lock-plugin107-native-unlock1.qcow2` and `recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN107_NATIVE_UNLOCK1/` under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/`.

The DVMI2 IOKit helper v12 was installed using the guest restore ramdisk into the separate sealed child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/input-v12-install-replay2b-20261009/disk.qcow2`; the installer emitted `DVM_HID_INSTALL_DONE`. P108 boots from that child with the DVMI2 QEMU UART bridge and the post-shell render observers. At this record update P108 is still in early boot: status has seen helper PID 85 announce state `I`, and pings have been acknowledged, while IOKit initialization has not yet emitted ready state `R`. The next valid unlock attempt requires `R`, gesture ACK/dispatch evidence, and a corresponding post-shell surface/frame change. No unlock result is inferred yet. D572/D582 remain hypotheses; the guest display model and A408 path are unchanged.

#### PLUGIN112: acknowledged native input left the captured surface on the lock screen

P112 ran as a fresh child of the durable v6-input-install child. Shell return, SpringBoard's non-null home entity/workspace callbacks, and post-shell render hooks were present; the v6 DVMINPUT1 helper reached ready state. An ordinary direct upward swipe, a Home-button event, and a second upward swipe each received relay ACKs and reached the input callbacks. The post-input IOSurface still contains only sparse lock chrome on a black background, without wallpaper, passcode keypad, or Home icons. This does not prove an unlock failure: the direct backend serializes 21 motion samples, and its timing may not represent a single buffered gesture. The next fresh-child attempt switches only to the helper's buffered Recap `R` swipe and captures the resulting IOSurface; if a passcode prompt appears, stop for user entry. P112 ended by clean host HMP quit, not guest panic. Durable child SHA-256 `5d60f913709b73f5758d1fed79afc7f7578da33d4013b3aac280fa657eac9a4c` passed `qemu-img check -U`; records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN112_HOME_UNLOCK1/`. No model, fixture, A408, SpringBoard, CLASS16, or scanout changes were made; D572/D582 remain hypotheses.

#### PLUGIN119: post-shell/home callbacks validate; v6 helper startup remains unready

P119 used a fresh durable child of P113, preserving the replay2b D5825-class chain. DataMigrator and the BKS/check-in/shell ladder returned. The post-shell observer armed successfully; ADFL and workspace transitions ran, and SpringBoard returned non-null home entities six times with repeated CA commits. No poster-scene or backboardd render-update/A407 hit was recorded, so the lifecycle path still does not establish a visible Home frame.

The launchd-only v6 helper started as PID 20 but emitted only `DVM_INPUT_START` and `DVM_INPUT_INITIALIZING` during the 40-minute bound. It emitted no ready/error/ACK, and the guarded host trigger sent no packet. P119 is consequently an input startup coverage miss, not a swipe/unlock result. Uniformly black HMP captures are not treated as IOSurface evidence. No passcode or model/display change occurred. Child SHA-256 `bd299c598e26114997e112ac06b0df5da61493aad027661792aecbd6b0c753ed` passed `qemu-img check -U`; the durable child and recovery bundle are `ui-class16-refresh60-dm-lock-plugin119-home-unlock1.qcow2` and `recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN119_HOME_UNLOCK1/` under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/`. The next run uses the sealed DVMI2 helper installation and waits for QEMU's explicit `R` state before one gesture. D572/D582 remain hypotheses.

#### PLUGIN120: input helper AMFI gate identified; merged trust cache prepared

P120's serial log proves the DVMI2 helper launch was blocked by trust-cache coverage, not by the guest input protocol: `/usr/local/libexec/dvm-input` was repeatedly rejected as ad-hoc signed under CT policy 0. The run passed only `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc` (3,938 entries), omitting v12 helper CDHash `5135a276753e8b656e05651c91c20f4927a14c1e`. The host status had no helper PID, ACK, or dispatch, so its guarded input trigger correctly sent nothing. This is a helper-launch setup failure and not a UI/input verdict.

The helper's exact one-entry cache was merged into the accepted dev6 cache, producing `/Users/steve/dvm-artifacts/bp-patched-dev6/input-v12-native/system-plus-dev6-input.tc` with SHA-256 `d60beb27dd6fc3726f205164842de245b9f4f6a2fa5e8bc40a95351a52fb7266`; the helper CDHash is present in all 3,939 entries. This changes only the `-tc` boot artifact, not guest files or DCP/model behavior. The next child will use that merged cache and will not send input until both the helper ready state and the non-null home-entity observer are present. P120 child SHA-256 `4d1acec8529113dfe4f834fd40327459330f092a23efe36f017c0ea559e4cec4` passed `qemu-img check -U`; full records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN120_DVMI2_HOME_UNLOCK1/`.

#### PLUGIN122: manual DVMI2 startup did not reach ready

P122 used the durable P121 child and merged v12 trust cache but set `INPUT_BOOTSTRAP=1`, starting the helper through a manual launch path. It remained in DVMI2 initialization state `I` and did not reach `R` during the covered interval; the guarded trigger sent no input. This is a helper-startup miss, not a UI or gesture result. The next run restores P121's automatic launch configuration (`INPUT_BOOTSTRAP=0`). P122's durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin122-dvmi2-home-unlock2.qcow2` has SHA-256 `e3474dabb25db5eeeaa078339615e079b2e8db82d13bd0f7ae36f8fa95083300`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN122_DVMI2_HOME_UNLOCK2/`.

#### PLUGIN123: post-shell Home/swipe was acknowledged but the captured screen stayed black

P123 was a fresh child of durable P121 using the validated automatic DVMI2 launch, merged helper trust cache, and the corrected post-home display-state probe. The helper reached state `R` as PID 85. SpringBoard returned non-null home entities, issued workspace transitions, attached a poster host/context scene, and logged 32 CA commit entries. The same boot recorded `DM_PERFORM_RETURN x0=1`; BKS migration/check-in and shell-service returns followed, with migration-check-in `x0=0`.

After the readiness gate, the host sent one ordinary Home key and one upward swipe. DVMI2 reported 15 host events with no failed dispatch; Home and swipe records were ACKed. Yet all four gesture before/after framebuffer samples and the final capture were byte-identical black frames (1179×2556, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). No SpringBoard input/hit-test observer was armed, so UIKit receipt is still unproved. The post-home render observer logged no backboardd render-update, display/OGL, swap, or IOSurface hit; the A408-build callback was armed only as an observation and did not hit. DVMI2 reported zero presents and zero input-to-present samples. This is not an unlock failure or firmware verdict: the next cut must join the gesture to SpringBoard input handling and explain why the home/lock CA activity did not feed a backboardd update.

An initial trigger instance had a readiness-marker typo and watched only the growing log tail; it sent no gesture. The durable trigger now searches the correct `POSTHOME_PRESENT_READY` marker in the log prefix, and the input result above came from a restarted trigger with the ready gate satisfied. Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin123-dvmi2-autolaunch-home-unlock3.qcow2` (SHA-256 `f2d54dee5940d067d4be0f228157ae4e3a700769c71b074bc329a071de8030e`) passed `qemu-img check -U`; all records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN123_DVMI2_AUTOLAUNCH_HOME_UNLOCK3/`. No passcode was entered and no model, fixture, SpringBoard, A408, or scanout change was made. D572/D582 remain hypotheses.

#### PLUGIN128: acknowledged SpringBoard gesture; no compositor pixel sample

P128 booted from durable P127 with the accepted merged DVMI2 trust cache, CLASS16, and the existing D572/D582/ECHO/A407 hypothesis profile; A408 remained disabled. `DM_PERFORM_RETURN` had `x0=1`. SpringBoard migration check-in returned `x0=0`, then BKS check-in and shell-service returned on the matched thread. The post-shell compact callback module armed successfully and saw ADFL, workspace creation/transitions, repeated non-null home-entity returns, and poster host/context-scene attachment.

The DVMI2 helper reached ready state. An acknowledged Home event caused more workspace/home-entity activity. A single acknowledged upward swipe reached SpringBoard `UI_EVENT`, hit-tested `_UISystemGestureWindow`, returned `UISystemGestureView`, and reached `SPRINGBOARD_HID_EVENT`. Thus the input delivery edge is established, but an unlock or visible Home transition is not. Before/after HMP captures remained uniformly black at 1179×2556 with hash `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. This compact run did not arm the CA/backboardd/IOSurface path, so it cannot attribute the black capture to composition, swap, or scanout.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin128-min-home-unlock1.qcow2` (SHA-256 `cf1946fd14e786557f544f2fa0505a690f840e1d0bea3e150b58edff4c2fb04b`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN128_MIN_HOME_UNLOCK1/`. Next run pairs one ordinary swipe with the existing read-only swap IOSurface capture gated on input ACK. No passcode was entered; no model, fixture, A408, scanout, SpringBoard, or CLASS16 change was made. D572/D582 remain hypotheses.

#### PLUGIN130 / PLUGIN131: Home entity was late; swipe was not sent

P130 used the wrong run defaults for this cut (`INPUT_BOOTSTRAP=1` and `dm_post_shell_home_render_callbacks`); the host trigger was cancelled before input, so it contributes no UI verdict. P131 booted from durable P128 with `INPUT_BOOTSTRAP=0`, the accepted input TC, the DVMI2 helper ready, and `dm_post_shell_home_unlock_render_callbacks` armed with gated surface sampling. Shell service returned. The Home key was acknowledged, but the trigger's 180-second wait expired without a non-null Home entity, so no swipe was sent. The SpringBoard home-entity return arrived later at `1791614769.679` (about 343 seconds after Home) during the controller's observation interval. Thus the P131 result is delayed Home-entity initialization; it is not an unlock attempt or a compositor-pixel classification. HMP remained uniformly black and the gated IOSurface capture did not open.

P131's durable child is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin131-home-unlock-render1.qcow2` (SHA-256 `b5ba0911db77dd10a2dd4c1adedc356b7bb82488238ffb1a0611bd1cb7d6ad17`); `qemu-img check -U` passed. Records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN131_HOME_UNLOCK_RENDER1/`. The next controlled run waits 600 seconds for the non-null entity before its single ordinary swipe; D572/D582 remain hypotheses, A408 remains disabled, and no passcode is automated.

#### PLUGIN132: gesture reached SpringBoard's system-gesture view; frame stayed black

P132 is a durable P128 child with the corrected DVMI2 setup and gated post-gesture surface observer. The Home key was acknowledged, and SpringBoard returned a non-null home entity at `1791615274.582` (about 333 seconds after the key). One ordinary upward swipe then received successful helper ACKs. It reached SpringBoard `UI_EVENT`, hit-tested through `_UISystemGestureWindow` to `UISystemGestureView`, and invoked `SPRINGBOARD_HID_EVENT`. The trace proves delivery to the system-gesture view, not an accepted unlock or completed lock-screen dismissal.

The pre/post-swipe HMP frames were identical uniform black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). CA commit entry ran 32 times (the callback's cap) and a return was observed, but none of the armed backboardd render-update, display, OGL, swap, or IOSurface callbacks produced a post-home hit during the 300-second observation. The gate opened after the ACK, yet no `posthome-iosurface-*.json` file was written. This leaves both gesture recognition/lock dismissal and the CA-to-backboard display handoff open; it does not identify a model or firmware defect. No passcode was entered.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin132-lockscreen-swipe1.qcow2` (SHA-256 `610d0298e8abd5d3ca6ad9c51fc7ad80e1775df422954a266dcce71d698a037e`) passed `qemu-img check -U`. Full evidence is under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN132_LOCKSCREEN_SWIPE1/`. The next run adds read-only SpringBoard cover-sheet/home-gesture recognizer and lock-transition hooks to the existing gated render observers.

#### PLUGIN133: pan touch callbacks ran; recognizer acceptance and pixels remain open

P133 booted a fresh durable child of P132 with the accepted DVMI2 trust cache and the existing read-only shell, input, gesture, and render observers. Shell-service returned; after the acknowledged Home key, a non-null SpringBoard home entity appeared at `1791616716.021`. The one upward swipe was acknowledged and dispatched without helper errors. Its `UI_EVENT` hit-tested through `_UISystemGestureWindow` to `UISystemGestureView`. `SBFluidSwitcherScreenEdgePanGestureRecognizer` and `SBCoverSheetScreenEdgePanGestureRecognizer` reached `touchesBegan`; the latter reached `touchesMoved`.

The lock-screen manager transition callback ran, but with `forUserGesture=0` (`x3=0`) and zero velocity (`d0=d1=0`), so do not attribute it to accepted swipe dismissal. No `gestureRecognizerShouldBegin`, `SBHomeGestureInteraction` handler, or cover-sheet dismiss begin/change callback ran. Poster context-scene attachment and 42 CA commit entries followed, but no armed backboardd render/update/display/OGL/swap/IOSurface hook hit. All framebuffer samples remained uniform black (`d312521c…f42b9`); no visible Home or unlocked frame appeared.

This closes input delivery through the two recognizers' touch callbacks, not recognizer acceptance, lock dismissal, or scanout. The next read-only discriminator is `UIGestureRecognizer setState:` and `_shouldBegin` for those two recognizer classes, while retaining the transition and compositor hooks. P133's durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin133-lock-gesture1.qcow2` (SHA-256 `5415b4d4950aa79c9e7b0febada0f84858f1eb28111b0f77d841dd1e89db1ce9`) passed `qemu-img check -U`; records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN133_LOCK_GESTURE1/`. No passcode or guest/model/fixture/SpringBoard/A408/scanout modification was made; D572/D582 remain hypotheses.

#### PLUGIN134: UIKit explicitly fails both screen-edge recognizers

P134 booted a fresh durable child of P133 with the same input and read-only rendering profile. Home-entity initialization and the native swipe were acknowledged. The UIKit `UIGestureRecognizer setState:` callback recorded state `5` (`Failed`) three times: the cover-sheet recognizer at `1791617757.445894`, the FluidSwitcher recognizer at `1791617757.578570`, and the cover-sheet recognizer again at `1791617757.921956`. UIKit `_shouldBegin`/driver-update hooks and SpringBoard home/dismiss acceptance hooks did not fire. This shows recognizer failure before a begin/action callback, but does not name the cause.

All captured frames remained uniform black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`); no backboardd render/display/OGL/swap or IOSurface event was logged. P134 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin134-gesture-state1.qcow2` (SHA-256 `6dabcef60e9762d0385d3e428174972f5a4e1dacdacd2999b337d744ff60e7bd`) passed `qemu-img check -U`; evidence is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN134_GESTURE_STATE1/`. The follow-up captures the Fluid subclass's `setState:` caller and screen-edge active-zone predicate, and starts the swipe closer to the physical edge. No passcode or guest/model/fixture/SpringBoard/A408/scanout modification was made; D572/D582 remain hypotheses.


#### PLUGIN141: accepted Cover Sheet dismissal still produces no visible frame

P141 is a fresh durable child of P134. Its initial swipe missed the active bottom-edge zone, so the same live guest received one corrected buffered DVMI2 gesture from `(589,2540)` to `(589,500)` over 600 ms and 32 steps. The helper acknowledged all 35 records and SpringBoard accepted the cover-sheet and system-gesture `shouldBegin` checks. `COVERSHEET_DISMISS_GESTURE_BEGAN` and `CHANGED` ran; the pan ended; and `LOCKSCREEN_GESTURE_TRANSITION` ran with `forUserGesture=1` and velocity `0.2715`. SpringBoard then returned a non-null home entity. This identifies the earlier lock-gesture gate as cleared for this input sequence.

The frame stayed uniformly black at 1179×2556 (`d312521c…f42b9`). SpringBoard entered CA commit 58 times, but no post-shell backboardd `Server::render_update`, `render_display`, OGL, framebuffer swap, or IOSurface callback hit. DVMI2 counters remained at zero presents and zero input-to-present samples. The next narrow discriminator adds QuartzCore's existing client-side `0x9c4x` message builder/send hooks and backboardd's corresponding server receive hook. A send without a receive would localize the gap before server dispatch; a matched receive without render would localize it after receipt. This run does not identify which case applies and is not a firmware diagnosis.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin141-gesture-center600ms1.qcow2` has SHA-256 `8c88db5fdcad9a14b103e635cb137ce02500cdfef004a7f797890153d46cab44` and passed `qemu-img check -U`; the complete bundle is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN141_GESTURE_CENTER600MS1/`. The per-gesture event summary is `events/bottom-center-swipe.json`. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.


#### PLUGIN142: CA server receives post-home transactions; display path stays quiet

P142 used a fresh durable P134 child with the accepted input trust cache and the established display hypotheses unchanged. SpringBoard returned non-null home entities. The trigger completed a bottom-center `(589,2540)→(589,500)` swipe and a second pause-aware retry; both were acknowledged/dispatched without failures. No accepted dismiss/home pan was logged on this boot; the sole `LOCKSCREEN_GESTURE_TRANSITION` had `x2=x3=0` and zero velocity. HMP before/after captures remained uniform black at 1179×2556 (SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).

With `DVM_POSTHOME_CA_HANDOFF_TRACE=1`, the client emitted 13 `0x9c44` and 3 `0x9c45` send-wrapper events, and backboardd's bounded 32-event receive sample included both commit-class IDs and nontrivial payload sizes. This is receive-side activity, not a cross-task port-identity proof. SpringBoard recorded 68 CA commit entries; all installed post-home render/display/OGL/finish/swap/IOSurface hooks remained at zero hits. The helper reported zero presents and zero input-to-present samples through its 600-second observation. This narrows the open edge to post-receive scheduling/eligibility or the route into `render_for_time`; it does not identify a DCP/A407 defect or classify the unaccepted P142 swipe as a lock-gesture failure.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin142-ca-handoff1.qcow2` has SHA-256 `947e900c389721292e48341aee39666d33a85442232303ab84031dec6a17b729`; `qemu-img check -U` passed. The evidence bundle is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN142_CA_HANDOFF1/`. The next read-only trace adds scheduler/can-update/`render_for_time` decision markers while retaining the existing surface observers. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN147: shell and Home entity reached; V170 still skips the render update

P147 reached `SHELL_SERVICE_RETURN`, then a non-null SpringBoard home entity. The DVMI2 helper acknowledged and dispatched two bottom-center upward swipes without dispatch failures, but the captured lock transition had `forUserGesture=0`; neither swipe is classified as a successful dismissal. The 1179×2556 HMP frame stayed uniformly black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The bounded post-input observation produced no IOSurface capture or present sample.

The post-home QuartzCore trace recorded at least 2,048 active `render_for_time` full-path iterations. V178 and V248 returned 0, VF8 and V170 returned 1, and no post-V170 Server render/surface callback followed. The post-call x19-derived display fields are invalid because x19 was stack-like at that point. A new read-only source hook now samples the actual pre-call x0 display receiver and x1 Update plus predicate inputs; it was syntax-checked after P147 and awaits fresh-boot validation. No DCP-originated report or firmware behavior is inferred from this QuartzCore result. D572/D582 remain hypotheses, and A408 stayed disabled.

P147's durable child and evidence bundle are `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin147-render-gate1.qcow2` (SHA-256 `e932a63396e2ad65c7d9ec8d319befed4562ef24acb63cda1775d35591cb985e`) and `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN147_RENDER_GATE1/`; the child passed `qemu-img check -U`.

### PLUGIN149 — active display predicate, with power-write identity correction

P149's pre-V170 hook validated the active display at `0x793ecf8000` and its state object at `0x7521fe4000`: all 70 sampled updates entered `RFT_FULL_PATH`, carried `Update+0x140=0x80010`, had active state bytes 4/5=`0/1` and mode byte `+0x2bb=0xff`, then returned `ignore=1` from V170. No Server render/surface path followed in the covered LLDB interval; the framebuffer remained uniform black after a non-null SpringBoard Home entity and acknowledged input.

The three logged power-state target/store pairs were for display ID 2 at `0x793ed08000` with state `0x7521fe8000`, not the active V170 object/state. Their byte transitions do not explain active state byte 5. The watch on the active state object reported no writes before LLDB stopped at `CAN_UPDATE_OUTER_AFTER_INNER` (`0x188e772e8`); later screen sampling continued after instrumentation loss. Treat the active writer as unresolved. This is AP-side QuartzCore evidence, not a DCP report/payload finding. The next cut should use read-only breakpoint samples keyed by active display identity and omit raw watchpoints and the can-update breakpoint that stopped P149.

Records: `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN149_POSTHOTPLUG_BRANCH1/`.

### UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN150_NATURAL_MUTATOR1: setup required, no Home surface

P150 used a fresh child of the durable P149 post-hotplug parent with the merged `system-plus-dev6-input.tc`, CLASS16, the unchanged D572/D582/ECHO/A407 hypothesis profile, A408 off, and DVMI2 enabled. The post-sync ladder reached shell return; SpringBoard entered ADFL at `1791630627.774` and again at `1791632007.995`. A late register-only setup trace saw the second ADFL call invoke `updateInSetupMode`, return `x0=1`, and pass `x2=1` at `SB_SETUP_SET_REASON_ENTRY`: Setup mode was required on that path, but no Setup Assistant activation is proven.

The helper ACKed a Home key after shell return. No non-null SpringBoard Home entity appeared during the 2,406-second window. The helper then sent one ordinary upward swipe after its 600-second no-entity wait, with no delivery errors; neither input changed the HMP capture. Every frame was uniform black (`1179×2556`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`), with zero DVMI2 presents or input-to-present samples. This is not a Home or unlock success.

P150's setup-progress observer was installed after its first ADFL entry; the activation and ADFL-epilogue observers arrived about 628 seconds after the second ADFL entry. Their quiet intervals do not rule out earlier events. P151 installs all three alongside the shell-time UI observer and withholds the swipe until SpringBoard reports a non-null Home entity. See `docs/re/setup-launch-runtime.md` for the static setup-path evidence and its limits.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin150-natural-mutator1.qcow2` (SHA-256 `dfbb8cbdd32464437549532873c208d182ad163525eeabdd58d5577ce77b6ecb`) passed `qemu-img check -U`. Records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN150_NATURAL_MUTATOR1/`. No passcode, model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

### UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN151_HOME_GATED1: input bootstrap rejected by AMFI

P151 was stopped before the UI test. It enabled `INPUT_BOOTSTRAP=1` but used `system.tc`, which did not authorize `/usr/local/libexec/dvm-input`; AMFI rejected the helper and DVMI2 remained in launch state. No Home or swipe input was sent, so P151 has no UI verdict. Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin151-home-gated1.qcow2` (SHA-256 `17ab58ca5f9f5c29a933a1ebffbb4fab57172577b99248727e7aa84c9e43496f`) passed `qemu-img check -U`; evidence is under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN151_HOME_GATED1/`. P152 corrects the launch setup by using the merged DVMI2 trust cache and `INPUT_BOOTSTRAP=0`.

### UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN152_HOME_GATED1 — Home state without a display render

P152 is a durable fresh child of P149 using the merged DVMI2 trust cache and `INPUT_BOOTSTRAP=0`. The migration/shell ladder returned; SpringBoard selected setup mode and later returned a non-null Home entity with workspace and poster-context activity. A short `(589,2450)→(589,650)` swipe reached `UISystemGestureView`, but both observed screen-edge recognizers entered Failed state (5). This was not P141's accepted bottom-edge gesture and is not an unlock verdict.

The framebuffer stayed uniformly black (`1179×2556`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). CA/FB swap bookkeeping was observed, but no Server render-update, display/OGL render, finish-update surface, IOSurface sample, or DVMI2 present followed. This remains AP-side scheduling/display evidence; it does not establish a DCP or D572/D582 firmware defect. The durable child is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin152-home-gated1.qcow2` (SHA-256 `66415b93381af50a03c90ff25754af23000d6b2f1dbc4b924b5568a85ca436c6`); the recovery bundle is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN152_HOME_GATED1/`. P153 uses the P141 bottom-edge 600 ms/32-step gesture and the natural display scheduler trace. No passcode or device-model/fixture/scanout modification was made.

### UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN153_BOTTOM_EDGE_HOME1 — acknowledged input, no scene or frame

P153 was a fresh child of durable P152 with the merged input trust cache and read-only observers. The shell/check-in ladder returned. SpringBoard selected Setup-required reason 1 (`updateInSetupMode` returned 1); no Home-entity return, return-to-lock transaction, Setup lookup/activation, or `finalizeStartupAfterScenesDidConnect:` hit was recorded. A final Home key and a correctly placed `(589,2540)→(589,500)` 600 ms/32-step swipe, sent after detaching LLDB, were acknowledged and dispatched without errors. The Home-key frame was unchanged, the swipe produced no observed presented frame, and HMP remained uniformly black (1179×2556, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).

Backboardd's `render_for_time` full path ran at least 4,096 times, but V178 returned decimal 250 (`0xfa`); no later V248/V218/VF8/V170 or server surface path was recorded. A late LLDB reattach hit SIGTRAP in user code and destabilized run state until detached. That is not a SpringBoard crash or a rendering verdict. P153 was intentionally stopped after about 736 virtual seconds, before its configured 7,200-second bound. Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin153-bottom-edge-home1.qcow2` (SHA-256 `3f66ab894ea9548dae748352c43d18758baf357fdef5b36318346ccd77c022b7`) passed `qemu-img check -U`; recovery records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN153_BOTTOM_EDGE_HOME1/`. The next read-only checkpoint records SpringBoard scene-configuration request/return and main-scene completion to locate the first missing startup edge. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN154_SCENE_GATE1: scene path hit; helper rejected by trust cache

P154 used a fresh P152-lineage child and captured SpringBoard scene finalization plus a non-null Home-entity return. The run used `system.tc`, which did not authorize the installed DVMI2 helper. The host stopped at the helper-readiness gate and sent no gesture. This is lifecycle coverage only. Records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN154_SCENE_GATE1/`.

#### UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN155_HOME_TOUCH1: rendered lock IOSurfaces, black HMP capture, gesture not accepted

P155 used a fresh child of durable P152, the merged `system-plus-dev6-input.tc`, and DVMI2 with bootstrap disabled. SpringBoard selected setup-required reason 1, returned a non-null Home entity, connected scenes, finalized startup, and attached the poster scene. These events establish SpringBoard lifecycle progress, not a visible Home screen.

After input, backboardd entered `Server::render_update`, created and rendered 1179×2556 IOSurfaces, and passed them through framebuffer swap handoff. The captured surfaces contained 6,243 / 9,736 / 6,984 nonblack pixels. The middle sample visibly contains the lock-screen status/time, charging indicators, Chinese swipe prompt, info glyph, and home indicator. HMP captures remained uniformly black with SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`; the surface result is not proof of scanout.

The 600 ms, 32-sample bottom-center swipe was ACKed, but 11 of 33 motion updates were coalesced; 22 records dispatched without failure. SpringBoard reported `LOCKSCREEN_GESTURE_TRANSITION forUserGesture=0`; therefore it did not accept this input as the dismissal gesture. The matched setup activation observer logged 24 Buddy application lookup entries and three setup-exit callbacks, but neither the actual lookup return nor application activation was captured. A new dynamic return probe now follows each setup lookup's saved LR, and the next gesture run waits for each HID dispatch before emitting another motion point. Neither probe has been boot-validated yet.

Child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin155-home-touch1.qcow2` has SHA-256 `00c1f7d813346d9cea73227decdde3297648be08c1f340f876e09cd6d82f0992`; `qemu-img check -U` passed. Records and raw surfaces are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN155_HOME_TOUCH1/`. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN161: input delivered but SpringBoard Home/gesture markers absent

P161 reached `SHELL_SERVICE_RETURN`; DVMI2 ACKed the Home key and two bottom-edge swipes without reported dispatch failure. The helper captures remained at the uniform-black hash `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. The optional CA/scheduler/hotplug/mutator observer set was enabled, and LLDB detached at a render-scheduler stop; later events therefore cannot be used to infer SpringBoard recognition. P161 had no non-null Home entity, gesture-acceptance result, IOSurface sample, or visible UI verdict. P162 is the observer-light control; its `run_post_shell_home_render.sh` default intentionally does not include the lock-gesture callback module, so its input can only be credited through the recorded UIEvent/hit-test/HID path until a gesture-enabled wrapper is selected.

P161's durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin161-unlock-double1.qcow2` has SHA-256 `f818357564be5a2193c822ec93b292d7f5acbc1395fa154528802e5039a57e10` and passed `qemu-img check -U`. Records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN161_UNLOCK_DOUBLE1/`. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.


#### PLUGIN167: Home entity exists; natural render update returns ignored

P167's shell ladder returned and SpringBoard later produced a non-null Home entity. Backboardd `render_for_time` repeatedly took the full-path predicate, but sampled `ignore_update_p` returned 1 with active-display update flags `0x80010`, mode byte `+0x2bb=0xff`, and state bytes 4/5=`0/1`. No later CA/server/surface hook fired; HMP stayed uniform black. The swipe did not complete because the host input monitor timed out an unacknowledged record (status `L`, six dispatched records, pending wire data). No guest service-loss notification was captured, so there is no gesture verdict. This is a scheduler/display-gating clue and input coverage miss, not proof of firmware or model incorrectness. Durable child and records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/` in the `PLUGIN167_UNLOCK_PRESENT_SCHED1` bundle; `qemu-img check -U` passed. The next run removes the high-frequency scheduler hooks while keeping the gesture and present observers. D572/D582 remain hypotheses; no A408/scanout change was made.

#### PLUGIN168 / PLUGIN169: helper trust-cache miss corrected; P164-lineage swipe still fails

P168 used `system.tc`, which does not authorize the installed DVMI2 helper; it never reached readiness and sent no input. P169 corrected the TC to the merged helper cache and ran as a fresh P164 child. The shell/check-in path returned, SpringBoard acknowledged Home, and its Home entity was non-null. After an initial swipe started too high, the exact P141 bottom-edge path `(589,2540)→(589,500)` over 600 ms/32 steps was fully ACKed (38/38), dispatched 33 records, and had no coalescing or dispatch errors. The Cover Sheet recognizer nevertheless entered UIKit Failed state (5); no accepted user-gesture transition occurred. A later Power command was ACKed but did not change the capture.

P169's read-only backboardd hooks captured three IOSurfaces. The second reconstructed 1179×2556 BGRA surface has 7,091 nonblack pixels and visibly contains only sparse lock chrome (time/status, charging mark, info glyph, home bar) against black; no wallpaper, large lock clock/poster, or Home icons appear. The HMP dump remains uniformly black, so the surface cannot be used to assert scanout. An extra manual swipe was interrupted outside the trigger and left the input status with contact/button down; it is excluded from gesture scoring. The P164 child therefore did not reproduce P141's accepted dismissal and does not contradict P141's separate P134-lineage result.

Durable P169 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin169-unlock-present-twoswipe1.qcow2` (SHA-256 `31aee86d2153c82cf9ecffdeaa02c1a4259477c15c8759635deb44260d7ec397b`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN169_UNLOCK_PRESENT_TWOSWIPE1/`. The next cut uses a fresh child of P134, P141's proven accepted geometry, and simultaneous gesture/IOSurface observation. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN170: accepted gesture without visible Home or post-transition surface

P170 used a fresh P134 child and the accepted merged input trust cache. After shell/check-in return and non-null Home-entity creation, the late bottom-edge swipe `(589,2540)→(589,500)` was accepted by the Cover Sheet/system-edge recognizers. Dismissal began, changed, and ended; the lock transition recorded `forUserGesture=1` at `1791647530.652`. Input finished with no contact held or dispatch errors.

The last backboardd `render_update`/display/OGL chain preceded the transition by about 4.5 seconds. SpringBoard CA commits continued afterward, but no subsequent render/display/OGL result or non-null IOSurface swap was observed. HMP remained uniform black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`); a Home key after dismissal also left its frame unchanged. The IOSurface sample just before transition showed sparse lock chrome on black. P170 therefore proves gesture acceptance but not visible unlock or a D572/D582 defect. P171 enables the read-only pre-V170 scheduler decision trace.

Durable P170 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin170-p134-unlock-present1.qcow2` (SHA-256 `b152eba6a4c5641a8f4d498be99d680828d6a2d9eeedb1de30c5583add9198aa`) passed `qemu-img check -U`; evidence is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN170_P134_UNLOCK_PRESENT1/`. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

### PLUGIN171 — scheduler predicate is directly observed, but input-to-render join is open

P171 used a durable P134 child and the merged native-input trust cache. SpringBoard's shell/check-in ladder returned, and Home entity initialization returned non-null. HMP stayed uniformly black. Before input, 128 active-display `render_for_time` samples reached the full path with update flags `0x80010`, display mode byte `+0x2bb=0xff`, and state bytes 4/5=`0/1`; V170 `ignore_update_p` returned 1 each time. This agrees with the predicate evidence in GATE236 and places the sampled skip before Server render/surface creation. It does not establish a DCP/A407 fault.

LLDB lost its QEMU connection around the first swipe attempt; after resuming the guest and releasing the held contact, the later bottom-edge gesture completed (37 ACKed records, 33 dispatches, no dispatch errors/coalescing). Before/after screendumps remained the same uniform black frame. Because the gesture was after debugger loss, P171 has no same-boot post-gesture scheduler or gesture-acceptance verdict. The durable child and records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/` as `ui-class16-refresh60-dm-lock-plugin171-p134-render-decision1.qcow2` and `recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN171_P134_RENDER_DECISION1/`; `qemu-img check -U` passed. Next capture should pair a low-interruption active-display update sample with prepared-layer contribution and the recognized gesture. D572/D582 remain hypotheses; A408 stayed off.


### PLUGIN172: accepted dismissal transition, but no visible unlocked/Home surface

P172 used a fresh durable P134 child, the merged DVMI2 trust cache, and the unchanged display hypothesis profile. Shell/check-in returned, SpringBoard returned a non-null Home entity and issued workspace create/execute requests. The lock gesture observer recorded `LOCKSCREEN_GESTURE_TRANSITION` with `x3=1`; this is accepted user dismissal input, not proof of visible Home.

Three pre-transition IOSurfaces contained sparse lock chrome on black. HMP captures remained uniformly black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). CA commits continued after the accepted transition, but the later framebuffer swap handoffs had null IOSurface arguments; no post-transition Server/OGL render or non-null IOSurface was captured. P172's active-display Update/V170 samples were not gated to the transition and cannot be used as a post-dismissal verdict. P173 adds an accepted-transition marker and gates both Update/V170 and CA message sampling on it.

Durable P172 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin172-p134-update-gate-min1.qcow2` (SHA-256 `1caccd925ae5763d846864792047b251869e918408cd71533e40444f0bc8e9f4`) and records `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN172_P134_UPDATE_GATE_MIN1/` passed `qemu-img check -U`. A408 stayed off and D572/D582 remain hypotheses.

### PLUGIN173: post-transition updates pass the V170 ignore gate; HMP remains black

On a fresh P134-lineage child, SpringBoard returned a non-null Home entity and logged workspace create/execute activity. The lock transition recorded `x3=1` at `1791652814.948` (PC `0x22dd39560`), proving accepted user dismissal input only. The automated swipe helper exited 4; the transition is not assigned to that attempt without a matching input record.

Three transition-gated active-display updates sampled 2,063–2,066 seconds later had display ID 1, mode byte `+0x2bb=0xff`, state bytes 4/5=`0/1`, and Update flags `0x01000018`; V170 `ignore_update_p` returned `w0=0` for each. These post-transition samples pass V170. The P173 runtime probe did not include the subsequent compare/branch or downstream Server/surface sites, so its quietness does not classify what follows. The successor hooks were added for P174 and require fresh runtime validation.

HMP captures through the final frame were uniform black at 1179×2556 (`nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). P173 had earlier sparse lock-chrome IOSurface samples but no post-transition nonblack surface verdict. Reattachment reset the present observer's Home-return state; absence of later present rows is a coverage limitation. Six early logger errors came from a missing `time` field before runtime correction; subsequent rows were valid. No passcode or model, fixture, SpringBoard, CLASS16, SGX, A408, or scanout change was made; D572/D582 remain hypotheses.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin173-p134-ca-after-accept1.qcow2` (SHA-256 `86f544c856c9e77665a0541e58603f5bc478969ad3b99ed7893ae5e50fd2aab5`) passed `qemu-img check -U`; evidence is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN173_P134_CA_AFTER_ACCEPT1/`. P174 keeps A408 off and correlates those natural successor branches on a fresh accepted transition.

#### PLUGIN174 — shell-return timing correction and rendered lock surface

P174's trigger readiness marker was not a shell-return gate. It sent Home at `1791656519.886`; the matched `SHELL_SERVICE_RETURN` arrived at `1791657652.055`. The initial accepted lock gesture also preceded shell return. SpringBoard later emitted automatic `x3=0` lock transitions and continued workspace/Home-entity/poster-scene work. Post-shell manual Home/swipe records were ACKed, but no later gesture-dismissal transition was observed.

Two later backboardd OGL render entries were recorded, followed by full-size IOSurface captures with 9,884, 7,108, and 10,734 nonblack pixels. These are sparse lock chrome on black; HMP remained uniformly black at the standard hash. The result is not visible Home or scanout. A new trigger option now requires the explicit shell-service-return marker before input, and read-only SpringBoard lock manager/coordinator entry/return probes are armed for the next run. Those additions are not yet boot-validated. Durable P174 child and records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/` under `ui-class16-refresh60-dm-lock-plugin174-p134-postv170-path1.qcow2` and `recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN174_P134_POSTV170_PATH1/`. A408 remains off; D572/D582 remain hypotheses.

#### PLUGIN176: lock chrome reaches IOSurface; gesture result uninstrumented

P176 was a fresh child of durable P134 with the merged DVMI2 trust cache and an explicit shell-service-return input gate. SpringBoard returned Home entities and generated workspace activity. The Home key and two `(589,2540)→(589,500)` bottom-edge swipes were fully ACKed and dispatched (37/37 and 38/38 records; zero coalescing or dispatch failures), but before/after HMP hashes remained uniformly black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The trigger completed its bounded post-input observation; this does not establish gesture recognition.

Backboardd created 1179×2556 BGRA IOSurfaces and handed them to the framebuffer swap path. The third captured surface has 10,228 nonblack pixels of 3,013,524, including status time/indicators, the Chinese swipe-up prompt, info glyph, and home indicator on a black field; it has no wallpaper, large lock clock, or Home icons. HMP captures adjacent to those render/swap events remained entirely black. Lock chrome is present in compositor output, while scanout and unlock remain unproven. The P176 readiness line selected the older `home-render` bundle and emitted no gesture-recognizer or `LOCKSCREEN_GESTURE_TRANSITION` result; ACKed input cannot fill that gap.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin176-p134-shellgate-scenesurface1.qcow2` (SHA-256 `6c223fc94bdcc20e1be1448854a299b2ad3a550f032b69f4ac59c683cd2b75fe`) passed `qemu-img check -U`; logs and raw surfaces are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN176_P134_SHELLGATE_SCENESURFACE1/`. The next child explicitly loads the current gesture/present composite and gates update sampling on the accepted transition. A408 remains off; D572/D582 remain hypotheses.


#### PLUGIN178: touch reaches UIKit, rejects edge pan; lock surface renders but HMP stays black

P178 used a fresh P134 child, merged native-input trust cache, shell-return gate, and a single bottom-center upward swipe. Shell/check-in returned and SpringBoard created a non-null Home entity. All input records were ACKed and dispatched; SpringBoard hit-tested the touch in `UISystemGestureWindow`. The matched edge-pan recognizers entered Failed state (5), and no `forUserGesture=1` lock transition followed. The BackBoard direct-touch service result is unscored because the loaded runtime module reported only 9 hooks; the three service-resolution/handler points were added afterward. P179 is the first run that can score those sites, and only if its 12-hook arm-ready marker appears.

P178 did enter six Server/display/OGL render cycles late in its 1,200-second post-input window. Its sampled lock IOSurface contains 10,540 nonblack pixels, including status/time, charging, swipe prompt, info, and home indicator; its HMP frames remain uniformly black. Thus compositor content exists while scanout is still black; neither Home nor unlock is shown. This is a surface/scanout distinction, not proof of a DCP firmware error.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin178-p134-dispatch-route1.qcow2` (SHA-256 `939220717e4e45df9f15bed5aeaa89e5e68723163ae1c788a99d82c117b71b25`) passed `qemu-img check -U`; recovery records are at `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN178_P134_DISPATCH_ROUTE1/`. No passcode or text was sent; A408 stayed off and D572/D582 remain hypotheses.

#### PLUGIN181: accepted SpringBoard dismissal, no visible Home frame

P181 used a fresh child of durable P134 with the merged DVMI2 input trust cache and required this boot's shell-service return before Home/swipe input. The Home key was ACKed and a non-null Home entity followed. A normal bottom-edge gesture resulted in SpringBoard's accepted lock-dismissal callback (`LOCKSCREEN_GESTURE_TRANSITION`, `x3=1`, `1791679625.405`). A delayed follow-up swipe was also ACKed/dispatched without failure or coalescing, but its before/after HMP frame hash was unchanged.

The 3,900-second post-shell HMP sampler completed with every 1179×2556 frame uniform black (`nonblack_gt8=0`, hash `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). In 65 paired post-transition Update observations, `IOMFBDisplay::ignore_update_p` returned 1 with `Update+0x140=0x80010` and mask `0xa` clear. No downstream Server/OGL/surface event was logged. The pre-call observer did not emit, so display-state inputs remain unmeasured. LLDB stopped at the optional V170 skip-target breakpoint (runtime `0x190315c48`) and quit; the runner then failed to re-freeze the guest. This limits post-stop debugger coverage but does not erase the completed input/frame result or imply an IPC/device-model fault.

The durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin181-p134-update-gate-min2.qcow2` (SHA-256 `376588885715dedae8706d75c0ada7864bfd6c493a0f7037c017289ab3130905`) passed `qemu-img check -U`; logs and captures are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN181_P134_UPDATE_GATE_MIN2/`. The next read-only probe removes the skip-target breakpoint and samples the V170 implementation entry to recover the display and Update arguments. A408 stayed off; D572/D582 remain hypotheses.

#### PLUGIN179: gesture accepted, no visible frame; scheduler follow-up armed in P180

P179 ran from durable P134 with the accepted merged DVMI2 input trust cache and a shell-service-return gate. SpringBoard returned a non-null Home entity; the Home key was ACKed; and a normal bottom-edge swipe `(589,2540)→(589,500)` was ACKed/dispatched and produced a same-boot `LOCKSCREEN_GESTURE_TRANSITION` (`x3=1`, `1791673177.065`). A later manual Home/tap/swipe sequence was delivered after LLDB detach and is coverage-only.

The HMP captures stayed uniformly black for the trigger's 2,142-second post-shell interval (1179×2556; `nonblack_gt8=0`; hash `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). Post-transition Update flag sampling reached nine rows; the last had prepared bit 22 set, no `0x28000` mask bits, `Update+0x140=0`, and state byte `+0x5da=0x43`. The 31 sampled SpringBoard CA commits ended before the accepted transition. P179's readiness record says `render_scheduler_trace=false`, so it does not classify the natural `render_for_time`/V170 successor or prove a post-transition compositor handoff. The read-only update-gate wrapper now honors an environment-set scheduler flag; P180 enables it alongside the accepted-transition gate to measure that missing edge.

The frozen P179 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin179-p134-touch-service1.qcow2` (SHA-256 `78c93f124b56db296d492be3d0ca9d180daa22d8b285e6578d37d745df65ac20`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN179_P134_TOUCH_SERVICE1/`. No passcode or guest/model/fixture/A408/scanout change was made; D572/D582 remain hypotheses.


#### PLUGIN180 — full scheduler trace outlasted LLDB coverage

P180 was a fresh durable-P134 child with the merged input trust cache and explicit shell-return/Home-entity gates. Gesture, compositor/present, and minimum Update hooks reached their ready markers. Enabling the full `render_for_time` scheduler observer produced a large per-update event stream; LLDB then lost its QEMU connection at `1791675237.298`. No same-session `LOCKSCREEN_GESTURE_TRANSITION x3=1` was observed. The sole pre-disconnect recognizer-state row was `_UIRelationshipGestureRecognizer` → Failed (5), and all scheduler values were pre-transition. No post-dismissal render conclusion follows from those rows.

The guest continued after debugger loss. Subsequent manually delivered Home/swipe/power/tap events had no dispatch failures or held contact but are unscored because gesture callbacks were no longer attached. The 3,900-second HMP sampler recorded only uniform black 1179×2556 frames (`nonblack_gt8=0`, hash `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`); no A407 IDs appeared. The bounded result is loss of transition-to-render coverage, not a new negative gesture or compositor verdict. The durable child is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin180-p134-render-decision2.qcow2`, SHA-256 `96e457ea99be76c656f5d54ce65618628171e9f03361534233bdc7d8a7a2228e`; `qemu-img check -U` reported no errors. The runner stopped after the sampler bound; trigger status 143 is cleanup. P181 keeps the accepted-transition and minimal Update-gate observers but disables full scheduler and BackBoard input logging. D572/D582 remain hypotheses; A408 stayed off.

#### PLUGIN184: accepted swipe-to-dismiss, but no visible Home frame

P184 was a fresh P182 child using the merged native-input trust cache. The shell/check-in ladder completed and SpringBoard returned a non-null Home entity. A manual Home key and bottom-edge swipe were acknowledged and dispatched with no failures or coalescing. BackBoard routing reached SpringBoard, `SBCoverSheetScreenEdgePanGestureRecognizer` passed `shouldBegin`, the delegate accepted the path, and the recognizer ran through began/changed/ended. SpringBoard's lock manager then logged a user-initiated dismissal transition (`x3=1`, `for_user_gesture=1`) at `1791686696.572`.

HMP captures remained uniformly black at 1179×2556 before and hundreds of seconds after that transition (SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The composite readiness record says `update_gate_min_trace=false`; this run therefore did not sample the post-transition `ignore_update_p` decision. The result closes gesture acceptance but not the display/update path or visible unlock. The later automatic lock-manager transition (`x3=0`) is a separate event.

P184 was stopped through the probe stop file after more than 280 seconds post-transition, then frozen and copied. Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin184-p182-unlock-diag1.qcow2` (SHA-256 `7342232e1e82be70fb0f6719255eb5ee73986bf9686fe40053d37b085c4f8b9`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN184_P182_UNLOCK_DIAG1/`. The trigger now accepts the composite observer's readiness marker, and a fresh child will install the minimal Update/V170 observer after an accepted transition. A408 remained off; D572/D582 remain hypotheses.

#### PLUGIN185: Update observer stopped before the Home gate

P185 was a fresh durable P182 child. Its custom post-shell observer, corrected trigger readiness, and minimal Update observer all reported ready. The observer nevertheless placed `RFT_V170_IMPL_ENTRY` before the accepted-gesture gate; the first hit stopped LLDB at breakpoint 78.1 (`0x19755a1a0`) and the command file quit. Consequently no Home-entity return, accepted gesture, or post-transition Update row was captured. The trigger's Home key was sent, but its required non-null Home-entity marker never arrived while callbacks were attached. A later manual upward swipe after debugger loss had 37/37 ACKs and 33 successful HID dispatches, with no failure/coalescing; its before/after HMP frames were identical and black, and it has no SpringBoard gesture verdict.

P185 is a coverage failure at the observer boundary, not evidence that V170 rejected an update or that the lock gesture failed. The child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin185-p182-posttransition-v1701.qcow2` (SHA-256 `07ff9ae0fcbb975f2b4981cb42c0980065a6f228f9f7959abf7aef134f5e9fa1`) passed `qemu-img check -U`; evidence is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN185_P182_POSTTRANSITION_V1701/`. The probe now waits to install Update hooks until the gesture callback writes the accepted `x3=1` marker, and verifies the callback command on each hook. A408 stayed off; D572/D582 remain hypotheses.


#### PLUGIN186: accepted lock dismissal reaches OGL, but surface attribution is missing

P186 ran from the durable P182 child with shell return, non-null Home entity, and accepted user-gesture lock dismissal (`x3=1`, PC `0x225361560`). The marker-gated Update observer saw three same-Update OGL entries; each update had flags `0x18` and V170 returned zero. No Server/display/surface/swap callbacks were installed after the transition, so OGL entry cannot be promoted to a rendered or presented Home frame. The completed 2,645-second HMP sample remained uniformly black; input was ACKed and the guest responsive. This is an accepted-transition plus renderer-entry result, with the OGL-to-surface edge still open.

The durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin186-p182-posttransition-v1702.qcow2` (SHA-256 `f60c7ac261a7e09d77015ccb22023ed8a0eb641e9292f5b55934a23fbaa99982`) passed `qemu-img check -U`; evidence is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN186_P182_POSTTRANSITION_V1702/`. P187 arms read-only Server/display/surface/swap probes after the accepted transition. No model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN188 — post-shell accepted gesture still produces no visible surface

P188 used a fresh child of the durable P182 parent with the merged native-input trust cache. The BKS shell/check-in return and SpringBoard Home entity were observed; the ordinary bottom-edge swipe was fully delivered and accepted by SpringBoard (`SHOULD_BEGIN_RETURN_VALUE=true`, gesture began/ended, `LOCKSCREEN_GESTURE_TRANSITION x3=1` at `1791694519.803`). HMP stayed uniformly black for the completed 3,000-second post-input sample (1179×2556, `nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). A later Home key was acknowledged but did not change the frame.

Three observed post-transition `FB_SWAP_HANDOFF` calls carried null surface arguments (`x2=x3=0`); natural `render_for_time` observations continued to return `ignore_update_p=1` with flags `0x80010` and mask `0xa` absent through +2,694 seconds. The result is accepted gesture + black output + null observed swap surfaces, not a completed unlock and not a firmware/device-model verdict. This boot did not arm the display-mode source or mutator-flags traces. P189 enables both read-only probes after the accepted transition.

Durable child: `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin188-p182-swipe-sequence-present1.qcow2`, SHA-256 `6b476a3c0f618a86b5275e437af3a089f925ba29419f5a79c46c7d72ea80cce2`; recovery records: `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN188_P182_SWIPE_SEQUENCE_PRESENT1/`. `qemu-img check -U` passed. No passcode or guest/model/fixture/SpringBoard/CLASS16/A408/scanout change was made; D572/D582 remain hypotheses.

#### P189 — observer setup failure before Home/gesture scoring

P189 cleared the shell/check-in ladder but did not score post-shell UI. `dm_gate_light_callbacks.install` raised `UnboundLocalError` before writing `ready.gate-light.json`: a local `import sys` later in the function shadowed the module-level `sys` used by the optional display-mode import. Consequently the dynamic post-shell UI observer could not recover the same-boot slide and reported `missing-gate-light-slide`; the trigger did not produce a Home or swipe verdict. The redundant import is removed and syntax/diff checks pass. P189 is a probe-coverage miss, not a display-mode result. The durable child and records are documented in `iomfb-dseries.md`.


#### P190 — display 1 mode query error; no gesture verdict

P190's mode trace recorded display object ID 1 returning `0xe00002bc` from its mode-array query, falling back to mode `-1`, and retaining `-1` after the post-set method returned success. Its power-mode lookup also returned `-1`; secondary display objects accepted mode 0. The shell ladder and `SB_ADFL_ENTRY` ran, but no non-null Home entity or accepted gesture was observed. The trigger was terminated before the Home-entity timeout; two subsequent manual swipes were interrupted by `VM not running`, then the partial touch was released and the helper ended neutral. This boot therefore does not link the mode error to post-transition rendering. P191 uses the trigger's built-in timeout-swipe path under continuous LLDB coverage. Durable evidence and child are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN190_P182_MODE_SOURCE_HOME1/` and `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin190-p182-mode-source-home1.qcow2`.


#### P191 — slide resolver coverage miss before UI hooks

The P182 child did not produce a qualifying cache-backed EL0 PC during the resolver's 120-second window (`attempts=[]`). The debugger and deferred post-shell SpringBoard observer never armed, so no Home entity, gesture, or display result was observed. Its serial trace stopped before launchd's cache-map/boot messages; the run is a setup/boot-coverage miss, not a SpringBoard stall diagnosis. The durable child and logs are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN191_P182_MODE_SOURCE_TIMEOUT_SWIPE1/` and the sibling P191 qcow2.

#### P192 — post-shell Home and accepted lock gesture still have no visible frame

P192 reached the BKS shell-service return, created a non-null SpringBoard Home entity, and accepted a user-driven lock transition (`for_user_gesture=1`, `x3=1`). The Home key after that transition and the two bottom-edge swipes were acknowledged; none changed the HMP frame. The Home/swap before/after captures and final frame share the uniform-black hash `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. The accepted-transition observer did not confirm its stricter pan-sequence tuple, but its direct transition return has `x3=1`; it is not a visible unlock result.

The marker-gated P192 observer recorded 94 active-display V170 samples through +1,625 seconds after acceptance. Display ID 1 remained at mode `-1` (`+0x2bb=0xff`), state bytes 4/5 were `0/1`, and natural `Update+0x140` was `0x80010` with mask `0xa` absent. Every V170 returned `ignore=1`; there were no `OGL_RENDER_ENTRY`, `CALL_OUTER_A`, IOSurface, or DVMI2-present events in the armed window. This ties the accepted-transition window to the same natural pre-OGL rejection seen in earlier runs, while leaving the upstream source of mode and Update eligibility unresolved. P192 is read-only: no passcode, D572/D582 correction, guest/model/fixture, SpringBoard, CLASS16, A408, or scanout change.

The child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin192-p182-mode-source-timeout-swipe1.qcow2` (SHA-256 `7f59752e37e33af8035c5e0e80de41b20b0cbee367c29d22dc17b6b254b1b54a`) passed `qemu-img check -U`; artifacts are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN192_P182_MODE_SOURCE_TIMEOUT_SWIPE1/`.

#### PLUGIN193 — accepted dismissal, no visible Home frame

P193 was a fresh child of durable P182 with native input enabled. `DM_PERFORM_RETURN`, BKS migration/check-in return, and `SHELL_SERVICE_RETURN` completed; the shell service returned `x0=0x1fcf49c28` at `1791703886.291`. SpringBoard created a non-null Home entity. The Home key and bottom-edge swipe were acknowledged and dispatched; `shouldBegin` returned true and the Cover Sheet gesture completed. The lock manager recorded a user-gesture dismissal (`x3=1`, `for_user_gesture=1`) at `1791704315.112653`; a subsequent Home key was also acknowledged. No passcode was entered. These events prove accepted UI state and input, not a visible Home screen.

Every captured 1179×2556 frame remained uniformly black (`nonblack_fraction=0`; SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). There were 209 post-transition SpringBoard CA commits, but the CA handoff/message and scheduler portions were not armed in P193. The run therefore does not establish whether committed CA work entered the backboardd render path.

The read-only primary-display mode query returned `0xe00002bc` for display ID 1 at `1791702933.990745`, chose fallback/current/pending mode `-1`, and its power-mode lookup also returned `-1`; other display objects accepted mode 0. Later framebuffer handoff observations carried null surface arguments, while later update activity mostly targeted display ID 2. This is a concrete mode-source discrepancy correlated with the black output, but its causal relation to the accepted transition is open and the trace does not show that a DCP response is incorrect. D572/D582 remain hypotheses. No model, fixture, guest, SpringBoard, CLASS16, A408, or scanout changes were made.

The durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin193-p182-accepted-present-mutator1.qcow2` (SHA-256 `6756b122659fac3992c86c67cc7b2c0b1e78c187552c7ee5473004dfea0e331b`) passed `qemu-img check -U`; evidence is under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN193_P182_ACCEPTED_PRESENT_MUTATOR1/`. P194 is the read-only follow-up correlating post-transition CA handoff/message and scheduler events with the primary-display mode provider.

#### PLUGIN194 — mode-provider hook setup failed before UI scoring

P194's optional provider installer referenced nonexistent `SITES`, raising `NameError` before the gate-light readiness event. The requested eight hooks are in `render_gate_callbacks.USER`; the installer has been corrected to select that table by label, and static site coverage plus Python compilation pass. The runner stopped on the explicit setup error, before shell return or input, so P194 carries no UI or display-mode verdict.

Partial child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin194-p182-ca-mode-scheduler1.qcow2` (SHA-256 `6a088d8163e4462ab52e7a288a85c5997e239a66558da8f9795b06be376c596a`) passed `qemu-img check -U`; partial records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN194_P182_CA_MODE_SCHEDULER1/`. P195 uses the corrected read-only provider hooks from boot.

#### P195 display-mode result — null primary ColorElements remains the first measured failure

P195's read-only BackBoard provider hooks confirmed that the primary display
query uses service `0x13303` / connection `0x11803`; its `ColorElements` read
returned null, and `_kern_GetSupportedDigitalOutModes` returned
`0xe00002bc` with null mode and timing outputs. BackBoard consequently selected
mode `-1` and the power-mode lookup also returned `-1`. This same boot's active
display updates retained the `0xff` mode sentinel and were rejected before
rendering. Secondary display objects accepted mode 0. The observation narrows
the guest-side visible-UI blocker to the primary display property/mode source,
but it does not identify the required producer or prove an incorrect DCP
response.

Static DCP firmware contains `CopyColorElements`, `CopyTimingElements`, and
`CopyDisplayAttributes` methods, plus MIPI-side `constructColorElements` and
`constructTimingElements` strings. Those strings establish implementation
surface only. Existing AFK traces still show no command on the started
`DCPAVVideoInterfaceProxy` before the mode query, and the current QEMU DCP
model does not execute the RTKit firmware or generate spontaneous
endpoint-0x37 property callbacks. Neither fact supplies the expected request,
payload, or ordering. No DCP behavior was changed on P195; the next valid step
is to recover/confirm the AP request or D126–D128 callback input from a
matching firmware/runtime path before modeling it.

#### P196 — mode-provider stop and static ColorElements gate

P196 stopped at the planned BackBoard `ColorElements` property-result marker: the live primary-display property was null and the mode query returned `0xe00002bc` with no mode/timing output. The early and live LLDB logs prove the MIPI AP breakpoints were installed, but none hit before this stop. This is limited coverage, not a verdict on the complete MIPI driver path. The DCP trace for the same boot shows the scripted D120 callback returning four zero bytes and no D126/D127/D128 property transfer; the model does not execute the DCP RTKit task.

Static disassembly of the larger-task firmware gives a concrete producer gate. In `UnifiedPipeline::start_ios_gated()` at `0x429b718`, the code loads the global `AppleDCPDisplayService` object (`0x49850a8`), then its provider at `service+0x58`, and calls that provider's vtable slot `+0x130` at `0x429c118`. A zero return skips `constructColorElements` at `0x429c134` (implementation `0x4288a60`). The publisher tests each object and uses the transfer helper at `0x4289694`; the five keys are assembled in order from the call block at `0x42893ac`. This identifies the provider-method return as the next firmware-side gate, but not the provider's dynamic class or implementation behind that slot. It does not prove this path ran in P196 or provide data for a D126-D128 payload.

P196 used a fresh P182 child and made no model or property changes. Its durable child passed `qemu-img check -U`; launch profile, inputs, hashes, and logs are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN196_P182_MIPI_PROVIDER1/`. D572/D582 remain hypotheses. Continue by tracing the provider's build-matched class/vtable producer and source state for the `+0x130` return, and keep synthetic D120/A475 experiments labeled as controls rather than hardware answers.

#### P197 — provider relationship closed; vtable implementation remains open

Static xrefs show the start-style body at `0x431e550` receives its provider in `x1`, stores it at `this+0x58` (`0x431e634`), and publishes the service object itself at global `0x49850a8` (`0x431e658`). Its superclass-start tail path restores the same `this`/provider pair. This resolves the earlier ambiguity: `start_ios_gated()` calls a method on the service's matched provider; it does not establish a separate global-controller class. The provider's dynamic class, vtable `+0x130` implementation, and return value remain unknown from the stripped image. P196 cannot sample that DCP call because the QEMU model does not execute the DCP RTKit task. Static extraction notes and the source binary hash are preserved at `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN197_DCP_PROVIDER_GATE_STATIC/static-chain.txt`. No guest, DCP response, model, fixture, SpringBoard, CLASS16, A408, or scanout change was made. The next useful cut is a build-matched provider class/slot identification or a real matching-hardware callback capture; do not synthesize the slot result or property data.

#### P198 — MIPI selector source and AP device-match lead

Disassembly of DCP AppleDCPSynopsysMIPIDSIController::handleStart(AFKService *) narrows how it chooses indexed timing/config keys. It calls the controller's vtable +0x3e8 at 0x4132dfc; that result is checked against -1. The non-default path obtains a provider-side base value (provider vtable +0x1e0, then +0x48) and formats the key with "%s-%u"; the default/zero path uses unsuffixed display-timing and mipi-config. The separate mipi-swmpr property query starts later at 0x4132f68, so that key is not the selector. The meaning of +0x3e8 and the provider base value remain unresolved. This is static dataflow only; it does not prove either property lookup occurred in P196.

The P182 bootkc's AppleSynopsysMIPIDSI personality matches IOClass=AppleSynopsysMIPIDSIController, IOProviderClass=AppleARMIODevice, and IONameMatch=mipi-dsim-1,synopsys. The saved P182 system tree (/Users/steve/dvm-artifacts/bp-patched-dev6/system.dtree, SHA-256 172805b97889b6b4259f4cd44ea39c5bba591a6299e15cf633502391a2fe4430) has no node name or compatible with mipi-dsim-1. Its /arm-io/disp0 node is disp0,t8140 and has scalar timing keys such as frame-duration-nclks, frame-duration-lpm-nclks, and mipi-support-ioav=1, but not mipi-swmpr, display-timing, display-timing-lpm, or mipi-config. The only display-timing* tree property is dispext0/display-timing-info, a separate external-display path. The AP kext also contains the mipi-swmpr key and reads the timing keys from its IOService provider; this key overlap suggests a provider to inspect but does not join it to the DCP AFK object. P196's seven correctly slid AP MIPI probes were installed and had no hit before the planned null-ColorElements stop, which is bounded coverage and not proof the kext never starts.

#### P199 — CLCD2 dual-pipe mode-table candidate did not cover primary property publication

P199 was a 900-second bounded child of the durable P182 `plugin182-p134-v170-entry-home1` image, with the accepted dev6 trust cache and the existing P182 boot inputs. BackBoard reached its natural mode query: `IOMFB_MODE_COLOR_PROPERTY_RESULT` at LLDB log line 745 returned `value=None` for service `0x11f03` / connection `0x13603`; the kernel mode call returned immediately after that null ColorElements result. The four CLCD2 probes at static `0x918d3f8`, `0x918d540`, `0x918d5e8`, and `0x918d61c` were installed at runtime slide `0x20000000` but had zero dynamic hits. Disassembly shows this candidate builds a table by querying the primary object's vtable `+0xb38` and, conditionally, its attached object at `+0x61b8`; it is a dual-pipe/tiled-mode helper, so its silence is a coverage miss for that helper, not evidence about the ordinary primary-display ColorElements producer. The boot produced 193,985 serial lines with zero XNU panics and no restore-shell marker; this system-volume profile does not use the restore-shell marker as a success criterion.

The child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin199-p182-clcd2-mode-properties1.qcow2` (SHA-256 `56eff03479e7a71367cd28ef5b785dfe5b700d12fc9afc8e61d5533ae7eae8ec`) passed `qemu-img check -U`. The complete run record is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN199_P182_CLCD2_MODE_PROPERTIES1/`. No display property, mode, DCP response, model, fixture, SpringBoard, CLASS16, A408, or scanout value changed. D572/D582 remain hypotheses. Do not repeat the dual-pipe helper probe; the next cut must identify the primary service's real property writer or the relevant DCP property-transfer input.

The next read-only discriminator is early IORegistry matching/start coverage for mipi-dsim-1,synopsys, with the matched provider's timing/config property values captured if it starts. Only then should those values be joined to DCP's AFK lookup/transfer path. This lead does not establish that the absent node caused null ColorElements; it does not justify borrowing the external timing blob, fabricating a panel record, or changing the DCP model, fixture, SpringBoard, or scanout. D572/D582 remain hypotheses.

#### P200–P201 — BackBoard property observer and bounded null result

P200 did not produce a mode verdict: the wrapper imported
`render_gate_callbacks.py` as an ordinary Python module, leaving LLDB's
breakpoint callbacks unregistered. P201 corrected this with an explicit
`command script import` in `tools/re/iomfb_mode_property_callbacks.py`; its
ready marker confirmed all eight observer breakpoints. At the first live
primary-display query, service `0x13f03` / connection `0x13e03` returned a
null `ColorElements` value, and the mode call returned `0xe00002bc` with null
mode/timing outputs (shared-cache slide `0x15258000`). The planned observer
stop followed Early Boot; there were 1,060 serial lines and zero XNU panics.

The bounded serial prefix contained `AppleDCPLinkService (pipe:0)`,
`AP DRIVER START!`, and `AppleCLCD2` registration, with no textual
`AppleSynopsysMIPIDSI` or `mipi-dsim-1` marker before the query. This is not
proof that the AP MIPI controller never loads, and it does not connect its
provider to the DCP AFK property publisher. P200 was a callback setup failure;
P201 is a valid null-property measurement. No home/unlock result or DCP
firmware behavior is inferred.

Durable P201 child:
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin201-p182-iokit-match2.qcow2`
(SHA-256
`f5fdcd731520848b157dd4d5cdba8d9bc3f51457c847a61a03b19aae6fc23e70`),
`qemu-img check -U` passed. Launch inputs and records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN201_P182_IOKIT_MATCH2/`.
D572/D582 remain hypotheses. Continue with static class/dataflow analysis to
test whether the AP MIPI provider can feed the DCP `ColorElements` builder;
do not synthesize a property or transfer payload.

#### PLUGIN202 — Home entity returned; duplicate input-helper bootstrap blocked gesture coverage

P202 was a fresh child of durable P182 with the native-input trust cache and
read-only early MIPI / post-shell observers. The migration, DM perform, BKS
check-in, and shell-service returns completed. SpringBoard entered ADFL and
returned two non-null Home entities. The shell-return, Home-entity-return,
and CA-commit HMP captures were all uniformly black (1179×2556; nonblack
fraction 0; SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The
primary display's `ColorElements` read was null and the kernel mode query
returned `0xe00002bc`; all seven early MIPI AP probes were installed at the
resolved slide but had no hit in the bounded interval. This does not establish
that the MIPI driver never runs or identify a DCP-side producer.

P202 enabled the optional dynamic input-helper bootstrap even though the
native input launchd job was also active. The bootstrap successfully spawned
PID 31; the ordinary job then attempted PID 86 and reported the singleton
lock was held. The spawned helper remained in `I` after `DVM_HID_INIT begin`,
without `DVM_HID_INIT done`, `DVM_HID_SERVICE`, or `DVMI2R R`. The trigger
correctly withheld Home/swipe input until helper readiness. Thus P202 is an
input-helper setup mismatch, not a gesture rejection or unlock result. This
differs from P188, whose no-extra-bootstrap helper reached `R` and whose
accepted lock-dismissal transition was recorded, although P188 also remained
black. P203 reuses that no-bootstrap launch shape and keeps the
accepted-transition render observers armed.

The original runner stopped after preserving the run because it tried to
copy `home-trigger.log` onto itself under `set -e`; that copy was removed.
The P202 child was checked, durably copied, and checked again. Its durable
path is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin202-mipi-home1.qcow2`
(SHA-256
`7cab8e00e5266739115529faf911a59386f068a3b7d1f24a77b46a5ce0e9176b`); the
backing chain resolves to the P182 parent. Full records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN202_MIPI_HOME1/`.
No model, fixture, SpringBoard, CLASS16, A408, or scanout changes were made;
D572/D582 remain hypotheses.

#### P289 — post-shell control separates Lock Screen surface bytes from HMP output

P289 is a fresh durable P276 child using the accepted `system.tc` and the
existing CLASS16/D572/D582 hypothesis profile. `DM_PERFORM_RETURN` recorded
`x0=1`; BKS migration/check-in returned `x0=0`, then SpringBoard's
shell-service return fired. A user-gesture CoverSheet dismissal was accepted;
the legacy helper then sent its Home key. No second touch swipe was scheduled.
No passcode or authentication was used.

At guest time `1791808758.566068`, 312.29 seconds after the accepted gesture
and 138.33 seconds after shell return, BackBoard yielded a 1179×2556 surface
(stride 4864, pixel-format value `1111970369`, BGRA-tagged). Its decoded
pixels show a black screen with status/battery indicators, home indicator, and
a localized “Swipe up to open” prompt. Only 10,430/3,013,524 pixels are
nonblack. The final HMP screenshot at +420 seconds remains uniformly black.
This confirms sparse Lock Screen content at one IOSurface boundary while
leaving surface submission/scanout unresolved; it does not demonstrate Home
icons or authentication.

The first sample was after the acknowledged post-transition Home key, so the
control excludes the second swipe, not all later input. P284's temporary
white surface during its follow-up swipe remains a distinct observation and
cannot be attributed causally by this timing comparison. The useful next cut
is to send one ordinary swipe after P289's lock prompt is present and pair
surface bytes to the existing swap/HMP sampling. Full evidence and source
hashes are in the durable P289 recovery record linked in `iomfb-dseries.md`.
No DCP response or mode was altered; D572/D582 remain hypotheses.

#### PLUGIN206 — accepted dismissal still produces only null-surface swaps

P206 reached shell-service return and a non-null SpringBoard Home entity. Its
native-input helper ACKed/dispatched the Home key and bottom-up swipe; the
same boot recorded `LOCKSCREEN_GESTURE_TRANSITION` for a user gesture
(`x3=1`). The framebuffer remained uniform black before input, after the
accepted transition, and at the final capture (1179×2556; SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).

The transition-gated observer saw 321 SpringBoard CA commits and six
BackBoard swap set-layer/handoff pairs; every swap had a null IOSurface
argument. No downstream Server render, OGL, finish-update, or IOSurface
allocation hit was recorded. P206 did not arm the display-mode/provider,
scheduler, or CA send/receive traces, so it does not name why those surfaces
are absent. P207 attempted those observers but cannot classify post-dismissal
rendering: the mode-provider callback raised `KeyError: 49` and detached
LLDB, and its selected generic render module did not install the accepted-
gesture observer. The primary `ColorElements` query was null and mode lookup
returned `0xe00002bc` before input, consistent with prior measurements. P207
was stopped as a coverage failure; helper ACKs and a black frame do not
prove dismissal or a downstream render result. The callback now tolerates
orphan breakpoint IDs; the next run must load the gesture-aware transition
module before input. P207's checked child is durably stored at
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin207-p182-v12-home-display-join1.qcow2`;
full records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN207_P182_V12_HOME_DISPLAY_JOIN1/`.
D572/D582 remain hypotheses; this is not a DCP firmware verdict.

P208 closed shell service and armed the gesture-aware UI observer, but the
input helper never became ready because the runner used the standard firmware
ramdisk instead of the V12 native-input ramdisk. It recorded no Home/swipe
input and no accepted dismissal; the only transition had `x3=0`. The
boot-time primary mode query again returned null `ColorElements` and
`0xe00002bc` before input. This confirms the repeated mode-query result but
does not classify post-dismissal rendering. P208's checked child and records
are listed in `iomfb-dseries.md`; the next attempt explicitly supplies the
V12 `ramdisk-input.dmg`. D572/D582 remain hypotheses.

P209 explicitly supplied that V12 ramdisk and trust cache, but the guest
started the protocol-1 `DVM_INPUT` v6 helper rather than the DVMI2 V12 helper.
The host's readiness pings were unanswered; no touch/key input was sent and
no user-gesture transition was observed. P209 did re-enter the SystemApp
client-sync path and repeat the primary display mode-query failure (`-1` /
`0xe00002bc`), but it ended before BKS/shell return and before any post-shell
Home or render result. This is input-helper/setup coverage, not an unlock or
render verdict. Its durable child and logs are documented in
`iomfb-dseries.md`; next use a disk state proven to run V12 and require DVMI2
`R` before input. D572/D582 remain hypotheses.


#### PLUGIN210 — SpringBoard reports a user-gesture lock transition; Home remains black

P210 used a fresh child of the durable P182 V12-install parent with the V12
native-input trust cache and ramdisk, no input-helper bootstrap, and the
post-shell mode, gesture, CA, scheduler, update-gate, and frame observers.
The guest completed DM_PERFORM_RETURN, BKS migration/check-in return, and
SHELL_SERVICE_RETURN. SpringBoard returned a non-null Home entity. Native
input then ACKed and dispatched the Home key and a 32-step bottom-up swipe
without drops. The lock-manager transition call recorded x3=1 /
for_user_gesture=1 at 1791725195.468; the separate gesture-sequence
correlator did not confirm the expected Cover Sheet pan tuple, so this is
not evidence of a visible unlocked screen.

Every sampled 1179x2556 HMP frame through 1,082 seconds after shell return
was uniformly black (nonblack fraction 0; SHA-256
d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9). The
boot-time primary display ID 1 mode-array query returned 0xe00002bc with
null mode data, fell back to -1, and kept mode/current/target/pending at
-1. In the short post-transition debugger interval, active display ID 1
still had mode -1 (+0x2bb=0xff); its Update+0x140 was 0x80010 with
mask 0xa clear, and ignore_update_p returned 1. There were no
Server::render_update, Display::render_display, OGL, finish-update,
IOSurface, or present events in that interval. SpringBoard CA commits and
CA sends occurred, but the sampled BackBoard CA receive was message 40001
while the SpringBoard sends were message 40004; these observations are not
a joined handoff.

Coverage is intentionally split: although the trigger observed for 900
seconds after input, LLDB stopped at RFT_FULL_PATH about 26 seconds after
the recorded lock transition and then quit. Thus the later 1,082-second
evidence is HMP frame sampling, not continuous render-hook coverage. P210
confirms Home entity creation, an accepted for_user_gesture transition,
and continued black output; it does not establish a full Cover Sheet pan,
visible Home/unlock, or a DCP firmware/model defect. The measured primary
mode-source failure remains the leading display blocker, not a proven cause
of the gesture result. No image, DCP response, model, fixture, SpringBoard,
CLASS16, A408, or scanout behavior was changed; D572/D582 remain hypotheses.

The checked child
/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin210-p182-v12-home-display-join4.qcow2
(SHA-256
499537eb6022075bca87066a23de1ab66db677199ddf00ece8d2c207b5da9b16)
passed qemu-img check -U; its backing chain resolves through the V12
installed child to durable P182. Full records are under
/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN210_P182_V12_HOME_DISPLAY_JOIN4/.
The next read-only cut should identify the primary display's mode/property
producer or explain the natural ignore_update_p rejection before making
any display-model change.


#### PLUGIN212 — SpringBoard produces a sparse lock IOSurface; HMP stays black

P212 was a fresh V12-input child of durable P210. The migration, BKS
check-in, and shell-service path completed; SpringBoard entered ADFL, created
non-null Home entities, executed workspace transitions, and attached poster
scene/context `0x20e293eee`. The configured post-shell observer was the
render-only module, so this run did not arm the lock-gesture recognizer or
write an accepted-transition marker. Native Home/swipe input reached
BackBoard and SpringBoard, including system-gesture hit testing, without
reported transport or dispatch failures. A later acknowledged Home key left
the before/after HMP frame hash unchanged. These inputs do not establish an
accepted unlock.

Unlike P210's short post-transition observer, P212 recorded repeated
`Server::render_update`, `Display::render_display`, OGL, surface creation,
`fb_swap_set_layer`, and framebuffer-handoff activity. A captured 1179x2556
BGRA IOSurface contained sparse lock-screen chrome: top status time/battery,
the lower info glyph, and the home indicator; samples contained 6,953 to
12,973 nonblack pixels out of 3,013,524. It had no wallpaper, large lock
clock, or Home icons. Nine timed HMP captures through post-shell +962s
remained uniformly black (zero pixels above threshold; SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).
Thus lock-layer pixels exist in a BackBoard IOSurface, but a visible scanout
or unlocked/Home frame is still unproven. No A407 completion was traced;
D572/D582/A407 scanout remain hypotheses, and A408 stayed disabled.

P212 was stopped deliberately after the useful surface and bounded HMP
samples, not after a guest failure. The child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin212-p210-mipi-home2.qcow2`
(SHA-256
`83b01fafd2437655c71808e260e7b5a8cce7e09184ba71781becb9fdc986a39e`)
passed `qemu-img check -U`; records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN212_P210_MIPI_HOME2/`.
The next child arms the recognizer and accepted-transition callback before
input, then samples natural Update/V170 eligibility and the resulting
render/swap path. No passcode, guest/model/fixture/SpringBoard/CLASS16,
A408, or scanout changes were made.

#### P213 — input reaches the system-gesture view, but Cover Sheet pan fails

P213 used a fresh child of durable P210 with the V12 native-input path and
gesture/mode/frame observers ready. The shell ladder returned and
`SB_HOME_ENTITY_READY` supplied a non-null Home entity. Two bottom-up swipes
each dispatched all 33 points without helper errors. BackBoard resolved the
initial contact to `(589,2540)`; SpringBoard's `_UISystemGestureWindow`
`hitTest` returned `UISystemGestureView`. The Cover Sheet and FluidSwitcher
edge-pan recognizers received touch-began, and the Cover Sheet recognizer
received movement, but UIKit ended it in state 5 (Failed). No
`HOME_GESTURE_PAN_SHOULD_BEGIN`, Cover Sheet dismissal begin/change, or
accepted `LOCKSCREEN_GESTURE_TRANSITION` (`x3=1`) followed. The before/after
HMP captures retained the same uniformly black frame hash
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`.

P141 had accepted the same bottom-edge profile on its second attempt after a
higher first swipe; P213 did not run that sequence. It is a useful exact
control to preserve, not evidence that the first swipe is a required gesture
priming step. The display observer independently repeated ID 1 mode/target/
pending `-1`, state bytes `[0,1]`, null `ColorElements`, and no valid primary
mode. This display condition remains separate from the gesture failure; P213
does not prove it caused recognizer state 5. The durable child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin213-p210-accepted-update1.qcow2`
(SHA-256
`1fb6d4b85ceb57a7a93c1cd3959c8568839b87289b32542e51dd4a8a6af1792d`)
passed `qemu-img check -U`. Full captures, event logs, and launch profile are
under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN213_P210_ACCEPTED_UPDATE1/`.
No passcode or guest, device-tree, DCP response, model, fixture, SpringBoard,
CLASS16, A408, or scanout behavior was changed; D572/D582 remain hypotheses.

#### PLUGIN215 — accepted Cover Sheet transition does not produce a visible frame

P215 used the durable replay2b/P210 parent, V12 native input, and read-only
SpringBoard/Home/render observers. The 15-second upward pan began successfully;
the recognizer returned true from `shouldBegin`, Cover Sheet dismissal began
and ended, and the user-gesture transition marker was accepted at
1791734240.853. SpringBoard's workspace-transition method returned `x0=1`
and its lock-manager `lockUI` call returned `x0=0`. The follow-up Home key was
acknowledged, but the HMP frame remained uniformly black before and after
(SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). This
is positive gesture/workspace evidence, not a Home-screen success.

After the accepted transition, SpringBoard continued CA commits. The last
observed BackBoard `render_update` / `render_display` / OGL / surface allocation
events were still pre-transition; later framebuffer layer swaps carried a
null IOSurface (`x3=0`). Thus the first measured gap is CA commit to a new
BackBoard display update/surface. P215's CA-message probe was installed by the
general post-shell module and exhausted its 64-send limit before the real
gesture, so it cannot tell whether a post-transition CA message was sent. The
dedicated `dm_post_shell_home_transition_present_callbacks` module was not
selected; this is the next run's targeted correction. A408 was observed only
at its build entry; no outbound completion/presentation was inferred. Sparse
pre-gesture lock IOSurface pixels are not evidence of post-transition content.

The child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin215-p210-ca-handoff1.qcow2`
(SHA-256
`f00b8edffb9b6460b6b8bbd41407d8e1207a4839ecc45b9a195e7dda20d0c2ae`)
passed `qemu-img check -U`. Captures and launch profile are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN215_P210_CA_HANDOFF1/`.
The next fresh P210 child will arm CA-message/render hooks only after an
accepted user-gesture transition, preserving the successful P215 input
sequence; no passcode or guest/model/fixture/firmware/scanout change was made.

#### PLUGIN218 — legacy input works; first swipe was not accepted

P218 used the durable P210 parent and requested the V12 input ramdisk/TC, but
the running guest service identified itself as protocol-1 v6. The DVMI2 bridge
records were rejected by that helper; an independent legacy relay received
ACKs for both Home and touch. Since Home was sent before the 15-second swipe,
the swipe was not a clean first-action lock-screen test: SpringBoard's
`HOME_GESTURE_PAN_SHOULD_BEGIN` returned zero, and the subsequent
lock-manager transition lacked a matching Cover Sheet pan sequence. Uniformly
black HMP frames through +481 seconds do not classify Home rendering. The
durable child `ui-class16-refresh60-dm-lock-plugin218-p210-true-transition-ca3.qcow2`
(SHA-256 `b971c40bedd1b9410866fb802652bd081aa866582083b747b5f6e9bde8483b7a`)
passed `qemu-img check -U`; the complete bundle is under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN218_P210_TRUE_TRANSITION_CA3/`.
The next test uses only DVMINPUT1 relay input and sends the upward pan before
any Home key; no passcode or guest/model/display patch was used.

#### P219 — accepted dismissal is real; natural BackBoard update exits at V178

P219 used the durable P210 replay2b parent, `system.tc`, legacy input, and
deferred observers. One fully serialized 34/34-ACK upward swipe was followed
by SpringBoard's confirmed Cover Sheet dismissal and an accepted
user-gesture lock transition at `1791738306.205`; the Home entity was
non-null, workspace transitions returned success, and an acknowledged Home
key followed. The earlier malformed/coalesced relay attempts were released
and are not scored. No passcode was supplied.

Every captured 1179×2556 HMP frame remained uniformly black (SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The
post-transition scheduler trace recorded repeated `render_for_time` full
branches; the `+0x178` call returned `250` (`0xfa`), so the natural path
branches away before the later V248/V218/VF8/V170 and outer-render callbacks.
Three nearby framebuffer swap calls carried null surfaces. The only sampled
power-state target was display ID 2, not joined to the actual render
receiver. Thus the accepted SpringBoard transition is closed, while the
reason for the `+0x178` result and missing visible Home frame remains open.
P219 did not log the receiver's `+0x330` bytes; P220 adds that sample and a
read-only writer watch, gated on the accepted transition. It does not alter
the display state or force a render path.

Durable child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin219-p210-legacy-swipe1.qcow2`
(SHA-256
`e3a3a48796d6578185d3ed6deb57f917974273bc8e15ad333413f4d3d19cd6e3`)
passed `qemu-img check -U`. Records are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN219_P210_LEGACY_SWIPE1/`.
D572/D582 remain hypotheses; no passcode, device property, DCP response,
model, fixture, SpringBoard, CLASS16, A408, or scanout value was changed.

### P220 — accepted lock dismissal still has no supported primary mode

P220's accepted user gesture and non-null Home entity are same-boot, but the
active display remains at mode `-1`. Across the debugger-covered interval,
65 paired active-display updates had flags `0x80010`, state bytes
`01 01 00 00 00 01 00 00`, and V170 `ignore_update_p` returned 1 each time.
The active display therefore did not enter OGL on those samples. The writer
watch only covered the first few seconds after the gesture before LLDB
 detached; later HMP frames (uniform black through +602 seconds) are not
render-hook coverage. All six saved dev1–dev6 device trees have the same
SHA-256, so an alternate saved MIPI tree is unavailable. This is evidence
for the unchanged mode/property boundary, not proof that a particular DCP
response or device-tree property is correct. The durable P220 child and
recovery records are documented in `iomfb-dseries.md`.

Static review of the recovered H17P DCP image also scoped
`HotPlugDetectChangeOccurred`: its named call sites are in the
`DCPAVDPTXRemotePortEPClient`/remote-port handling path. The relevant handler
compares the sender to the object's stored `_port` (`this+0x118`) before
updating link/plug state. This is an external DisplayPort notification path;
it is not evidence for the built-in MIPI panel's missing internal-display
handler or `ColorElements`. Do not synthesize this HPD event for the internal
panel. The reconstructed image hash, static-only status, and QEMU limitation
are recorded in `iomfb-dseries.md`; this binary is not executed by QEMU's DCP
model.

### P221 — AP MIPI provider lookup sites resolved, live match still open

Static bootkc xrefs resolve the AP controller's property lookup sequence.
`AppleSynopsysMIPIDSIController::start(IOService *)` is its vtable `+0x2b0`
override at `0xfffffff0096f0e68`; `x21` holds the supplied IOService provider.
The provider's IORegistryEntry `+0x118` is queried for `display-timing`
(`0xfffffff0096f1be8`), `display-timing-lpm`
(`0xfffffff0096f1c44`), and `mipi-config-lpm`
(`0xfffffff0096f1ca0`), followed by existing probes at
`0xfffffff0096f1bec`, `0xfffffff0096f1c48`, and
`0xfffffff0096f1ca4` to sample the returned property objects. Earlier in the
same routine the provider supplies `#lanes`, `mipi-version`, `phy-test-num`,
and `phy-test`. This defines what a positive AP match should inspect, but it
does not prove that the driver started or that its provider supplies the DCP
AFK properties behind the `start_ios_gated()` `+0x130` gate. P196/P211/P212
installed the entry/property probes without a hit in their captured windows;
that remains a bounded no-hit, not a driver-failure verdict. Keep the next
capture at the early match/start edge, and join AP values to DCP property
publication only if the actual provider identity is observed. No guest,
model, fixture, device-tree, DCP-response, or scanout changes were made.

#### PLUGIN223/224 — Home transition accepted, but natural display updates are ignored

P223 does not carry a renderer verdict: its swipe used framebuffer-pixel coordinates instead of the native input protocol's normalized coordinates, and the deferred render installer was over-gated by auxiliary gesture-sequence reconstruction. P224 corrected both. On the durable replay2b parent, DataMigrator, SystemAppMigrator, DM perform, BKS check-in, and shell-service returns completed. SpringBoard initialized a non-null Home entity, requested and successfully executed a workspace transition, then accepted the user-gesture lock dismissal (`x3=1`) at `1791744911.763`. A SpringBoard CA commit followed. HMP frames at the CA commit and about 95 seconds after the accepted transition were still uniformly black (SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).

On the active display ID 1, P224's sampled post-transition Updates retained mode `-1` / byte `+0x2bb=0xff`, state bytes `01 01 00 00 00 01 00 00`, flags `0x80010` (`flags & 0xa == 0`), and V170 `ignore_update_p` returned 1. That places the sampled Updates at the same pre-render rejection seen in P192/P220; it does not identify why the supported mode remains absent or prove the DCP/model response is wrong. Present hooks were armed, but LLDB stopped at breakpoint 169 roughly 19 seconds after acceptance, so later hook silence is coverage loss. No passcode, DCP response, mode, model, fixture, SpringBoard, CLASS16, A408, or scanout behavior was changed. D572/D582 remain hypotheses.

P224's durable child is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin224-posthome-present2.qcow2` (SHA-256 `c9db7ea681d515feab6e62686dd5f7214e6e654429f4832d3af79b29a54fba24`); `qemu-img check -U` passed. Full records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN224_POSTHOME_PRESENT2/`. Next, trace the natural primary-display mode-selection/provider path without injecting mode or DCP values.

#### P225 — accepted lock dismissal; natural mode property still absent

P225's fresh replay2b child completed migration, check-in, and shell-service
return, created a non-null SpringBoard Home entity, and accepted a matched
user-gesture Cover Sheet dismissal at `1791746486.296`. Its 64 later
presentation events show CA commits, BackBoard rendering/surface work, and
framebuffer swaps, while every captured frame through +722 seconds stayed
uniformly black (SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). This
closes the SpringBoard gesture/state edge but not visible Home rendering or
passcode unlock.

At three natural BackBoard mode queries, `ColorElements` was null. The first
primary query was service `0x13803` / connection `0x13703`; its supported-mode
call returned `0xe00002bc` with zero mode/timing outputs. Later null-property
queries used other service/connection pairs. These logs prove the missing
value at the consumer; they do not prove its source or that it is the only
reason for black pixels. Prior DCP analysis bounds the firmware publisher's
`start_ios_gated()` provider `+0x130` gate and subsequent AFK transfer, but
QEMU substitutes its RTKit/AFK model and does not execute the recovered DCP
image. The next useful step is a read-only boot-early join of AP MIPI, DCP
MIPI proxy, AFK report, IOAV publication, and internal-display handling. No
`ColorElements` payload, DCP response, mode, model, fixture, device tree,
SpringBoard, CLASS16, A408, or scanout behavior was changed; D572/D582 remain
hypotheses.

The input helper was corrected to send Home before waiting for Home entity,
and to send a Recap lift if the Recap swipe is rejected. Recap swipe/release
now emit kind `R` on the legacy relay instead of touch kind `T`. These edits
compiled cleanly and P225's manual relay confirmed the corrected gesture.
Durable child and complete logs are recorded under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN225_HOME_MODETRACE1/`.

### P226 — interface-16 PUBLISH observed, downstream decode not joined

The P226 reset-time AFK observer recorded a 300-byte report on interface 16
named `dcpmipi-controller-epic`; the early session later captured parse/decode
activity for interface 13 (`dcp-lpdptx-port-epic`) but no corresponding
interface-16 parse/decode/handoff before slide resolution ended that session.
Thus P226 did not have an absent interface-16 PUBLISH. It has a report-to-
parse/handoff gap in the bounded early capture. `IOMFB_AP_START` returned
`x0=1`; the EDT-read observer did not hit in that interval.

In the same boot, SpringBoard returned the shell service, built Home state,
and accepted the user lock-dismissal gesture, but HMP frames through +361
seconds were black. The active primary display fell back to mode `-1`, and
the natural supported-mode query returned `0xe00002bc`. No display property,
mode, or DCP answer was fabricated. P226's image and complete run record are
listed in `docs/re/iomfb-dseries.md`.

#### P227 — Home dismissal accepted; natural primary mode remains unavailable

On a durable replay2b child, P227 completed the migration/check-in/shell
ladder, initialized SpringBoard's Home entity, and returned success from the
workspace execute transition. A normalized Recap gesture then produced a
confirmed Cover Sheet dismissal at guest time `1791749422.812` with
`for_user_gesture=1`. The helper captured a final HMP frame after a complete
240-second post-transition interval; it was still uniformly black
(`nonblack_fraction=0`, hash
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). This
closes the SpringBoard state transition, not visible Home or passcode unlock.

Before the gesture, BackBoard's natural query of the active display ID 1
found null `ColorElements` on service `0x13603` / connection `0x11c03`;
`_kern_GetSupportedDigitalOutModes` returned `0xe00002bc` with no mode or
timing outputs, and BackBoard selected `-1`. After the accepted gesture,
64 active-display scheduler samples still showed mode `-1`; every sampled
V170 `ignore_update_p` returned 1. The read-only hotplug-state watch was
armed on the display's watched state pointer and recorded no later write.
Presentation probes armed after the gesture, logged one CA commit, and saw no
Server render-update, display-render, surface, or swap callback in their
covered interval. The final frame and these callbacks make the missing
primary mode/property path the immediate unresolved display boundary, but
do not identify its publisher or prove that it is the only cause of black
output.

P226's 300-byte interface-16 `dcpmipi-controller-epic` PUBLISH is still not
joined to interface-16 parsing, an internal-display event, or publication of
`ColorElements`. P227 added no DCP-side report evidence and changed no DCP
response, property, mode, model, fixture, device tree, SpringBoard, CLASS16,
A408, or scanout behavior. D572/D582 remain hypotheses. The durable P227
child, SHA-256, final frame, and full logs are recorded in
`docs/re/iomfb-dseries.md` and the P227 recovery bundle.

#### PLUGIN230 — reset PUBLISH confirmed; parser path remains unjoined

PLUGIN230 repeated the interface-16 `dcpmipi-controller-epic` PUBLISH in a
reset-time AFK trace and verified the post-slide AFK parse/decode/handoff,
DCP-MIPI proxy, IOAV video, and internal-display hooks were armed. Through the
natural primary-display mode query, the trace recorded no interface-16
parse/decode/handoff or DCP-MIPI proxy start, and no IOAV video/internal-
display consumer event. The mode query still found null `ColorElements` and
returned `0xe00002bc`. This is a bounded observation ending at the natural
query, not a permanent no-parser verdict.

GATE221/224 remain the positive control for queue order: when external
DPTX/HDCP announcements are omitted, the queued MIPI publish decodes and the
proxy starts/opens on interface 12. That did not establish a MIPI report
schema or publish `ColorElements`. The next useful cut is the kernelcache
personality/provider flow that would instantiate the IOAV video classes,
paired with the first command/report after the MIPI proxy opens. No DCP answer,
ColorElements value, mode, model, device tree, or scanout content was
synthesized. The PLUGIN230 child and full capture are in the durable recovery
bundle listed in `docs/re/iomfb-dseries.md`.


#### P231 static — the `+0x130` provider return is an output-state gate

Cross-references in the saved T8140 DCP image (`t8140dcp.macho`, SHA-256
`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`)
show the same `AppleDCPDisplayService` provider (`service+0x58`) virtual slot
`+0x130` being used in three related paths. `UnifiedPipeline::start_ios_gated`
calls it at `0x429c118` and skips `constructColorElements` (`0x4288a60`) on a
zero return. At `0x428626c`, the result with its low bit ignored is compared to
2; values 2 or 3 enter a branch that passes the literal `external` to a
service method at `this+0x180`. At `0x4290cd0`, a nonzero result makes the
logged `System Headless` value false; a zero result instead calls the same
provider's `+0x1d8` method and sets that diagnostic when its result is `-1`.

This establishes that `+0x130` is a shared output/display-state gate relevant
to both external-output handling and the `ColorElements` builder. It makes a
zero return a concrete static explanation for why the builder would be
skipped, but does not identify the provider's dynamic class, name the method,
or establish the return value on this emulated boot. The QEMU model does not
execute this DCP RTKit path, and no provider value, property, firmware reply,
device-tree entry, or scanout content was changed. The next discriminating
evidence is the build-matched provider implementation or a live callback/value
capture on matching hardware; another guest boot without that capability
would not score this gate.

#### P232 static — the internal `Plug` event is the alternate ColorElements producer

The same firmware image has a second, unconditional call into
`constructColorElements` that narrows the internal-panel path. In the
`VideoInterfaceIOAV.cpp` event dispatcher at `0x42879dc`, the `w1 == 0`
branch is labelled `VideoInterface::Plug` (`0x4287bd4` references that
string). When the event carries a non-null `x2`, this branch records the
plug state and submits callback target `0x428b584` through the interface's
service channel (`0x4287bf8`–`0x4287c28`). That callback calls
`constructColorElements` at `0x428b590`/`0x428865c`. Separately, the
`VideoInterface::Plug Sync callback` method at `0x42884c8` checks provider
slots `+0x850` and `+0x1f0`, then calls the same builder at `0x4288550`.
These are distinct call paths from `UnifiedPipeline::start_ios_gated`'s
`+0x130` output-state gate in P231.

This makes a type-0 `VideoInterfaceIOAV` plug event, followed by the callback
at `0x428b584` (or the sync callback), the next concrete firmware-side
producer to distinguish for the missing primary `ColorElements` property.
It does not establish which AP/firmware component emits that event, whether
the event reaches this method in the emulated boot, or whether it is the sole
cause of the null registry property. The separate DCP headless-message
handler at `0x431f700` calls provider `+0x130` and, on its non-null parameter
branch, schedules `0x429f790`; static control flow does not connect that
handler to the `VideoInterfaceIOAV.cpp` type-0 plug dispatcher, so the two
must not be conflated. The existing QEMU model does not execute the DCP
RTKit task, and the guest probes through P230 did not observe the internal
video-interface path. The next useful capture is the actual type-0 event
producer/consumer and callback on a runtime that executes this firmware (or
a build-matched provider implementation); repeating the same guest boot
cannot observe these firmware PCs. No property, DCP response, mode, model,
device tree, SpringBoard, or scanout value was changed. D572/D582 remain
hypotheses.

#### P233 static — AP vtable slot is not a DCP provider identity

The boot-matched AP kernelcache (`firmware/bootkc`, SHA-256
`dc0f5b6a6fa848053c301949c8376c216c6223c047203b93e408a93d3440f906`) was
cross-checked with `ipsw kernel cpp --methods --entry
com.apple.driver.AppleMobileDispH17P-DCP`. It identifies `AppleCLCD2` as a
`UnifiedPipeline2` subclass (vtable `0xfffffff00808c778`); its `+0x130` slot
resolves to `UnifiedPipeline2::fn_0x130` at `0xfffffff00b1ecdec`. This is an
AP-kernel method, not a resolution of P231's DCP-side provider vtable call at
`0x429c118`. The two addresses belong to different images, and the equal slot
offset does not prove object identity, matching method semantics, or the DCP
return value. Do not use the AP implementation to synthesize the DCP gate.

The extracted DCP firmware Mach-O was preserved outside the repository at
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN233_DCP_PLUG_TRACE_STATIC/t8140dcp.macho`
(SHA-256 `8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`);
the accompanying `static-crosscheck.txt` records the command and limits. The
provider-class/value question and the type-0 Plug producer remain open. No
guest boot, DCP response, device-tree property, model, or pixel data changed;
D572/D582 remain hypotheses.


#### P234 static — start_ios_gated sets the Plug callback's readiness gate

In the same durable T8140 DCP Mach-O (SHA-256
`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`),
`UnifiedPipeline::start_ios_gated` begins at `0x429b718` and stores `1` to
global byte `0x4984ec4` at `0x429b750`. In the type-0 `VideoInterface::Plug`
event path, callback `0x428b584` (which calls `constructColorElements`, P232)
is submitted at `0x4287c18`–`0x4287c28`; a separate readiness callback
`0x428b568` is conditionally submitted at `0x4287c54`–`0x4287c64`. It writes
`1 & ~byte` to its out parameter and returns zero. A nonzero result enters the
“AP not ready yet, mark hot-plug pending” path at `0x4287c7c`–`0x4287c9c`.
This targeted disassembly scan found one direct store to the byte, but does
not rule out indirect or aliased writes and does not prove the global's
source-level name.

The result narrows the next firmware-side question to startup ordering and
callback outcomes: does `start_ios_gated` set the flag before the internal Plug
event, what does the readiness callback return, and does the submitted builder
callback run? The observed instruction order alone does not establish that the
builder is gated by the readiness result.
It does not explain a runtime guest by itself: the current QEMU DCP model does
not execute these RTKit firmware PCs, so another normal guest run cannot
observe this path. No QEMU response, model, device-tree property, SpringBoard
state, or scanout data was changed; D572/D582 remain hypotheses. The DCP image
and search record are preserved under
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN234_AP_READY_FLAG_STATIC/`.

#### P235 source cross-check — interface-16 PUBLISH is emulator-generated

Read-only inspection of `qemu-sptm/hw/arm/darwin_dcp.c` confirms that
`dcp_afk_started()` builds the service PUBLISH frame in `dcp_announce()` and
queues it through `darwin_afk_send_qe()`. Thus the observed interface-16
`dcpmipi-controller-epic` PUBLISH proves the AP-side announce/matching input
was supplied by this QEMU model; it is not evidence that the emulated DCP ran
its `VideoInterfaceIOAV` firmware path. The same source's `dcp_afk_recv()`
handles AP-originated EPIC frames and optionally returns an explicitly
labelled synthetic probe response. Firmware-originated D-series IOMFB
callbacks remain a separate opt-in script in `darwin_iomfb.c`; the model does
not derive them from the recovered DCP firmware.

Combined with P230–P234, this leaves no evidence-grounded D-series payload
that can be sent to create the type-0 `VideoInterface::Plug` event or the
primary `ColorElements` property. The PUBLISH is a host-created service
announcement, interface-12 startup only proves proxy binding/open, and the
firmware callback body/value is still unknown. Therefore no callback,
property, mode, or pixel response was added. The next valid discriminator is
a captured real callback/payload or a runtime that executes this DCP image;
another ordinary boot or a guessed D-series script would not classify the
missing display producer. No guest, model, fixture, SpringBoard, CLASS16,
A408, or scanout behavior changed. D572/D582 remain hypotheses.

#### P236 static — AFKArray metadata children are IDs, not embedded elements

Follow-up disassembly of the same durable T8140 DCP Mach-O
(`t8140dcp.macho`, SHA-256
`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`)
narrows what the `0x41c8ed4` AFK metadata serializer does with an array. The
array materializer at `0x41be504` allocates a `0x28`-byte object and copies
the input object references into its backing vector; it does not flatten the
referenced objects at construction. In the serializer, the `children` path
walks that vector, loads each child object's 32-bit field at `+0x48`, boxes
that scalar through `0x41c4600`, and inserts the boxed value into a new array.
The surrounding metadata path separately reads the object's `+0x48` and
`+0x4c` fields for the serialized `id` and `state` entries, and reads `+0x8`
for `retain`. The constant keys `class`, `id`, `state`, `retain`, `children`,
`clients`, and `config` are present at `0x43c9a7a` onward.

This is a concrete metadata-object shape: `children` contains child IDs, not
the per-element ColorElement dictionaries. It therefore closes the question
of whether the AFK metadata serializer itself recursively embeds those
property dictionaries, but it does **not** recover the final DCP-wire
`ColorElements` body, the runtime class string, or the outer wire encoder.
The AP `consumer-element-array` remains a separate OSSerialize parser test.
No callback, property, mode, QEMU model, fixture, device-tree value, or pixel
data was changed. D572/D582 remain hypotheses; the remaining payload gap
requires the property object's runtime structure plus the serializer/transfer
caller that emits the actual DCP body, or a real firmware callback capture.

#### P237 static — `constructColorElements` has a second provider gate and distinct property sources

In the same durable T8140 DCP Mach-O (SHA-256
`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`),
`constructColorElements` at `0x4288a60` reads its provider from `this+0x5cd0`
and calls provider vtable slot `+0x70` at `0x4288aa0`. If bit 0 of that return
is clear, control exits through `0x4288c70` before the property arrays are
requested. This is an additional builder-side gate beyond the
`start_ios_gated` `+0x130` return described in P231; its source-level meaning
and runtime value are not identified.

When that test passes, the builder calls provider slots `+0x80`, `+0x88`, and
`+0x78` at `0x4288ad4`, `0x4288afc`, and `0x4288b24`. Their returned references
are materialized through `0x41be504` and stored at builder offsets `+0x5c58`,
`+0x5c70`, and `+0x5c78`. The later publication calls map those fields to
`TimingElements`, `PreferredTimingElements`, and `ColorElements`, respectively
(`0x42893d8`, `0x42893f0`, and `0x42893c0`). Slots `+0x140` and `+0x150`
(`0x4288b48`, `0x4288b74`) instead feed `0x41bfb60` and populate `+0x5c80`
and `+0x5c88`, later published as `DisplayAttributes` and `Transport`
(`0x4289408`, `0x4289420`). This identifies the firmware-side producer
interfaces and object-flow boundary; it does not reveal their concrete
implementations, returned arrays, element dictionaries, or serialized wire
payload.

The type-0 Plug callback path reaches this builder through `0x428865c`:
`0x428b590` calls that wrapper, and `0x428894c` calls `constructColorElements`
(`0x4288a60`). The next discriminating real-firmware capture is therefore
provider identity plus the `+0x70` return and `+0x78` returned array on that
same invocation, followed by the property transfer. The QEMU DCP model does
not execute these firmware instructions, so this static result cannot be
validated by an ordinary guest boot. No model, callback, property, mode,
device-tree value, SpringBoard, or pixel data changed; D572/D582 remain
hypotheses.

#### P238 — measured shell-to-SpringBoard path reaches a Home entity; display stays black

P238's replay2b child completed DataMigrator/SystemApp sync, `DM_PERFORM`,
BKS migration/check-in, and shell-service return. SpringBoard created and
returned non-null Home entities and a workspace execute transition returned
success. The ADFL call returned its non-null object about 187 seconds after
entry. The legacy input helper ACKed the ordinary Home event and every
normalized Recap swipe point and release. The Cover Sheet pan passed
`shouldBegin` and ran
began/changed/ended, but there was no subsequent lock-manager transition with
`for_user_gesture=1`; the relay therefore did not send the gated follow-up Home
event. This is a SpringBoard transition-coverage miss, not a display-content
classification.

The read-only post-home callbacks logged three SpringBoard CA commit entries
and two returns, but no Server render-update, display-render, OGL, surface, or
swap event. The Update/V170 observer was armed but correctly emitted no rows:
its configured post-transition gate never opened. The HMP samples remain
uniformly black (fraction 0.0; hash
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`) through
the Home-entity return and a CA commit. P227 on the same lineage had already
recorded an accepted gesture, so P238 isolates a run-to-run transition
difference while confirming the logical shell/Home construction path. The
primary display diagnosis remains the measured null `ColorElements` / mode
`-1` and ignored update path from P227; P238 adds no new display-mode or DCP
provider evidence. No guessed firmware property, mode, input transition, or
pixels were injected. D572/D582 remain hypotheses; the P238 durable recovery
bundle and child are identified in `docs/re/iomfb-dseries.md`.

For the next replay2b run, the legacy host trigger now waits for the measured
`SB_ADFL_ENTRY_RETURN` as well as `SB_HOME_ENTITY_READY` before submitting the
bottom-edge swipe. P238 recorded that return about 187 seconds after entry;
the revised ordering is host-only and remains unvalidated until another boot.

#### P239 — lock transition accepted; post-transition compositor path advances, captured pixels stay black

P239 validates the host-side ADFL ordering change: the DVMINPUT1 Home event
was ACKed, `SB_HOME_ENTITY_READY` appeared, and the matching ADFL call returned
its non-null object about 227 seconds after entry before the swipe was sent.
The swipe's accepted Cover Sheet pan began, changed, and ended; the lock
manager then recorded `for_user_gesture=1` at `0x233a59560`. The gated second
Home event was ACKed after that transition. This is stronger than P238's
unaccepted gesture and proves the guest reached the logical lock-dismissal
transition. It does not claim that icons or wallpaper became visible.

Post-transition evidence: 158 SpringBoard CA-commit entries; one BackBoard
server render-update, display-render, and OGL-render entry; successful
IOSurface allocation and surface creation return markers; and 15 framebuffer
swap set-layer/handoff pairs. But every set-layer call had null `x3`, every
handoff had null `x2`, and the first qword read from the set-layer `x1` header
was a pointer value different from the allocation candidate `0x756eabab80`.
The header value's type and relationship to the handoff object were not
established. So this run does not show the newly allocated IOSurface reaching
swap. V170 returned `x0=1`.
However the sampled display mode at `+0x2b8` remained `-1`, and all 1179x2556 HMP screenshots, including
the final 300-second post-transition capture, were uniformly black with
`nonblack_fraction=0` and the repeated hash
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. These
are observed pipeline callbacks and allocation/swap-site executions, not
proof of non-black draw data or physical scanout. The next cut is to decode
the swap-layer and IOSurface identities and compare them with the render
result and persistent mode sentinel. Do not inject guessed `ColorElements`,
mode data, DCP properties, or pixels. The offline DCP provider analysis
remains static; the QEMU DCP model does not execute the firmware builder, and
D572/D582 remain hypotheses.

The durable child is
`~/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin239-adfl-home1.qcow2`,
SHA-256 `4d5e3c7f7f7c2a82957cf581bcf19f0e728541cc819e8ec8b0d1c4eddd49457d`;
`qemu-img check -U` passed. See the P239 recovery bundle at
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/`
`UI_CLASS16_REFRESH60_DM_PLUGIN239_ADFL_HOME1/`.

#### P240 instrumentation — accepted-transition surface/swap identity (boot-validated by P240C)

The read-only observer now uses the accepted `for_user_gesture=1` marker as a
separate gate for IOSurface memory reads. It records at most two allocation
return candidates and their metadata/pixel captures, compares those pointers
against set-layer `x3`, and records the handoff `x0+0xb38` dispatch-slot value
and its numeric relation to the recent same-thread set-layer header. These
pointer comparisons do not establish object type or ownership. P240C
boot-validated the gate and callbacks, but the metadata decoder did not recover
valid geometry and the set-layer candidate did not match the allocated pointer.
See P242 for the bounded live result. The probe makes no guest/model/DCP
changes and does not force A408 or fabricate display data.

#### P241 static — provider arrays are cached behind a paired-presence flag

The same saved T8140 DCP image used for P237 shows a second guard inside
`constructColorElements`. After the provider's `+0x70` method passes bit 0,
the builder tests `this+0x5ca6` bit 0 (`0x4288ab4`–`0x4288abc`) and returns
without querying array getters if already set. Otherwise provider `+0x80` and
`+0x78` results are materialized at builder `+0x5c58` and `+0x5c78`.
Immediately before the common return (`0x428952c`–`0x4289540`), the firmware
sets `this+0x5ca6` bit 0 only when both stored pointers are non-null. This
establishes a cached TimingElements/ColorElements pair guard in that control
flow, not the returned contents or the reason either getter might fail.

A matching-hardware trace should add the pre-call flag byte and both getter
returns/stored fields to P237's provider/`+0x70` capture, then follow the
property transfer. The binary SHA-256 is
`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`; its
symbols are stripped, no runtime provider implementation/value is recovered,
and the QEMU model does not execute the firmware instructions. This is offline
static analysis only; no guest/model/property/mode/device-tree/pixel changes.

#### P242 — shell, lock dismissal, and Home are measured; visible presentation remains open

P240C used a fresh child of the durable replay2b parent with the accepted trust cache and native input. SystemApp sync returned, then `DM_PERFORM_RETURN`, BKS migration/check-in returns, and SpringBoard shell-service return were recorded on the same boot. SpringBoard later returned a non-null Home entity and ADFL call; the bottom-edge Cover Sheet pan was accepted by `shouldBegin`, began and ended, and produced `for_user_gesture=1`. A follow-up Home event was acknowledged. These markers close the logical shell-to-lock-dismissal/Home path, not the visible-pixel path.

After the accepted gesture, the read-only observer recorded one active-display Update whose V170 return was `w0=0` (not ignored), then Server `render_update`, `render_display`, IOSurface creation/allocation, and OGL entry. In this sample the active mode field remained `-1`, but it is correlation only: the Update passed the sampled ignore gate and reached downstream render calls. The metadata decoder did not recover valid IOSurface geometry, so allocation success is not pixel proof. Fifteen set-layer/handoff pairs followed; set-layer `x3` and handoff `x2` were null in all 15. In all 15 same-thread pairs, handoff `x0` equaled the first qword of the immediately preceding set-layer `x1` descriptor, and the sampled `+0xb38` slot was consistently `0x246d3eeec`. This joins the handoff to the descriptor-referenced object, without assigning that object's dynamic type or the slot's behavior. That object differed from the allocated candidate. HMP stayed uniformly black for the full 300-second post-transition observation.

P103 on this same D5825-class profile previously captured a non-null IOSurface in `x3` at the same set-layer entry and decoded sparse lock-screen chrome from that surface. P240C therefore records a true null `x3` at this boundary after lock dismissal, rather than a missing pixel-decoder result; it does not name the reason the Home-layer submissions lack a surface.

The next useful cut is to identify the set-layer `x1` object and the actual IOSurface argument/return path, then join a valid pixel-bearing surface to the transition's Update and handoff. Do not infer that the `-1` mode caused the null swap surface, or change DCP responses, A407, mode data, or pixels without a measured producer/consumer join. Durable child, frames, and complete logs are in `~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN240C_SURFACE_JOIN1/`; child SHA-256 is `17c2c652b22391ca2b5786226be6ad2ea14c4169f4bceb3c23ce41fe4d448a1b` and `qemu-img check -U` passed. No QEMU model, firmware, device-tree, fixture, mode, or pixel changes were made; D572/D582 remain hypotheses.

#### P243 — accepted gesture arrived beyond the trigger's former 60-second wait

P243 revalidated the same-boot `DM_PERFORM_RETURN` → BKS migration/check-in
return → SpringBoard shell-service return ladder. Home key input was
acknowledged; the Home entity and non-null ADFL return were observed; and the
Recap swipe was acknowledged. SpringBoard subsequently accepted the lock
dismissal with `for_user_gesture=1` and matched pan-began/pan-ended evidence.
The accepted marker was written roughly five seconds after the host's
hard-coded 60-second wait expired, so the observer stopped before capturing
the post-dismissal OGL/pixel window. This is a trigger-timing miss, not a
negative UI transition.

The legacy trigger now supports `--accepted-transition-timeout` (default 180
seconds), exposed by `LEGACY_ACCEPTED_TRANSITION_TIMEOUT`; P244 is the fresh
replay2b validation. P243 child SHA-256 is
`e196eae65e54fcb27a6879f6ae0df3245120320d94cbb5dfe782627b128fb5cf`, and its
recovery bundle is `recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN243_OGL_LAYER_JOIN1/`.
No guest, DCP/model, device-tree, mode, or pixel changes were made.

#### P244 — gesture correlation window was too narrow

P244 reached SpringBoard Home and returned from ADFL after shell service
return. The acknowledged swipe produced a lock-manager event with
`for_user_gesture=1`; the recorded accepted `shouldBegin`, dismiss-began, and
pan-ended events share the same recognizer and process/thread identity. The
transition arrived 17.04 seconds after pan-end, just outside the correlator's
15-second end-to-transition bound, so the trigger declined to continue into
the post-transition render sample. The correlator now allows 30 seconds but
retains the same recognizer, accepted-begin, PID/TPIDR, and event-order checks.
P245 is the fresh-child validation. This run supplies no pixel conclusion.
P244's child and logs are preserved in the durable recovery records; no
guest/DCP/model/device-tree/mode/pixel values changed.

#### P245 — live shell/Home path advances through an accepted gesture; pixels still do not reach the captured framebuffer

P245 closes the P244 correlator-timing gap. On one replay2b child, the migration/check-in/shell-service ladder returned, SpringBoard returned a non-null ADFL object and constructed Home entities, and the lock manager recorded a user-caused transition after accepted `shouldBegin`, matching dismiss-began, and matching pan-ended callbacks. This validates the gesture gate and Home-side control path, not visible Home output.

The final HMP sample was taken 300 seconds after the accepted transition. All captured frames remained uniformly black (1179x2556; `nonblack_fraction=0`; SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). A transition-window Update returned through the sampled V170 gate and reached Server render-update/OGL; IOSurface create/allocation calls returned, but no identity join connects that allocation to the swap descriptor. Across 15 same-thread framebuffer set-layer/handoff pairs, set-layer `x3` and handoff `x2` were null, while handoff `x0` matched the preceding set-layer `x1` descriptor's first qword in every pair. The mode read was `-1` in the sampled state, but P245 does not establish it as causal. The next measured edge remains the descriptor-referenced object's dynamic type and its actual pixel-bearing surface/contents producer.

P245's child is `~/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-plugin245-ogl-layer-join3.qcow2`, SHA-256 `c03be03711819b6167dc8cf4dcf9db207ee3c7fb965931be521fae479ffae638`; `qemu-img check -U` passed. Logs and frames are in `recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN245_OGL_LAYER_JOIN3/`. The run validates the widened gesture-correlation interval and accepted-transition wait. No firmware, QEMU model, device-tree, display-mode, or pixel values were altered; D572/D582 remain hypotheses.

#### P246 static — narrow the ColorElements provider factory inputs

In the saved T8140 DCP image (`t8140dcp.macho`, SHA-256
`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`),
the `UnifiedPipeline.cpp` routine beginning at `0x42861f8` calls provider
factory `0x42a4890` twice. Before the first call (`0x4286f70`–`0x4286f80`),
it reads the 32-bit member at `this+0x6730`, reduces it to a nonzero boolean,
and passes `(boolean, 0)`; the returned pointer is stored at `this+0x5cd0`
(`0x4286f84`–`0x4286f88`), the provider consumed by
`constructColorElements` at `0x4288a60` (P237). The second call at
`0x4287080`–`0x4287090` passes the same boolean and selector `1`, then stores
its result at `this+0x5cd8`. Other code tests the `+0x6730` member specifically
against `2` (for example `0x427737c`–`0x4277398`), so firmware treats it as a
multi-valued state; its source-level name and runtime value remain unknown.

The factory first calls helper `0x4022ac8`, tests bit 0 of its out word, and,
when set, queries the key `iomfb_vistub` at `0x42a48c0`–`0x42a48e4`. The key
query's out word is initialized to zero. Its result is combined with the
caller's boolean by `csel` at `0x42a48dc`–`0x42a48e4`: a zero status selects
the caller boolean; a nonzero status selects the out word. A nonzero selected
value creates the `0x58`-byte object (vptr literal at `0x4a29438`). Otherwise
selector `1` creates a `0x98`-byte object (vptr literal `0x4a296b8`), and
selector `0` creates a `0x1e8`-byte object (vptr literal `0x4a29090`). This
establishes the factory's allocation branches and inputs, not the classes'
names or their methods: the stripped image's corresponding `__DATA_CONST`
vtable area is zero-filled, and there is no runtime instance to inspect
offline. The `iomfb_vistub` string alone does not establish which branch a
physical internal panel takes.

For a matching-device capture, record both inputs at `0x42a4890`, the global
and config lookup outcomes, returned object, and its later identity as
`this+0x5cd0` at the builder. The QEMU model does not execute this DCP firmware
code, so a normal VM boot cannot validate the factory branch. No firmware
response, model, property, mode, device-tree, or pixel data changed; D572/D582
remain hypotheses.

#### P249 static — additional consumers of the `+0x6730` state

The same saved image has a direct 32-bit getter at `0x428f190`–`0x428f19c`
(`return *(uint32_t *)(this+0x6730)`). Two additional accessors load a nested
pointer first: `0x4263cb4`–`0x4263cc4` reads from `*(this+0x80)+0x6730`, and
`0x42669f0`–`0x4266a00` reads from `*(this+0x90)+0x6730`. These are separate
base expressions; without a runtime object-identity join they must not be
collapsed into one physical-device state.

The consumer around `0x4277378`–`0x4277434` reads the `+0x90`-nested value,
compares it with `2`, records the equality in a byte at `x20+0x3b9`, and uses
the branch when selecting a `bics_mode` configuration value and storing a
result at `x19+0x1d68`. Other routines at `0x42e4ad4`, `0x42e5d00`,
`0x42e964c`, and `0x4300338` also test that nested word against `2`; their
branches affect different downstream fields. This confirms that the
multi-valued state participates in more than provider allocation, but does
not name the state or prove those objects alias the `UnifiedPipeline` whose
factory input P246 observed.

The constructor beginning at `0x429085c` contains a guarded writer path for
the same `this+0x6730` word; `0x4290a00` is an interior call site in that
constructor, not a function entry. After an earlier constructor check at
`0x4218468` succeeds,
this path calls `0x41e2618` and proceeds only when bit 0 of its return is set
(`0x4290a00`–`0x4290a08`); the other edge branches to `0x4290bcc`. On the
normal path through `0x41e2618`, that test is not a display-data-availability
gate: the helper dispatches the newly allocated object's vtable slot `+0x20`,
stores that virtual method's result in the helper object at `+0xc`, then
returns the constant `1` at `0x41e26b0`–`0x41e26c0`. Thus the constructor's
bit test is satisfied whenever the helper returns normally; it does not test
the virtual method's result or the `iomfb_system_type` query. On the
proceeding path it forms `x24=this+0x6730` at `0x4290a10`–`0x4290a18`,
initializes `*(uint32_t *)x24` to zero at `0x4290b6c`, then calls the property
helper at `0x42abf88` with key `iomfb_system_type`, an out pointer to a
four-byte stack word, and size `4` (`0x4290b40`–`0x4290b70`). If the helper
returns nonzero, the code copies that out word to `*x24` at `0x4290b9c`. A
zero return does not settle the field: a fallback consults a
global flag and either performs another four-byte query or calls a virtual
method. Either path can write `2` to `+0x6730` when its result is `1`; otherwise
the initialized zero remains. Nearby logging prints `IOMFB: SystemTypeBootArg - Detecting system
type %d`. This shows an `iomfb_system_type`-derived initializer and supports
the field's system-type role, but does not identify its enum values. The
shared offset strongly motivates joining this initializer's object to P246's
factory receiver; the dynamic object identity is still absent, so this remains
a static same-layout inference rather than runtime proof.

The property helper itself also has a visible prerequisite. At `0x42abf88`,
the code first runs `0x40363a0`, then asks `0x4022ac8` for a small state word;
if that word's bit 0 is clear it returns zero without calling the keyed
helper `0x4017ff0`. If bit 0 is set, it calls `0x4017ff0(key, out, size)`.
That callee walks and parses a global argument-text buffer before matching
the requested key. This makes the `iomfb_system_type` path a boot-argument
lookup behind a separate availability gate, consistent with the nearby
`SystemTypeBootArg` log. The state word's meaning and the actual argument
value on hardware remain unknown; this is not evidence that setting an
argument would create the missing `ColorElements` object.

A focused decode of that fallback makes the on-device observation more
specific. When the byte flag at `0x4984ec4` is set, the four-byte query call
at `0x4290c28`–`0x4290c40` receives selector `0x44313233`, null/zero secondary
arguments, an output word at `sp+0xc`, and length `4`; its output byte is
compared with `1` at `0x4290c44`–`0x4290c4c`. When the flag is clear, the code
loads the object at `0x49850a8`, follows `+0xa8` then `+0x58`, and invokes its
vtable slot `+0x128` at `0x4290c54`–`0x4290c7c`; the returned word is compared
with `1` at `0x4290c80`–`0x4290c84`. Either successful branch stores `2` to
the `+0x6730` field at `0x4290c88`–`0x4290c8c`. The selector's semantics and
the virtual slot's implementation remain unknown. This sharpens the
capture request to include the flag, selector/output, or virtual receiver and
return, alongside the field and factory input; it does not supply a value or
justify synthesizing one in QEMU.

The `0x42861f8` path separately forms `this+0x6730` in `x24` and later sets
byte `this+0x674a` to `1`; its nearby zeroing at `0x4286f44` covers
`this+0x6734` for `0x14` bytes, not the leading word. A device-side capture
should record the initializer's property result, the word at `+0x6730`, the
factory boolean/selector, and the returned object on the same receiver. Do not
infer the live value from `bics_mode`, `iomfb_vistub`, or the QEMU mode
sentinel. No firmware, model, device-tree, mode, or pixel data changed;
D572/D582 remain hypotheses.

#### P248 — Home state was observed, but the swipe was incomplete

P248 used a fresh child of the durable P134 gesture-state image. The
migration, check-in, and shell-service returns were recorded on one boot;
SpringBoard then returned non-null ADFL and Home-entity objects. After the
original LLDB session lost its QEMU connection, a reattached observer recorded
another non-null Home entity and one lock-transition callback with `x3=0`.
That callback is not accepted user-unlock evidence: no matching accepted
`shouldBegin`/pan sequence or `forUserGesture=1` marker was recorded.

The input helper's final status had `btn_down=true`, 16 dispatched events,
and 2 timeouts; its contact remained at `(abs_x=16370, abs_y=23282)`. The
orchestrator initially selected the legacy trigger, which was stopped before
sending legacy input; a DVMI2 trigger was then started manually on the same
guest. That DVMI2 attempt lost its debugger connection before dispatching the
full swipe and ended with incomplete input/debugger coverage, not an unlock
verdict. The sampled update gate still read mode `-1`, and HMP frames remained
black; neither observation identifies the cause. P249 adds a `finally`-path
best-effort touch release to the DVMI2 runner and retries from P134 with DVMI2
selected at startup and the high-frequency update-gate trace disabled. P248's
child and logs remain in
`recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN248_HANDOFF_SYMBOL2/`; its
child passed `qemu-img check -U`. No guest, firmware, model, device-tree,
display-mode, or pixel data changed.

#### P249 — shell and Home control path reached; swipe was not accepted

P249 used a fresh child of durable P134 with the merged DVMI2 input trust
cache, the low-trace post-shell observer, and an explicit shell-return gate.
DataMigrator, BKS migration/check-in, and SpringBoard shell-service returns
were observed. SpringBoard then returned non-null ADFL and Home-entity
objects; Home/workspace transition execution also returned success. This
confirms post-shell UI construction and control-path progress, not a visible
or unlocked Home screen.

The initial `(589,2540)->(589,500)` swipe was fully acknowledged and
dispatched (33 motion samples; zero coalescing or dispatch failures), and the
helper released contact. SpringBoard logged pan touches began and moved, but
no matching pan `shouldBegin`, Cover Sheet dismiss begin/change/end, or
accepted lock transition followed. Its lock-manager callbacks had
`forUserGesture=0`. A manually delivered second bottom-edge swipe was also
fully acknowledged; later lock-manager transitions likewise had
`forUserGesture=0`. The observer's configured 600-second window ended before
its 700-second automatic follow-up point, so no claim is made about that
scheduled follow-up. The final HMP frame and all 1179x2556 frame-sampler
captures were uniformly black (`nonblack_fraction=0`, SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).
Backboard nevertheless reached render-update, IOSurface create/allocation,
OGL, set-layer, and handoff callbacks; those entries do not prove that a
pixel-bearing Home surface reached scanout.

The child is preserved at
`~/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-plugin249-home-gesture-clean1.qcow2`
(SHA-256 `58683bdcdb8400d5c7e88815e42f9b95b588618e2b7d8f6627c992f2c277f354`),
backed by the durable P134 gesture-state image; `qemu-img check -U` passed.
The full event, LLDB, frame, and input records are in
`recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN249_HOME_GESTURE_CLEAN1/`.
The run made no firmware, model, device-tree, display-mode, or pixel changes;
D572/D582 remain hypotheses. The next gesture validation should use the
replay2b lineage on which P245 accepted the dismissal gesture, and ensure the
follow-up delay lies inside the observer window. The DVMI2 trigger now rejects
a nonzero follow-up delay that is not shorter than its observation window, so
the follow-up cannot be silently suppressed by that configuration again.

#### P251 static — the `+0x6730` fallback query is D123

The selector used by the P249 `+0x6730` initializer fallback is the already
known D123 FourCC (`0x44313233`). In the constructor beginning at `0x429085c`,
the path gated by global byte `0x4984ec4` calls `0x41d1a2c` at
`0x4290c28`–`0x4290c40` with no input, a four-byte output at `sp+0xc`, and
length four. The caller compares the first output byte with one and writes
`2` to `this+0x6730` only on that result (`0x4290c44`–`0x4290c8c`). This
connects the fallback to the D123 request family whose other call paths are
documented above; neither the byte's semantic meaning nor a relationship to
the missing primary `ColorElements` payload is established.

The separate caller at `0x42861f8` makes a no-input/no-output D123 request at
`0x4287044`–`0x428706c`, under the same global-byte gate. The shared helper
`0x41d1a2c` packages the selector and optional buffers into a request
descriptor before passing it through `0x41cec3c` to an indirect service
method. Because the image is stripped and the provider's dynamic class and
live reply are unavailable, the next useful hardware observation is a
same-receiver trace of both calls, the four-byte result, the `+0x6730` field,
and the factory's boolean/selector/object. This is a static capture recipe,
not grounds to synthesize D123 data, mode state, or `ColorElements` in QEMU;
D572/D582 remain hypotheses.

The earlier endpoint sweep measured D123 as a four-byte-output callback with
status zero in the QEMU/harness path. If the constructor's global-flag branch
is active and that same first output byte is zero, its static control flow
leaves `+0x6730` zero; this is a conditional consequence, not proof that the
hardware returns zero or that the field controls `ColorElements` publication.
The useful physical-device capture is the request/reply bytes together with
the flag, post-query field, factory boolean/selector, and returned provider
identity on the same receiver. No QEMU response was changed.


#### P250 — SpringBoard accepts lock dismissal; captured output stays black

P250 ran from a fresh durable replay2b child, `ui-class16-refresh60-dm-plugin250-replay2b-legacy-gesture1.qcow2` (SHA-256 `2ce4eec056c9aaa778056effa930c35bce762ed6da7f18c0d4468ada18eb6fba`); `qemu-img check -U` passed. The first shell-service return at guest time `1791776517.197` armed post-shell observation. SpringBoard returned non-null ADFL at `1791776658.0806`, created Home entities, and returned success from the workspace transition. The lock dismissal was positively accepted: `shouldBegin=true`, the same SpringBoard TPIDR and recognizer were seen across began/ended, and the transition recorded `for_user_gesture=1` and `gesture_sequence_confirmed=true` at `1791776819.5708`. Home-button down/up were ACKed. This proves a SpringBoard control-plane transition into Home state, not visible pixels.

The first recap-swipe relay timed out on its final touch-up. A later same-coordinate fallback release was ACKed. The older `LEGACY_INPUT_RESULT` reported `swipe_acked=true` incorrectly; the durable source now distinguishes `swipe_relay_completed` from `fallback_release_acknowledged`. The accepted-transition markers, rather than that legacy field, are the gesture evidence.

An HMP/frame sample covering 600 seconds after the accepted transition produced 18 identical 1179x2556 frames, all black (`nonblack_fraction=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). A sampled display mode of `-1` remains an observation, not a causal diagnosis. BackBoard/render activity continued; IOSurface create/allocation calls returned; 15 set-layer/handoff pairs had matching descriptor evidence (handoff `x0` equaled the first qword at the preceding descriptor address in set-layer `x1`; set-layer `x3=0`, handoff `x2=0`). This proves continuity of a descriptor through those sites only; it does not prove visible content or identity with the separately allocated IOSurface. The `+0xb38` dispatch candidate did not resolve to a loaded user-space module.

`DM_PERFORM_RETURN` recorded `x0=1` at `1791777036.814989`; a separate BKS migration/check-in return recorded `x0=0` at `1791777350.532`, followed milliseconds later by shell-service return. Their timing/thread evidence does not establish a single causal ladder. Durable child path: `~/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-plugin250-replay2b-legacy-gesture1.qcow2`; complete evidence is under `~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN250_REPLAY2B_LEGACY_GESTURE1/`. No firmware, model, device-tree, display-mode, or pixel values were changed; D572/D582 remain hypotheses.


#### P252 static — D123 output contract and next post-home identity probe

The AP bootkc callback table maps D123 to `0xfffffff00919b4f4` with four output bytes. Its handler saves `x4` as the output pointer and rejects `w5 < 4` with `0xe00002c2` before writing. On the success path it takes an index from callback context `x0+0x38`, resolves a 48-byte manager-table entry rooted at `0xfffffff00808fe48`, and passes the selected entry minus `0x90` to helper `0xfffffff009186e50`; the helper overwrites `x0` before preserving or reading that argument, so the selected entry does not affect its result. The handler stores the helper's low byte through `x4` and returns zero. P253 below recovers the helper's actual test.

The DCP call at `0x4290c28` passes null input/zero input length but an output pointer at `sp+0xc` and output size four; the shared helper `0x41d1a2c` carries these output arguments into its request descriptor. The AP check is on output capacity, so this call meets the handler's four-byte requirement. This rules out the tempting interpretation that P249's D123 call fails because its input length is zero. P253 identifies the AP result as a root DeviceTree target-type suffix predicate; for the saved tree's `D47` value, output zero is consistent. It does not establish a relationship between `+0x6730` and primary `ColorElements`. No reply, mode, or firmware value was synthesized.

#### P253 static — D123 tests the root DeviceTree target-type suffix

Re-disassembly corrected the AP helper call: D123 invokes
`0xfffffff009186e50`, not `0xfffffff00918b2a8` (a different routine). The
handler checks that the indexed slot in the 48-byte table rooted at
`0xfffffff00808fe48` is non-null, but passes slot-minus-`0x90` to a helper
that overwrites its incoming `x0` before preserving or reading it. The helper
resolves `IODeviceTree:/`, requests the `target-type` property, gets the
property's byte sequence and length, and tests the final three characters
against `sim` and `imb`. The D123 byte is therefore a global target-type
predicate, not a value derived from the indexed display-manager entry.

The saved, build-matched `system.dtree` decodes the root `target-type` as
`D47`, so the suffix predicate is false. The recorded QEMU/harness D123
status-zero/output-zero result is consistent with the AP code and this tree;
the DCP constructor's conditional store of `2` to `this+0x6730` is not taken
for this value. This branch is not evidence of a wrong D123 response or the
missing primary `ColorElements` property. P253 changed no response, firmware,
model, device-tree property, mode, or pixel data.

The command transcript, source hashes, symbol lookups, DCP call site, and
decoded root properties are preserved in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN253_D123_TARGETTYPE_STATIC/`.

#### P254 static — D123-derived factory input and `iomfb_vistub` override

The caller at `0x4286f70`–`0x4286f88` converts `this+0x6730` to a boolean,
passes it as `w0` with selector `w1=0` to `0x42a4890`, and stores the returned
object at `this+0x5cd0`. The caller boolean is zero only if the constructor
used the D123 branch and the alternate fallback did not later set the field;
the virtual fallback's result is unknown. The factory checks an internal
state bit before querying the
`iomfb_vistub` property. If that lookup succeeds, its value supersedes the
caller boolean; if it fails, the caller boolean is retained. A selected true
value chooses a `0x58`-byte object path (setting byte `+0x50`); otherwise
selector zero chooses a `0x1e8`-byte allocation. A nonzero selector chooses a
separate `0x98`-byte allocation.

This narrows the factory's static branch inputs but does not identify the
returned object's dynamic class, link it to the live primary-display
provider, or show that `iomfb_vistub` or D123 controls `ColorElements` or
visible pixels. Disassembly and hashes are preserved in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN254_DCP_FACTORY_BRANCH_STATIC/`.
No property, response, firmware, model, device-tree, mode, or pixel values
were changed.

For the measured Home path, the probe now has an opt-in `DVM_POSTHOME_SURFACE_LINEAGE_TRACE=1` mode. On a bounded number of `fb_swap_set_layer` hits it compares the allocation-return object and its internal/base candidates against descriptor qwords and one level of immediate heap-pointer children, and reports runtime class-pointer resolution candidates. It does not replace the decisive `x3` non-null IOSurface witness. A pointer match only links objects; it does not prove pixel-bearing content or scanout. The next replay2b child should use this diagnostic alongside the accepted-gesture marker and the same 600-second frame sample, so a null `x3` result can be separated from an allocation-to-descriptor identity path without treating mode `-1` as cause.

#### P255 static — provider results are copied into DCP property containers

Disassembly of `constructColorElements` at `0x4288a60` and the called helper
`0x41be504` shows each non-null provider result used for builder fields
`+0x5c58`, `+0x5c70`, and `+0x5c78` is copied as a pointer range into a
container; null results skip that copy. The helper does not parse or
synthesize display entries in the observed path. The
other provider results, assigned to `+0x5c80` and `+0x5c88`, pass through
`0x41bfb60`, which copies 16-byte pointer pairs. Both helpers call
`0x41c4888` on copied members; it dispatches vtable slot `+0x40` with a `+1`
reference adjustment. Adjacent `0x41c48c4` dispatches that slot with `-1`.
The stripped binary does not identify the dynamic operation or class, so this
is not labeled as a specific retain/release API.

The evidence supports only a static producer boundary: the constructor
wraps provider-returned ranges/pairs in containers. It does not establish a
live provider, nonempty values, a serialized payload, or a display mode.
Exact disassembly and hashes are in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN255_DCP_PROVIDER_ARRAY_MATERIALIZATION_STATIC/`.
No runtime property, firmware, model, device-tree, mode, or pixel value was
changed; D572/D582 remain hypotheses.

P280 adds an AP-side post-dismissal boundary to the P281 static DCP capture
plan. On a gesture-confirmed lock-manager transition, SpringBoard continued
CA commits and emitted 5,113 pre-syscall `CA_RENDER_MACH_MSG_SEND_CALL`
observations, while BackBoard recorded no later server-render-update entry
and its 12 later swap-layer calls all had null `x3`. No post-marker IOSurface
was supplied to sample. The HMP output remained black. These observations
identify no DCP Plug callback value, `ColorElements` payload, display mode,
or DCP response, so the real-device provider trace in P281 remains necessary
for firmware-specific claims. The next guest-side cut is to compare full
`0x9c42` message bodies at the SpringBoard send wrapper and render-server
receive; the pre-syscall event and task-local port fields alone do not prove
delivery. P280's durable child and full records are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN280_ACCEPTED_MARKER_SURFACE_SAMPLE1/`.

#### P257 static — DCP serializes each property object into the chunk buffer

The transfer helper at `0x4289694` closes the static handoff from P255's
provider containers to the D126-D128 wire calls. It receives a property
object in `x2` and key in `x1`, creates a `0x1000`-capacity output object via
`0x41c6750`, and calls the property object's vtable `+0x30` with that output
object. The return bit controls the helper's success/error branch. On success,
the output object's vtable `+0x48` supplies the serialized byte count. The
helper sends D126 with a four-byte total-size value of count plus one, obtains
source bytes via output-object vtable `+0x50`, then emits D127 records with at
most `0x1000` data bytes and the offset/length in the trailing eight bytes of
each `0x1008`-byte input. It finishes with D128 carrying the 64-byte key.
The call block still publishes the five properties in the P237 order.

For the `ColorElements` array created by `0x41be504`, P236 identifies that
object's `+0x30` vtable target as the AFK metadata serializer at `0x41c8ed4`.
The serializer writes metadata and child IDs into the provided output; it
does not recursively embed ColorElement dictionaries. P257 therefore links
the serialized top-level property object to the chunked DCP callback framing,
but does not reveal actual serialized bytes, a live provider/result, the
dynamic class string, or proof that the producer ran in the observed QEMU
boot. Full disassembly and hashes are saved in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN257_DCP_PROPERTY_SERIALIZE_TRANSFER_STATIC/`.
No firmware, model, device-tree, callback response, mode, or pixel value was
changed; D572/D582 remain hypotheses.

#### P259 static — restored bytes do not yet identify the factory provider method

**Correction:** P260 below supersedes P259's BUND-to-Mach-O offset mapping and
the resulting candidate-byte interpretation. Keep P259 as the original
analysis record, but use P260's Type-4 layout and offsets for conclusions.

P259 restored the larger-task `__DATA_CONST` bytes from the durable DCP BUND:
bundle range `0xf31000..0xf86000` maps to Mach-O file range
`0x952000..0x9a7000`. The reproducible image and byte report are under
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN259_DCP_PROVIDER_VTABLE_STATIC/`.

The factory's selector-1 branch allocates a `0x98`-byte object, forms address
`0x4a296b8`, signs it with `PACDA`, and stores it as the object's first
qword. In the restored raw bundle that qword is zero. The bytes at
`0x4a296b8+0x130` are nonzero, but are not a validated method pointer: the
following 32-bit words include IEEE-754 values `700`, `920`, `1130`, `1415`,
`1730`, and `2175`, while the first word also has a float interpretation of
`35092304`. No schema or function-pointer decoding was established. The
prior artifact label `selector1_slot_0x130` was therefore corrected to
`selector1_vptr_plus_0x130_raw_unresolved`.

This selector-1 object is also not proven to be the receiver of the distinct
`UnifiedPipeline` provider `+0x130` call: that path loads its provider from
the global `AppleDCPDisplayService`. Raw zero vptr bytes do not establish a
runtime null implementation because task loading may transform the packed
data. This pass identifies no dynamic provider, `ColorElements`, timing
payload, display mode, or pixel result, and changes no firmware or QEMU
response. D572/D582 remain hypotheses.

#### P260 static — Type-4 BUND mapping corrects P259's `rdat` offsets

P260 checked the BUND type-4 layout against the `ipsw fw dcp` extraction and
the extracted P233 Mach-O. The BUND ranges are `rtxt=[0x32000,0x5b8000)` and
`rdat=[0x5c4000,0xf94000)`. The Mach-O places `rtxt` at its first loadable
segment's file offset and starts `rdat` at the second segment's file offset;
reconstructing those ranges reproduces the P233 Mach-O SHA-256 exactly:
`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`.
The implementation reference used for this layout is the pinned
[ipsw `pkg/bundle/runtime.go` source](https://github.com/blacktop/ipsw/blob/37e6badcbba4b82c4b66cd0376bd23f88839ad08/pkg/bundle/runtime.go).

Under the verified mapping, Mach-O file offset `0x952000` (`__DATA_CONST`)
comes from BUND offset `0xa71000`. The selector-1 vptr at file `0x98a6b8`
maps to BUND `0xaa96b8`, and the raw qword is zero. The previously discussed
`vptr+0x130` address at file `0x98a7e8` maps to BUND `0xaa97e8`, whose raw
bytes are also zero. `__mod_init_func` at file `0x9a6350` maps to BUND
`0xac5350`, also zero. P259 instead copied BUND offset `0xf31000` to file
`0x952000`; that is not the Type-4 `rdat` mapping. Its apparent float sequence
near `0xf697e8` therefore was not evidence about the selector-1 vtable slot.

The corrected `__DATA_CONST` file range (`0x952000..0x9a7000`, `0x55000`
bytes) is entirely zero in this extracted image; its SHA-256 is
`179a0bea61861bbf7b5c52d8d60d662991c65c3d424aa03564c43f568ac06b58`.
`ipsw macho info --fixups` reports no fixups, and the raw `__mod_init_func`
section (`0x90` bytes) is zero. This further limits static attribution but
still does not rule out runtime loader or DCP initialization that populates
those structures.

This corrects the static reconstruction only. The zero raw slots do not
establish runtime null pointers because loader or initialization work may
transform the image, and no live class, provider method, `ColorElements`
payload, mode, or pixel result is identified. The validator,
reassembly report, hashes, and superseded-offset note are preserved in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN260_DCP_RDATA_MAPPING_CORRECTION_STATIC/`;
the durable record includes the validator and the full-segment/fixup checks;
the reusable validator is `tools/re/dcp_validate_runtime_data_mapping.py`.
No firmware, device-tree, model, QEMU response, mode, or pixel value was
changed; D572/D582 remain hypotheses.

#### P265 static — Plug Sync callback's pre-builder provider branch

The saved Type-4 T8140 DCP Mach-O from P233 (SHA-256
`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`)
was disassembled around `VideoInterface::Plug Sync callback` (`0x42884c8`),
its `ColorElements` wrapper (`0x428865c`), and the adjacent Plug callback
targets. At `0x42884c8`, the callback preserves `this` in `x19` and its
second argument in `x20`. It calls the provider object at `this+0x5ee0`,
vtable slot `+0x850` (`0x42884e0..0x4288508`). A zero return proceeds directly
to the next provider call. A nonzero return tests the 32-bit field
`this+0x42bc`; if that field is zero, the callback logs “System is powering
down, dropping this hotplug” and returns `0x80000007` before the next call or
builder. If the field is nonzero, it continues. Thus the observed early exit
is the conjunction “slot `+0x850` nonzero and field `+0x42bc` zero,” not a
simple zero-return rejection. The log text associates this branch with
powering-down handling, but does not prove the field's source-level name.

The continuing path calls slot `+0x1f0` on the object at `this+0x5cd0`, with
the saved callback argument in `x1` (`0x4288520..0x4288548`), then calls the
wrapper at `0x428865c`. This function also calls `constructColorElements`
(`0x4288a60`) at `0x428894c`. The builder-side slot `+0x70` gate and property
getters remain as documented in P237/P241; this pass adds the callback-side
branch and ABI context ahead of those known gates. It does not assign names
or semantics to the `+0x850` / `+0x1f0` methods or `+0x42bc` field, and it
does not identify their runtime values or prove that this callback executes.

The disassembly and input hash are preserved in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN265_DCP_PLUG_SYNC_CALLBACK_STATIC/`.
This is offline static analysis only: no real-hardware callback capture,
runtime provider class, `ColorElements` payload, mode, or pixel result was
obtained. No firmware, device-tree, model, QEMU response, or display data was
changed; D572/D582 remain hypotheses.

#### P266 static — Plug Sync's `+0x5ee0` receiver is wired during init

In stripped function `func_429085c`, the firmware allocates a 0x30-byte object
at `0x4290a78`, stores a vptr address point (`0x4a28f28`) at `0x4290ab8`, and
stores that object in the enclosing object’s `+0x6110` at `0x4290adc`. The
callee-saved `x20` continues to hold that allocation through `0x4290f04`, where
the same pointer is also stored in enclosing-object `+0x5ee0`. Thus these two
fields alias the same constructed object in this init routine. Later in the
function, calls through `+0x5ee0` use vtable slots `+0x28`
(`0x4290f74..0x4290f9c`) and `+0x20` (`0x4290fec..0x4291010`; call at
`0x429100c`, clear at `0x4291010`). This structurally joins the Plug Sync
callback's `+0x850` receiver (`0x42884e0..0x4288508`, P265) to an object
allocated and installed by this init routine. The firmware image does not name
its class or virtual methods.

The saved Mach-O has zero bytes at the static vtable address point and reports
no fixups, so this pass cannot resolve the `+0x850` implementation from the
file. That is not evidence of a null runtime vtable; loader-time state was not
captured and the firmware task does not execute in the current QEMU model. The
disassembly and input hash are preserved in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN266_DCP_PLUG_MEMBER_WIRING_STATIC/`.
Next real-device capture should log the runtime `this+0x5ee0` pointer/vptr,
slot `+0x850` target and return, `this+0x42bc`, and subsequent `+0x1f0` call,
then join the builder provider/`+0x70`/`+0x78` values and property transfer.
No provider result, payload, mode, DCP behavior, or pixel value was changed;
D572/D582 remain hypotheses.

#### P272 static — `nold` inventory yields a secondary-image candidate

The Type-4 inventory parser found three header-only Mach-Os in `nold` at
bundle offsets `0xf9c000`, `0xf9c608`, and `0xf9d3c0`. Their `__TEXT`
`vmsize`/`filesize` pairs are `0x10000`, `0x4a4000`, and `0xe0000`;
the largest-`__TEXT` image at `0xf9c608` remains the ipsw-selected main image.
The reconstructed main image still matches the P233 extracted SHA-256
`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`.

After its verified payload, `rtxt` has `0xe2000` bytes, enough for the
`0xe0000` `__TEXT` file range of the next-largest header with `0x2000` left.
After the main image's `0x50e000` data span, `rdat` has `0x4c2000` bytes;
the next-largest image declares `0x436000` of non-text file span, leaving
`0x8c000`. A conditional reconstruction placed that image's `__TEXT` at
BUND `0x4d6000` and its non-text data beginning at `0xad2000`; under this
candidate, its `__DATA_CONST` would source BUND `0xef3000` and is all zero
(`0x11000` bytes; SHA-256
`b5822048397cfb7e72443fbe3f37fb07bbd0b397c11177cfd71cf768d44e924a`).

This is capacity arithmetic plus a conditional byte layout, **not** a
validated secondary-image mapping: the pinned ipsw source documents choosing
and rehosting the largest-`__TEXT` image, but does not establish the secondary
image's offsets or order. Therefore the candidate zero `__DATA_CONST` does
not establish that a loaded task has null vtables, and it does not identify a
provider, ColorElements payload, display mode, or pixel cause. The parser,
nine tests, main-image hash check, candidate reconstruction, report, and
commands are preserved in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN272_DCP_NOLD_SECONDARY_CANDIDATE_STATIC/`.
No DCP response, device-tree value, QEMU model behavior, mode, or scanout was
changed; D572/D582 remain hypotheses.

P272 follow-up checked the candidate's non-segment file references before
treating it as an image. The embedded header's `LC_SYMTAB` declares a 609,440
byte symbol table at file offset `0x517000` and a 741,008 byte string table at
`0x5abca0`, ending at `0x660b30`; the stitched segment payload ends at
`0x517000`. `otool -l` can display the header, but `nm` rejects this file as
truncated/malformed because those tables are absent. The candidate is therefore
only a segment-payload layout, not a complete Mach-O. Appending the tables after
those segments would require `0x149b30` bytes, exceeding the hypothesized
remaining `rdat` capacity (`0x8c000`) by `0xbdb30`; this rules out that simple
contiguous `rdat` extension, but does not locate the tables or establish the
actual secondary-image mapping. The validator now reports `LC_SYMTAB` ranges
and marks the candidate incomplete. Updated report, candidate bytes, source,
and eleven tests are in the same P272 recovery record.

The three header-only Mach-Os also provide stable identities without requiring
payload placement: `nold+0x0` UUID `E4E71A49-BDF9-3882-AA76-2A254FD366A7`,
main image `nold+0x608` UUID `7A8B83FB-1EC8-3F13-A7C4-D9CD5BC71718`, and
next-largest header `nold+0x13c0` UUID `62F319FD-46B6-3219-B2EF-F8BADDFA72E0`.
These identify the header records only; the last UUID does not validate the
candidate payload mapping. The validator and recovery record now preserve all
three IDs.

#### P267 static — callback receiver and property provider are separate member roles

The callback disassembly loads `this+0x5ee0` for its `+0x850` virtual call,
then on the continuing path loads `this+0x5cd0` for the `+0x1f0` call with
the saved callback argument. Its wrapper reaches `constructColorElements`,
whose builder independently reloads `this+0x5cd0` and uses provider slots
`+0x70`, `+0x80`, `+0x88`, and `+0x78`. Thus the static member roles differ:
the `+0x5ee0` callback receiver is not established as the property provider;
pointer equality between the fields is also not established. The firmware
still does not expose these virtual implementations in the saved data, and
no runtime result or payload follows from this wiring.

The P267 record and self-contained disassembly excerpts are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN267_DCP_PLUG_MEMBER_ROLES_STATIC/`.
The next useful real-device capture should record both member pointers/vptrs
independently, followed by `+0x850`, `+0x42bc`, `+0x1f0`, builder `+0x70`,
the `+0x78` array, and property transfer. No firmware, property, mode,
device-tree, QEMU response, or scanout content was changed; D572/D582 remain
hypotheses.

#### P268 static — Plug helper's dual-pointer prefix and registry view

The same initializer that installs the Plug Sync `+0x5ee0` object writes two
PACDA-signed pointer words into its 0x30-byte allocation: address point
`0x4a28f28` at helper `+0x0`, and address point `0x4a28f60` at helper `+0x8`
(the latter signed with modifier `helper+8`). It stores the enclosing
`+0x18` value and `w21` at helper `+0x10/+0x14`. If the `+0x18` value is at
most 15, it also places `helper+8` in a global table at a 0x30-byte indexed
stride. P266's `+0x5ee0` callback path consumes the first pointer word; the
second pointer and table view expose another interface candidate but do not
name it. The raw vtables are zero in this saved image with no fixups, so the
second target and runtime method semantics remain unresolved.

The P268 disassembly and notes are preserved in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN268_DCP_PLUG_HELPER_DUAL_POINTER_LAYOUT_STATIC/`.
This adds a runtime capture field (`helper+8` and its table entry) to the
hardware checklist; it does not supply a ColorElements value or justify a
firmware response change. D572/D582 remain hypotheses.

#### P270 static — reconstructed DCP segments distinguish vtable bytes from BSS

The Type-4 reconstruction checker now accepts virtual-address samples and reports the containing Mach-O segment plus whether each byte is file-backed or zero-fill. Rebuilding the pinned 24A5430a `dcpfw` `rtxt`/`rdat` ranges reproduced the P233 extracted Mach-O hash exactly (`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`). The P268 address points `0x4a28f28` and `0x4a28f60` map to file-backed `__DATA_CONST` offsets `0x989f28` and `0x989f60`; 32 bytes at each are zero in this reconstructed static image. In contrast, table base `0x4984ba0` lies in the `__DATA` zero-fill tail and has no file offset, so its runtime entry contents cannot be inferred from the saved file.

This sharpens the P268 capture target: inspect the loaded table and helper words at runtime, while treating the static vtable bytes as unavailable implementation data—not proof of null runtime dispatch. No provider method, ColorElements value, mode, firmware response, device tree, model behavior, or pixels were changed. The checker, five unit tests, sample report, and reproduction command are preserved in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN270_DCP_VM_SAMPLE_SEGMENTS_STATIC/`.

P275 reran the validator against the pinned 24A5430a bundle and P233 reference Mach-O. The reconstructed Mach-O hash still matches P233 exactly (`8175dc0e…45a08d8`). Address points `0x4a28f28` and `0x4a28f60` remain file-backed zero samples in `__DATA_CONST`; `0x4984ba0` remains in the `__DATA` zero-fill tail. This repeat validates the static inputs and mapping, not loaded vtable contents or runtime dispatch. The exact bundle/reference hashes, JSON report, validator, and test are preserved in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/P275_DCP_STATIC_VERIFY/`.


#### P273/P274 offline follow-up — A408 register discrepancy

The P273 recovery record identifies its bootkc by SHA-256
`da1e254ab81e31adae87c049da295b582dabbd4ba46096fc58f3e5467fc6e02c`. At
static `0xfffffff00a0c4664`, this file decodes `braa x16,x17`; immediately
before it, the vtable word is loaded into `x8` at `+0x4634`, copied into
`x16`, and `x17` is derived from the slot address. P273's `x8` value
`0xfffffff02a0c8ff4` is therefore retained as a target candidate, not proof
of a completed branch or A408 operation.

P274 captured the same runtime PC with `x8=0xfffffff02a0c8ff4`,
`x16=0xbe54fff02808d110`, and `x17=0x998`. These live values conflict with
the P273 file's register flow. P274's launch profile names the durable bootkc
path, and a later hash check shows that file is also `da1e254a…`; the separate
repository `firmware/bootkc` is `dc0f5b6a…` and was not the path in P274's
profile. A different host bootkc file is therefore not the leading
explanation, but no P274 guest-memory instruction dump has reconciled the
register discrepancy. The P274 probe had armed a
canonicalized `x16` candidate at `0xfffffff02808d110`; its silence cannot be
scored as a branch miss. The source now preserves raw `x8/x16/x17/x2` and
arms canonicalized `x8` as a candidate, pending validation against a fresh
run with captured bootkc identity or guest code bytes. Even a hit proves
only execution at that address, not callback identity or A408 semantics.

At the P273 `x8` candidate, saved static bytes show a separate indirect call
and nearby indexed-loop setup. This is adjacent control/data flow only; no
class, method, or operation is assigned. P273's accepted gesture and sparse
IOSurface remain measured; HMP was black. No DCP response, firmware, device
tree, model, or pixel data changed. D572/D582 remain hypotheses. Details are
in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN273_REPLAY2B_LEGACY_UART_ONLY_ACK120_HOME_UNLOCK1/`.

P276's offline QuartzCore symbol resolution corrected a consumer-side
interpretation of the `Mode(-1)` fallback: the sampled addresses at
`0x1e7058c58`, `0x1e7058e10`, `0x1e7058600`, and `0x1e7053bf9` are
`CADeviceSupportsAPT` / `CADeviceIsVirtualized` cached state, not mode
globals. This constrains AP-side branch analysis but adds no DCP provider,
firmware-return, or panel-payload evidence. The detailed symbol mapping is in
`docs/re/iomfb-dseries.md` under P276.

P276 also resolved the QuartzCore initializer inputs behind those cached
consumer-side flags. The `CADeviceSupportsAPT` block at `0x1844b3b08` uses a
length-15 CFString at `0x1e90712c0` pointing to `SupportsPerseus`
(`0x1848203f1`), then checks the separate `CA_DISABLE_APT` literal at
`0x184820401`. The `CADeviceIsVirtualized` block at `0x18453bea0` uses a
length-15 CFString at `0x1e90715c0` pointing to `IsVirtualDevice`
(`0x1848206b0`). This identifies the static preference/configuration keys
feeding QuartzCore's policy bits; it is not a DCP reply, panel property, or
real ColorElements/TimingElements payload. No firmware or model response was
changed.

P277 adds a measured AP-side boundary to the DCP/scanout question. A
1179×2556 Lock Screen IOSurface is pixel-bearing and is passed in `x3` to
`fb_swap_set_layer`; the HMP framebuffer remains uniformly black. The same
pointer later appears in `x2` at static `0x184472488`, but offline disassembly
identifies that PC as a call to Objective-C selector `sender` with no colon,
not a proven surface-handoff API. That register match does not show surface
consumption by virtual scanout. P277 recorded no DCP callback value, display-mode answer,
ColorElements payload, or firmware result, so it neither validates nor
refutes D572/D582 and gives no basis for changing the model. The detailed
pointer join, pixel metrics, and preserved frame are in P277 of
`docs/re/iomfb-dseries.md`.

P279 independently verified the AP-side IOSurface metadata layout against
the exact saved shared-cache image: `_impl` is loaded through the runtime
ivar-offset global, and its documented geometry/format/base fields match the
read-only pixel probe. This improves the measurement path but adds no DCP
provider return, firmware callback payload, display mode, or scanout result.
P278's post-transition non-null swap surfaces were not pixel-sampled because
the second-swipe helper moved the acceptance marker after the live sampler
had loaded its older implementation. The source gate now recognizes the
retained first marker, but this fix still needs fresh-boot validation. P278
observed five non-null `x3` values among 32 `fb_swap_set_layer` entries; it
did not sample the IOSurface bytes. Its later ordinary swipe was ACKed but
produced no second accepted-transition marker in the bounded window. The
DCP-side builder and provider values remain hardware-dependent, and D572/D582
remain hypotheses.

P278 reached SpringBoard's Home-entity/workspace path and recorded a
gesture-confirmed lock-dismissal transition, followed by 32 swap-layer entries
(five with non-null surface pointers) and uniformly black HMP frames. Because
the sampler missed the moved marker, IOSurface contents remain unclassified.
This establishes AP-side compositor activity only; it does not establish
surface consumption by scanout or identify a DCP/provider failure. The
durable child SHA-256 is
`e6bc857412039eb5279d4db7339ba3f64e08b2e092310aa9091cd215f2941890`; the
complete record is under
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN278_TWO_SWIPE_SURFACE1/`.

#### P281 static — minimal real-device Plug/ColorElements capture fields

The existing P265/P237/P255/P257 disassembly gives a bounded real-firmware
capture plan. These addresses are static VAs in the saved T8140 DCP Mach-O
(SHA-256 `8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`);
they must be relocated against the loaded image on a device, not copied as
runtime breakpoints. The initiating type-0 Plug Sync callback is
`0x42884c8`. On that invocation, preserve `this` (`x19`), callback argument
(`x20`), `this+0x5ee0`, its runtime vptr/slot `+0x850` target and return, and
the 32-bit `this+0x42bc` branch field. The continuing path separately calls
slot `+0x1f0` on `this+0x5cd0` with `x1=x20`, then enters wrapper
`0x428865c` and builder `constructColorElements` at `0x4288a60`.

At the builder, record the identity/vptr of `this+0x5cd0` and the return from
slot `+0x70` (bit 0 gates the property requests). If it passes, capture the
runtime target, return object, count, and element identities for provider
slots `+0x80` (published as `TimingElements`), `+0x88`
(`PreferredTimingElements`), and `+0x78` (`ColorElements`); also record slots
`+0x140` and `+0x150` and their `DisplayAttributes`/`Transport` transfers.
Do not collapse `this+0x5ee0` and `this+0x5cd0`: static analysis assigns them
different roles and does not prove pointer equality. The builder's later
property-transfer order and each return should be timestamped on this same
callback, so an absent property can be distinguished from a builder gate.
The transfer call sites in static order are `ColorElements` `0x42893c0`,
`TimingElements` `0x42893d8`, `PreferredTimingElements` `0x42893f0`,
`DisplayAttributes` `0x4289408`, and `Transport` `0x4289420`. At each one,
record null versus non-null input, the object's class when readable, and the
transfer result. That makes a failed `+0x70` gate, a missing provider value,
and a downstream serializer/transfer failure distinct observations.
The corresponding static `x1` constants resolve to the exact NUL-terminated
property keys in `__TEXT,__const`: `0x43cd04b` `ColorElements`, `0x43cd03c`
`TimingElements`, `0x43cd148` `PreferredTimingElements`, `0x43cd160`
`DisplayAttributes`, and `0x43cd184` `Transport`. These key addresses are
firmware static VAs, not runtime AP addresses.

For the `ColorElements` object, record the dynamic class/vtable and the
serialized output around helper `0x4289694`: its property serializer is
vtable slot `+0x30`; P236 identifies the known AFK metadata serializer at
`0x41c8ed4` as emitting child IDs rather than recursively embedding element
dictionaries. Capture the serialized bytes and the referenced child objects
separately. The D126/D127/D128 transfer uses a four-byte total length of
serialized count plus one, data chunks no larger than `0x1000`, and a 64-byte
property key. These boundaries identify where to snapshot a live value, but
do not predict that value or the panel mode.

Pair that DCP event with the same device/build's AP-side DeviceTree and
IOService properties, display-mode query/return, and the IOSurface/swap result.
An `IODeviceTree.txt`/`IOService.txt` pair is useful for matching providers
and published properties, but cannot by itself supply callback registers,
provider returns, or serializer bytes. Those require a diagnostic environment
that can observe the loaded DCP/RTKit execution; an ordinary iPhone owner
should not be told that stock USB/Xcode access exposes these internal values.
This is a capture specification derived from static control/data flow, not a
claim that a retail phone can currently provide the trace. The full field
checklist is in [`dcp-plug-capture-plan.md`](dcp-plug-capture-plan.md). The
pinned Mach-O, bundle, and supporting disassembly are maintainer-side
artifacts under
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN281_DCP_PLUG_CAPTURE_PLAN_STATIC/`;
they are not included in this repository. That record preserves the
31-instruction transfer-site disassembly and five constant-string bytes, so
the call order and property-key names can be checked against the pinned image.
No firmware, model, callback response, device-tree value, mode, or pixel was
changed; D572/D582 remain hypotheses.


### P282 — AP surface evidence is not a DCP Plug response

P282 captured recognizable Lock Screen UI pixels in two non-null BackBoard IOSurfaces passed to `fb_swap_set_layer`: 1179×2556 at full RGB range, with time/status, charging battery, information control, and home indicator. The same boot's HMP framebuffer remained uniformly black through the 900-second post-transition observation. This establishes content on the AP/BackBoard side of the swap boundary, but gives no runtime DCP Plug callback registers, `ColorElements`/timing objects, serializer bytes, kernel submission result, or scanout proof. The P281 DCP Plug capture plan remains necessary for claims about real-device provider values and mode; the next QEMU trace instead follows the measured surface into H17P submission and mapping without assuming a DCP response.

### P283 — AP Lock Screen pixels do not reveal the DCP Plug response

P283 captured decoded Lock Screen content in BackBoard IOSurfaces that were
also passed as non-null `x3` at `FB_SWAP_SET_LAYER_ENTRY`. This strengthens
the AP-side pixel evidence beyond a detached allocation sample. HMP remained
black; the A408 records are unresolved indirect dispatches, with no proven
A407 mapping result or H17P `swap_submit`. P283 contains no runtime DCP Plug
callback arguments, provider objects, `ColorElements`/timing serialization,
or physical scanout evidence. It therefore does not validate or refute the
D572/D582 hypothesis. Full records and images are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN283_CA_9C42_FULLBODY_SWAP_SUBMIT2/`.

The run's follow-up swipe helper started after the QEMU monitor endpoint had
closed and sent no input. A new helper is now wired to start before setup and
wait for the accepted-gesture marker; the next run can measure whether a
second ordinary swipe produces a new lock transition without automating
passcode entry.

#### P285 static — the builder's `+0x70` gate has an HPD log branch

In the pinned Type-4 T8140 DCP Mach-O, `constructColorElements`
(`0x4288a60`) calls property-provider slot `+0x70` at `0x4288aa8`; the test
at `0x4288aac` skips all later property getters when result bit 0 is clear.
The early path sets up a diagnostic at `UnifiedPipeline.cpp:0x2779` with
message `HPD not present, skipping mode export` and tail-branches through
the shared logger at `0x40311bc`. The value `0x40000001` in `x0` is a logger
argument, not an observed API error returned to the caller. This pins down
the precise static signature a real DCP Plug capture should recognize, but
does not establish that the branch executes or explain QEMU's null
`ColorElements`: the QEMU RTKit model does not run this firmware path. The
upstream P231 `+0x130` gate is distinct and can bypass the builder entirely.
No DCP response or display value was changed. The concise disassembly,
referenced strings, input hash, and limits are preserved in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN285_BUILDER_GATE_LOG_STATIC/`.
D572/D582 remain hypotheses.

#### P286 static — two candidate provider words feed the active builder provider

Static entry `0x428fd5c` selects `this+0x5ce0` when `w1==0` and
`this+0x5cd8` when `w1!=0`, then writes that pointer to `this+0x5cd0`. On the
nonzero branch, byte `this+0x5641==1` is cleared; if bit 0 of
`this+0x674a` was clear, the code emits the internal log
`found stale fTimingsUpdatePowerState, pipe may not power on properly. Cleaning up`
at `UnifiedPipeline.cpp:0x3bb8` and sets the bit. These are observed static
stores and branch conditions, not names for the selector API or evidence of a
hardware hot-plug transition. Record both candidate pointers, `w1`, the
selected `+0x5cd0`, and both bytes alongside P285's `+0x70` result. The
restored image has no raw pointer reference joining either candidate to
P259's factory object. No runtime value, property, mode, firmware, or pixel
was recovered or changed; the QEMU DCP model does not execute this path. The
small disassembly and string record are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN286_PROVIDER_SELECTION_STATIC/`.
D572/D582 remain hypotheses.


#### P284 — accepted UI transition produced a white surface; HMP stayed black

P284 cleared the measured migration and shell-service ladder on one boot:
`DM_PERFORM_RETURN` returned `x0=0`, then the BKS migration/check-in returns
and `SHELL_SERVICE_RETURN` followed. SpringBoard later published a non-null
Home entity and entered ADFL. After an acknowledged Home-key relay and an
ordinary swipe, the gesture recognizer and CoverSheet accepted a lock-screen
transition (`x3=1`). This is UI-path evidence, not authentication; no
passcode was entered.

The first post-transition BackBoard IOSurface was nearly black. The next
sample, taken 3.43 seconds later while the follow-up swipe was in progress,
was a populated 1179×2556 surface with a white canvas, black Dynamic Island, time `5:06`, status indicators, and charging
battery, but no Lock Screen content or app icons. The HMP screendump at the
420-second endpoint remained uniformly black. The decoded IOSurface proves
that BackBoard held those pixels at its sampled boundary; it does not prove
that the surface was submitted, accepted by DCP, scanned out, or that Home UI
was visible. The follow-up swipe was ACKed and the second IOSurface turned white during
that swipe, but no second accepted gesture marker appeared. About 86 seconds
after the first accepted marker, LockAllUI/LockUI callbacks and a workspace
transition occurred (`x0=1` on transition return); the causal relationship is
unknown. The child, integrity result, event/log files,
and frames are preserved in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN284_TWO_SWIPE_PRESENT1/`; the decoded sample SHA-256 is
`c795b490aa609f396ef5cf2ccfc0244a5a59937025b94ff94c63c16429c802b0`. No
model, firmware, device-tree, mode, or fixture response changed. D572/D582
remain hypotheses.

#### P287 static — pair selector and colorimetry property extraction

The pinned Type-4 T8140 DCP Mach-O has a static helper at `0x42a796c` that
tests bit 0 of byte `[x0+0x1b9]`. If clear, it logs at
`UnifiedPipeline.cpp:0x5f2` and returns `0x80000104`. Otherwise it reads
candidate-list pointers from `[x1+0x18]` and `[x1+0x20]`, with fallback calls
through provider vtable slots `+0x78` and `+0x80`. It compares the first two
32-bit words at `x1+0` and `x1+4` against values returned by vtable `+0x48`
on each candidate's `+0x18` object, scanning the first list and then the
second. The no-pair path logs at line `0x628`, then
`WARNING: no color or timing modes from the link` at line `0x5fb`, and returns
`0x80000001`. On a match, the second-list candidate receives two `+0xd0`
calls (selector arguments 0 and 1); their 32-bit results are stored through
caller-provided pointers at `x1+8` and `x1+0x10`. The helper retains the
selected candidates, populates more state, and tail-branches to `0x42a5184`
with `x0` set to the owning object, `x1=0`, and `x2=1`. The terminal status
is not established by this slice.

Adjacent helper `0x42a7c88` uses its third argument to select property keys:
zero selects `Chromaticity` and `DefaultWhitePoint`; nonzero selects
`DolbyVisionChromaticity` and `DolbyVisionWhitePoint`. It reads Red, Green,
and Blue child X/Y values into six float slots at the caller's x2 buffer, and
white-point X/Y into x1 offsets 0 and 4. Getter integers are converted with
a 16-bit fractional shift before being narrowed to floats. The literal keys
support a colorimetry interpretation, but units, ranges, mode association,
runtime values, and API semantics remain unknown.

This gives a real-device capture concrete fields to sample: readiness byte,
list pointers, comparison keys/returns, selected candidates, selector outputs,
selected normal-versus-Dolby-Vision property keys, and the X/Y output buffers.
This remains static guidance only; the QEMU DCP model does not execute this
firmware path. None of the branches or values was observed in the VM. Static
disassembly through the matched path, strings, buffer map, and source-image
hash are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN287_LINK_MODE_FALLBACK_STATIC/`.
No display input was changed; D572/D582 remain hypotheses.

#### P288 static — DCPDisplayInterface fallback is measurable but not yet a ColorElements join

The pinned Type-4 T8140 DCP binary has a helper at `0x41df3a4` whose log
arguments identify `DCPDisplayInterface.cpp:0x51a`. It gets a display
interface through the input object's vtable `+0x198`, then queries that
interface's `+0x78` and `+0x80` link-mode lists. If either is null, the helper
logs `no color or timing modes from the link. Faking, since we have non-null
VI.` and returns through a one-entry fallback: two output counts become 1,
two 24-byte records are allocated, interface methods `+0x138` and `+0x130`
populate words in the second record, and the first includes `0x003c0000` at
offset `+0x0c` plus `0x20` at `+0x10`. The exact record meanings are not
established. `0x003c0000` equals 60 in 16.16 representation, but that is only
a numeric observation until a schema or runtime value confirms units.

This names an alternate lower-level fallback to P287's `UnifiedPipeline`
pair selector. The similar “no color or timing modes from the link” wording
does not prove the two helpers share records or that this output is the
primary display's `ColorElements`/`TimingElements`. A real-device capture
should record the `+0x198` result, list results, `+0x130`/`+0x138` return
values, output counts/records, and status, then follow object identity into
the DCP builder and publication. Both `0x41df3a4` and P287's `0x42a796c`
call vtable `+0x78`/`+0x80` with matching PAC-diversifier immediates
`0x8c10`/`0x37f7`; that is a structural slot match, not proof of the same
receiver or returned lists. The pass recovered no direct call/data reference
connecting `0x41df3a4` to the publication. The QEMU model does not run this
firmware path; no response or display value was changed. Disassembly,
source strings, and hashes are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN288_DCP_INTERFACE_MODE_FALLBACK_STATIC/`.
D572/D582 remain hypotheses.

#### P290 static follow-up — the pair selector's zero-input modeset depends on prior state

The same pinned DCP disassembly resolves part of P287's previously open tail
branch. At `0x42a7c84`, a matched candidate pair tail-branches to
`0x42a5184` with `x1=0` and `x2=1`. The `x1==0` branch in `0x42a5184` logs
`plug gated: modeset received.` at `UnifiedPipeline.cpp:0x38f` and jumps to
`0x42a5264`; it does not execute the nearby code that stores `x22` into
`[self+0x188]`. The function then loads the preexisting `[self+0x188]`, sets
its default status to `0x8000000a`, and branches to the return when that
pointer is null. Thus the null-state outcome is statically `0x8000000a`. If
the pointer is non-null, it calls `0x42a6634`, which checks cached byte
`[self+0x1b8]`; if that bit is clear, it calls the prior object's vtable
`+0xf8`, stores the returned bit in `[self+0x1b8]`, and returns that bit. A
false result logs `IOAVVideoInterface open failed` at
`UnifiedPipeline.cpp:0x127`; the caller cleans up and still returns its
default `0x8000000a`. A true result continues into the existing modeset/state
path, but this slice does not establish its final status or visible display
result. The zero `x1` from P287 does not by itself prove that the selected
pair is rejected or published: the runtime value of `[self+0x188]` and the
result/cached state of this open gate are the missing discriminators.

A separate caller at `0x42a9d18` passes `x8` in `x1` after its own gates,
then calls `0x42a6634` and returns zero. That caller does not establish the
prior state or return behavior of P287's tail-call path. This is static
control-flow only; no runtime value, device-tree property, color/timing
payload, or scanout behavior is inferred. The excerpt and input/disassembly
hashes are preserved in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN290_MODESET_NULL_INPUT_STATIC/`.
An additional constructor/cleanup excerpt shows the initialization path in
`0x42a6d68` setting `[self+0x188]` to null and `[self+0x1b8]` to zero after
the non-null-input and helper-success checks. Cleanup at `0x42a4ca8` releases
the `+0x188` value when non-null and then clears the field. Together with the
replace/retain path at `0x42a5240..0x42a5260`, this supports a managed pointer
field at `+0x188` and a cached state byte at `+0x1b8`; the pointee's concrete
class and method meaning remain unknown. The argument loaded from global
`0x4985090` at `0x42a666c` is in the Mach-O `__DATA,__bss` range
`[0x4929000, 0x49907e8)`. Static zero-fill alone does not establish the
runtime argument value. A cross-reference found a writer at `0x431e590` in
the AppleDCPDisplayService.cpp routine beginning `0x431e550`: it stores that
routine's entry `x0` at the global near the `DCPI2C, Publish` log and a
subsequent `0x4280950` call. This supports an object-pointer flow into later `x1`
arguments, but persistence and runtime identity remain unverified. For a
matching-device capture, record the global after initialization, the pointed-
to object's class/type if available, the vtable slot `+0xf8`, the call result
and cached byte, and the caller's branch/status through `0x42a527c` and
cleanup/return. The lifecycle excerpt is
`modeset-object-lifecycle-and-global.txt` in the same recovery directory
(SHA-256 `b6e8801e0513736db67e3e30a74448f386245dff8bbe24407bab7a1afdf4d690`).
On matching hardware, the high-value sample is: capture `self` and
`[self+0x188]`/`[self+0x1b8]` at `0x42a6634`; identify the pointed-to
interface by runtime metadata if possible; record its vtable `+0xf8`, runtime
`x1` from `0x4985090`, `x2`/`x3`, the returned `w0` at `0x42a6690`, the
cached byte afterward, and the caller's branch/status through `0x42a527c`
and cleanup. Preserve raw values; do not invent a property or substitute a
guessed interface object. D572/D582 remain hypotheses.

The same BSS address is also loaded at a distinct close-labeled callsite:
caller `0x42a62a0` tests `[self+0x1bd]`, reaches interface vtable `+0x100` at
`0x42a65e8` with `x0=[self+0x188]`, `x1=[0x4985090]`, and `x2=0`, then logs
`ioav i/f is now closed!` and clears cached state. Treat this separately
from open-labeled helper `0x42a6634` / vtable `+0xf8` at `0x42a668c`; using
the same global address does not make the operations equivalent or reveal a
payload type. The expanded dual-callsite sample plan is in
`modeset-object-lifecycle-and-global.txt` and
`matching-device-runtime-capture.txt` under the durable PLUGIN290 static
record directory.

P290 dual-global follow-up: a targeted cross-reference in the same
`0x431e550` AppleDCPDisplayService startup routine shows entry `x0` stored at
both `0x4985090` (`0x431e590`) and `0x49850a8` (`0x431e658`). Prior static
analysis identifies `0x49850a8` as the AppleDCPDisplayService global, so this
path initializes both slots from the same pointer. That strengthens the
static pointer-flow hypothesis for `x1` at the VideoInterfaceIOAV open- and
close-labeled calls. It does not establish runtime equality, later overwrites,
dynamic type, virtual-method semantics, display properties, or scanout. The
updated lifecycle excerpt SHA-256 is
`b6e8801e0513736db67e3e30a74448f386245dff8bbe24407bab7a1afdf4d690`; the
matching-device checklist now asks to compare both globals at startup and both
callsite `x1` values.

P290 offline follow-up — DCPDisplayInterface fallback output shape: the
`0x41df3a4` helper queries the receiver at vtable `+0x198`, then queries that
result at `+0x78` and `+0x80`. If either list query returns null, the helper
logs `DCPDisplayInterface.cpp:0x51a`, “no color or timing modes from the
link. Faking, since we have non-null VI,” and emits two one-entry output
arrays before returning status zero. The output descriptor in `x2` supplies
count-pointer slots at `+8` and `+0x18` and array-pointer slots at `+0` and
`+0x10`. Each count is set to one and each array receives one 24-byte record.
The first record has four 32-bit ones and `0x20` at `+0x10`; the second
stores the `+0x130` and `+0x138` call results at `+4/+8` and `0x003c0000` at
`+0x0c`. Those are observed record bytes, not decoded field meanings or
units. When both list results exist, a separate list/count path performs
descriptor-driven comparisons. This fallback wording and shape do not prove
that the arrays are the primary panel's `ColorElements`/`TimingElements` or
that they reach the `UnifiedPipeline` builder. Full disassembly, exact output
offsets, and runtime-sampling plan are in
`fallback-helper-output-shape.txt` in the durable
`UI_CLASS16_REFRESH60_DM_PLUGIN290_MODESET_NULL_INPUT_STATIC` record. Its
SHA-256 is
`29376240f4693620b5cfccbf65f9e2613201eb2109482b2860b504c43136a858`.

P291 final measured shell-to-lock result: after the shell/check-in and
SpringBoard setup markers, two separate cover-sheet pan gestures were
accepted by SpringBoard. Three readable 1179x2556 BackBoard IOSurface captures
at `FB_SWAP_SET_LAYER_ENTRY` contain recognizable Lock Screen clock/status;
one also contains the swipe-up instruction. These captures all precede the
second accepted gesture. About 169 seconds after that second gesture,
backboardd made three more `FB_SWAP_SET_LAYER_ENTRY` calls, but each had x3=0
at the observed IOSurface argument; x2 was 0/1/2 and the same numeric x1 layer
pointer was reported. The adjacent sender calls also carried no x3 surface.
This burst provides no pixel sample and is not a blank-frame result. During
the remaining 548 seconds, CA commit and Render Mach messages continued, but
there were no more swap, OGL/server/display render, or IOSurface samples.
The observation ended at its 900-second post-first-transition bound. HMP
screendumps remained black. No passcode was entered, authenticated unlock was
not observed, and no Home-screen image was established. Durable final
chronology and pixel manifest are in
`recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN291_RETURN_DESCRIPTOR1/iosurface-capture-final.txt`
and `iosurface-lockscreen/manifest.json`.

#### P291 host screendump source trace — HMP samples the QEMU console surface

The `screendump` command does not read the captured BackBoard IOSurface
pointer. `qmp_screendump()` in `qemu-sptm/ui/ui-qmp-cmds.c` looks up the
requested console (or console index 0 by default), waits for that console's
update, and serializes its current `DisplaySurface`. For this machine, the
`darwin-fb` console is initially backed directly by the XNU boot-framebuffer
RAM described by `boot_args.Video` (`qemu-sptm/hw/arm/darwin_fb.c`). Its
surface is replaced with copied DCP pixels only if
`darwin_fb_present_bgra()` is called. The only call found is in
`qemu-sptm/hw/arm/darwin_iomfb.c`, after `iomfb_scanout()` parses a swap/A407
surface descriptor, resolves the `dart-disp0` DVA, and copies the pixels.

Therefore P291's black HMP images and nonblack BackBoard IOSurface samples
are measurements at different boundaries. HMP black establishes that the
QEMU console surface contained black pixels at capture time; it does not
establish that SpringBoard's captured IOSurface was black or that a real
device scanout path discarded it. The source trace explains why those
observations can coexist, but does not establish whether P291 reached the
IOMFB presenter or why it did not. Follow-up against P291's still-live QEMU
process narrows this: its environment had `DARWIN_DCP_IOMFB_A408_SCANOUT=0`
and `DARWIN_DCP_IOMFB_A407_SCANOUT=1`, while the model reads the distinct
`DARWIN_DCP_IOMFB_SCANOUT` name to enable the A408 copy path. The A408-named
RPCs in the log therefore do not imply pixel copy. A407 scanout was configured
as enabled, but P291 stderr through guest time `1791813213.887` had no A407
candidate, `iomfb: presented`, scanout failure, or DART scanout line. The
`A408_SCANOUT` spelling was not read by QEMU; the runner now explicitly sets
the actual `DARWIN_DCP_IOMFB_SCANOUT=0` switch for future runs. This accounts
for why the QEMU console remained on its black boot framebuffer in this run;
it does not explain a real device's physical panel output. Keep
`FB_SWAP_SET_LAYER_ENTRY` surface bytes, IOMFB/DART presenter logs, and HMP
console dumps as separate stages. No VNC, mode, firmware, or display response
was changed. Source locations are
`ui/ui-qmp-cmds.c:qmp_screendump`, `hw/arm/darwin_fb.c:darwin_fb_present_bgra`
and `darwin_fb_init`, and `hw/arm/darwin_iomfb.c:iomfb_scanout`.

P291 final artifact integrity: `iosurface-capture-final.txt` is SHA-256
`04b07056f6e8675515b066d7e5e11f17c1f7908ca00b48b5494465e62c58db13`; the
full HMP-versus-IOSurface source trace, including the post-gesture x3=0 burst
and remaining 548-second no-swap/render interval, is
`qemu-hmp-vs-iosurface-source-trace.txt`, SHA-256
`04da7891ca12db5ef1a19f414e29db9981fa89dbe28c6228f62be94a72c15a68`.
Neither the null-surface burst nor later callback silence is a pixel verdict.

P292 offline DCP reference audit: the complete saved disassembly has no direct
same-image branch to helper `0x41df3a4`, and the Mach-O has no exact raw
pointer or chained-fixup target for it. The nearby `constructColorElements`
and `constructTimingElements` strings are diagnostic labels passed to the
DCP logger on allocation-failure paths, not builder calls. The separate
`0x4288a60` virtual-interface path has no demonstrated consumption of the
helper's two arrays. Their schema and route to publication therefore remain
unknown. See
`recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN292_FALLBACK_BUILDER_XREF_STATIC/fallback-to-elements-reference-audit.txt`
(SHA-256
`93fe8bc9b9284f765754a47c26ef79e869ea13d4dd4673313ab2137f199c8bde`) for
the search scope and limitations. The helper and the builder use provider
slots `+0x78/+0x80` with matching PAC diversity values `0x8c10/0x37f7`; that
supports a shared-interface hypothesis but does not prove identical receiver
or range objects. The useful real-device discriminator is a same-event
pointer comparison of input `+0x198`, builder `this+0x5cd0`, the two slot
returns, and the helper's output arrays. Durable instruction excerpts and
reference-search output are saved as `helper-fallback.dis`,
`log-label-callsite.dis`, `unified-provider-slots.dis`,
`fixup-chain-details.txt`, and `reference-search.txt` beside the P292 note.

P293 observer-side follow-up: the BackBoard swap probe now records the x3
surface-argument classification before emitting its JSON event and continues
counting pointer statuses after the bounded image-read cap. Null, invalid,
read-attempted, and valid-but-capped cases are separate. The classifier has
four passing pure tests and the Python/runner syntax checks pass, but the
change has not been boot-validated. It does not revise P291's no-sample
observation. Durable note:
`recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN293_POSTHOME_SURFACE_STATUS_PROBE/probe-hardening.txt`
(SHA-256
`422c4e5454d04ea3d7bef37ac2ef9fc7e1bda38c9d4d578ab26660ebc3498d0e`).

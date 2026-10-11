# AFK/EPIC protocol references

Scope: the AFK ring-buffer transport and EPIC message framing that ride on
top of an RTKit endpoint, as used by the DCP's `DCPEndpoint1`..`23`
(mailbox endpoints `0x20`-`0x36`, per `docs/re/dcp-bringup.md` and
`CLAUDE.md`). Goal: enough to write a QEMU device model that plays the
**firmware (IOP) side** of this protocol against XNU's real AP-side driver.

This does not cover the IOMFB protocol on endpoint `0x37` (`DCPEndpoint24`)
— that is a distinct RPC-with-callbacks framing (m1n1 calls it `dcpep`)
layered directly on RTKit, not on AFK/EPIC. See "What this does not cover".

## Sources actually used, and one dead end

| Source | Commit | Notes |
|---|---|---|
| `AsahiLinux/m1n1`, `proxyclient/m1n1/fw/afk/{epic,rbep}.py` | `60e53e7` (2026-08-31) | AP-side exploration code, macOS-era firmware. Path differs from the task brief: current m1n1 has no single `afk.py`; the ring-buffer layer is `afk/rbep.py` and the EPIC layer is `afk/epic.py`. |
| `AsahiLinux/linux` (Asahi kernel fork), `drivers/gpu/drm/apple/{afk.c,afk.h,dcp.c,dcp-internal.h,parser.c,parser.h,systemep.c,dptxep.c,av.c,epic/dpavservep.c}` | `77cb8f2` (2026-08-20) | **Primary source for this doc.** A real, shipping AP-side OS driver, so message *direction* is unambiguous from the code (which functions send vs. which are invoked from the RTKit receive callback). Targets DCP firmware through macOS 13.5 (`enum dcp_firmware_version` in `dcp-internal.h:29-33`: `DCP_FIRMWARE_V_12_3`, `DCP_FIRMWARE_V_13_5` — no later tag exists in this tree). |
| `torvalds/linux` master | `8931299` (2026-09-01) | **Dead end.** The task brief pointed at `drivers/gpu/drm/apple/{afk,epic,dcp}.c` on mainline Linux; that path does not exist there (`git ls-tree -r HEAD` on a full sparse-metadata clone finds no `gpu/drm/apple` directory at all, and no `afk`/`epic` filename anywhere in the tree). The DCP/AFK/EPIC driver has never been upstreamed; it exists only in the Asahi fork above. Recorded so nobody re-tries the same fetch. |
| `ChefKissInc/QEMUAppleSilicon` | `cc4302a` (2026-07-29) | No AFK/EPIC/DCP-firmware modelling at all — confirmed by grepping the whole tree for `afk`/`epic`/`rtbuddy` (no hits outside a `mailbox.c` unrelated to Apple RTKit `mailbox.c`, and `hw/display/apple_displaypipe_v{2,4}.c` which drive display hardware registers directly, bypassing DCP firmware entirely). `hw/misc/apple-silicon/a7iop/rtkit.c` is their RTKit mailbox model, roughly the counterpart of our `darwin_asc.c`, but nothing sits on top of it for DCP. See "What this does not cover". |
| `qemu-sptm/hw/arm/darwin_asc.c`, `qemu-sptm/include/xnu/darwin_asc.h` (this repo) | working tree | Our existing RTKit management layer. AFK is a personality layered on `DarwinASCOps` — see "Where this plugs into `darwin_asc.c`". |
| `docs/re/dcp-bringup.md`, `CLAUDE.md` (this repo) | working tree | Endpoint numbering (`0x20`-`0x36` = `DCPEndpoint1`..`23`) and the `EPICName` strings the task asks for, both derived from the real iOS 27 kernelcache's `IOKitPersonalities`. |

Every struct/enum/table below cites `file:line` in one of the cloned trees
under `/tmp/dvm/ref/` (not committed; re-clone with the commands in
`.claude/agents/ref-miner.md` to reproduce).

## Where this plugs into `darwin_asc.c`

`darwin_asc.c` already implements the generic RTKit management endpoint
(hello, endpoint map, `START_EP`, power states) and exposes three hooks
(`qemu-sptm/include/xnu/darwin_asc.h:11-18`):

```c
void (*started)(void *opaque);                          // RTKit handshake done
void (*ep_start)(void *opaque, uint8_t ep, uint32_t flag); // AP started/stopped an endpoint
bool (*handle)(void *opaque, uint8_t ep, uint64_t msg);    // AP -> IOP message on a non-mgmt endpoint
```

A DCP AFK/EPIC model is a `DarwinASCOps` personality for the `dcp` node:
- `ep_start(opaque, ep, flag==2)` for `ep` in `0x20..0x36` is where the
  firmware-side AFK state machine for that endpoint begins (see "Ordered
  handshake" below) — this is XNU's `apple_rtkit_start_ep()` equivalent,
  confirmed at `afk.c:107` (`apple_rtkit_start_ep(ep->dcp->rtk, ep->endpoint);`
  called from `afk_start()`).
- `handle(opaque, ep, msg)` receives every AFK opcode the AP originates on
  that endpoint — `0x80`, `0xa1`, `0xa2`, `0xa3`, `0xc0` in the table below
  (the AP-→-firmware column), starting with `0x80` itself: `INIT` is not
  something `ep_start` delivers, it arrives as this endpoint's first
  `handle()` call.
- Sending is `darwin_asc_send(dev, ep, msg)` — the same function already
  used for RTKit management replies.

The 64-bit RTKit message payload (`msg0` in `darwin_asc.c`'s `ASCMsg`) is
where AFK opcodes live; the endpoint number in the *mailbox* frame (`ep` in
`darwin_asc_send`) is unrelated to the *channel* number inside EPIC frames
(§2) — one RTKit endpoint (e.g. `0x22`) carries many EPIC channels, one per
announced service.

---

## 1. AFK ring-buffer transport

### 1.1 Direction of every opcode (this is the part most likely to be
gotten backwards — see the task's caveat)

Established unambiguously from `AsahiLinux/linux`'s real AP-side driver:
opcodes are dispatched by direction because `afk_receive_message_worker()`
(`afk.c:681-724`) is *only* reached for messages the AP received (wired as
the RTKit `recv_message` callback via `dcp_recv_msg` → `afk_receive_message`,
`dcp.c:222-249`, `afk.c:726-741`), while `afk_send()` (`afk.c:51-54`) marks
every opcode the AP explicitly transmits.

| Opcode | Name (Linux `enum rbep_msg_type`, `afk.c:24-37`) | Direction | Sent when |
|---|---|---|---|
| `0x80` | `RBEP_INIT` | **AP → firmware** | Immediately after RTKit `START_EP` for this endpoint (`afk_start()`, `afk.c:102-115`: `apple_rtkit_start_ep()` then `afk_send(..., RBEP_INIT)`) |
| `0xa0` | `RBEP_INIT_ACK` | firmware → AP | Ack for `INIT`. Linux only `break`s on receipt (`afk.c:690-691`) — it does **not** gate anything, `afk_start()`'s completion is `RBEP_START_ACK` instead. Send it anyway; a real AP may assert on its absence. |
| `0x89` | `RBEP_GETBUF` | **firmware → AP** | Firmware requests a DMA buffer: `SIZE` (bits 31:16, units of 0x40 bytes) + `TAG` (bits 15:0, firmware-chosen correlation id) (`afk.c:41-42`, handled `afk.c:117-145`) |
| `0xa1` | `RBEP_GETBUF_ACK` | AP → firmware | AP allocates the buffer and replies with `DVA` (bits 47:0) = the buffer's IOP-visible (DART-mapped) address (`afk.c:43`, `afk.c:142-144`) |
| `0x8a` | `RBEP_INIT_TX` | **firmware → AP** | Firmware designates a sub-range of that buffer, by `OFFSET`(47:32)/`SIZE`(31:16)/`TAG`(15:0) in 0x40-byte units, as the ring **the AP transmits into** (`afk.c:45-47`, dispatched to `ep->txbfr` at `afk.c:705-706`) |
| `0x8b` | `RBEP_INIT_RX` | **firmware → AP** | Same fields, designates the ring **the AP receives from** (`ep->rxbfr`, `afk.c:709-710`) |
| `0xa3` | `RBEP_START` | AP → firmware | Sent automatically by the AP once *both* `txbfr` and `rxbfr` are validated (`afk_init_rxtx()`, `afk.c:195-196`) — not a separate driver call |
| `0x86` | `RBEP_START_ACK` | **firmware → AP** | Firmware acks `START`; this is what completes `afk_start()`'s wait (`afk.c:693-694`) — **this is the "endpoint is live" signal**, not `INIT_ACK` |
| `0xa2` | `RBEP_SEND` | AP → firmware | AP wrote a new EPIC frame into its TX ring; carries the new `WPTR` (bits 31:0) (`afk_send_epic()`, `afk.c:878-879`) |
| `0x85` | `RBEP_RECV` | **firmware → AP** | Firmware wrote a new EPIC frame into the AP's RX ring; same `WPTR` field. AP drains with `afk_recv()` in a loop until `rptr==wptr` (`afk.c:713-716`, `afk.c:587-679`) |
| `0xc0` | `RBEP_SHUTDOWN` | AP → firmware | `afk_shutdown()` sends this first (`afk.c:89-91`) |
| `0xc1` | `RBEP_SHUTDOWN_ACK` | **firmware → AP** | Completes the shutdown wait (`afk.c:697-698`) |

All fields are packed into the single 64-bit RTKit message; the opcode
itself is bits `[63:48]` (`RBEP_TYPE`, `afk.c:22`). `BLOCK_SHIFT = 6`
(`afk.c:39`): every size/offset/tag quantity above the low 16 bits is a
count of 64-byte blocks, not bytes.

Firmware-authored implication for our model: on `ep_start`, send `INIT`
is what the **AP** does — our firmware-side model instead *waits* for
`INIT` (`0x80`) as the first `handle()` callback, then drives `GETBUF` →
(wait for `GETBUF_ACK`) → `INIT_TX` + `INIT_RX` → (wait for `START`) →
send `START_ACK`. This is spelled out as an ordered sequence in §3.

**m1n1 cross-check**: `proxyclient/m1n1/fw/afk/rbep.py` defines the same
eleven opcodes with the same values (`rbep.py:13-51`) and the same field
layouts (`AFKEP_GetBuf.SIZE = 31,16` etc., `rbep.py:14-16`). Because m1n1's
`AFKRingBufEndpoint` is written generically enough to play either role (its
`start()` sends `0x80` *and* it has a handler for incoming `0x80` named
`Hello`, `rbep.py:186-187,206-211`), it does not by itself resolve
direction — the Linux driver above is what settles it.

### 1.2 Ring buffer header (per ring, at the base of the region `INIT_TX`/
`INIT_RX` designated)

```c
// afk.h:74-82
struct afk_ringbuffer_header {
    __le32 bufsz;      // usable ring size in bytes (excludes this header)
    u32 unk;
    u32 _pad1[14];      // pads bufsz+unk out to a 0x40-byte block
    __le32 rptr;
    u32 _pad2[15];      // pads rptr out to a 0x40-byte block
    __le32 wptr;
    u32 _pad3[15];      // pads wptr out to a 0x40-byte block
};                       // sizeof == 0xC0 (three 0x40 blocks)
```

Validated at `afk_init_rxtx()` (`afk.c:182-189`): the AP reads `bufsz` out
of the header itself and asserts `bufsz + sizeof(header) == SIZE` from the
`INIT_TX`/`INIT_RX` message — i.e. **the firmware must have already written
a correct `bufsz` into the header before sending `INIT_TX`/`INIT_RX`**, and
the region named by `OFFSET`/`SIZE` is `header(0xC0 bytes) + payload(bufsz
bytes)`. `rptr`/`wptr` are byte offsets into the *payload* area (i.e.
relative to `buf = hdr + 1`, `afk.c:191` / `afk.h:155-158`), not absolute
offsets into the buffer.

m1n1's `AFKRingBuf.__init__` (`rbep.py:85-98`) derives the same 0x40 block
size generically by reading the first two u32s and dividing
`(size - bufsz) / 3` — algebraically identical to the fixed `0xC0` above
since `3 * 0x40 == 0xC0`, just written defensively instead of as a literal
constant.

Both rings (AP's TX and AP's RX) get this same header shape; they are two
independent regions inside the one buffer the firmware requested via
`GETBUF`, at whatever `OFFSET`s the firmware chose in its two `INIT_TX`/
`INIT_RX` messages (typically contiguous, but nothing enforces that — the
model should honor whatever offsets/sizes it advertises).

### 1.3 Ring queue entry (one per EPIC frame, written at the ring's current
`wptr`/read at `rptr`)

```c
// afk.h:84-91
struct afk_qe {
#define QE_MAGIC 0x20504f49   // little-endian bytes 49 4f 50 20 = "IOP "
    __le32 magic;
    __le32 size;      // bytes of EPIC payload that follow (epic_hdr + epic_sub_hdr + payload)
    __le32 channel;   // EPIC channel number (see §2)
    __le32 type;      // EPICType: NOTIFY=0, COMMAND=3, REPLY=4, NOTIFY_ACK=8
    u8 data[];         // epic_hdr, then epic_sub_hdr, then payload — see §2
};
```

`QE_MAGIC` is literally the ASCII bytes `"IOP "` (space-padded); m1n1
additionally accepts `"AOP "` for the AOP coprocessor's own AFK rings
(`rbep.py:130,135` — `assert magic in [b"IOP ", b"AOP "]`), so the magic is
per-coprocessor, not fixed protocol-wide. For DCP it is always `"IOP "`.

**Wrap-around**: if a queue entry would run past the end of the ring, the
writer places a *bare* `afk_qe` header (magic+size only, no payload
following — `size` still describes the real payload that lives at offset 0)
at the current `wptr`, then writes the real header+payload starting at
offset 0 of the ring. The reader detects this by checking
`rptr + size + sizeof(hdr) > bufsz` after reading a header; if so it
re-reads the header from offset 0 and continues from there. Read side:
`afk_recv()` `afk.c:606-653`. Write side: `afk_send_epic()` `afk.c:781-835`
(the `hdr2`/`wptr = sizeof(*hdr)` case). Every entry (real or wrap-marker)
starts on a `1 << BLOCK_SHIFT` (0x40-byte) aligned offset
(`afk.c:658,872-874`), and `wptr`/`rptr` wrap to `0` exactly at `bufsz`,
never left equal to `bufsz`.

Ring-full is detected purely by comparing prospective new `wptr` against
the peer's `rptr` (no fixed low-water-mark reservation beyond "must fit a
bare header" in the wrap case) — see the three `-ENOMEM` cases in
`afk_send_epic()`, `afk.c:787-829`.

**Ordering/memory-barrier note** (relevant only if the model's ring reads
race a vCPU rather than a real DMA-coherent link): Linux issues `dma_rmb()`
before reading a queue entry and `dma_mb()` before publishing the new
`rptr` (`afk.c:613,664`). Our model reads/writes guest memory synchronously
under the BQL, so this has no direct QEMU-side consequence, but it
confirms the wire contract: **write the payload, then the queue entry
header, then update the shared `wptr`, then send the RTKit notify message**
— in that order — matching `afk_send_epic()`'s sequence exactly.

---

## 2. EPIC framing (inside `afk_qe.data`)

### 2.1 Two-level header

```c
// afk.h:93-99
struct epic_hdr {
    u8 version;        // observed constant 2 (afk.c:851: ehdr->version = 2;)
    __le16 seq;         // per-endpoint sequence counter, increments every send (ep->qe_seq)
    u8 _pad;
    __le32 unk;          // always 0 on send
    __le64 timestamp;    // always 0 on send (afk.c:853)
} __attribute__((packed));   // 16 bytes

// afk.h:101-110
struct epic_sub_hdr {
    __le32 length;       // payload length in bytes, following this sub-header
    u8 version;          // observed constant 4 (afk.c:859: eshdr->version = 4;)
    u8 category;         // EPICCategory
    __le16 type;          // EPICSubtype (or an EPICType.NOTIFY_ACK-style command code — same field, meaning is per-category)
    __le64 timestamp;     // always 0 on send
    __le16 tag;            // correlation id for command/reply matching (see §2.4)
    __le16 unk;             // always 0 on send
    __le32 inline_len;      // payload_len-4 if category==REPLY, else 0 (afk.c:864-867)
} __attribute__((packed));   // 24 bytes
```

Wire order inside `afk_qe.data`: `epic_hdr` (16B) then `epic_sub_hdr` (24B)
then the payload (`epic_sub_hdr.length` bytes) — built in that order at
`afk_send_epic()` `afk.c:849-870`, parsed in that order at
`afk_recv_handle()` `afk.c:511-527`.

```c
// afk.h:133-151
enum epic_type {           // afk_qe.type AND epic_hdr's implicit "outer" type
    EPIC_TYPE_NOTIFY = 0,
    EPIC_TYPE_COMMAND = 3,
    EPIC_TYPE_REPLY = 4,
    EPIC_TYPE_NOTIFY_ACK = 8,
};
enum epic_category {        // epic_sub_hdr.category
    EPIC_CAT_REPORT = 0x00,
    EPIC_CAT_NOTIFY = 0x10,
    EPIC_CAT_REPLY = 0x20,
    EPIC_CAT_COMMAND = 0x30,
};
enum epic_subtype {          // epic_sub_hdr.type, when category==REPORT
    EPIC_SUBTYPE_ANNOUNCE = 0x30,
    EPIC_SUBTYPE_TEARDOWN = 0x32,
    EPIC_SUBTYPE_STD_SERVICE = 0xc0,
};
```

**m1n1 cross-check and a version wrinkle**: m1n1's `epic.py` defines the
same fields but as a `construct` `EPICSubHeader` with `version=4` (the
default) *and* an alternate `EPICSubHeaderV2` with `version=2` that drops
the 64-bit `timestamp` for a `pad`/`Int64ul` in a different position
(`epic.py:46-66`), selectable via `send_epic(..., version=2|4)`
(`epic.py:427-432`). m1n1 also documents two more subtypes Linux's smaller
driver never emits: `RETCODE_WITH_PAYLOAD = 0xa0` and `RETCODE = 0x84`,
`STRING = 0x8a` (`epic.py:27-34`) — plausible on replies from services
Linux doesn't implement (e.g. richer macOS-only services). **Treat the v4
sub-header as current** (Linux, which tracks the newer of the two firmware
generations it supports, always builds v4) and the v2 layout as a
legacy/older-firmware fallback — flag explicitly as version-sensitive,
since neither reference targets iOS 27 firmware (see "Version-sensitive").

### 2.2 Service announce: how a channel gets a name (the `EPICName`
mechanism)

A **channel** is a firmware-chosen `u32` (independent from the RTKit
endpoint number) identifying one logical service multiplexed over the
shared ring. Before any command/reply traffic, the firmware announces a new
channel with a `REPORT`/`ANNOUNCE` frame:

- `afk_qe.type = EPIC_TYPE_NOTIFY`, `epic_sub_hdr.category = EPIC_CAT_REPORT`,
  `epic_sub_hdr.type = EPIC_SUBTYPE_ANNOUNCE (0x30)`.
- Payload = a fixed 32-byte NUL-terminated/padded name string, followed by
  an optional serialized property dictionary (`afk_recv_handle_init()`,
  `afk.c:241-325`; `props = payload + 32`, `props_size = payload_size - 32`).

The property dictionary, when present, is parsed by `parse_epic_service_init()`
(`parser.c:647-707`) and can carry three keys:

| Key | Type | Meaning |
|---|---|---|
| `EPICName` | string | Stable machine-readable service id — **this is literally the string XNU's `AFKEndpointInterface` kext matches via `IOPropertyMatch`**, e.g. `dcpdptx-port-epic` (`dptxep.c:574`, confirmed: `if (strcmp(name, "dcpdptx-port-epic")) return;`), `dcpav-service-epic` / `dcpdp-service-epic` (`epic/dpavservep.c:23,197,203`) |
| `EPICProviderClass` | string | The IOKit driver class name expected to bind, e.g. `AppleDCPDPTXRemotePort`, `DCPAVSimpleVideoInterface`, `DCPAVAudioInterface` (`dptxep.c:601-606`, `av.c:280,285`) |
| `EPICUnit` | signed 64-bit int | Instance index for multi-instance services (e.g. DP port 0 vs 1, `dptxep.c:568-582`) |

Confirms and extends `docs/re/dcp-bringup.md`'s note that "Sub-services
attach by `EPICName`": the wire mechanism is this announce report, and the
task's requested names (`dcpav-controller-epic`, `dcpav-device-epic`,
`dcpav-service-epic`, `dcpav-video-interface-epic`, `dcpav-power-epic`,
`dcpdptx-port-epic`) are `EPICName` property values inside this dictionary,
not the raw 32-byte name field (which Linux's own services instead set to
things like `"system"`, `"DCPAVSimpleVideoInterface"` — see
`systemep.c:105-111`, `av.c:278-289`). **The Linux driver itself matches
services by `EPICProviderClass` by default** (`afk_recv_handle_init()`,
`afk.c:291-299`: `service_name = epic_class`) and only by `EPICName`
directly for endpoints that opt in via `ep->match_epic_name = true`
(`afk.h:187`; set for `dpavservep` at `epic/dpavservep.c:221`). XNU's own
matching key is `EPICName` per the kernelcache `IOKitPersonalities` evidence
already in `docs/re/dcp-bringup.md` — **our model must populate all three
properties** (`EPICName`, `EPICProviderClass`, `EPICUnit`) in every announce
so it satisfies whichever key a given kext's personality actually matches
on; we do not have the kext source, only the matched property name.

**Property dictionary wire format** (needed to actually build this
payload): it is Apple's own tag/length/value serialization, *not* XML
plist or bplist. Parser at `parser.c:18-287`:

```c
#define DCP_PARSE_HEADER 0xd3          // parser.c:18 — leading LE u32, must equal exactly 0xd3

enum dcp_parse_type {                   // parser.c:20-26
    DCP_TYPE_DICTIONARY = 1,
    DCP_TYPE_ARRAY = 2,
    DCP_TYPE_INT64 = 4,
    DCP_TYPE_STRING = 9,
    DCP_TYPE_BLOB = 10,
    DCP_TYPE_BOOL = 11,
};

struct dcp_parse_tag {                  // parser.c:28-34, 4 bytes, 32-bit-aligned before every tag
    unsigned int size : 24;              // element count (dict/array) or byte length (string/blob) or bool value
    enum dcp_parse_type type : 5;
    unsigned int padding : 2;             // must be 0
    bool last : 1;
} __packed;
```

Blob = `u32 header(==0xd3)` then one root tag+value (typically a
`DCP_TYPE_DICTIONARY` whose values are `key(STRING), value` pairs — see
`dcp_parse_foreach_in_dict` usage throughout `parser.c`). Strings and blobs
are followed by their raw bytes, 4-byte realigned before the next tag
(`parse_tag()`, `parser.c:52-60`: `ctx->pos = round_up(ctx->pos, 4)`).
`DCP_TYPE_INT64` values are a raw 8-byte little-endian `s64` immediately
after the tag (`parser.c:181-198`). This is the same format m1n1 calls
`OSSerialize()` in its `construct` grammar (`epic.py:71`,
`EPICAnnounce.props`) — Linux's version above is the concrete byte-level
spec.

### 2.3 Standard service call/reply (the RPC layer most services use)

Once a channel exists, the common pattern (`EPIC_SUBTYPE_STD_SERVICE =
0xc0`) is a `group`/`command` RPC:

```c
// afk.h:112-120 — carried as the COMMAND payload (category=COMMAND, type=STD_SERVICE)
struct epic_cmd {
    __le32 retcode;
    __le64 rxbuf;    // AP-allocated DMA buffer, IOP writes its reply here
    __le64 txbuf;    // AP-allocated DMA buffer, holds the request
    __le32 rxlen;
    __le32 txlen;
    u8 rxcookie;
    u8 txcookie;
} __attribute__((packed));

// afk.h:122-130 — the actual request/response layout inside txbuf/rxbuf
struct epic_service_call {
    u8 _pad0[2];
    __le16 group;
    __le32 command;
    __le32 data_len;
#define EPIC_SERVICE_CALL_MAGIC 0x69706378   // opaque magic constant, LE bytes 78 63 70 69 — see afk.c:1009
    __le32 magic;
    u8 _pad1[48];
} __attribute__((packed));   // exactly 64 bytes (static_assert at afk.h:131)
```

Call flow (`afk_send_command()` `afk.c:887-985`, `afk_service_call()`
`afk.c:987-1039`): AP allocates `rxbuf`/`txbuf`, sends `epic_cmd` as a
`COMMAND`/`STD_SERVICE` frame with a unique `tag` (`(cmd_tag<<8)|slot_idx`,
`afk.c:927-931`), firmware processes it and sends back a `REPLY` frame on
the same channel whose payload is another `epic_cmd` with `retcode` filled
in (`afk_recv_handle_reply()`, `afk.c:357-430` — matched by `tag`, not by
sequence). The `EPICName`/service-specific request bytes live *inside*
`txbuf`/`rxbuf`, wrapped in `epic_service_call` when using the
group/command convention (not all services use it — `system`'s
`setProperty` call, opcode `0x43`, sends a raw serialized dict directly as
the command payload with no `epic_service_call` wrapper — see
`systemep.c:18-24,36-39` and `AFKSystemService.setProperty` in
`epic.py:287-294`).

Firmware can also push unsolicited data on an existing channel as a
`NOTIFY`/`REPORT` frame (`afk_recv_handle_std_service()`'s `REPORT` branch,
`afk.c:497-502`) — the AP does not reply to these, it just invokes
`ops->report()`.

### 2.4 Teardown

Firmware closes a channel with `REPORT`/`TEARDOWN` (subtype `0x32`), no
payload beyond the header (`afk_recv_handle_teardown()`, `afk.c:327-355`).
The AP does not ack this.

---

## 3. Ordered startup handshake (firmware's actions, in order)

Combining §1.1 direction and §2.2, here is the full sequence for one AFK
endpoint (e.g. `0x22`) from RTKit's perspective through the first service
being live. **FW = our model, AP = XNU's real driver.**

1. **AP → FW**, RTKit management: `START_EP` for this endpoint
   (already implemented — `darwin_asc.c`'s `MGMT_START_EP` handler fires
   `DarwinASCOps.ep_start(opaque, ep, flag=2)`). *Not an AFK message.*
2. **AP → FW**: `RBEP_INIT` (`0x80`) — this endpoint's mailbox is live,
   bring up AFK (`afk.c:107-108`).
3. **FW → AP**: `RBEP_INIT_ACK` (`0xa0`) — no payload beyond the opcode.
4. **FW → AP**: `RBEP_GETBUF` (`0x89`) — `SIZE` (blocks of 0x40) for a
   buffer large enough to hold both rings' headers+payloads, `TAG` chosen
   by firmware (any value, echoed back later) (`afk.c:119-123`).
5. **AP → FW**: `RBEP_GETBUF_ACK` (`0xa1`) — `DVA`, the buffer's
   IOP-visible address (`afk.c:142-144`).
6. Firmware writes `bufsz` (and zeroes `rptr`/`wptr`) into the header of
   each of the two sub-ranges it is about to designate (§1.2) — a local
   step, no wire traffic.
7. **FW → AP**: `RBEP_INIT_TX` (`0x8a`) — `OFFSET`/`SIZE`/`TAG` (`TAG` must
   equal step 4's tag) for the ring the AP will write into
   (`afk.c:150-159,705-706`).
8. **FW → AP**: `RBEP_INIT_RX` (`0x8b`) — same, for the ring the AP will
   read from (`afk.c:709-710`).
   *(Order of 7 vs. 8 is not enforced by the AP — `afk_init_rxtx()` fires
   the same validation regardless of which arrives first, and only sends
   `START` once both are ready — `afk.c:195-196` — so a model may send
   them in either order.)*
9. **AP → FW**: `RBEP_START` (`0xa3`) — sent automatically once step 7 and
   8 both validated (`afk.c:195-196`).
10. **FW → AP**: `RBEP_START_ACK` (`0x86`) — **this is the point the AP
    considers the endpoint live** (`afk_start()`'s `wait_for_completion`
    unblocks here, `afk.c:110-114`).
11. For each service the firmware wants to expose on this endpoint: **FW →
    AP**, a `REPORT`/`ANNOUNCE` EPIC frame on a fresh channel number, with
    the 32-byte name + `EPICName`/`EPICProviderClass`/`EPICUnit` property
    dict (§2.2), delivered via a ring write + `RBEP_RECV` (`0x85`)
    notification (§1.1's data-path row). This is what XNU's kext matching
    reacts to and is presumably what happens once per logical sub-driver
    (`dcpav-controller-epic`, `dcpav-device-epic`, etc. — six to twenty-odd
    of these per DCP AFK endpoint based on `DCPEndpoint1..23`'s existence).
12. Steady state: either side notifies the other of new ring data with
    `RBEP_SEND` (`0xa2`, AP→FW) / `RBEP_RECV` (`0x85`, FW→AP); command/reply
    traffic per §2.3 happens per-channel from here on.
13. Teardown (if ever needed): **AP → FW** `RBEP_SHUTDOWN` (`0xc0`), **FW →
    AP** `RBEP_SHUTDOWN_ACK` (`0xc1`) (`afk.c:89-100`).

`dcp_start()` (`dcp.c:503-570`) shows the AP brings AFK endpoints up in a
fixed order relative to each other — `systemep` first
(`systemep_init()`, unconditional), then optionally `dpavservep`
(gated on `unstable_edid && !dcp_has_panel(dcp)` — likely irrelevant to a
full emulation target), then `ibootep`/`dptxep` (gated on
`fw_compat >= DCP_FIRMWARE_V_13_5`), then IOMFB (`iomfb_start_rtkit()`,
not AFK), then optionally `avep`. Whether the real DCP firmware's *own*
internal ordering of `RBEP_INIT` (step 2, per endpoint) across the
different mailbox endpoints matches this AP-side call order is not
something this Linux driver can show — it is the AP calling into RTKit's
`apple_rtkit_start_ep()` per endpoint from its own probe routine, and each
`START_EP` independently triggers the sequence above. **Our model does not
need to replicate cross-endpoint ordering** since each endpoint's handshake
is self-contained; it only needs to answer whichever `START_EP`s XNU issues
and in whichever order.

---

## 4. Endpoint-number correlation (informational, not confirmed for t8140)

Linux's endpoint numbers (`dcp-internal.h:36-46`) are M1/M2-era (macOS
12.3-13.5). They line up suspiciously well with the `DCPEndpoint<N>`
numbering already reverse-engineered from the real iOS 27 kernelcache in
`docs/re/dcp-bringup.md` (`N = endpoint - 0x1f`):

| Endpoint | Linux name (M-series, `dcp-internal.h`) | Our `DCPEndpoint<N>` (t8140, from kernelcache) |
|---|---|---|
| `0x20` | `SYSTEM_ENDPOINT` | `DCPEndpoint1` |
| `0x21` | `TEST_ENDPOINT` | `DCPEndpoint2` |
| `0x22` | `DCP_EXPERT_ENDPOINT` | `DCPEndpoint3` |
| `0x23` | `DISP0_ENDPOINT` | `DCPEndpoint4` |
| `0x28` | `DPAVSERV_ENDPOINT` | `DCPEndpoint9` |
| `0x29` | `AV_ENDPOINT` | `DCPEndpoint10` |
| `0x2a` | `DPTX_ENDPOINT` | `DCPEndpoint11` |
| `0x2b` | `HDCP_ENDPOINT` | `DCPEndpoint12` |
| `0x2d` | `REMOTE_ALLOC_ENDPOINT` | `DCPEndpoint14` |
| `0x37` | `IOMFB_ENDPOINT` (not AFK) | `DCPEndpoint24` (not AFK, per `dcp-bringup.md`) |

**This is a correlation across a ~4-year, several-major-version gap, not a
citation of an iOS 27 source — do not treat the specific endpoint↔service
mapping as confirmed.** The generic RTKit endpoint-map mechanism means the
AP always discovers which endpoints exist from the firmware's own map
message (already handled in `darwin_asc.c`), so a model does not strictly
need to guess which numeric endpoint carries which service — but knowing
plausible candidates is useful for deciding which `EPICName` values to
announce on which endpoint first while bringing the display up.

---

## 5. Version-sensitive — flagged explicitly per the task's caveat

Every item below is a place where iOS 27 / A18 DCP firmware could plausibly
differ from what's cited, because both references target macOS-era
firmware roughly 3-4 years and several major versions older:

- **EPIC sub-header version**: Linux always builds `version = 4`
  (`afk.c:859`) against firmware up to `DCP_FIRMWARE_V_13_5`
  (`dcp-internal.h:29-33`). m1n1 additionally knows a `version = 2` layout
  (`epic.py:57-66`) with a different field order, presumably for even
  older (M1-launch-era) firmware. iOS 27's DCP could use `4`, a newer `5`+
  we have no reference for, or something else — **not directly confirmed
  for our target**.
- **`EPICSubtype` completeness**: Linux's driver only emits/expects
  `ANNOUNCE`, `TEARDOWN`, `STD_SERVICE` (`afk.h:147-151`); m1n1 additionally
  lists `RETCODE_WITH_PAYLOAD (0xa0)`, `RETCODE (0x84)`, `STRING (0x8a)`
  (`epic.py:27-34`) seen on services Linux doesn't implement. The full
  subtype space for iOS 27's DCP-specific services (`dcpav-controller-epic`
  etc., none of which Linux implements) is **unknown** — only the generic
  framing (§2.1) and the announce/std-service mechanics (§2.2-2.3) are
  well-attested.
- **Ring header padding layout** (`afk_ringbuffer_header`'s exact
  `_pad1`/`_pad2`/`_pad3` sizes): derived from Linux's `static_assert`-free
  struct, cross-checked only algebraically against m1n1's generic
  block-size derivation (§1.2) — both agree the *total* is `0xC0` bytes
  split into three `0x40` blocks, but neither is a first-party Apple
  header; the individual field offsets (`bufsz` at `+0`, `rptr` at `+0x40`,
  `wptr` at `+0x80`) are what to treat as load-bearing, not the padding.
- **`epic_service_call`'s 48-byte trailing padding**: unexplored — Linux
  never reads it, always zero-fills on send (`afk_service_call()`,
  `afk.c:1003-1010` uses `kzalloc`). Could carry meaning on newer firmware.
- **Endpoint↔service numeric mapping**: see §4 — explicitly a correlation,
  not a citation.
- **`EPICName` matching being the primary key**: confirmed only indirectly
  — Linux's own driver prefers `EPICProviderClass` (§2.2) and only uses
  `EPICName` for one endpoint (`dpavservep`, opted in via
  `match_epic_name`). That XNU instead keys on `EPICName` broadly is a
  fact from *this repo's own* kernelcache research
  (`docs/re/dcp-bringup.md`), not from either reference — the two are
  independent, consistent lines of evidence, but neither reference proves
  the other's usage pattern.
- **RTKit `START_EP` flag encoding**: Linux's `APPLE_RTKit_MGMT_STARTEP_FLAG`
  is `BIT_ULL(1)` (`drivers/soc/apple/rtkit.c:51`, mainline Linux, commit
  `8931299`) vs. our `darwin_asc.c`'s `MGMT_STARTEP_FLAG(m) ((m) & 3)`
  checked against `== 2` — consistent (bit 1 set), noted only because it's
  a place the two disagreed in *representation* though not in *value*, and
  is out of scope for this doc (RTKit management is already implemented
  and working per `CLAUDE.md`).

## What this does not cover

- **The IOMFB protocol** (endpoint `0x37`/`DCPEndpoint24`) — a separate,
  non-AFK RPC-with-callback framing (`iomfb.c`, `iomfb_v12_3.c`,
  `iomfb_v13_3.c` in the Asahi tree; `dcpep.py`/`ipc.py`/`manager.py` in
  m1n1). This is the protocol that actually carries display mode setting,
  framebuffer swap, etc. — AFK/EPIC only gets the *sub-services* (audio,
  DP-alt-mode ports, EDID/AV service, system logging) up. Getting a picture
  out still needs this layer separately reverse-engineered/modelled.
- **What payload each specific `dcpav-*`/`dcpdptx-*` service expects for
  its `STD_SERVICE` group/command calls.** Linux implements only a handful
  of DCP's real services (`system`, `powerlog-service`, `dcpav-service-epic`,
  `dcpdp-service-epic`, `AppleDCPDPTXRemotePort`, `DCPAVSimpleVideoInterface`,
  `DCPAVAudioInterface`) — none of the names in the task's list
  (`dcpav-controller-epic`, `dcpav-device-epic`,
  `dcpav-video-interface-epic`, `dcpav-power-epic`) appear anywhere in
  either reference. Their `group`/`command` numbers, argument structs, and
  even whether they're `EPICName` or `EPICProviderClass` values are
  **entirely unknown from these sources** — that's kext disassembly work
  (`DCPAVFamilyProxy`/`AppleDCPDPTXProxy`, per `docs/re/dcp-bringup.md`),
  not something either reference body reaches.
- **How many channels/services a real DCP firmware announces on endpoint
  bring-up, or in what order** — neither reference captures a live
  announce trace; §3 step 11 is structurally correct (this is how any
  announce happens) but not populated with a real sequence.
- **QEMU-side prior art** — confirmed none exists; QEMUAppleSilicon solves
  the display problem by modelling display-pipe hardware registers
  directly and never brings up a DCP coprocessor at all, which is a
  materially different strategy than this project's goal of getting XNU's
  unmodified `AppleCLCD2`/`IOMFB`/`DCPEndpointV2` stack to run against an
  impersonated firmware. There is no existing "pretend to be the DCP over
  RTKit" QEMU model to diff against.
- **AOP/other coprocessors' use of AFK** — m1n1 hints AOP (audio) also
  speaks AFK (the `"AOP "` ring magic, `EPICServiceAnnounce`'s different
  20-byte name field in `epic.py:75-83`), suggesting the announce payload
  shape isn't perfectly uniform across coprocessors. Irrelevant to DCP
  specifically but a reminder that "AFK" is a shared library, not a
  DCP-only protocol, and other coprocessors' variations shouldn't be
  assumed to apply here.

## iOS 27 corrections, derived live (2026-09-02)

Two fields this document and `darwin_epic.c` had wrong, both established by
instrumenting a boot rather than from the M1-era references.

### The message header's byte 0 is a sequence counter, not flags

`darwin_epic.c` documents it as flags, "bit 0 means a 0x18-byte extra block
follows". Across one boot the AP sent `0x0, 0x1, 0x2 ... 0xf` on successive
frames — a clean monotonic counter. Whatever the flag reading was based on, the
AP is using this byte as a sequence/tag. Echoing it in replies is harmless and
probably right; it is not on its own what makes a reply acceptable (tested).

### The command body's u32 at +4 is a length inbound and a return code outbound

The standard-service (`0xc0`) body starts with an 8-byte header. Byte 1 is the
command id the AP matches replies against (`0xfffffff008b8eca8`,
`ubfx w1, w26, #8, #8`). The u32 at +4 is **not** a symmetric field:

- **Inbound** it is the payload length. Across three commands in one boot it
  was always `body_len - 8`: `0x60`/`0x68`, `0x50`/`0x58`, `0x90`/`0x98`.
- **Outbound** the AP reads the same offset as a return code.

Echoing it back made `DCPAVRemoteSACControllerProxy` — interface 8, whose
command carried `arg 0x50` — report exactly:

```
IOReturn DCPAVRemoteSACControllerProxy::bootCompleteGated() error: ret = 0x50 (UNDEFINED)
```

The value matching the request length, on the one interface whose request
carried that length, is what identified the field. Zeroing it clears the error.

### Sequence, once replies are answered at all

With no reply the AP sends one `REPORT OPEN` and one command and stops. With a
reply it opens every announced interface and keeps going:

```
REPORT OPEN   iface 1..10
REPORT CLOSE  iface 6          (dcpav-audio-interface-epic, declined)
COMMAND 0xc0  iface 1 (body 0x68), 8 (0x58), 10 (0x98)
```

13 OPEN reports and 1 CLOSE in a 150-second boot.

GATE204–209 later traced the AP-side owners of those three `COMMAND 0xc0`
frames: interface 1 is `DCPAVControllerProxy`, interface 8 is
`DCPAVRemoteSACControllerProxy`, and interface 10 is `DCPDPDeviceProxy`.
GATE207 read the payload at the verified DCPAV header offset `message+0x40`
and found zeros across each request payload in that boot. The QEMU responder
used for these probes is deliberately synthetic: it zero-allocates the reply
body and copies only the command header. Its successful transport completion
does not demonstrate that the firmware would accept those replies or that
they contain the data required for IOAV display properties.

# The D-series callback direction on DCP endpoint 0x37 (DCP -> AP)

Companion to `docs/re/iomfb-link.md`, which covers the class-0/class-1
handshake and the AP -> DCP (`A`-series) half of the class-2 RPC layer. This
document covers the other half: the requests the **firmware** originates and
the AP answers, which is what the DCP does to drive the rest of display
bring-up on real hardware and which our model did not implement at all.

Source: iOS 27.0 beta (24A5430a), iPhone17,3 (t8140/H17P), kernelcache
`firmware/bootkc`. Unslid VAs; `file_offset = VA - 0xfffffff007004000`; the
panic addresses the guest prints are these `+ 0x20000000`. Both DCP kexts are
stripped (`nsyms=0`), so every function name is `fcn.<addr>` identified by
string cross-reference or by call shape.

Two kexts are involved and it matters which:

| kext | `__TEXT_EXEC` | role |
|---|---|---|
| `com.apple.iokit.IOMobileGraphicsFamily-DCP` | `0xfffffff00a0b2080`-`0xfffffff00a0dd120` | the wire protocol (`link_send_message`, `link_handle_message`, `rpc_caller_gated`, `rpc_callee_gated`) and the D350/D400/D450/D550-D6FF/D700 handlers |
| `com.apple.driver.AppleMobileDispH17P-DCP` | `0xfffffff0091792c0`-`0xfffffff00919e9c4` | `AppleDCPLinkServiceSoC`, the top-level `link_rpc_lookup`, and the D000/D100/D200/D300 handlers |

(Ranges read out of each `LC_FILESET_ENTRY`'s own `LC_SEGMENT_64`; the entry
headers are packed low in the file, the code is not.)

## 1. The callee path, end to end

`link_handle_message` (`fcn.fffffff00a0cfac0`) dispatches class 2 on subkind:

```
0xa0cfc28  ubfx w21, w28, #6, #2      ; subkind
0xa0cfc2c  cmp  w21, #1
0xa0cfc30  b.eq 0xa0cfd0c             ; subkind 1 -> a completion for one of OUR requests
0xa0cfc34  cbnz w21, 0xa0d0308        ; subkind 2/3 -> error
0xa0cfc38  cmp  w26, #2               ; class must be exactly 2
0xa0cfc3c  b.ne 0xa0d0310
0xa0cfc40  ...                        ; subkind 0 -> an INBOUND REQUEST
```

Both branches start by looking up an RPC slot with a key built from the
message:

```
0xa0cfc44  lsl    x8, x28, #0x18
0xa0cfc48  and    x8, x8, #0x100000000     ; bit 8 (ack) -> bit 32 of the key
0xa0cfc4c  bfxil  x8, x28, #0xa, #6        ; bits[15:10] (tag) -> bits[5:0]
```

and pass it to the gated lambda `fcn.fffffff00a0d0318`, which is the whole
correlation mechanism in twenty instructions:

```
x10 = chan; w8 = key.tag; w9 = key.ack
w11 = chan[0x70]
x12 = (w9 == w11) ? 0x78 : 0x218          ; 0xa0d0328-0xa0d0334
x11 = chan + x12                           ; base of a 4-entry slot array
if (w8 >= 4) panic                         ; 0xa0d033c, "%s:%d" line 0x254
slot = x11 + w8 * 0x68                     ; 0xa0d0350-0xa0d0360
*found_flag = 0
if (slot[0x60] == 0) {                     ; slot free -> claim it
    slot[0] = tag; slot[4] = ack; slot[0x60] = 1;
    *found_flag = 1;
}
*out = slot
```

So there are **two arrays of four 0x68-byte RPC slots** on the channel, chosen
by whether the message's ack bit equals `chan[0x70]`, and the tag is a
**slot index limited to 0..3**, not a free-running counter.

`chan` is `L + 0x1f0` where `L` is the `AppleDCPLinkService` link-state object
(`link_send_message(chan + 0x1f0, ...)` at `0xa0ce07c`, and `link_state_init`
below writes both). The two arrays therefore live at `L+0x268` and `L+0x408`.

### chan[0x70] is zero, and that fixes the ack bit we must send

`link_state_init` (`fcn.fffffff00a0cf5b8`) does, at `0xa0cf5e8`:

```
str wzr, [x19, #0x260]        ; L+0x260 == chan+0x70 == 0
```

`link_send_message` uses that same byte as bit 8 when no `ctx` is passed
(`counter_ptr = ctx ? ctx+4 : chan+0x70`, `0xa0ced10`), which is why the AP's
own opening announce `0x0100000000000040` has bit 8 clear. Since the lambda
routes `ack == chan[0x70]` to the AP's *own* outgoing array at `chan+0x78`,
**an inbound request from the firmware must set bit 8** or it will collide with
the AP's own outstanding calls. This is a derived fact with two independent
supports (the `str wzr` and the observed message), not a guess.

### The shared heap is one buffer, carved into eight fixed windows

`link_state_init` allocates `0x80000` bytes (`mov w0, #0x80000; bl
0xa0dc2f0` at `0xa0cf5cc`) and hands each slot a 32 KB TX window and a 32 KB
RX window (`0xa0cf654`-`0xa0cf6e0`, both loops four iterations, stride `0x68`):

| slot array | slot+0x08 / +0x10 / +0x14 (TX) | slot+0x18 / +0x20 / +0x24 (RX) |
|---|---|---|
| `chan+0x78` + t*0x68 (AP's own calls) | `heap + t*0x8000`, off `t*0x8000`, len `0x8000` | `heap + 0x60000 + t*0x8000` |
| `chan+0x218` + t*0x68 (**inbound**) | `heap + 0x20000 + t*0x8000` | `heap + 0x40000 + t*0x8000`, off `0x40000+t*0x8000`, len `0x8000` |

and the end of the arena is recorded at `L+0x5a8 = heap + 0x80000`
(`add x8, x8, #0x80, lsl #12`, `0xa0cf6e4`).

`rpc_callee_gated` reads an inbound request from `slot[0x18] + offset`
(`0xa0cf0d0`-`0xa0cf0d4`) and bounds-checks it against `slot[0x24]`
(`0xa0cf054`-`0xa0cf060`). So:

> **The `offset` field of an inbound class-2 message is relative to
> `heap + 0x40000 + tag*0x8000`, not to the start of the heap.**

The same reading fixes a latent bug on the receive side of our model: the AP's
own request for tag `t` lives at `heap + t*0x8000 + offset`
(`ldr x8,[x21]` where `x21 = slot+8`, `0xa0ce608`-`0xa0ce60c`), and we were
reading it at `heap + offset`. That is right only for tag 0, which is the only
tag the AP has ever used so far.

### rpc_callee_gated: what the AP validates, and how it replies

`fcn.fffffff00a0cf020`. `x19` = the slot, `x20` = `chan`:

```
w21 = slot[0x4a]           ; the u32 the receive path stored from msg>>16
w23 = w21 >> 16            ; size          (msg bits[47:32])
x28 = w21 & 0xffff         ; offset        (msg bits[31:16])
w26 = slot[0x48]           ; the 16-bit header
w24 = slot[0x4e]           ; msg bits[63:48]
```

guards, in order, each returning an error rather than panicking:

| check | where | meaning |
|---|---|---|
| `slot[0x24] - offset >= size` | `0xa0cf054`-`0xa0cf060` | request fits the 32 KB window |
| `(header & 3) == 2` | `0xa0cf09c`-`0xa0cf0a4` | class 2 |
| `(header & 0xc0) != 0x40` | `0xa0cf0a8`-`0xa0cf0b0` | **subkind must not be 1** |
| `size >= 0xc` | `0xa0cf0b4`-`0xa0cf0bc` (`w21>>18 > 2`) | room for the request header |
| `slot[0x24] - offset > 0xb` | `0xa0cf0c0`-`0xa0cf0cc` | ditto, against the window |
| `size == 0xc + in_len + out_len` | `0xa0cf0dc`-`0xa0cf0fc` | the two lengths come from the heap, so we control them |

then `name = *(u32*)(slot[0x18] + offset)` (`0xa0cf100`), the name is formatted
big-endian for a trace call (`0xa0cf108`-`0xa0cf134`), and the handler is
looked up through a **virtual method at vtable offset +0x540** of the object at
`chan+0x68`, PAC modifier `0x160b` (`0xa0cf13c`-`0xa0cf168`). That is
`link_rpc_lookup`. A null return takes the `"%s: link_rpc_lookup callback not
found: '%s'"` path (string `0xfffffff007992778`).

The handler ABI, from `0xa0cf1e8`-`0xa0cf1fc`:

```
handler(x0 = chan, x1 = slot, x2 = in, x3 = in_len, x4 = out, x5 = out_len)
```
called `blraa x23, x17` with `x17 = 0x6eb1` — the same PAC modifier every
`link_rpc_lookup` leaf signs its pointer with, which is how the enumeration
below is known to be reading the right table. `in` is
`slot[0x18] + offset + 0xc` (or NULL when `in_len == 0`) and `out` is
`in + in_len` (or NULL when `out_len == 0`).

The completion the AP sends back, `0xa0cf298`-`0xa0cf2b4`:

```
msg = (incoming_header & 0xff3e) | 0x40 | (incoming_bits[63:48] << 48)
link_send_message(chan, slot_as_ctx, msg)
```

`0xff3e` clears bit 0 and bit 6; `| 0x40` sets subkind 1; class stays 2 because
bit 1 survives the mask. Passing the slot as `ctx` makes `link_send_message`
re-derive the tag from `slot[0]` and bit 8 from `slot[4]`, so **the completion
carries our tag and our ack bit**, and bits[47:16] — the status field —
are **zero on success**.

## 2. The dispatch table

`AppleDCPLinkServiceSoC::link_rpc_lookup` is at `0xfffffff00917d328`
(`bti c; mov x0, x1` then a switch on `w2`). It routes by hundreds block; the
five blocks that live in the other kext go through `__auth_got` thunks whose
chained-fixup words decode as shown:

| names | dispatcher | via |
|---|---|---|
| D000-D0FF | `0xfffffff00917a604` | direct `b` at `0x917d34c` |
| D100-D1FF | `0xfffffff009199c40` | `0x917d364` |
| D200-D2FF | `0xfffffff00919ce2c` | `0x917d378` |
| D300-D34F | `0xfffffff0091827bc` | `0x917d398` |
| D350-D3FF | `0xfffffff00a0b2408` | thunk `0x919dd24`, got `0x808f7b8` |
| D400-D44F | `0xfffffff00a0d0538` | thunk `0x919dd34`, got `0x808f7c0` |
| D450-D4FF | `0xfffffff00a0d3068` | thunk `0x919dd14`, got `0x808f7b0` |
| D550-D6FF | `0xfffffff00a0d6cb0` | thunk `0x919dd54`, got `0x808f7d0` |
| D700-D74F | `0xfffffff00a0b2414` | thunk `0x919dd44`, got `0x808f7c8` |
| anything else | `0xfffffff00917f454` | not-found tail |

Note there is **no D500-D54F block**: those names fall straight through to the
not-found tail.

`tools/re/dcp_dtable.py` emulates that whole chain for all 1000 candidate
names and prints the handler each one resolves to, plus the handler's own
argument guards (`cmp w3,#N` for `in_len`, `cmp w5,#M` for `out_len`, both
returning `0xe00002c2` = `kIOReturnBadArgument`). Re-run it on a new
kernelcache rather than trusting this table. A `?` means no guard was found in
the first 48 instructions before the first call — usually a handler that takes
no buffer on that side, but sometimes one whose check is further in.

```
D000  handler 0xfffffff00917a634  in ? out 0x4
D001  handler 0xfffffff00917a688  in ? out 0x4
D002  handler 0xfffffff00917a6dc  in ? out ?
D003  handler 0xfffffff00917a700  in 0x4 out 0x14
D004  handler 0xfffffff00917a7e4  in 0x8 out ?
D005  handler 0xfffffff00917a8b4  in 0x8 out ?
D006  handler 0xfffffff00917a984  in 0x54 out 0x50
D007  handler 0xfffffff00917aa88  in 0x4 out ?
D100  handler 0xfffffff00919a0f0  in ? out ?
D101  handler 0xfffffff00919a15c  in ? out 0x4
D102  handler 0xfffffff00919a1d8  in 0x44 out ?
D103  handler 0xfffffff00919a2f4  in ? out ?
D104  handler 0xfffffff00919a360  in 0x44 out ?
D105  handler 0xfffffff00919a4a0  in 0x80 out ?
D106  handler 0xfffffff00919a5e8  in 0x40 out ?
D107  handler 0xfffffff00919a6ec  in ? out 0x4
D108  handler 0xfffffff00919a780  in ? out 0x4
D109  handler 0xfffffff00919a814  in ? out 0x4
D110  handler 0xfffffff00919a8a8  in ? out 0x4
D111  handler 0xfffffff00919a93c  in ? out 0x4
D112  handler 0xfffffff00919a9d0  in ? out 0x4
D113  handler 0xfffffff00919aa64  in ? out ?
D114  handler 0xfffffff00919ac7c  in 0x10 out ?
D115  handler 0xfffffff00919ad9c  in 0x10 out ?
D116  handler 0xfffffff00919aeb0  in 0xc out ?
D117  handler 0xfffffff00919afa0  in 0x14 out ?
D118  handler 0xfffffff00919b0b4  in ? out ?
D119  handler 0xfffffff00919b234  in 0x4 out ?
D120  handler 0xfffffff00919b338  in ? out 0x4
D121  handler 0xfffffff00919b3cc  in ? out 0x4
D122  handler 0xfffffff00919b460  in ? out 0x4
D123  handler 0xfffffff00919b4f4  in ? out 0x4
D124  handler 0xfffffff00919b588  in 0x64 out ?
D125  handler 0xfffffff00919b718  in 0x48 out ?
D126  handler 0xfffffff00919b8fc  in >=0x4 out >=0x4
D127  handler 0xfffffff00919ba00  in >=0x1008 out >=0x4
D128  handler 0xfffffff00919bb80  in >=0x40 out >=0x4
D129  handler 0xfffffff00919bc90  in 0x1c out ?
D130  handler 0xfffffff00919bddc  in 0x8 out ?
D131  handler 0xfffffff00919bec8  in 0x10 out ?
D132  handler 0xfffffff00919c008  in 0x4 out ?
D133  handler 0xfffffff00919c104  in ? out 0x4
D200  handler 0xfffffff00919cfd0  in ? out 0x4
D201  handler 0xfffffff00919d044  in 0xc out ?
D202  handler 0xfffffff00919d204  in 0x18 out ?
D203  handler 0xfffffff00919d388  in ? out 0x4
D204  handler 0xfffffff00919d3dc  in ? out 0x4
D205  handler 0xfffffff00919d430  in 0xc out ?
D206  handler 0xfffffff00919d558  in ? out 0x4
D207  handler 0xfffffff00919d5ac  in ? out 0x4
D208  handler 0xfffffff00919d600  in 0x4 out ?
D209  handler 0xfffffff00919d6a8  in ? out 0x8
D210  handler 0xfffffff00919d6fc  in 0x4 out 0x51c
D211  handler 0xfffffff00919d7e0  in 0x4 out 0x14
D300  handler 0xfffffff0091827e4  in 0x10 out ?
D400  handler 0xfffffff00a0d08c0  in 0x4c out 0xc04
D401  handler 0xfffffff00a0d0ac0  in 0x50 out 0xc04
D402  handler 0xfffffff00a0d0c08  in 0x4c out 0xc04
D403  handler 0xfffffff00a0d0d1c  in 0x4c out 0xc04
D404  handler 0xfffffff00a0d0e6c  in 0x48 out 0xc04
D405  handler 0xfffffff00a0d0f78  in 0x4c out 0xc04
D406  handler 0xfffffff00a0d10c8  in 0x48 out 0xc04
D407  handler 0xfffffff00a0d11d4  in 0x48 out 0xc04
D408  handler 0xfffffff00a0d12e4  in 0x8 out ?
D409  handler 0xfffffff00a0d13fc  in 0xc out ?
D410  handler 0xfffffff00a0d1528  in 0x14 out ?
D411  handler 0xfffffff00a0d1670  in 0x10 out ?
D412  handler 0xfffffff00a0d17c0  in 0x448 out ?
D413  handler 0xfffffff00a0d19a8  in ? out ?
D414  handler 0xfffffff00a0d1bb0  in 0x50 out ?
D415  handler 0xfffffff00a0d1d68  in 0x4c out ?
D416  handler 0xfffffff00a0d1f0c  in 0x84 out ?
D417  handler 0xfffffff00a0d20c0  in 0x448 out ?
D418  handler 0xfffffff00a0d227c  in ? out ?
D419  handler 0xfffffff00a0d2458  in 0x50 out ?
D420  handler 0xfffffff00a0d25dc  in 0x4c out ?
D421  handler 0xfffffff00a0d2744  in 0x84 out ?
D422  handler 0xfffffff00a0d28c4  in 0x48 out ?
D423  handler 0xfffffff00a0d29fc  in 0x44 out ?
D424  handler 0xfffffff00a0d2b58  in 0x44 out ?
D450  handler 0xfffffff00a0d3098  in 0x8 out ?
D451  handler 0xfffffff00a0d31d0  in 0x14 out ?
D452  handler 0xfffffff00a0d3318  in 0x8 out ?
D453  handler 0xfffffff00a0d3434  in 0x8 out ?
D454  handler 0xfffffff00a0d3550  in 0x4 out ?
D550  handler 0xfffffff00a0d7440  in 0x444 out ?
D551  handler 0xfffffff00a0d7638  in ? out ?
D552  handler 0xfffffff00a0d786c  in ? out ?
D553  handler 0xfffffff00a0d7a84  in ? out ?
D554  handler 0xfffffff00a0d7cb8  in 0x4c out ?
D555  handler 0xfffffff00a0d7e80  in 0x4c out ?
D556  handler 0xfffffff00a0d8048  in 0x48 out ?
D557  handler 0xfffffff00a0d81f4  in 0x80 out ?
D558  handler 0xfffffff00a0d83a8  in 0x80 out ?
D559  handler 0xfffffff00a0d855c  in 0x444 out ?
D560  handler 0xfffffff00a0d8728  in ? out ?
D561  handler 0xfffffff00a0d8930  in ? out ?
D562  handler 0xfffffff00a0d8b1c  in ? out ?
D563  handler 0xfffffff00a0d8d24  in 0x4c out ?
D564  handler 0xfffffff00a0d8ec0  in 0x4c out ?
D565  handler 0xfffffff00a0d905c  in 0x48 out ?
D566  handler 0xfffffff00a0d91d4  in 0x80 out ?
D567  handler 0xfffffff00a0d935c  in 0x80 out ?
D568  handler 0xfffffff00a0d94e4  in 0x80 out ?
D569  handler 0xfffffff00a0d9638  in 0x44 out ?
D570  handler 0xfffffff00a0d9784  in ? out ?
D571  handler 0xfffffff00a0d996c  in 0x8 out ?
D572  handler 0xfffffff00a0d9aa4  in 0x4 out ?
D573  handler 0xfffffff00a0d9bcc  in 0x4 out ?
D574  handler 0xfffffff00a0d9d0c  in 0x4 out ?
D575  handler 0xfffffff00a0d9e4c  in 0x58 out ?
D576  handler 0xfffffff00a0d9fcc  in 0x4 out ?
D578  handler 0xfffffff00a0da0e4  in ? out ?
D579  handler 0xfffffff00a0da180  in 0x4 out ?
D580  handler 0xfffffff00a0da2b4  in ? out ?
D581  handler 0xfffffff00a0da350  in ? out ?
D582  handler 0xfffffff00a0da3ec  in 0xc out ?
D583  handler 0xfffffff00a0da4f8  in 0x4 out ?
D584  handler 0xfffffff00a0da634  in ? out ?
D585  handler 0xfffffff00a0da6d0  in 0xc out ?
D586  handler 0xfffffff00a0da804  in 0x8 out ?
D587  handler 0xfffffff00a0da91c  in ? out ?
D588  handler 0xfffffff00a0da99c  in 0x18 out ?
D589  handler 0xfffffff00a0daa9c  in 0xe4 out ?
D590  handler 0xfffffff00a0dac24  in 0x14 out ?
D591  handler 0xfffffff00a0dad28  in 0x4 out ?
D592  handler 0xfffffff00a0dae7c  in ? out ?
D593  handler 0xfffffff00a0daf18  in 0x8 out ?
D594  handler 0xfffffff00a0db034  in 0x730 out ?
D595  handler 0xfffffff00a0db1a8  in ? out ?
D596  handler 0xfffffff00a0db3b4  in 0x14 out ?
D597  handler 0xfffffff00a0db4c0  in 0x4 out ?
D598  handler 0xfffffff00a0db5fc  in 0x4 out ?
D599  handler 0xfffffff00a0db720  in 0x4 out ?
D600  handler 0xfffffff00a0db844  in 0x4 out ?
D601  handler 0xfffffff00a0db968  in ? out 0x4
D602  handler 0xfffffff00a0dba10  in ? out 0x4
D603  handler 0xfffffff00a0dbaac  in ? out ?
D700  handler 0xfffffff00a0b243c  in 0x4 out ?
# 139 callbacks accepted by link_rpc_lookup
```

`D577` is absent — the D570-D579 switch has no case for it (`0xa0d6ee0`
jumps straight from D576 to D578), which is a good sanity check that the
emulation is following the real control flow rather than assuming a dense
range.

The three property-transfer handlers are now identified for this build by the
iOS 27.0 H17P symbolicated `AppleMobileDispH17P-DCP` kext: D126 calls
`UnifiedPipeline::setDCPAVPropStart`, D127 calls
`UnifiedPipeline::setDCPAVPropChunk`, and D128 calls
`UnifiedPipeline::setDCPAVPropEnd`. The AP handler checks at
`0xfffffff00919b8fc`, `0xfffffff00919ba00`, and `0xfffffff00919bb80` confirm
their wire shapes: D126 starts with a `u32` total size; D127 is at least
`0x1008` bytes (`data[0x1000]`, `u32 offset`, `u32 length`, with length at
most `0x1000`); D128 is a 64-byte NUL-terminated key. All three require an
output buffer of at least four bytes for status. These layouts also match
Asahi Linux's `dcp_set_dcpav_prop_*_req` structures and its `v13_3` callback
table (which the driver selects for firmware 13.5); the iOS 27 identification
comes from the matching H17P kext rather than extrapolating Linux's older
firmware support.

This identification gives names and argument shapes, not proof that this
firmware emits the callbacks during the observed boot or what property values
it sends. The D128 H17P path has an `OSUnserializeXML failed` error string;
Asahi's older Linux parser uses a different binary format, so do not reuse its
property payload as an iOS 27 example. Other entries in the table remain
unnamed semantic-wise; the disassembly only establishes their accepted names
and argument constraints.

## 3. What the model implements

`qemu-sptm/hw/arm/darwin_iomfb.c`, all behind `DARWIN_DCP_IOMFB` as before:

- the **transport** for the callee direction: build a request in the inbound
  window, send class 2 / subkind 0 with bit 8 set, correlate the AP's class-2 /
  subkind-1 completion by tag, log the status and the bytes the AP wrote back.
  Every field of that is sourced above.
- an **experiment harness**, `DARWIN_DCP_IOMFB_CB`, which issues a scripted
  list of callbacks once the AP goes quiet. It is not a model of anything: it
  exists so a boot can answer "does the AP accept this name, and what does it
  do next", which is the only way the next gate gets named.

It does *not* implement any callback's semantics, and it does not send any
callback by default.

## 4. Measured: callback transport and nested AP work

Five boots, `-enable dcp`, `DARWIN_DCP_IOMFB=4`, differing only in what the
model sends. Logs in `/tmp/dvm/iomfb2/probe/`. All reach a shell with 0 panics
and 11/11 AFK endpoints.

| tag | what we sent | what the AP did |
|---|---|---|
| `R5` | nothing (`DARWIN_DCP_IOMFB` unset) | 0 `iomfb:` lines, 0 `ep 0x37` lines |
| `L4` | level 4, no `_CB` script | 0 callbacks sent; the A-series flow is unchanged |
| `CB3` | `D000`, bit 9 clear | answers `0x...0102`, nothing further |
| `CB5` | `D000`, bit 9 set | answers `0x...0102`; **request window read back unchanged** |
| `CB6` | `D000`, then one class-2/subkind-1 on the same slot | `0x...0342`, status 0, output `01 00 00 00` |

The `CB6` exchange in full:

```
iomfb: ep 0x37 callback #1 'D000' (0x44303030) in 0 out 4 -> heap+0x40000 (tag 0) size 0x10
iomfb: IOP -> AP ep 0x37 0x0000001000000302  (class-2 callback 'D000')
iomfb: AP -> IOP ep 0x37 0x0000001000000102 | class 2 subkind 0 ack 1 flag9 0 tag 0
iomfb:   cb window after +0000: 30 30 30 44 00 00 00 00 04 00 00 00 00 00 00 00
iomfb: IOP -> AP ep 0x37 0x0000000000000142  (PROBE kick: class-2 subkind-1 on our own slot)
iomfb: AP -> IOP ep 0x37 0x0000000000000342 | class 2 subkind 1 ack 1 flag9 1 tag 0 payload 0x0
iomfb:   callback 'D000' completed, status 0x0, out: 01000000
```

### Why this is the guest doing the work, not us seeing what we hoped

Two independent checks, because "no failure appeared" is not evidence:

1. **The completion is arithmetically derived from our request.**
   `rpc_callee_gated` builds its reply as `(stored_header & 0xff3e) | 0x40`
   (`0xa0cf298`-`0xa0cf2a8`). Our request header was `0x302`, and
   `(0x302 & 0xff3e) | 0x40 == 0x342` — exactly what came back. Note the
   message that *triggered* the reply carried bit 9 **clear** (`0x142`), so
   `0x342` cannot be an echo of it; it can only have been rebuilt from the
   header the AP stored when it took our request. The reply is the handler's.
2. **The output byte is one we never wrote.** We zero the whole request
   window before sending, and the `cb window after` dump in `CB5` proves the
   AP had not touched it. In `CB6` the same region reads `01 00 00 00`, which
   is what `D000`'s handler at `0xfffffff00917a634` computes and stores with
   `strb w8, [x19]` (`0xfffffff00917a674`).

Read together, `CB5` and `CB6` also rule out the lazy reading of `CB5`: the AP
had received and acknowledged the request there too, and still ran nothing.

### The named next gate

An inbound class-2 request is **not** dispatched on arrival. The AP answers
with `class 2 / subkind 0 / ack 1 / bit 9 cleared`, carrying our own offset and
size back, and then stops. `rpc_callee_gated` only runs after a further
class-2 / **subkind 1** message on the same slot.

The old `CB6` interpretation was wrong. Its apparent `PROBE kick` was a
synthetic class-2/subkind-1 message generated by the harness, not evidence of
a firmware “reflection” or third leg. The immediate-kick experiment is
negative: QEMU commit `a7045ca` caused the guest's first panic to be
`rpc_callee_gated: message is not ok: 0x142` at `link_protocol_priv.h:921`.
The synthetic kick must not be used as a model or recommended fix.

The corrected positive control sends `D120` after `A353` and lets the AP drive
the nested transaction. In `/tmp/dvm/probe/IOMFB_NESTED_D120_FIX1.stderr.log:111-176`,
the callback-context AP requests are `A389`, `A454`, `A103`, `A453`, `A104`,
`A105`, `A024`, `A478`, `A032`, and `A000`; the model answers each, then the
AP sends the genuine `D120` completion with status `0` and output
`00000000`. The `D120` handler is at `0xfffffff00919b338`; zero is expected
for this zeroed probe, not a failure. The relevant addresses are `boot` at
`0xfffffff009188040`, the first `A389` wrapper at `0xfffffff00917f11c`, and
the link geometry at `0xfffffff00a0cf654-0x6e`.

The run reaches `Early boot complete` at
`/tmp/dvm/probe/IOMFB_NESTED_D120_FIX1.serial.log:267`, reaches the shell, and
has no `panic(` line. This proves nested callback transport and the real
handler/completion path; it does not prove `default_fb_gated` or scanout.

Historically, the synthetic second message took
`link_handle_message` to `0xa0cfd0c`, whose slot lookup finds the slot *in
use* (we claimed it with the request), so it falls to `0xa0cfda0` and the
lambda at `0xa0d03f8`, which stores the word at `slot+0x48` and then calls
`[slot+0x58]`'s `vtable+0x108` — a wake. Two readings fit equally well:

- **a wake we are tripping by accident**, and the real dispatch trigger is
  something else entirely that we have not found; or
- **the protocol's third leg**, i.e. inbound requests are a three-message
  handshake (request, receipt, confirm) and a real DCP sends the third too.

The corrected D120 run is the transport-positive control; it supersedes the
synthetic-kick recommendation.

## 5. Historical sweep: 23 zero-input callbacks

Probe `SW1`, one boot, `DARWIN_DCP_IOMFB=4 DARWIN_DCP_IOMFB_CB_KICK=1` and a
23-entry script of every enumerated name whose handler declares no input
guard. 312 serial lines, 0 panics, shell reached, 11/11 AFK endpoints.

**All 23 completed with status 0**, and each returned a different value, which
is the strongest available evidence that the dispatch is real and per-name
rather than a fixed reply we are misreading:

| returns 1 | returns 0 | other |
|---|---|---|
| D000 D001 D107 D108 D110 D206 D207 | D101 D109 D111 D112 D120 D121 D122 D123 D133 D200 D203 D601 D602 | D204 -> `02`, D209 -> `0616000000000000` (0x1606) |

D002 has no output and completed cleanly. No name was rejected, so
`link_rpc_lookup` resolving these 23 is confirmed live, not only by emulation.

This is historical harness evidence only; the synthetic kick is not a model.
**The negative result still matters.** After all 23, the
AP issued no further A-series RPC: the boot still shows only `A401`, `A465`,
`A353`, exactly as it does with no callbacks at all. So none of the
zero-input getters is the event that resumes the pixel path. Whatever restarts
the AP is either one of the callbacks that carries a real input struct (the
`0x44`-`0x730`-byte ones, which we cannot fill in without deriving their
layout) or is not a callback at all.

That bounds the next search usefully: it is no longer worth sweeping the
getters, and the `in`-carrying names are where the remaining signal is.

## 6. D120 layout initialization and the first real IOSurface

The D120 callback's layout phase is now understood well enough to exercise the
real default-surface path.  Its boot-side gate is at
`0xfffffff009188074`; when `A454` returns one, the writer at
`0xfffffff009187e24` fills the D120 layout state and returns at
`0xfffffff009188030`.  The successful experiment supplied this exact 44-byte
`A033` response (FourCC `ARGB` in the wire representation, followed by the
one-plane/4-byte format fields):

```
4152474200000000000000000000000000000000000000000000000000000000000000000000000001000000
```

The trace shows the writer changing the state from zero to a descriptor whose
leading fields are `41524742 01000000 04000000`, followed by the D586 request
and local create path (`/tmp/dvm/PERSIST_DCP_A033_BGRA_1.iomfb.jsonl:50-66`).
For 1179x2556, D586 computes a required `0xb8c000` bytes; the layout capacity
getter at `0xfffffff02a0c0684` returns `0xc00000`, and the simple layout path
at `0xfffffff02a0c0954` completes.  The local-create trace records return
`w0=0x00b8c000` and a subsequent surface store.  The guest confirms the
corresponding decision at `/tmp/dvm/probe/PERSIST_DCP_A033_BGRA_1.serial.log:326`:

```
layout_buffers: size_override = 12107776 > max = 12582912, proceeding with default size = 12107776
```

This is evidence that the default IOSurface object is created and published;
it is not evidence of a visible frame or scanout.

The `A385` experiment remains negative.  `A385=01` is an unnamed boolean
response, not a decoded display semantic.  In
`PERSIST_DCP_A385_ONE_2`, the guest reaches Early Boot, emits the DCP hotplug
notification (`/tmp/dvm/probe/PERSIST_DCP_A385_ONE_2.serial.log:654,1395`),
and produces four identical blank screendump hashes, but SpringBoard is later
reported as a critical crashed process (`:11057`).  There is no panic.  Thus
the response does not fix the persistent display path; this run still emitted
4,971 A385 requests.  Welcome/Hello and scanout remain unproven.

## 7. Live-source cross-check and capture path

### DCP-side producer recovered statically

The matching larger-task Mach-O (`/tmp/dvm/gate232-firmware/machos/dcpfw.macho`)
does contain the producer that was missing from the earlier trace-only
analysis. In the reconstructed image, `0x4288a60` gathers property objects
from its display-controller object and stores them in per-property fields.
The send block at `0x42893ac` checks those fields individually, then calls
`0x4289694` once per present property. Its key pointers resolve directly in
the image's constant data:

| Send call | Key | Property object field |
|---|---|---|
| `0x42893c0` | `ColorElements` | `self+0x5c78` |
| `0x42893d8` | `TimingElements` | `self+0x5c58` |
| `0x42893f0` | `PreferredTimingElements` | `self+0x5c70` |
| `0x4289408` | `DisplayAttributes` | `self+0x5c80` |
| `0x4289420` | `Transport` | `self+0x5c88` |

The transfer helper at `0x4289694` constructs the three wire calls observed
on the AP side: `D126` with a four-byte total size, `D127` with a fixed
`0x1008`-byte input containing up to `0x1000` data bytes plus offset and
length, and `D128` with a 64-byte key. It sends chunk offsets in order and
uses `min(remaining, 0x1000)` for each chunk. The D126 size field is the
helper's serialized byte count plus one; the D127 lengths cover the serialized
bytes themselves, which accounts for the `length - 1` convention in m1n1's
older manager. This identifies who constructs the callback sequence and its
wire framing, but not the serializer's actual output or its XML contents.
`tools/re/make_dcpav_prop_cb.py KEY BODY` packages a raw serialized body
(without the terminating NUL) into the QEMU callback-script format. The
existing `setup_gate_probe.sh` accepts `DARWIN_DCP_IOMFB_CB` as an override;
for scripts too large for one environment value, QEMU also accepts
`DARWIN_DCP_IOMFB_CB_FILE` and reads the script from that host file. Each
D126/D127/D128 ACK must be checked
individually: the QEMU script continues after a failed ACK. An ACK and even a
registry property establish only AP acceptance of the supplied bytes; they do
not demonstrate that this DCP firmware built or emitted those bytes. There is
no verified one-element H17P `ColorElements` body in this checkout yet.

The per-element ColorElement dictionary and the outer DCP wire wrapper are
separate formats. `0x41be504` installs the BSS AFKArray vtable twin: its
`+0x20` slot uses PAC `0x5b0a`, and its `+0x30` `da66` slot reaches the AFK
metadata serializer at `0x41c8ed4`. That serializer emits the AFK `class` /
`id` / `retain` / `children` structure; its children path boxes AFKObject IDs
and does not embed the ColorElement property dictionary. The exact class and
children encoding for a DCP-wire `ColorElements` AFKArray remains unbuilt.

The AP-side experiment is intentionally a different candidate named
**`consumer-element-array`**: OSSerialize XML containing an array with one
dictionary in the shape emitted by ColorElement `vt+0x48` (`0x411ce98`). It
tests whether the AP parser/registry and mode-provider consumer accept the
inner property shape. It is not the AFKArray DCP wire body and cannot show that
the DCP builder at `0x413f790` ran. Its ElementData stack tail is zero-padded
only as a test choice; the runtime mode remains uncertain (`0` versus `0xd`).

The exact candidate and its measured run are recorded below. Keep the
`consumer-element-array` label on its body, callback script, and result
summary so this AP consumer test cannot be mistaken for a DCP-wire capture.

For the fallback kind-8 element, `0x413f790` initializes this descriptor before
calling `0x411d210`: `kind=8`; `mode=0` or `0xd`; the qword at descriptor `+8`
is `0x0000001000000000`; the u32 at `+0x10` and byte at `+0x14` are zero.
The ctor copies the full 32 bytes at `0x411d2b0`, so descriptor bytes `+0x15`
through `+0x1f` are stack residue in this recovered code, not proven zeros.
The object allocator (`0x41c4848` → `0x4024108` → zero-fill helper
`0x40031fc`) does zero the object before construction; it does not make those
copied descriptor bytes deterministic.

The element serializer `0x411ce98` (through base serializer `0x4119400`)
supports this field inventory. Values below are the fallback values; `mode`
remains selected at runtime:

| Key | Value / source | Evidence |
|---|---|---|
| `Score` | `240999` for mode 0; `200999` for mode `0xd` | `0x411c83c` score table + `0x411d2c8`–`0x411d2dc`; emitted by `0x4119400` |
| `ID` | 0 | base init creates OSNumber(0) at `0x41195ec`–`0x41195fc` |
| `IsVirtual` | 0 | base serializer reads `+0x24`; zero-filled allocation, no fallback write |
| `ElementType` | 1 | `0x411cecc`–`0x411cee0` |
| `ElementData` | 32-byte data below | `0x411cf00`–`0x411cf0c` |
| `Depth` | 8 | descriptor word 0 |
| `PixelEncoding` | `mode` (0 or 13) | descriptor word 1 |
| `DynamicRange` | 0 | descriptor word 2 low u32 |
| `EOTF` | 0 | descriptor word 3 |
| `Colorimetry` | 16 | descriptor word 2 high u32 |
| `SupportsDSC` | 0 | descriptor byte `+0x14`, masked to 2 bits |
| `StandardType` | 0 | ctor `w1=0` stored at element `+0x48` |
| optional white-point/luminance/HDR fields | absent | fallback passes null optional pointers |

Known bytes, in order, for the first 21 of 32 `ElementData` bytes are:

```text
08 00 00 00  00 00 00 00  00 00 00 00  10 00 00 00
00 00 00 00  00
```

The next eleven bytes are not initialized in the descriptor. A zero-padded
32-byte form is only a convenient canonical test candidate, not a
byte-accurate H17P capture.

### AP consumer replay with TimingElements

The `consumer-element-array` test first sent only the ColorElements body in
`UI_COLOR_CONSUMER1C`. All three D126/D127/D128 callbacks completed with
status zero and `01000000`; BackBoard read ColorElements as a non-null CFArray
(`type_match=True`), while TimingElements was null and the signal-type-1 mode
provider returned `0xe00002bc` with no mode reference.

The first combined attempt, `UI_COLOR_TIMING_CONSUMER1`, loaded only the first
three callback entries because its six-entry inline environment value was
truncated. It is a repeat Color-only result, not a Timing measurement. The
file-backed replacement `UI_COLOR_TIMING_CONSUMER2` parsed all six entries
from `/tmp/dvm/consumer-element-array.ColorPlusTiming.synthetic640x350.callback.txt`:

| AP-visible score | `UI_COLOR_TIMING_CONSUMER2` result |
|---|---|
| D126/D127/D128 transport | Both properties completed all three callbacks with `status 0x0`, `out: 01000000`; D126 totals were `0x2ef` (Color) and `0x987` (Timing). |
| Registry properties | ColorElements and TimingElements were both non-null CFArray (`property_type=0x13`, `type_match=True`). |
| Mode provider | Signal type 1 returned `status=0`; mode array count was one and the timing/mode reference was non-null. |

This clears the previous AP mode-provider `0xe00002bc` result for this
synthetic pair. It does **not** mean the display installed the candidate
640x350 mode: subsequent display-mode handling still used the all-ones
sentinel `0xffffffffffffffff`. Thus the measured result is parser/registry
acceptance and provider success, while real mode selection and visible pixels
remain unproven.

The tested Timing body is
`/tmp/dvm/consumer-element-array.TimingElements.synthetic640x350.xml`.
`0x411bb1c` establishes `ElementType=0` and an 0x50-byte ElementData field;
`0x4122640` establishes the nested attribute names. For this v1 consumer probe,
ElementData is 80 zero bytes as an opaque placeholder, and the attribute
dictionaries use active/total 640x350 with other values zero. The nearby
640x350 tuple is a compact mode-list entry, not an 0x50-byte record. The v1
body also omits `IsInterlaced`, `IsSplit`, and `PreciseAspectRatio`; these
remain serialization-shape gaps to address before treating it as a close
model of the firmware output. The pair is not DCP AFKArray wire data and does
not prove that `0x413f790` ran or built either property.

Run evidence is in `/tmp/dvm/probe/UI_COLOR_TIMING_CONSUMER2.stderr.log`,
`/tmp/dvm/UI_COLOR_TIMING_CONSUMER2.lldb.log`, and
`/tmp/dvm/probe/UI_COLOR_TIMING_CONSUMER2.serial.log`. The 300-second profile
finished without an XNU panic; it did not reach a shell.

### QuartzCore single-entry fallback

The `0x13` type check in `_kern_GetSupportedDigitalOutModes` is CFArray, not
CFData: the local CoreFoundation runtime reports `CFArrayGetTypeID() == 19`
and `CFDataGetTypeID() == 20`. The replayed top-level `<array>` is therefore
accepted as an array of element dictionaries, and the kernel-backed provider
returns those arrays to QuartzCore. Earlier notes calling these outputs
CFData were incorrect.

Static control flow in `IOMFBDisplay::update_framebuffer_locked` at
`0x1847af1f8..0x1847af5f0` makes the fallback condition more specific. It
calls `CFArrayGetCount` on the ColorElements array; a count greater than one
sets the multi-entry flag. Otherwise it checks the TimingElements array and
sets the same flag only when that count is greater than one. The later gate
sequence at `0x1847af29c..0x1847af31c` reads cached APT and virtualization
state alongside the multi-entry flag; the `+0xe10` once token and `+0xc58`
APT byte are not default/preferred-mode fields. When control reaches
`0x1847af5e0`, it constructs `Mode(-1)` and adds it to the mode set. Thus IDs,
scores, and `ColorModes` do not by themselves change the array-count branch;
the current trace shows one Color entry and reaches the single-entry route.
Current-mode evidence remains the returned mode set and the display object's
`+0x2b8/+0x2c0` fields.

The next replay keeps the same one-element geometry and `ElementData`, but
uses Color ID 1, Timing ID 1, positive Timing Score 1, `IsPreferred=true`,
and `ColorModes=[1]`. It is a semantic-coherence probe, not a panel-native
mode. `UI_COLOR_TIMING_IDS1` sent the file-backed six-entry script; all six
D126/D127/D128 callback completions returned status zero and `01000000`. Both
provider outputs passed the CFArray type check and the provider returned
status zero. The Color and Timing arrays each had count one. QuartzCore set
the multi-entry flag to zero and inserted `Mode(-1)`, keeping the all-ones
sentinel. An offline symbol check below corrects the earlier interpretation
of the fallback-gate samples: the logged `0xdb22` at `+0xe10` was not a
default-mode global, and the `preferred=0` label at `+0xc58` was not a
preferred-mode flag. The coherent IDs and serialized `IsPreferred` bit
therefore did not change the measured mode result. The fallback happens
before the one-entry element IDs are used by `update_digital_modes`.

Run evidence is in `/tmp/dvm/UI_COLOR_TIMING_IDS1.lldb.log`,
`/tmp/dvm/probe/UI_COLOR_TIMING_IDS1.stderr.log`, and
`/tmp/dvm/probe/UI_COLOR_TIMING_IDS1.serial.log`. GATE252's bounded object
headers plus both `CFArrayGetCount` callsites now expose exactly what the
provider returned and which QuartzCore branch was taken. The actual display
mode remains represented by the returned mode set and the display object's
mode fields, not by the three policy/cache globals corrected below.

#### P276 offline correction — fallback globals are APT/virtualization state

The saved QuartzCore Mach-O at `/tmp/dvm/qc-extract/QuartzCore` resolves the
addresses read at the single-entry fallback gate through its symbol table:
`0x1e7058c58` is
`__ZZ19CADeviceSupportsAPTE12supports_apt`, `0x1e7058e10` is
`__ZZ19CADeviceSupportsAPTE4once`, `0x1e7058600` is
`__ZZ21CADeviceIsVirtualizedE4once`, and `0x1e7053bf9` is
`__ZZ21CADeviceIsVirtualizedE14is_virtualized`. At
`IOMFBDisplay::update_framebuffer_locked`, `0x1847af29c..0x1847af2b4`
checks the `CADeviceSupportsAPT` once state and cached support bit;
`0x1847af2b8..0x1847af31c` checks the `CADeviceIsVirtualized` once state and
cached virtualization bit. The once values and support bit are not
default/current display-mode globals. Earlier probe fields `default_e10` and
`preferred_c58` were misleading names; new rows use
`supports_apt_once_e10` and `supports_apt_c58`. The old `0xdb22` sample at
`+0xe10` is retained as a once-state observation, not a mode identifier.

The initializer blocks expose their input strings. The CFString at
`0x1e90712c0` has length 15 and points to the `__cstring` literal
`SupportsPerseus` at `0x1848203f1`. The block
`___CADeviceSupportsAPT_block_invoke` (`0x1844b3b08`) passes that object to
`0x1881509a0` and stores its returned byte at `0x1e7058c58`; it then passes
the separate literal `CA_DISABLE_APT` (`0x184820401`) to `0x1881383b0`,
converts a non-null result through `0x188148f60`, and clears the support byte
when that conversion is true (`0x1844b3b2c..0x1844b3b44`). The helper APIs
are not assigned names here. Likewise, the CFString at `0x1e90715c0` has
length 15 and points to `IsVirtualDevice` at `0x1848206b0`; the block
`___CADeviceIsVirtualized_block_invoke` (`0x18453bea0..0x18453bec4`) passes
it to `0x1881509a0` and stores the returned byte at `0x1e7053bf9`. These
are explicit preference/configuration inputs to cached policy bits, not
panel timing or display-mode payloads.

This static mapping explains which policy/cache predicates feed the
single-entry fallback, but does not establish why runtime APT or
virtualization state has its observed value, nor does it provide native panel
mode data or a DCP response. Mode evidence still comes from the mode-set
contents and display-object `+0x2b8/+0x2c0` samples. The source of a real
panel-native ColorElements/TimingElements pair remains open.

#### P276 home follow-up — accepted dismissal does not reach sampled render server

P276 completed the measured shell and Home control path on one fresh
replay2b child. `DM_PERFORM_RETURN`, BKS migration/check-in returns, and
`SHELL_SERVICE_RETURN` were recorded; SpringBoard returned a non-null ADFL
result and Home entity, and `SB_WORKSPACE_EXECUTE_TRANSITION_RETURN` was
`x0=1`. The same Recap pan recognizer returned `shouldBegin=true`, began and
ended, and BackBoard emitted `ACCEPTED_LOCKSCREEN_GESTURE_TRANSITION` with
`for_user_gesture=1` at `1791796340.687735`. No passcode was submitted.

The accepted transition was followed by 72 SpringBoard
`CA_COMMIT_ENTRY` callbacks through guest time `1791796751.388764`. The
post-transition HMP sample at 420 seconds was still uniformly black, as were
all 16 captures: 1179x2556, `nonblack_fraction=0`, SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. The
CA commit-return breakpoint disabled itself after its first hit, so the 72
entries do not establish that 72 commits completed and this run cannot name a
first incomplete commit. The follow-up probe retains the return breakpoint
and pairs each return to an entry by thread, stack pointer, and saved entry LR.
armed `SERVER_RENDER_UPDATE_ENTRY`, `DISPLAY_RENDER_DISPLAY_ENTRY`,
`SERVER_OGL_RENDER`, IOSurface create/allocate, and framebuffer set-layer /
handoff observers each recorded hits before the gesture but none after it.
This bounds the measured gap between continuing SpringBoard commits and the
sampled render/swap path; it does not prove that every unobserved CA transfer
or render path is absent. The next test should correlate one post-transition
SpringBoard commit/transaction with a render-server receipt, keeping the same
post-dismissal observer window. It should not force a mode or infer DCP
firmware behavior from the black HMP frame.

The child and full recovery bundle are preserved at
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin276-recap-diagnostic-home1.qcow2`
and
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN276_RECAP_DIAGNOSTIC_HOME1/`.
The child SHA-256 is
`6d82839b8dba9457e23d38d7a1d6503c25e1b408c25a1e4ec5a1dbf4d059586a`; the
recovery record reports a clean `qemu-img check -U`.

`UI_COLOR2_TIMING1_CONSUMER1` duplicated the Color dict while giving the
second XML object distinct serialization IDs; both Color elements retain
semantic ID 1 and Score `0x3ad67`. Timing is the unchanged single synthetic
element. The file-backed script still has six D126/D127/D128 callbacks, all
with transport status zero and `out: 01000000`. The AP returned both
properties as CFArray. QuartzCore measured Color count 2, set `w24=1`, and
entered `update_digital_modes` (`0x1847b1930`) without hitting
`FALLBACK_MINUS_ONE`. Its output mode set was empty at the return and at
`DISPLAY_MODE_SET_ENTRY`; this cleared the earlier singleton gate but did not
install a mode. The repeat `UI_COLOR2_TIMING1_GATEBYTE1` used the same payload
and measured `display+0x755b == 1` at entry. The bit-0 check at
`0x1847b1a2c` therefore did not prevent processing.

`UI_COLOR2_ATTR_GATE1` measured `x25=1` at `0x1847b1e9c`, so that branch
does not skip the mode loop. The saved `x25` is the count returned by
`CFArrayGetCount` at `0x1847b1a34`: `x4` is the one-entry Timing array. The
two-entry Color array in `x3` is counted at `0x1847b1a44` and iterated
earlier at `0x1847b1dd0`. The next loop starts at `0x1847b1eec` with one
Timing entry. Thus the empty output follows entry into per-element processing.
The property fetch at `0x1847b1d54` uses the `DisplayAttributes` CFString
key object `0x1e9060ce0`, but its result is separate from the `x25` count
gate. A `PreferredTimingElements` fetch occurs at `0x1847b1c88`.

`UI_COLOR2_TIMING_LOOP1` measured the DisplayAttributes result at
`0x1847b1d6c`: returned `x0` and saved `x20` were both zero. Despite the
missing property, the one-entry Timing loop ran (`index=0`, `count=1`), its
element and Horizontal/Vertical attribute dictionaries were non-null, and
the first filter at `0x1847b1fa4` had mask `0xffffffff` with bit zero set.
It later reached `0x1847b2094`, the skip/increment join, and returned an
empty mode set. The null DisplayAttributes result did not itself skip this
Timing element.

`UI_COLOR2_MODE_PATH1` narrowed the discard path with the same payload. It
hit `0x1847b2034` then `0x1847b20a4`, not the early `0x1847b2090` skip.
It continued through `0x1847b219c` and `0x1847b23b4`; at the latter the
nested array count returned one, so its null-array exit was not taken. The
inner loop subsequently exhausted that one entry and reached
`0x1847b3690` -> `0x1847b37a4` -> `0x1847b2094`, leaving the output
mode set at count zero. The next question is which check within the inner
loop (`0x1847b2548` through `0x1847b3680`) rejects this synthetic
candidate. This run does not establish that DisplayAttributes is irrelevant
to that later check.

`UI_COLOR2_COLOR_WALK1` and `UI_COLOR2_COLOR_TREE1` followed that inner
candidate. The candidate entered `0x1847b2548` with a live 640-pixel
horizontal active (`x21=0x280`). Its fetched selector `w28` was zero at
`0x1847b25f8`, so it took `0x1847b27bc`, then `0x1847b2d14` with
`[sp+0xf0]=0`. It passed `0x1847b2d88` and `0x1847b2dd0`. The lookup root
at `[sp+0x170]` was non-null at `0x1847b2e24`, but the path exited to
`0x1847b3680` before reaching `0x1847b2e54`. The tree is built earlier from
the two Color entries (`0x1847b1dd0` onward). `UI_COLOR2_COLOR_MATCH1`
measured the failed comparison at `0x1847b2e2c`: sought `0`, tree key `1`;
the search reached `0x1847b2e40` (no match) then `0x1847b3680`. The
synthetic Timing body had `ColorModes=[1]`. The nested code looks up keys
on each `ColorModes` entry, including `ID`, `IsVirtual`, `PixelEncoding`,
`Depth`, `Colorimetry`, `EOTF`, and `DynamicRange`; an integer entry therefore
returned zero for `ID`. The QuartzCore CFString at `0x1e9076240`, queried
at `0x1847b214c`, decodes to `ColorModes` in the extracted Mach-O.

`UI_COLOR2_COLORMODE_DICT1` tested a full ColorElement dictionary nested
under `TimingElements.ColorModes`. Its Timing D126/D127 completed with
transport status zero, but D128 returned `00000000`, and the serial log
reported `OSUnserializeXML failed`; the Timing property was not accepted.
This is a parser result, not a mode-selection result.

`UI_COLOR2_COLORMODE_MIN1` changed only that one `ColorModes` child in the
previously accepted Timing XML: the integer with object ID 4 became a small
dictionary with the same object ID and `ID=1`, `IsVirtual=false`,
`PixelEncoding=0`, `Depth=8`, `Colorimetry=16`, `EOTF=0`,
`DynamicRange=0`, and `StandardType=0`. All six callback completions had
transport status zero and `out: 01000000`. The AP mode provider returned
non-null Color and Timing CFArray properties with matching type IDs and
status zero. The inner lookup sought `1` and matched tree key `1` at
`0x1847b2e2c`, then passed `0x1847b2e54` and reached `0x1847b3564`.
`update_digital_modes` returned a mode set with **one entry**,
`0x1080000000578280`; `set_all_modes` and `DISPLAY_MODE_SET_STORE` received
that value. The preceding `fetch_current_mode` returned `-1` *before* the
store; `+0x2b8` and `+0x2c0` were also `-1` at the setter's entry. Static
code at `0x184681c38` and `0x184681c3c` stores the two mode arguments into
those fields, and later scalar-call hits show execution continued past the
stores. This establishes the AP display object's mode-set update, not a
post-update current-mode read. A separate downstream checkpoint remains
necessary to establish selected-current semantics and any hardware-facing
modeset.
No DCP firmware producer, physical modeset, scanout, or pixels were proven.
This remains a `consumer-element-array` AP parser/selection experiment with
synthetic 640x350 attributes and zero-filled Timing `ElementData`.

`UI_COLOR2_POST_STORE1` replayed a callback script regenerated from the
saved fixtures and byte-identical to MIN1's script. At
`DISPLAY_MODE_POST_STORE` (`0x184681c40`), display fields `+0x2b8` and
`+0x2c0` both read `0x1080000000578280`; they still held that value at the
caller checkpoint `0x1847af678`. This confirms that the setter arguments
stuck in the AP display object. The prior `fetch_current_mode` call returned
`-1` before these stores. The later IOConnect scalar call on the same
connection used selector `0xf` with two inputs `[0,16]`; static disassembly
identifies its caller as `_kern_SetDebugFlags` (`0x22a3c3234`), not a
digital-out modeset. `_kern_SetDigitalOutMode` is a separate function at
`0x22a3c3570` using scalar selector `0x17` with two inputs. No selector
`0x17` call was observed before this bounded probe stopped, so DCP mode
application remains open.

`UI_COLOR2_DIGITAL_OUT1` continued the same fixture replay until
`_kern_SetDigitalOutMode` (`0x22a3c3570`) ran. Immediately before that,
the display object still had `+0x2b8=+0x2c0=0x1080000000578280` at the
post-store and caller-return checkpoints. The kernel-backed setter received
`color_id=1`, `timing_id=1`; its `IOConnectCallScalarMethod` used selector
`0x17` with exactly two input scalars `[1,1]` on the mode provider's
connection. Thus the synthetic mode did advance from AP mode-set population
to the kernel digital-out request with matched IDs. This entry-triggered
stop did not synchronously freeze the guest at that instruction: the watcher
stopped QEMU after noticing the log, so later RPCs in its model trace cannot
be classified as strictly pre-call. It did not establish DCP acceptance,
scanout, or pixels.

`UI_COLOR2_SEL17_RETURN1` replayed the same fixtures with return hooks:
`_kern_SetDigitalOutMode(color_id=1, timing_id=1)` sent scalar selector
`0x17` with `[1,1]`; both `IOConnectCallScalarMethod` and its wrapper returned
`0x0`. `SEL17_SHIM1` then installed kernel breakpoints at the alternate
`0xfffffff00a0e4e10` forwarding thunk and
`IOMobileFramebufferShim::set_digital_out_mode` (`0xfffffff00919114c`).
Neither fired before the wrapper returned. Those are not the implementation
used by this `AppleCLCD2` connection.

`UI_COLOR2_SEL17_DISPATCH1` hit `IOMobileFramebufferUserClient`'s
`externalMethod` at `0xfffffff00a0e5820` with selector `0x17`, then the
scalar handler at `0xfffffff00a0e6c80`. The handler read two 32-bit IDs
from its input array as `(1,1)` and dispatched through owner vtable slot
`+0x5d8` (PAC `0x6110`). `UI_COLOR2_SEL17_OWNER1` read the owner at
`userClient+0xd8`; its vtable resolves to `AppleCLCD2` at
`0xfffffff00808c778`. That class inherits slot `+0x5d8` from
`IOMobileFramebufferAP`, target `0xfffffff00a0c94bc`. The target ran with
`w1=1, w2=1` and constructed RPC input as two u32 IDs on the stack
(`0xfffffff00a0c94d0`–`0xfffffff00a0c94d4`). At
`0xfffffff00a0c9500`–`0xfffffff00a0c9510`, it calls its virtual RPC method
at `+0x8b0` with FourCC `A413` (`0x41343133`), input length eight, output
length four. Its output word was `0` after the RPC; it returned `0`, as did
the IOConnect call and userspace wrapper.

The same `OWNER1` QEMU trace has `A413` RPC `#10`, input
`01 00 00 00 01 00 00 00`, followed by an **emulated** class-2 completion
with status `0` and zeroed output. This identifies `A413`, not the earlier
`A412`, as the digital-out request carrying the selected Color/Timing IDs in
this run. It establishes the AP-to-emulated-DCP request and model reply;
QEMU does not run the H17P DCP firmware, so this is not a firmware ACK,
completed modeset, scanout, or pixels. The later `A479`–`A500` requests in
`SEL17_RETURN1` appear only after thousands of `A385` polls and are not
needed to identify the immediate selector path.

The four-byte `A413` output is an **IOMFB error code**, independent of the
class-2 transport completion. The AP method initializes it to `0xaaaaaaaa`,
calls the RPC, then loads the output word at `0xfffffff00a0c9514` and passes
it through the GOT stub at `0xfffffff00a0dc4a0`. Its target
`0xfffffff00a0ed538` maps output `1`–`18` through a fixed IOReturn table;
`0` and values outside that range return `0`. In particular, output `1`
becomes `0xe00002bc`, while model output `0` becomes success. The AP method
does not use the RPC function's direct `w0` after the call. The QEMU model
currently forces transport status `0` and, without an `A413` override, writes
four zero bytes. Thus `OWNER1` proves how **this model reply** makes the
IOConnect selector succeed, not what a real H17P DCP would return.

| A413 output | Kernel return |
|---|---|
| `0` | `0` |
| `1`–`6` | `e00002bc`, `e00002c7`, `e00002c9`, `e00002c2`, `e00002d5`, `e00002c0` |
| `7`–`12` | `e00002bd`, `e00002d8`, `e00002e2`, `e00002be`, `e00002e3`, `e00002d6` |
| `13`–`18` | `e00002eb`, `e00002f0`, `e00002d0`, `e00002c8`, `e00002c1`, `e0000001` |

The live QuartzCore caller is the direct call at `0x1847afdf4`. It saves the
setter result in `w26`, performs two cleanup calls, then `cbnz w26` at
`0x1847afe0c` branches to `0x1847b0f54` on error. Zero falls through to
`0x1847afe10` and continues mode setup. This establishes a downstream
dependency on the decoded A413 output; the `OWNER1` result was the zero arm.
`render_gate_callbacks.py` now marks the test and both branch destinations.
`UI_COLOR2_A413_ERR1` replayed the identical six-callback fixture script with
the established level-4 overrides plus `A413=01000000`. All six D126–D128
callbacks completed with transport status `0` and output `01000000`. The
synthetic mode was again stored in display `+0x2b8/+0x2c0`, and the kernel
method again received `(color_id=1,timing_id=1)`.

For **A413 itself**, the model sent class-2 transport status `0` and wrote
output word `1`. The AP method returned `0xe00002bc`, as predicted by the
table; `IOConnectCallScalarMethod` returned the same error. QuartzCore saved
`w26=0xe00002bc` at `0x1847afe0c` and hit the failure destination
`0x1847b0f54`, not the zero-result continuation. This measures the negative
arm of the AP consumer contract against an injected model error; it remains
silent about how real H17P firmware handles `(1,1)`.

One probe label needed correction: in `ERR1`, the old
`IOMFB_SET_DIGITAL_OUT_MODE_KERN_EXIT` breakpoint was at `0x22a3c360c`,
**before** `mov x0,x20`, so it printed a stale `w0=0` after the cleanup call.
The preceding IOConnect return and the subsequent QuartzCore `w26` both show
`0xe00002bc`; the hook now stops at `0x22a3c3610`, after that move. Evidence:
`/tmp/dvm/UI_COLOR2_A413_ERR1.lldb.log` and
`/tmp/dvm/probe/UI_COLOR2_A413_ERR1.stderr.log`. This bounded guest exited
with zero XNU panics.

`UI_COLOR2_MODE_RESET1` followed the zero-output A413 control beyond that
return. It accepted the same six property callbacks and stored selected mode
`0x1080000000578280` into the active display's `+0x2b8/+0x2c0`. A dynamic
write watchpoint on that exact `+0x2b8` address then caught
`0x184681cb0` storing `-1` into **the same display object**, while `+0x2c0`
still held the selected mode. This is a real current-mode reset after the
synthetic AP modeset, not confusion with an auxiliary display. The setter at
`0x184681c7c` compares old `+0x2b8` to the caller-supplied mode at `[x1]`
through `0x1843e3c44`; a non-match takes `tbz` to the store. Evidence:
`/tmp/dvm/UI_COLOR2_MODE_RESET1.lldb.log` and
`/tmp/dvm/probe/UI_COLOR2_MODE_RESET1.stderr.log`; zero XNU panics.

`UI_COLOR2_MODE_RESET_CALLER1` recovered the setter's saved return address:
`0x1847afe6c`, immediately after the call at `0x1847afe68` in the same
mode-update routine that called `_kern_SetDigitalOutMode`. The caller passes
`x23` as the source pointer; `0x1847afb8c` made that pointer
`display+0x6b00`. By the setter's store, `[display+0x6b00]` was `-1`.
`+0x2c0` still held the selected mode. This identifies the immediate source
of the overwrite but not why `+0x6b00` held `-1` after the modeled A413
success. The next useful comparison is `+0x6b00` just after selection,
immediately before A413, and at the setter, plus its intervening writer.
Evidence: `/tmp/dvm/UI_COLOR2_MODE_RESET_CALLER1.lldb.log`; zero XNU panics.

`UI_COLOR2_PENDING6B00_1` and `UI_COLOR2_POWER_LOOKUP1` close that
comparison. On the active display, `+0x6b00` starts at `-1`, then the
mode-update path writes selected mode `0x1080000000578280` at `0x1847af898`
*before* A413. A413's modeled zero output returns success. During the later
power-change/hotplug path, `0x1845be0a8` returns `-1`; `0x1845bdd54` copies
that result back into `+0x6b00`, then the mode-update caller passes
`display+0x6b00` to the current-mode setter. The latter overwrites `+0x2b8`
with `-1` while `+0x2c0` keeps the selected mode. Thus this is a later
state-refresh overwrite, not failure to store the selected mode initially.

`UI_COLOR2_HOTPLUG_MODE1` locates the lookup failure more tightly. The active
display's `getHotplugState` call returned IOReturn `0`, state `0`. The mode
lookup at `0x1845be0a8` received two zero keys, traversed its mode list,
reached the list-match branch at `0x1845be2c8`, and returned `-1` from the
matched node's `+0x10` value. The selected mode was still present in
`+0x2b8/+0x2c0/+0x6b00` at that point. The next contract question is why
the modeled display reports the zero-key, `-1` mode state after A413 success;
the zero A413 result alone does not establish a real current mode. The logs
are `/tmp/dvm/UI_COLOR2_PENDING6B00_1.lldb.log`,
`/tmp/dvm/UI_COLOR2_POWER_LOOKUP1.lldb.log`, and
`/tmp/dvm/UI_COLOR2_HOTPLUG_MODE1.lldb.log`; all three bounded guests exited
with zero XNU panics.

The queried IDs come through `_IOMobileFramebufferGetDigitalOutMode`
(`0x22a396884`), which requests GetBlock kind `0x41` through selector `0x4f`.
Its successful return copies block u32 fields `+4/+8` to the two output
pointers (`0x22a3968e4-0x22a3968ec`). The block reaches the model as A442
(`in=92`, `out=4100`): request `in+8=0x41`, requested copy length
`in+0x54=0x2c`, result prefix `out+0..0x2b`, trailing IOReturn at
`out+0x1000`. This is **not** A414, despite A414 occurring nearby in the
trace. The normal model zeros A442's result, so this AP call returns `(0,0)`.

`DARWIN_DCP_IOMFB_ECHO_MODE=1` is a deliberately opt-in **AP-consumer
probe** in `qemu-sptm/hw/arm/darwin_iomfb.c`. After an A413 whose modeled
output word is zero, it remembers that request's Color/Timing IDs and puts
them at `out+4/+8` only for later A442 block-`0x41`, length-`0x2c` reads.
It does not claim that real H17P firmware stores or reports IDs this way.
`UI_COLOR2_MODE_ECHO1` replayed the same six synthetic property callbacks:
the A413 input was `(1,1)`, the targeted A442 reply yielded lookup keys
`(1,1)`, and `0x1845be0a8` returned the selected packed mode
`0x1080000000578280` rather than `-1`. The power-change path retained that
mode in `+0x6b00`; `+0x2b8/+0x2c0` also held it at the lookup. Evidence:
`/tmp/dvm/UI_COLOR2_MODE_ECHO1.lldb.log` and
`/tmp/dvm/probe/UI_COLOR2_MODE_ECHO1.stderr.log` (zero XNU panics).

`UI_COLOR2_MODE_ECHO_RENDER2` and `UI_COLOR2_MODE_ECHO_A4081` continued
the same labeled probe. The active render path now passed its second V178
check with the mode still selected and natural display dimensions
`1179×2556`. `IOMFBDisplay::create_surface` ran; the IOSurface allocation
returned a non-null object, and an A407 RPC appeared. This is the first
measured natural surface allocation with the synthetic mode pair and
current-mode echo, without bounds or swap-register forcing. The earlier
`UI_COLOR2_MODE_ECHO_RENDER1` stopped during breakpoint installation because
its stop regex also matched `COMMAND_LIST_PROOF`; it is not render evidence.

The longer `A4081` run never sent A408. At `FINISH_SWAP_CLEAR_GATE`,
`[x23+0x10].bit0=1`; `FINISH_POST_CLEAR_CA14` had `w24.bit0=0`, and the
subsequent `swap_set_layer` calls all had null `x2` despite the saved
IOSurface. This is the clear-layer path, not a surface handoff. SpringBoard
crashed; launchd requested a reboot, which this QEMU configuration could not
complete and eventually reported as `Halt/Restart Timed Out`. That later
panic is not an A408 failure. Evidence:
`/tmp/dvm/UI_COLOR2_MODE_ECHO_RENDER2.lldb.log`,
`/tmp/dvm/probe/UI_COLOR2_MODE_ECHO_RENDER2.stderr.log`,
`/tmp/dvm/UI_COLOR2_MODE_ECHO_A4081.lldb.log`, and
`/tmp/dvm/probe/UI_COLOR2_MODE_ECHO_A4081.stderr.log`. The next gate is why
`finish_update` takes the clear-layer branch after a successful allocation;
the previously forced x2 experiments are in
`docs/re/quartzcore-render-bounds-followup.md` (GATE98–128).

The same run reached the active server's bit-17-clear `RFT_FULL_PATH`, but
the first `IOMFBDisplay::can_update` (`vt+0x178`) returned `0xe9` while the
mode was still selected and display state bytes were `(0,1,0,0,0,0)`.
That path skipped surface creation. Older longer controls show later
`can_update=0` after state bytes change, but then `+0x2b8=-1` and
`ignore_update_p` blocks the update. The selected mode and ready display
state have not yet coexisted in a measured full render update.

The successful input bodies are preserved at
`tools/re/fixtures/consumer-element-array.color2-ids1.xml` and
`tools/re/fixtures/consumer-element-array.timing1-colormode-dict.synthetic640x350.xml`.
`tools/re/make_dcpav_prop_cb.py` packages each body as D126/D127/D128;
concatenating the two scripts yields the six-callback replay.

### MODE_ECHO presentation eligibility A/B (synthetic AP consumer)

`UI_COLOR2_MODE_ECHO_PROVENANCE1` kept the original Color/Timing fixtures and
stopped at the first `finish_update` after a non-null 1179×2556 IOSurface
allocation. The current mode was `0x1080000000578280`, whose bits 55:59 are
`1`. `IOMFBDisplay::needs_passthrough()` (the active display's vtable `+0x6c0`,
implementation `0x18441968c`) returned zero; the join stored `w24=0`, so
`update_surface` was skipped. Separately, `display+0x7545` bit 0 was one and
the clear-layer calls passed null surfaces. The `+0x7545` byte is initialized
to one in the AppleDisplay constructor (`0x1845f3b88`) and can be reasserted by
`set_all_layers_to_null` (`0x1847adff8`); the latter was observed on other
display instances, not the active allocated display. A clear call alone does
not establish that a later non-null update cannot run.

The `needs_passthrough` predicate checks nonzero current mode, bit 28 clear,
mode class (bits 55:59) in `0x10..0x1c`, then returns the inverse of
`display+0x755e` bit 0. The mode class is inserted by
`update_digital_modes` at `0x1847b2f10` from ColorElement attributes, not by
the QEMU A413/A442 echo. The original nested `ColorModes` tuple
(`PixelEncoding=0`, `Depth=8`, `EOTF=0`, `DynamicRange=0`) took the class-1
fallback; the class check failed before `+0x755e` was read.

`UI_COLOR2_CLASS16_COHERENT1` changed only the **synthetic input bodies**:
both outer ColorElements and nested Timing.ColorModes use `Depth=10`,
`EOTF=2`, `PixelEncoding=0`, `DynamicRange=0`, with the 32-byte Color
ElementData updated consistently. The original fixtures and
`DARWIN_DCP_IOMFB_ECHO_MODE=1` behavior were unchanged. All six scripted
D126/D127/D128 callbacks completed with transport status zero and output
`01000000`; Color and Timing were non-null at the AP property reads. At the
first post-allocation decision, the mode was `0x1b00000000578280`, class
`0x16` (22), bit 28 clear. The valid class makes the `+0x755e` load reachable;
the observed byte was **1**, so `needs_passthrough()` still returned zero.
The join stored `w24=0`, `update_surface` was skipped, and `+0x7545` bit 0
separately drove null clear-layer calls. A407 appeared; A408 did not appear
before the bounded stop. The AppleDisplay constructor zeroes the 4-byte word
at `display+0x755c` (`0x1845f3be0`), which includes `+0x755e`; the observed
one therefore arose later in construction. This proves the coherent semantic tuple fixes the
synthetic packed-mode class, but not the presentation gate. Evidence:
`/tmp/dvm/UI_COLOR2_MODE_ECHO_PROVENANCE1.lldb.log`,
`/tmp/dvm/UI_COLOR2_CLASS16_COHERENT1.lldb.log`, and
`/tmp/dvm/probe/UI_COLOR2_CLASS16_COHERENT1.stderr.log`.

The read-only `UI_CLASS16_LATCH_PROVENANCE1` and `UI_CLASS16_LATCH_BRANCH1`
probes identified the `+0x755e` writer and checked display identity. On the
AppleDisplay instance that later allocated the IOSurface, the constructor
sets `+0x7545` from zero to one at `0x1845f3b88`, then zeroes the word at
`+0x755c` at `0x1845f3be0`. During construction,
`AppleDisplay::update_display_limits()` writes `+0x755e=1` with a byte store
at `0x1845f6e5c`. At that branch, the local `+0x7537` byte was zero, and the
cached result of `CADeviceIsVirtualized()` was one. The global at
`0x1e7053bf9` is `CADeviceIsVirtualized()::is_virtualized`; the global at
`0x1e7058600` is its `once` token, **not** a current mode. Its initializer
queries the `IsVirtualDevice` key before caching the result. Thus
`needs_passthrough()==0` after class `0x16` follows QuartzCore's virtual-device
policy; it is not evidence of a missing DCP reply. Do not force `+0x755e` to
zero to infer the normal virtualized compositor path.

At the first post-allocation `finish_update`, the watched instance was the
same AppleDisplay and still had `+0x755e=1` and `+0x7545=1`. The latter was
also reasserted on this instance by a halfword store at `0x1847b06dc` in
`IOMFBDisplay::update_framebuffer_locked(unsigned int)`. A first clear of
`+0x7545` on this instance has not yet been observed; an initial null-layer
operation does not establish that a later non-null attach cannot occur.
Evidence: `/tmp/dvm/UI_CLASS16_LATCH_PROVENANCE1.lldb.log` and
`/tmp/dvm/UI_CLASS16_LATCH_BRANCH1.lldb.log`.

`UI_CLASS16_CLEAR_RELEASE1` kept the same CLASS16 fixtures and MODE_ECHO and
watched the active display's `+0x7545` byte from its constructor. The watch
recorded the constructor's `0→1` store and the `1→1` halfword store in
`update_framebuffer_locked`, then a non-null surface allocation and the first
`finish_update` on that same display with the byte still one. It recorded no
`1→0` store before userspace shut down and the subsequent Halt/Restart nested
panic stopped the boot. This is a bounded negative observation, not proof
that `+0x7545` can never clear. In the local `set_all_layers_to_null(bool)`
implementation (`0x1847addb0`), bit 0 already set takes the early exit;
otherwise the function submits null layers and sets the byte to one at
`0x1847adff8`. Its behavior supports reading the byte as an all-layers-null
state, but does not establish that it must clear *before* the first non-null
`swap_set_layer`. The ordinary virtualized layer-attachment path remains open.
Evidence: `/tmp/dvm/UI_CLASS16_CLEAR_RELEASE1.lldb.log` and
`/tmp/dvm/probe/UI_CLASS16_CLEAR_RELEASE1.serial.log`.

The next static cut and `UI_CLASS16_LAYER_DECISION1` move the immediate
branch upstream of `+0x7545`. `finish_update` loads `display+0x66e8` at
`0x18446bf04`: a null pointer, or bit 0 set in that object's `+0x12c` byte,
sets `w25=0`; otherwise it sets `w25=1`. At `0x18446c6c8`, `w25=0` takes the
null-layer branch. The read-only stop after the same non-null IOSurface
allocation measured **`display+0x66e8=0`, `x19=0`, `w25=0`** on the active
display. Thus no object existed on which to test `+0x12c`; `+0x7545=1` was
the fallback clear state, not a proven prerequisite that had to be cleared
before attaching a surface. The real-surface call at `0x18446c860` takes
`x2=[x19+0x18]`; other real-surface callsites in this routine use `x22` or
`x27`. Static stores to `display+0x66e8` occur in `detach_layer` at
`0x18454a748` and `clone_update` at `0x1847ab994`, with a clear in
`delete_detached_layer` at `0x18457f7ec`. The next question is whether the
observed update reaches those producers and what prevents a live layer object
from being retained. No non-null `swap_set_layer`, A408, or scanout is
claimed. Evidence: `/tmp/dvm/UI_CLASS16_LAYER_DECISION1.lldb.log`.

This `+0x66e8` lane is specifically a **detached layer**, not necessarily the
ordinary composited display layer. `IOMFBDisplay::detached_layers_valid_p`
(`0x184479b0c`) returns its valid result when the pointer is null; it only
checks `+0x18` and `+0x12c` when a detached layer exists. The null pointer
therefore explains this invocation's detached-layer clear calls, but does not
by itself prove a missing object required for all presentation. Keep the
ordinary layer/update path separate while tracing the producer.

`UI_CLASS16_LAYER_PRODUCER1` kept the same fixtures and stopped on the first
post-allocation detached-layer decision. It saw no `clone_update`,
`detach_layer`, or clear hook on the active display before that decision.
The optional detached-layer pointer was never installed in that window; this
does not rule out the ordinary composited path. Evidence:
`/tmp/dvm/UI_CLASS16_LAYER_PRODUCER1.lldb.log`.

### Ordinary surface decision (2026-09-30)

`finish_update` has two non-detached calls that can pass a real `Surface*` to
`swap_set_layer`: `0x18446d0c8` passes `x22` from
`current_page_surface()`/`allocate_surface()`, and `0x18446e280` passes
`x27` from the page-surface path. `current_page_surface()` uses the active
page index to find a cached `Surface*` beneath `display+0x6568`, and can
allocate one if its cached entry is unsuitable. These are distinct from the
optional `DetachedLayer` call at `0x18446c860`.

The read-only `UI_CLASS16_ORDINARY_PATH1` run found the surface before
either ordinary callsite: the active frame object in `x26` had
`[x26+0x20] == last_alloc` (`0x7bd2d74000`). At `0x18446cb60`, however,
`w8=0x05`, so its bit-2 branch went to `0x18446cc0c`, which calls
`swap_set_layer` with `x2=0`. Neither ordinary non-null callsite ran before
the later userspace shutdown. The `last_alloc` pointer is a CA `Surface*`
at this stage; this result does not establish a submitted IOSurface or DCP
presentation. Evidence: `/tmp/dvm/UI_CLASS16_ORDINARY_PATH1.lldb.log`.

`UI_CLASS16_ORDINARY_BIT2_1` then stopped on that same bit-2 decision. The
frame byte at `+0x44` was **`0x01` before** `0x18446c974` and **`0x05`
after** it. The surface was already present at `+0x20`. This store is reached
because `Render::Update+0x140` was `0x80010` (bit 3 clear); the static
branch at `0x18446c968` skips the bit-2 set only when that update bit is
one. Thus the first observed ordinary attempt has a real surface owner, but
`finish_update` itself marks the frame for the null-layer arm before it can
select either real-surface callsite. The semantic meaning and producer of
update bit 3 were the next upstream question. No byte, flag, or `x2` was
forced. Evidence:
`/tmp/dvm/UI_CLASS16_ORDINARY_BIT2_1.lldb.log`.

The static setter is `Render::Update::add_context`: at `0x184440e60` it
tests bit 22 of its prepared-layer result at `sp+0xf0`; if set, it ORs bit 3
into `Update+0x140` at `0x184440e70`. It can also reach that OR when the
separate `sp+0x170 & 0x28000` mask is nonzero and `Update+0x5da` bit 0 is
clear. The read-only `UI_CLASS16_ORDINARY_SOURCE1` run correlated this
function's `Update*` with the exact `Update*` later seen by `finish_update`.
Its sampled prepared-layer word was `0x10000800008` (**bit 22 clear**), the
mask was zero, and the bit-3 setter did not run (`set8=0`). The same object
reached the ordinary decision with `+0x140=0x80010`. This identifies an
upstream compositor/update-flag miss on this synthetic-mode boot, rather
than a lost surface pointer or a required `DetachedLayer`. The `0x80010`
value and absent bit-22/`0x28000` inputs also occurred in natural GATE239,
but that earlier run stopped at `ignore_update_p`; the present pointer
correlation is specific to this later MODE_ECHO surface attempt. Next trace
the prepared-layer input that would set bit 22 on this `Update`, while
keeping the mode fixtures and echo fixed. No A408 or pixels are claimed.
Evidence: `/tmp/dvm/UI_CLASS16_ORDINARY_SOURCE1.lldb.log` and
`docs/re/quartzcore-render-bounds-followup.md` (GATE239/240).

### Prepared-layer bit 22 on the first surface update (2026-09-30)

`UI_CLASS16_PREPARED_BIT22_1` kept the coherent class-`0x16` Color/Timing
fixtures and `DARWIN_DCP_IOMFB_ECHO_MODE=1` unchanged. A read-only
`prepare_layer` trace correlated its input with the exact `Render::Update*`
(`0x1046ffe48`) later seen at the first post-allocation null-layer decision.
The two sampled layer colors were both RGBA `(0,0,0,1)`. At
`0x184433594`, the zero-RGB test gave `w9=1` and the alpha-one selection
gave `w25=1` for each. The `0x184433a74` test therefore skipped the
`0x20400000` OR that includes bit 22. The final prepared word was
`0x10000800008`; `add_context` saw bit 22 clear and no `0x28000` mask,
left `Update+0x140` bit 3 clear, and `finish_update` took the null-layer
arm with the allocated `Surface*` still in the frame. This is consistent
with an opaque-black/clear optimization on **this** update; it does not
establish that bit 22 universally means “visible” or “drawable.”

`UI_CLASS16_PREPARED_LATER1` held the same fixtures and ran through the
guest's shutdown instead of stopping at that first decision. It logged one
`prepare_layer`/`add_context` update: again two opaque-black samples,
`w25=[1,1]`, bit 22 clear, and the null-layer decision. No later
non-black or bit-22-bearing prepared update appeared before launchd logged
`rebooting due to critical process crashes: SpringBoard` at 11:37:15.
The subsequent `Halt/Restart Timed Out` panic is shutdown fallout, not a
diagnosis of the SpringBoard crash. The bounded result is that this boot
never supplied a later composited frame to the observed `add_context`
path; the reason SpringBoard failed to supply one remains open. In
particular, the first black clear should not be called the presentation
failure by itself. Prior GATE154 showed that an injected non-black RGBA
could set bit 22 and drive a forced blue A408, but that is a controlled
counterexample, not evidence of natural SpringBoard content.

The next natural-path question is upstream of `prepare_layer`: why no later
client commit with non-black/contents-backed layers reaches the render
server before SpringBoard's critical crash. Keep the mode fixtures and echo
fixed while identifying SpringBoard's first failure and any later CA
commit/send path; do not force bit 22 or a surface pointer to answer that.
Evidence: `/tmp/dvm/UI_CLASS16_PREPARED_BIT22_1.lldb.log`,
`/tmp/dvm/UI_CLASS16_PREPARED_LATER1.lldb.log`, and
`/tmp/dvm/probe/UI_CLASS16_PREPARED_LATER1.serial.log`.

### First SpringBoard crash on the synthetic class-0x16 mode (2026-09-30)

`UI_CLASS16_SB_REASON2` used the same coherent Color/Timing fixtures and
MODE_ECHO. RAM snapshots at +62 and +67 guest seconds after `Early boot
complete` captured the earliest SpringBoard crash record (PID 35, later
repeated by PIDs 168 and 177). The record reports signal 5 / `EXC_BREAKPOINT`
at runtime `0x1be7e78b0`. With this boot's dyld-cache slide `0x163e0000`,
the PC is `0x1a84078b0` in
`-[FBSDisplayMode _initWithWidth:height:preferredScale:scaleOverride:refreshRate:gamut:hdr:rotation:virtual:validityCheck:]`
in FrontBoardServices. This is the method's deliberate `brk #0` following
`__bs_set_crash_log_message_stub`, not a fault in the first opaque-black
QuartzCore update. A separate chronod SIGABRT also appeared in the dump;
it is not the SpringBoard PID-35 reason.

The formatted crash message survived in guest RAM:

```
FBSDisplayMode cannot be configured as requested : <FBSDisplayMode: ...; 320x175@2x (640x350/2) 0Hz p3 HDR10>
```

Thus SpringBoard is receiving the **synthetic 640x350 mode at 0 Hz**. The
class-0x16 Timing fixture has both Horizontal and Vertical `SyncRate` and
`PreciseSyncRate` set to zero, so the zero refresh is consistent with its
input; the exact field-to-FrontBoard conversion and first failed validity
subcheck have not yet been measured. The first invalid-mode branch reaches
the fatal trap when `validityCheck=0`. Launchd's subsequent critical-process
reboot was logged at 11:46:51, 80.65 seconds after its 11:45:30 `Early boot
complete` line. No later non-black `Render::Update` appeared before that
crash in the preceding extended trace. This remains a synthetic AP-path
experiment, not a native DCP timing payload or pixel result.

Evidence: `/tmp/dvm/UI_CLASS16_SB_REASON2.t62.oskcdata.txt`,
`/tmp/dvm/UI_CLASS16_SB_REASON2.t67.oskcdata.txt`,
`/tmp/dvm/UI_CLASS16_SB_REASON2.ram/t62/010040000000.bin`,
`/tmp/dvm/probe/UI_CLASS16_SB_REASON2.serial.log`, and
`/tmp/dvm/UI_CLASS16_SB_REASON2.slide.json`.

### Nonzero-refresh A/B and FrontBoard confirmation (2026-09-30)

The candidate fixture
`tools/re/fixtures/consumer-element-array.timing1-colormode-dict.class16-60hz.synthetic640x350.xml`
changes only the four rate values. For the existing total dimensions
640x350, it supplies vertical 60 Hz and horizontal 21,000 Hz as 16.16
fixed-point values (`0x003c0000` and `0x52080000`) in both `SyncRate` and
`PreciseSyncRate`. Firmware serializer `0x4122754`–`0x4122790` derives
rounded `SyncRate` with `(raw + 0x8000) & 0xffff0000` and emits the raw
`PreciseSyncRate`. The six-entry callback file kept the three Color entries
byte-identical to the class-0x16 control, and MODE_ECHO was unchanged.

`UI_CLASS16_REFRESH60_AB1` completed both D126/D127/D128 transfers with
`status=0`, `out=01000000`. It ran for 210 seconds total: from `Early boot
complete` at 11:58:07 until the 12:01:15 collection, with **no** SpringBoard
critical-process reboot and no XNU panic. The 0 Hz control had rebooted
80.65 seconds after `Early boot complete`. The candidate still logged only
one prepared `Render::Update`: two opaque-black RGBA layers and a null-layer
clear. No later non-black/bit-22 update was observed in this bounded window.

The focused `UI_CLASS16_REFRESH60_FBS_FP1` constructor trace confirmed the
rate at the actual FrontBoard boundary. SpringBoard called
`-[FBSDisplayMode _initWithWidth:height:preferredScale:scaleOverride:refreshRate:gamut:hdr:rotation:virtual:validityCheck:]`
with width 640, height 350, `preferredScale=2` in `x4`, and
**`refreshRate=60.0` in `d1`**; the strict `validityCheck=0` call reached the
normal return path with a non-null object. The preceding
`-[FBSDisplayMode _initWithCADisplayMode:scale:rotation:]` obtains the
`CADisplayMode` `preferredScale` selector at `0x1b0004df0` and `refreshRate`
selector at `0x1b00050d0`, then passes them in `x4` and `d1` respectively
(`0x1a8407288`–`0x1a84072f0`). An earlier draft hook mislabeled `x4=2`
as the refresh rate; that was the 2x scale, not 2 Hz. The corrected hook
at `0x1a8407828` records the returned object after `mov x0,x19`.

The zero-refresh FrontBoard trap is closed for this synthetic AP consumer
probe. SpringBoard survival by itself has not produced a drawable frame in
the observed 210-second window. The next question is why the client/render
server supplies no later non-black prepared update, while leaving this
accepted Color/Timing pair and MODE_ECHO fixed.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_AB1.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_AB1.serial.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_AB1.stderr.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_FBS_FP1.lldb.log`, and
`/tmp/dvm/UI_CLASS16_REFRESH60_FBS_CALLER1.lldb.log`.

### Client commit versus server update after the black clear (2026-09-30)

With the accepted 60 Hz Timing fixture, coherent class-`0x16` Color fixture,
and MODE_ECHO unchanged, `UI_CLASS16_REFRESH60_CLIENT_COMMIT1` watched the
QuartzCore client builder and `0x9c42`–`0x9c45` message sends for 330 seconds.
It captured four backboardd `0x9c42` send-wrapper hits during startup, but
no SpringBoard hit at the builder or send sites. There was no XNU panic or
SpringBoard critical-process reboot in the collection window. This is a
bounded absence at those sites, not proof that SpringBoard never commits.

`UI_CLASS16_REFRESH60_UPDATE_FORK1` is **invalid evidence**: LLDB failed to
register the imported client callback module. The corrected
`UI_CLASS16_REFRESH60_UPDATE_FORK2` installed and proved all 16 breakpoint
commands. In its 330-second window, SpringBoard entered the
`FBSDisplayMode` constructor five times, four with width 640 and height 350.
That confirms SpringBoard process attribution and mode activity on the same
boot. Backboardd again sent four startup `0x9c42` messages. Its server side
hit one AddContext preparation, one `SERVER_RENDER_UPDATE_ENTRY`, and one
`FINISH_LAYER_DECISION`; no second server update appeared. No selected
SpringBoard scene request/create/activation/update hook and no SpringBoard
client commit/send hook fired. The guest again had no XNU panic or
SpringBoard critical-process reboot.

The observed path is therefore **one initial backboardd display update**, not
a later drawable SpringBoard update. The next local target is SpringBoard's
scene/window and `CAContext` hosting lifecycle: determine whether the home
scene becomes active and its layer tree is mutated or flushed. The FrontBoard
hooks cover named implementations, not every possible home-scene path, so
their silence alone does not prove scene activation never occurred. Do not
resume bit-22 or `prepare_layer` analysis until a later client commit or
server update is observed.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CLIENT_COMMIT1.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_CLIENT_COMMIT1.serial.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_UPDATE_FORK2.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_UPDATE_FORK2.serial.log`, and
`tools/re/ca_update_fork_callbacks.py`.

`UI_CLASS16_REFRESH60_SB_CA1` then sampled generic QuartzCore entry points
for SpringBoard over another 330-second accepted-mode/MODE_ECHO run. All 11
breakpoint commands were armed; SpringBoard hit the 640x350 `FBSDisplayMode`
constructor five times. It never hit the sampled local/remote `CAContext`
creation methods, `-[CAContext setLayer:]`, or
`CA::Context::commit_transaction`, and sent no observed `0x9c42`–`0x9c45`.
Backboardd's four startup `0x9c42` sends were still visible. The first
SpringBoard constructor backtrace places it under
`SBSystemAppMain` → `_FBSystemShellInitialize` →
`+[FBDisplayManager sharedInstance]` → `FBSDisplayMonitor`/`FBSDisplaySource`.
Thus the measured SpringBoard activity is shell display setup, not evidence
of a hosted home-screen CA layer tree. The generic CA stops make the
scene-to-context gap more credible, while their silence remains a bounded
observation rather than proof that no alternate context path exists. The
next checkpoint is whether `_FBSystemShellInitialize` returns and an
`SBMainWorkspace` transition executes before any home-screen context or
root layer can appear.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_SB_CA1.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_SB_CA1.serial.log`, and
`tools/re/sb_ca_context_callbacks.py`.

`UI_CLASS16_REFRESH60_SHELL_WORKSPACE1` extended the same read-only probe
to the normal `_FBSystemShellInitialize` epilogue (`0x1c4a3c32c`), the
instruction following its call in `SBSystemAppMain` (`0x224458bf4`),
`SBMainWorkspace` request/transition methods, and home-controller/scene-layer
creation. All 20 breakpoints were armed. In 300 seconds the five
SpringBoard `FBSDisplayMode` calls occurred again, but neither shell-return
site nor any later workspace/home/CAContext site fired. Backboardd startup
`0x9c42` traffic remained visible; there was no SpringBoard critical reboot
or XNU panic. This bounds the missing progression **before the normal shell
return**, without yet identifying the blocking call. The constructor
backtrace is inside `+[FBDisplayManager sharedInstance]` called from
`_FBSystemShellInitialize`. `-[SBHomeScreenEntity init]` (`0x224229fe0`) and the literal
`com.apple.SpringBoard.workspace-entity.builtin.home-screen` are specific
Main-home success markers for a later workspace probe, once shell init
returns.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_SHELL_WORKSPACE1.lldb.log` and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_SHELL_WORKSPACE1.serial.log`.

`UI_CLASS16_REFRESH60_FB_MANAGER1` then bracketed that apparent singleton
stall. The shell hit `0x1c4a3bf00` before `+[FBDisplayManager
sharedInstance]`, while the singleton's dispatch-once block entered
`FBSDisplayMonitor` construction, made six display-source init/update calls,
and reached its epilogue. The shell then hit **`0x1c4a3bf04` after the
display-manager call**, with a non-null `x0`. The normal shell-init
pre-return site `0x1c4a3c32c` remained absent in the bounded 240-second
window. Display-manager construction is therefore not the observed stall;
the next bracket is within the remaining `0x1c4a3bf04`–`0x1c4a3c32c`
shell-init body. A constructor backtrace taken while inside the singleton
did not establish that it remained blocked there.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_FB_MANAGER1.lldb.log` and
`tools/re/fb_display_manager_bracket_callbacks.py`.

`UI_CLASS16_REFRESH60_SHELL_TAIL4` proved 12 read-only breakpoints through
the rest of that body. After `0x1c4a3bf04`, SpringBoard reached
`0x1c4a3bf30` and then `0x1c4a3bf94`; it never reached the next coarse
checkpoint `0x1c4a3bfcc` or the normal shell return in 210 seconds. Static
shared-cache annotation identifies the class loaded at `0x1c4a3bf98` as
`FBInterfaceOrientationService`; the call at `0x1c4a3bf9c` sends
`sharedInstance`. This bounds the stall to the region containing that call,
the following singleton setup at `0x1c4a3bfac`, and an optional indirect
call before `0x1c4a3bfcc`. It does **not** yet prove which nested call fails
to return. The next probe brackets `+[FBInterfaceOrientationService
sharedInstance]` and its dispatch-once/`init` path directly.

`SHELL_TAIL1`–`3` are invalid boot attempts: a temporary copy of the launch
wrapper omitted `DARWIN_SMP_PV=1`, so each hit a secondary-CPU startup panic
before userspace. `SHELL_TAIL4` used the repository wrapper and reached the
SpringBoard stops without a kernel panic. The accepted Color/Timing fixtures
and MODE_ECHO were unchanged.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_SHELL_TAIL4.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_SHELL_TAIL4.serial.log`, and
`tools/re/fb_shell_tail_bracket_callbacks.py`.

`UI_CLASS16_REFRESH60_ORIENTATION1` resolved the next nested method. The
`+[FBInterfaceOrientationService sharedInstance]` call at `0x1c4a3bf9c`
entered its dispatch-once block, constructed both the service and its server,
and **returned to the shell at `0x1c4a3bfa0`**. The shell still did not hit
`0x1c4a3bfcc` within 210 seconds, with zero XNU panics. Static ObjC
references show the shell's `x21` here is an
`FBMutableSystemShellInitializationOptions` instance. The call at
`0x1c4a3bfac` asks it for `registerAdditionalServicesBlock`; if non-null,
the shell invokes the block at `0x1c4a3bfc8` and only then reaches
`0x1c4a3bfcc`. This block/getter is the next narrow bracket; orientation
service initialization itself is cleared.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_ORIENTATION1.lldb.log` and
`tools/re/fb_orientation_bracket_callbacks.py`.

`UI_CLASS16_REFRESH60_SERVICES_BLOCK1` brackets the getter and indirect
call. `registerAdditionalServicesBlock` returned a non-null block, and the
shell reached `0x1c4a3bfc8` immediately before invoking it, but not
`0x1c4a3bfcc` after invocation within 210 seconds. The captured block's
invoke pointer, after subtracting the live cache slide, is
`0x224458e04`, symbolicated as `___SBSystemAppMain_block_invoke_2` in
SpringBoard. That thunk sends
`+[SpringBoard initializeSystemServicesBeforeApplicationDidFinishLaunching]`
(`0x2244cef28`). Thus the first **named** nested call that fails to return
within the window is SpringBoard's pre-application-launch service
initialization, not `FBDisplayManager` or `FBInterfaceOrientationService`.
This does not yet identify the inner service that holds it up; the next
bracket is inside that initializer. No XNU panic occurred in this probe.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_SERVICES_BLOCK1.lldb.log` and
`tools/re/fb_services_block_callbacks.py`.

`UI_CLASS16_REFRESH60_PRELAUNCH1` bracketed the SpringBoard initializer.
It entered (twice) and reached checkpoints after `SBApplicationServer`,
`SBFMobileKeyBag`, `SBBiometricsService`, `SBSoftwareUpdateService`,
`SBApplicationStateServiceSingleton`, and
`SBApplicationSupportServiceSingleton`. The next instruction loads
`CCSControlCenterServicesManager` and calls its `startServices` selector
at `0x2244cf0bc`; the following `0x2244cf0c0` checkpoint did not fire in
the 210-second window. This is the narrowest observed non-returning call
so far. Static ObjC metadata places the class in
`ControlCenterServices.framework`; its Swift `startServices` body calls
`+[CCSRemoteServiceProvider sharedInstance]` and then
`-[CCSRemoteServiceProvider resume]`. The current live trace does not yet
separate those two inner calls or prove why either waits. No XNU panic or
SpringBoard critical reboot occurred in this run.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_PRELAUNCH1.lldb.log` and
`tools/re/sb_prelaunch_services_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_START1` entered the Swift body of
`CCSControlCenterServicesManager.startServices`, passed its one-time
initialization, and called `+[CCSRemoteServiceProvider sharedInstance]`.
That singleton entered its construction block and
`-[CCSRemoteServiceProvider _init]`. The probe did **not** hit the
post-`sharedInstance` checkpoint or `-[CCSRemoteServiceProvider resume]`
in 210 seconds. The live stop is therefore in provider construction,
before its `resume` call. Static `_init` starts by requesting
`+[CCSModuleRepository sharedInstance]`, then sets up a module-settings
provider and an XPC listener. A dedicated provider-construction bracket
will identify which of those nested calls holds it. The accepted display
fixtures remain fixed; no kernel panic occurred.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_START1.lldb.log` and
`tools/re/ccs_start_services_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_PROVIDER1` entered
`-[CCSRemoteServiceProvider _init]`, passed its superclass initializer,
and called `+[CCSModuleRepository sharedInstance]`. Its dispatch-once
block entered `-[CCSModuleRepository
_initWithDirectoryURLs:allowedModuleIdentifiers:]`; the provider did
not reach its post-repository checkpoint in 210 seconds. The next bracket
belongs inside repository initialization, not provider `resume` or any
display method.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_PROVIDER1.lldb.log` and
`tools/re/ccs_provider_init_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_REPO1` passed the repository superclass and
input-copy setup, created both repository queues, entered its
`dispatch_sync` block, and entered
`-[CCSModuleRepository _queue_updateAllModuleMetadata]` at
`0x25909602c`. It did **not** reach the checkpoint after that metadata
update or the post-`dispatch_sync` checkpoint within 210 seconds. The
method's first nested call is `-[CCSModuleRepository
_queue_loadAllModuleMetadata]` at `0x2590968d4`; whether that call is
the actual wait needs a direct bracket. This remains a bounded
SpringBoard prelaunch-service stall, not evidence of a display-path
failure.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_REPO1.lldb.log` and
`tools/re/ccs_module_repo_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_METADATA1` entered
`_queue_updateAllModuleMetadata`, then entered and returned from
`-[CCSModuleRepository _queue_loadAllModuleMetadata]`. The loader
enumerated its directories/collections and reached its return; the caller
reached `0x259096054` after the load. It did not reach `0x259096068`
after the next call, whose selector is
`_queue_updateAllModuleMetadataForAllModuleMetadata:` (implementation
`0x259096294`), within 210 seconds. A direct bracket of that method is
needed to separate its dictionary/enumeration work from its later
metadata update calls. The 60 Hz Color/Timing fixtures and MODE_ECHO
remained unchanged.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_METADATA1.lldb.log` and
`tools/re/ccs_metadata_load_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_APPLY1` entered
`_queue_updateAllModuleMetadataForAllModuleMetadata:`, enumerated the
loaded metadata, compared the resulting dictionary, and returned from
`_queue_updateInterestingBundleIdentifiersForModuleMetadata:`. It did
not reach the checkpoint after its next call in 210 seconds. That next
selector is `_queue_updateAvailableModuleMetadataForAllModuleMetadata:`
(implementation `0x25909642c`). This narrows the prelaunch wait to
Control Center's availability filtering or a nested call inside it;
the filter needs a direct bracket before attributing the stall to a
specific condition. No XNU panic occurred.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_APPLY1.lldb.log` and
`tools/re/ccs_metadata_apply_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_AVAIL1` entered
`-[CCSModuleRepository
_queue_updateAvailableModuleMetadataForAllModuleMetadata:]`, passed
its dispatch-queue assertion at `0x259096458`, and did **not** reach
`0x259096464` after its first filter call in 210 seconds. The call at
`0x259096460` sends
`_queue_filterModuleMetadataByAssociatedBundleAvailability:`
(implementation `0x259096c58`). That is the first named helper on the
observed shell-init chain without a post-call hit. Static disassembly of
the helper shows a collection filter whose predicate constructs
`LSApplicationRecord` objects for associated bundle identifiers; the
live probe has not yet shown which predicate invocation or LaunchServices
operation is waiting. The last reached/first unreached PCs here are
`0x259096458`/`0x259096464` (static addresses). The shell still did not
return, and no XNU panic occurred. This is a bounded Control Center
prelaunch-service stall, not yet a proved LaunchServices root cause.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_AVAIL1.lldb.log` and
`tools/re/ccs_availability_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_RECORD1` entered the availability
filter's collection predicate. The first three metadata entries had no
associated bundle identifier and returned through the predicate. The
fourth returned the NSString `com.apple.mobilenotes` (decoded by
read-only inspection of its character storage). The predicate reached
`0x259096d0c`, calling
`-[LSApplicationRecord initWithBundleIdentifier:allowPlaceholder:error:]`;
the CoreServices implementations at `0x186dd29e4` (public selector)
and `0x186dd1348` (`fetchingPlaceholder:`) both entered. The
predicate's post-init `0x259096d10`, later property-query checkpoints,
and filter return remained absent for the 210-second window. Thus the
first actual associated-bundle lookup, rather than a later
`LSApplicationRecord` property query, holds the observed call chain.
This does not yet establish whether the wait is in local LaunchServices
database/context acquisition or IPC with `lsd`; the next bracket is
inside the record initializer. `com.apple.mobilenotes` is the first
lookup observed, not evidence that Notes itself is defective.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_RECORD1.lldb.log` and
`tools/re/ccs_ls_record_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_CONTEXT1` showed the lookup enters
`LSApplicationRecord`'s `_initWithNode:...`, then
`LaunchServices::Database::Context::_get` and
`_LSContextInitCommon` before any demonstrated XPC/`lsd` call. A
**different SpringBoard thread first** reached `_get` from
`-[FBApplicationDataStoreRepositoryManager
clearExpiredUninstalledApplicationsFromStoreIfNecessary]` via
`-[NSEnumerator allObjects]` and `-[_LSDBEnumerator
countByEnumeratingWithState:objects:count:]`. That thread's
`gSessionLock` try-lock returned 1 and its `__LSCopyLocalDatabase`
returned null; no later traced completion was observed. The CCS/Notes
thread reached the same lock's try-lock with return **0**, then had no
subsequent context checkpoint in the 210-second window. Another
SpringBoard thread likewise saw try-lock return 0. This points to
contention on the **local LaunchServices session lock**; it does not
establish an `lsd` IPC wait or a deadlock cycle. The next probe follows
the earlier lock holder's path after the null local-database result,
especially `_LSDatabaseNotificationCheck` and
`__LSCopyServerStore`.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_CONTEXT1.lldb.log`
and `tools/re/ccs_ls_context_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_OWNER1` followed the earlier lock holder
on the FrontBoard expired-app cleanup thread. After acquiring
`gSessionLock`, `__LSCopyLocalDatabase` returned null and
`_LSDatabaseNotificationCheck` returned. The thread then entered
`__LSCopyServerStore` at `0x186df21c8` from `_LSContextInitCommon`'s
call at `0x186dc8e50`; neither the post-call `0x186dc8e54` nor the
session-unlock checkpoints fired in 210 seconds. Meanwhile the CCS
Notes lookup thread's try-lock returned 0 and it reached the blocking
lock acquisition. This establishes that the first holder was inside
`__LSCopyServerStore` while CCS waited; it does not yet distinguish
waiting on the `lsd` XPC service from local store/reseeding work. The
next bracket is inside `__LSCopyServerStore`.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_OWNER1.lldb.log`
and `tools/re/ccs_ls_lock_owner_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_SERVERSTORE1` bracketed that holder's
`__LSCopyServerStore` call. `_LSDReadService` XPC connection acquisition
returned a non-null object, proxy acquisition returned, and the first
remote method call at `0x186df24c0` returned. The reply took the path
with the binary's `Database was seeding; client will retry and block`
log, then `_LSDatabaseBlockingFetchInterface` returned a non-null
object. The holder reached the `__LSCopyStoreFromServer.onceToken` test
at `0x186df25e8` with token 0, which branches to the cold
`dispatch_once` wrapper at `0x186f88828`; none of the later store-return
or retry checkpoints fired in 210 seconds. CCS remained blocked on the
same session lock. Thus the first XPC call is **not** the indefinite
wait. The next bracket is the `dispatch_once` body/wait and its
database-reseeding handshake; the trace does not yet prove whether
`lsd` is blocked or merely slow inside that later stage.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_SERVERSTORE1.lldb.log`
and `tools/re/ccs_ls_server_store_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_ONCE1` showed that the first-use
`dispatch_once` in the seeding path **returns**; the holder reached
`0x186df25f0` afterward. It then took the reseed-status branch at
`0x186df265c`, while CCS remained blocked on the session lock. The
helper on that branch is statically named
`___LAUNCH_SERVICES_IS_RESEEDING_ITS_DATABASE_AND_MAY_BLOCK__`
and sends the `_LSDReadService` selector
`getServerStoreBlockingWithCompletionHandler:`. This run did not
capture the second send/return boundary, so the next probe brackets
that selector directly. A missing breakpoint at the branch target is
not itself evidence that the branch was skipped.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_ONCE1.lldb.log`
and `tools/re/ccs_ls_reseed_once_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_BLOCKING1` reached the one-time
initialization and returned from it, then entered the reseeding branch
at `0x186df2680`. The FrontBoard cleanup thread entered the helper
`___LAUNCH_SERVICES_IS_RESEEDING_ITS_DATABASE_AND_MAY_BLOCK__`
(`0x186ef62a8`) and reached its Objective-C send at `0x186ef62b8`:
`getServerStoreBlockingWithCompletionHandler:` on the blocking-fetch
interface. Neither the helper's post-send `0x186ef62bc` nor the caller's
post-helper `0x186df268c` fired during the 120-second window. Other
SpringBoard threads reached the LS session-lock wait. This is a
**bounded client-side wait in the blocking server-store request**, not
evidence that the earlier nonblocking XPC call or `dispatch_once` hung.
The next check is the holder's blocked stack and whether `lsd`
received/answered this second request; an LS reseed deadlock is still a
hypothesis, not established by this trace alone.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_BLOCKING1.lldb.log`
and `tools/re/ccs_ls_reseed_once_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_HOLDERSTACK1` preserved the first LS
session-lock holder at the end of a 120-second window. A read-only dump
of its XNU thread saved state (`thread=0xffffffe0b033e0c0`, saved-state
flavor `0x15`) put its userspace PC at runtime `0x248e03cd4`, which
subtracts this boot's slide `0x11134000` to
`_mach_msg2_trap + 8` (`0x237ccfcd4`); its LR is
`_mach_msg2_internal + 76` (`0x237cd3308`). This confirms a Mach
message wait by the **lock holder**, consistent with the separately
bracketed second LaunchServices blocking-store request. It does not
yet show whether `lsd` received the request, which server operation is
pending, or whether the database reseed itself is stalled. The frozen
guest was released after collection.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_HOLDERSTACK1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_HOLDERSTACK1.saved.log`, and
`tools/re/ls_holder_saved_state.py`.

`UI_CLASS16_REFRESH60_CCS_LSD_ACTIVITY1` armed an `lsd`-filtered
`_mach_msg2_trap + 8` witness only when SpringBoard entered
`getServerStoreBlockingWithCompletionHandler:`. The SpringBoard send
was observed at time `1790805355.739`, and no post-send return fired
in the 130-second boot. From `1790805358.334` onward, multiple `lsd`
threads had successful Mach-message returns (`x0=0`; one observed
nonzero return was also recorded), proving that `lsd` was not wholly
stopped after this request began. This witness does **not** decode the
XPC selector or match an `lsd` receive to SpringBoard's particular
request. The remaining server-side question is whether the blocking
store request reaches its handler and what that handler awaits during
database seeding; generic `lsd` activity is insufficient to answer it.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LSD_ACTIVITY1.lldb.log`
and `tools/re/ccs_ls_lsd_activity_callbacks.py`.

Offline CoreServices disassembly identifies the server selector as
`-[LSDatabaseBlockingFetchClient
getServerStoreBlockingWithCompletionHandler:]` at `0x186f145c4`. On the
non-manual-rebuild arm it calls `__LSDatabaseWaitForSeeding` before
`-[LSDBExecutionContext syncRead:]`. The wait routine reads
`_seedingInFlightCount` at `0x1e40e4330`; while nonzero, it calls
`dispatch_async_and_wait` on the seeding workloop and repeats. The
counter is incremented by `__LSDatabaseMarkSeedingInFlight` and
decremented by `__LSDatabaseNotifySeedingComplete`. The latter follows
the seeding worker's `dispatch_sync` in
`LSDatabaseBuilder::createAndSeedLocalDatabase`'s block. The server
store callback can also enqueue an "unseeded database recovery rebuild"
if the local database is absent or unseeded. These are **static paths**,
not proof that SpringBoard's request reached one of them.

`UI_CLASS16_REFRESH60_CCS_LSD_HANDLER1` saw an `lsd` blocking-fetch
handler entry and `isForManualRebuild=0`, but no subsequent `syncRead:`
call on that handler thread; the 130-second window did not catch the
SpringBoard blocking send. `UI_CLASS16_REFRESH60_CCS_LS_SEEDCORR1`
then observed an `lsd` handler enter `__LSDatabaseWaitForSeeding` with
`_seedingInFlightCount=1` and reach `dispatch_async_and_wait`. Multiple
`lsd` threads reached that wait, with no observed return, counter-zero
loop, or `syncRead:` on the traced handler during the 240-second
window. That window again had **no SpringBoard blocking-send hit**.
Thus the `lsd` seeding wait is live and bounded, but attribution to the
specific SpringBoard request remains open. A later run must correlate
the SpringBoard send with a server handler/connection and then follow
the seeding worker to its completion or blocked dependency.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LSD_HANDLER1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_SEEDCORR1.lldb.log`,
`tools/re/ccs_ls_server_handler_callbacks.py`, and
`tools/re/ccs_ls_seeding_correlation_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_SEEDWORKER1` was a quiet 330-second
window with a wrapper callback that did not verify LLDB registration;
it is non-evidence. `SEEDWORKER2` exposed that registration failure
explicitly and was stopped before a long boot. The corrected
`UI_CLASS16_REFRESH60_CCS_LS_SEEDWORKER3` installed all 16 callback
commands and captured the following order (epoch seconds, same guest):

| Event | Time | In-flight seeds |
|---|---:|---:|
| `LSDatabaseBuilder::createAndSeedLocalDatabase` entry | 1790806854.124 | 0 |
| first `__LSDatabaseMarkSeedingInFlight` | 1790806898.920 | 0 before increment |
| builder worker enters `dispatch_sync` | 1790806898.937 | 1 |
| SpringBoard sends blocking server-store selector | 1790806899.188 | 1 |
| an `lsd` blocking-fetch handler enters | 1790806906.728 | 1 |
| that handler enters `__LSDatabaseWaitForSeeding` | 1790806906.748 | 1 |

Later seed jobs raised the counter from 1 to 2 and completed back to
1. Through the bounded window there was no return from the **first
builder worker's** `dispatch_sync`, no return from the traced handler's
seeding wait or `syncRead:` entry, and no return from SpringBoard's
blocking send. The first seed was therefore already in flight before
SpringBoard asked for the store, while later seed work could still
complete. This supports an original-seed dependency as the current
critical path, **not** a claim that all of `lsd` is stopped or that a
lock cycle has been proven. The handler followed SpringBoard's send by
about 7.5 seconds and used the same selector, but the probe did not
capture its peer PID/connection identity, so attribution of that
specific handler invocation to SpringBoard remains temporal rather
than unique.

Static disassembly of the first worker names its `dispatch_sync` target
as the builder's `_ioQueue` (`-[LSDatabaseBuilder initWithIOQueue:]`
stores the queue at builder `+0x8`). The queued block is
`___48-[LSDatabaseBuilder createAndSeedLocalDatabase:]_block_invoke_2`
at `0x186ec1e84`; a later stage in that block calls
`-[LSDBExecutionContext syncWrite:]`. The next live distinction is
whether this block starts on `_ioQueue`; if so, bracket its inner
operation before calling the original worker's wait a deadlock.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_SEEDWORKER3.lldb.log`
and `tools/re/ccs_ls_seed_worker_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_SEEDTARGET1` captured the `_ioQueue`
block entry at `0x186ec1e84` on the same `lsd` thread as the original
builder worker's `dispatch_sync`. Thus the sync call did schedule/run
its block; the miss lies **inside that block before its later
`syncWrite:` call** (no `syncWrite:` or outer sync return hit in the
240-second window). The server handler observed after SpringBoard's
send belonged to XPC peer PID **105**, while SpringBoard's cached PID
was **35**. The two events were only temporally adjacent: that handler
is **not** SpringBoard's request. SpringBoard's blocking send still had
no return; its particular server receive remains unobserved.

The block's static reason switch (`0x186ec1e84` onward) can call one of
several named rebuild-reason helpers. Those helpers enter
`__LSServer_LSEnumerateAndRegisterAllBundles` at `0x186e75940`,
whose body calls `-[LSRebuildStatisticsGatherer performRebuild:]` at
`0x186e75a54`. The next live probe must record the original block's
reason flags and bracket enumeration and `performRebuild:` before
attributing the delay to any bundle or path.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_SEEDTARGET1.lldb.log`
and `tools/re/ccs_ls_seed_target_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_REBUILDARM1` bracketed the original
`LSDatabaseBuilder` worker on one `lsd` thread. Its `_ioQueue` block
read rebuild reason flags `0x9` (including the **database-missing**
arm), entered `__LSServer_LSEnumerateAndRegisterAllBundles` twice, and
reached the call to `-[LSRebuildStatisticsGatherer performRebuild:]`.
Neither the post-call return, enumeration return, nor the later
`syncWrite:` call fired during the 210-second window. This localizes
the outstanding first seed to work within `performRebuild:`; it does
not yet identify a bundle, path, or individual nested call as stalled.
The next probe brackets the rebuild-context block and its system,
cryptex, and container scan stages.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_REBUILDARM1.lldb.log`
and `tools/re/ccs_ls_rebuild_arm_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_REBUILDSTAGES1` followed that same
database-missing worker through `performRebuild:`, its outer block,
`+[LSDatabaseRebuildContext withStatsGatherer:runWithRebuildContext:]`,
and the inner rebuild block. It called
`-[LSRebuildStatisticsGatherer performSystemContentScan:]` about 5.4
seconds after the inner block began. No return from that call, system
save, cryptex scan, container scan, or rebuild-context return was seen
in the 210-second window. Thus the first outstanding seed is in the
**system-content scan**, not yet at cryptex/container registration or
the final store write. The scan may be long-running; its inner block
and enumeration must be bracketed before attributing a particular
path or bundle.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_REBUILDSTAGES1.lldb.log`
and `tools/re/ccs_ls_rebuild_stages_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_SYSSCAN1` entered the system-scan block
and its scan driver. At least three item callbacks reached their local
initialize, prepare, and walk calls and returned from each; registration
callbacks also fired on other `lsd` threads. The scan driver and
system scan still had no return by the end of the 210-second window.
The probe sampled callback hits after the first three, so this does
**not** identify a last item or prove only three items were handled.
The next distinction is whether an asynchronous registration worker
remains in flight (and on which item) or all workers finish while the
driver's completion/drain does not.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_SYSSCAN1.lldb.log`
and `tools/re/ccs_ls_system_scan_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_REGWORK1` paired system-content item
pointers with asynchronous `registerICLItemForSystemContent:` workers.
The first 20 workers each entered and returned from the nested
`syncWrite:` and the registration method. The sampled **100th** worker
also returned from both. The scan driver did not return within 210
seconds, but these completed registrations show continuing first-seed
progress; the interval is not evidence of a deadlock or a specific bad
bundle. Jobs 21–99 were counted but not individually logged, and the
final count after 100 was not emitted. A longer, lower-overhead probe
must distinguish slow enumeration from a later outlier or a drain
problem.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_REGWORK1.lldb.log`
and `tools/re/ccs_ls_registration_callbacks.py`.

In the longer `UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS1` run, the
original system scan continued past 1,000 registrations. The scan
driver **returned naturally** after about 1,060 registrations;
`performSystemContentScan:` and
`__LSServer_LSEnumerateAndRegisterAllBundles` then returned. The
original seed reached its later `syncWrite:` call. This rules out the
system scan as a permanent deadlock in this boot and shows that the
earlier 210–330 second windows stopped during real first-seed work.
The seed write's return, seeding-count drop, and SpringBoard/CCS
unwind remain separate checkpoints until measured.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS1.lldb.log`
and `tools/re/ccs_ls_seed_progress_callbacks.py`.

`UI_CLASS16_REFRESH60_CCS_LS_FINALIZE1` booted from that saved child with
read-only hooks at the original seed's `syncWrite:` call/return, builder
return, seed completion, SpringBoard's blocking LS call, and the shell
unwind. No database-missing seed or blocking SpringBoard store request was
observed in the 1,200-second window. The seeding count was zero when
`CCSControlCenterServicesManager.startServices` reached its return site;
there was no SpringBoard critical reboot or kernel panic. This shows the
saved guest progressed past the earlier LS/CCS wait, but does **not**
directly prove that the previous boot's `syncWrite:` returned because that
return site was not armed there. `_FBSystemShellInitialize` still did not
reach its normal return site. The immediate next distinction is whether
this specific CCS return resumes the SpringBoard call at `0x2244cf0c0`.
A 600-second child-of-child correlation run (`CCS_AFTERSEED1`) hit neither
SpringBoard site, so it provides no caller verdict.

`UI_CLASS16_REFRESH60_CCS_CALLERPAIR1` returned to the earlier saved
`SEEDPROGRESS1` parent and armed the missing SpringBoard call-site pair.
`SB_BEFORE_CCS_START` at `0x2244cf0bc`, `CCS_START_RETURN` at
`0x259097f8c`, and `SB_AFTER_CCS_START` at `0x2244cf0c0` all fired on
the **same SpringBoard thread** (`tpidr_el1` value
`0xffffffdfe7da3060` in this boot); the CCS return backtrace contains
the expected `0x2244cf0c0` return site. The shell then reached
`0x1c4a3bfcc` immediately after invoking the additional-services block.
The seeding count was zero at all four hits. The specific CCS invocation
therefore returned normally to its SpringBoard caller, and the remaining
unobserved shell return lies **after** the services block in this boot.
`SHELL_PRE_RETURN` did not fire before this probe was manually ended about
three minutes after the block return; there was no kernel panic.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_FINALIZE1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_CCS_AFTERSEED1.lldb.log`, and
`/tmp/dvm/UI_CLASS16_REFRESH60_CCS_CALLERPAIR1.lldb.log`, and
`tools/re/ccs_ls_seed_finalize_callbacks.py`.

`UI_CLASS16_REFRESH60_SHELL_AFTERCCS1` then bracketed the remaining
`_FBSystemShellInitialize` tail from the same `SEEDPROGRESS1` parent.
It reached `0x1c4a3bfcc` and `TAIL_C054` at `0x1c4a3c054`, but not the
next coarse site `TAIL_C0B0` at `0x1c4a3c0b0` or the shell return in
300 seconds. Static cache disassembly shows the first call in that interval
is the `_startSystemShellService` selector at `0x1c4a3c058`, with its
return site at `0x1c4a3c05c`; later in the interval are two milestone
calls and a `sharedInstance` call. The next probe must bracket
`0x1c4a3c058`/`0x1c4a3c05c` before attributing the wait to that service.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_SHELL_AFTERCCS1.lldb.log` and
`tools/re/fb_shell_tail_bracket_callbacks.py`.

`UI_CLASS16_REFRESH60_SHELL_SERVICE1` then hit the exact
`_startSystemShellService` call at `0x1c4a3c058` on the active
SpringBoard thread but not its return at `0x1c4a3c05c` in 300 seconds.
The FrontBoard method is a two-instruction forwarder: it loads its
`BKSSystemShellService` ivar at `+0x20` and tail-calls `start`. Static
BackBoardServices metadata and disassembly identify that concrete class
and method, so the next slice belongs there rather than at the outer
selector name.

`UI_CLASS16_REFRESH60_BKS_START1` confirmed the forwarded call enters
`-[BKSSystemShellService start]` with check-in status zero and
`waitForDataMigration=1`. Its `_setCheckInStatus:1` call returned;
`_checkInWithServerForReason:waitForDataMigration:` entered on the same
thread, took the migration path, and its
`endpointForMachName:service:instance:` lookup returned a non-null
endpoint. The check-in method and outer shell-service call did not return
within 300 seconds. The next read-only bracket covers
`connectionWithEndpoint:`, `configureConnection:`, `activate`, and the
remote-target/check-in sends after the endpoint. No BackBoard reply or
data-migration completion is yet proven.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_SHELL_SERVICE1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_START1.lldb.log`, and
`tools/re/fb_system_shell_service_callbacks.py`.

`UI_CLASS16_REFRESH60_BKS_CONNECT1` continued the same migration branch
with paired call/return stops. Endpoint lookup, `connectionWithEndpoint:`,
`configureConnection:`, `activate`, and two `remoteTarget` calls all
returned on the same SpringBoard thread. The first call with no observed
return in the 300-second window was
`checkInAfterDataMigrationUsingCompletionBlock:` at `0x18a1476a0`
(return site `0x18a1476a4`) on the remote target. This names the client
boundary, not a BackBoard server receive or reply. The next split is the
concrete Objective-C dispatch/client wait versus a correlated server-side
request; the existence of an endpoint and activated connection alone does
not prove a successful check-in.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_BKS_CONNECT1.lldb.log`.

`UI_CLASS16_REFRESH60_BKS_SEND1` followed that exact proxy call on the
SpringBoard thread for a further 300 seconds. The call at `0x18a1476a0`
entered, but its return site `0x18a1476a4` did not fire. The same thread
entered `mach_msg2_trap` twice in the BoardServices synchronous message
path. Symbolication of the captured stack gives
`BSXPCServiceConnectionMessage sendSynchronouslyWithError:` →
`BSXPCServiceConnectionMessage _sendWithMode:` →
`xpc_connection_send_message_with_reply_sync` →
`_dispatch_mach_send_with_result_and_wait_for_reply` →
`mach_msg2_trap`. Thus the active client-side boundary is a synchronous
XPC reply wait during `checkInAfterDataMigrationUsingCompletionBlock:`.
No check-in return or shell return was observed in that window. This
does not prove the server handled the request or establish that data
migration itself is stalled. The two constant strings passed to
`endpointForMachName:service:instance:` decode to Mach name
`com.apple.backboard.system-app-server` and service
`DataMigrationCheckIn`. The next slice is a correlated receive/handler
and reply for that specific endpoint. Static metadata names the protocol
`BKSSystemShellDataMigrationCheckInServerInterface`, but the active
server-side implementation has not yet been identified.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_BKS_SEND1.lldb.log` and
`tools/re/bks_checkin_send_callbacks.py`.

`UI_CLASS16_REFRESH60_BKS_SERVER1` resolved this boot's backboardd PIE base
as `0x10252c000` from a live Mach-O frame, then armed ten backboardd
checkpoints before the SpringBoard check-in call. On that call, backboardd
hit the migration listener at `base+0x164d4` and connection configurator
at `base+0x16610`. Their BoardServices stack includes
`BSServiceConnectionListener didReceiveConnection:` and
`BSService _didReceiveConnection:`, proving connection delivery. The
`base+0x433b4` migration check-in handler, its pending/immediate/complete
branches, and the SpringBoard call return did not hit during the bounded
360-second run. This is a handler-level non-hit, not yet proof of an XPC
method-routing defect: the first run did not pair the listener/configurator
entries with their returns. The next bracket adds those returns and samples
the backboardd migration state without changing the guest.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_BKS_SERVER1.lldb.log` and
`tools/re/bks_server_receive_callbacks.py`.

`UI_CLASS16_REFRESH60_BKS_SERVER_RETURN1` paired the server listener and
configurator entries with their returns. Both returned normally on the
same backboardd thread (`configureConnection` in about 9 ms, listener in
about 23 ms). The server singleton byte previously identified as
`systemAppBlocked` at `+0x50` sampled zero at both entries. The client
check-in call again had no return, and no `base+0x433b4` handler or
pending/immediate/complete branch hit in the 240-second window. This
rules out a non-returning listener/configurator as the immediate wait;
it does not yet show whether the method message reached BoardServices'
generic `connection:handleMessage:` dispatch. That generic dispatch is
the next checkpoint.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_BKS_SERVER_RETURN1.lldb.log`.

`UI_CLASS16_REFRESH60_BKS_MSG1` additionally armed the shared-cache
`-[BSXPCServiceConnectionEventHandler connection:handleMessage:]` entry at
`0x18f8baf64` and its `handleError:` sibling before the client check-in.
After the SpringBoard check-in call, the server listener and configurator
again entered and returned, but neither generic message/error entry nor
the `base+0x433b4` migration method handler hit in 210 seconds; the client
call also did not return. This places the observed gap after connection
accept/configuration and before an observed BoardServices message dispatch.
The generic message site still needs a backboardd positive control before
calling this a proven method-routing failure. It could also be an earlier
connection/handshake wait within the synchronous client send.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_BKS_MSG1.lldb.log`.

`UI_CLASS16_REFRESH60_BKS_MSG_COVER1` left the generic BoardServices
message/error hooks active before the SpringBoard call as a coverage
control, then rearmed them at the call. Backboardd again accepted and
configured the connection, both server methods returned, and no generic
message/error callback, migration method handler, or client return was
observed during 210 seconds. There were also no earlier backboardd
`connection:handleMessage:` hits in this boot, so this did **not** give a
positive control for that specific hook. The defensible conclusion remains
bounded: the connection arrives, but the method request has not been
observed at the generic or concrete handler. The client stack is within
`BSXPCServiceConnectionMessage _sendWithMode:` at its synchronous XPC send;
the next useful slice is the message/handshake boundary and server-side
XPC receive, with a positive control before declaring a routing defect.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_BKS_MSG_COVER1.lldb.log`.

The lower libxpc probes now have same-connection attribution. In
`UI_CLASS16_REFRESH60_BKS_XPC_WRAP1`, the migration-listener wrapper's
`+0x8` pointer matched the `BSXPCServiceConnection` passed to
`connectionInitialized:withActivationGeneration:activeXPCConnection:...`.
That callback mapped it to XPC connection `0x781304c3c0`, whose read-only
peer-PID field was 35 (SpringBoard). About 50 ms after the client's
synchronous send, `_xpc_connection_mach_event` and
`_xpc_connection_call_event_handler` both ran on that exact XPC pointer.
The server-side accepted connection's name at `+0xa8` was null, so name
reads alone cannot identify the service; pointer continuity supplies the
identity. This proves an event reached libxpc on the migration connection,
but does not identify that event as the method payload. The more general
`UI_CLASS16_REFRESH60_BKS_XPC_RX1` and `...XPC_PEER1` runs validated
the low-level hooks and peer-PID read but were not alone sufficient for
service attribution.

`UI_CLASS16_REFRESH60_BKS_XPC_DISPATCH1` then saw that mapped pointer
receive an event, enter the libxpc handler, and reach
`BSXPCServiceConnection::_connection_handleMessage:fromPeer:withHandoff:`.
The concrete backboardd `DataMigrationCheckIn` handler still did not fire.
Static disassembly shows `_connection_handleMessage` also handles
activation and child-handoff messages, so entry alone is not proof of a
decoded RPC. `UI_CLASS16_REFRESH60_BKS_XPC_BRANCH1` resolved the branch:
it hit the child-identifier check and
`_lock_enqueueChildActivateEvent:forHandoff:` call, skipped the ordinary
`connection:handleMessage:` call, then returned in about 14 ms. The client
remained in its synchronous wait. The immediate missing boundary is the
queued child activation/handoff and whatever later delivers the concrete
method request, not the initial Mach/XPC connection. The next read-only
probe records the parent connection's `+0xa6/+0xa7` activation bytes,
pending-event slot at `+0x50`, and whether
`_connection_consumeLock_runPendedEventsIfAppropriate` runs.

`UI_CLASS16_REFRESH60_BKS_CHILD_PENDING1` showed that the matched
`BSXPCServiceConnection` had `+0xa6=1`, `+0xa7=1`, and `+0xab=0` at
`_lock_enqueueChildActivateEvent:forHandoff:`. It took the **immediate**
arm at `0x18f8bb1dc`, not the pending-array arm at `0x18f8bb210`, and the
parent `_connection_handleMessage` returned about 14 ms after entry.
Disassembly shows the immediate arm tail-calls the connection's
`BSServiceQueue` at `+0x28` with `_performAsync:withHandoff:`. The queued
block is the unique `0x18f8b9848` block, which tail-calls
`_connection_handleMessage:fromPeer:withHandoff:` again with handoff zero.
The next boundary is whether that block executes, and on which queue;
only then can the normal BoardServices method-dispatch arm be scored.

`UI_CLASS16_REFRESH60_BKS_CHILD_ASYNC1` reached
`-[BSServiceDispatchQueue _performAsync:withHandoff:]` on the same
handoff: its block argument `x2=0x104b86268` matched the block passed to
`_lock_enqueueChildActivateEvent:forHandoff:`. The parent message method
returned, but the unique `0x18f8b9848` child block did not execute in the
210-second window; no second `_connection_handleMessage` on that path,
concrete migration handler, or SpringBoard check-in return was observed.
Disassembly of `_performAsync:withHandoff:` shows that a non-null handoff
dictionary takes `xpc_dictionary_handoff_reply` rather than the ordinary
`dispatch_channel_async` arm. The observed handoff argument was non-null.
That lower handoff/reply scheduling call is the next call/return bracket.
The current evidence identifies a bounded absence of child-block
execution, not yet why libdispatch did not invoke it.

`UI_CLASS16_REFRESH60_BKS_HANDOFF_LIGHT1` did not arm because LLDB could
not resolve a callback imported by the lean wrapper; it is non-evidence.
The corrected `...HANDOFF_LIGHT3` run observed SpringBoard check-in, matched
child enqueue, `xpc_dictionary_handoff_reply`, the dispatch-Mach arm and
`dispatch_mach_handoff_reply`, both handoff returns, then the same-thread
parent BoardServices handler return about 53 ms after check-in. The original
child block did not execute in the 360-second window and SpringBoard did not
return. A later handoff on a different BS connection did execute the same
block-invoke function, validating that breakpoint but not completing the
original check-in. The original queue and reply identifier were
`0x787f1f2f00` and `0x1ef07`; the later case used `0x787f0ffb00` and
`0x20833`. These are identities, not yet a causal difference.

The local `swift-corelibs-libdispatch/src/mach.c` implementation shows that
the continuation is enqueued only after the **outer dispatch-Mach message
work item** returns, not merely after its BoardServices callout returns.
Guest disassembly independently places the post-callout test at
`0x1ae8d5cb4`, the handoff-async call at `0x1ae8d5db4`, and
`_dispatch_mach_ipc_handoff_async` at `0x1ae8d5ab8`. The guest block form
also calls `_dispatch_mach_handoff_set_wlh` at `0x1ae8d5750`, unlike the
open-source block form in this checkout. The next A/B must compare the
original and working cases through that outer work-item test, async enqueue,
and selected queue/workloop state before calling the queue scheduler stuck.

`UI_CLASS16_REFRESH60_BKS_HANDOFF_QUEUE1` and `...QUEUE2` armed their
handoff checkpoints but did not reach SpringBoard's check-in in 330 and
600 seconds, respectively. They are timing/coverage misses for the handoff.
The upstream-augmented `...QUEUE3` reached the request path on the same
SpringBoard thread: `_startSystemShellService` at 21:11:30.706,
`BKSSystemShellService::start` four milliseconds later, and the check-in
at 21:11:30.759. The matched child block was `0x106986268`; its handoff
dictionary was `0x74b2c44500`, queue `0x74b31dfb80`, and Mach reply
identifier `0x1c233`. Guest `_dispatch_mach_handoff_set_wlh` wrote the
nonzero `dih_wlh=0x74b31dde00` at continuation object
`0x74b2c24050+0x40`. The parent BS handler returned, then the **outer**
dispatch-Mach post-callout branch reached `_dispatch_mach_ipc_handoff_async`
with that same continuation object. Its virtual queue enqueue call returned
at `0x1ae8d5b04` at 21:11:30.818. Neither the object-matched
`_dispatch_mach_ipc_handoff_invoke` nor the child block nor SpringBoard's
check-in return fired before the window ended at 21:19:31. This is a
bounded **post-enqueue / pre-invoke** gap. It does not yet identify why the
queue did not invoke this continuation; a working handoff did not occur in
this boot for a same-boot queue/workloop comparison. The read-only probe
captures continuation bytes and queue/context bytes for that future A/B;
the open libdispatch source supplies control-flow order, while the guest
binary supplies actual field use and callsites. The `...QUEUE3` guest was
released after preserving its disk child and logs. A direct LLDB read of
`0x74b2c24050` from the paused kernel/vCPU context failed address
translation, including after selecting another exposed vCPU; this adds no
evidence about whether the continuation remains linked.

`UI_CLASS16_REFRESH60_BKS_HANDOFF_STATE1` used that live backboardd-context
capture on the same saved parent and unchanged display fixtures. SpringBoard
reached check-in and the matched handoff continuation was
`dih=0x7626c26580`, target queue `0x7626d9ba00`, target/workloop
`0x7626d99f00`. Before enqueue, the queue's `+0x30` field held
`0x105a01100`. Immediately after the virtual enqueue call returned it held
the exact `dih` pointer. Later backboardd callouts at +2.24 and +16.66
seconds still found that pointer at queue `+0x30`. Guest
`_dispatch_lane_push` disassembly identifies `+0x30` as the lane's item
tail, and `+0x68` as its head. Thus these snapshots establish that the
migration continuation remained the tail; they do **not** show it at the
head or prove that the queue had no older work.
The continuation's `+0x40` workloop field changed from `0x7626d99f00`
at enqueue return to `0xffffff89d92660ff` at the later callouts, the
64-bit bitwise complement of the original value. Guest disassembly shows
the post-enqueue call and `cas` of `wlh` to `~wlh` at
`0x1ae8d5b18`–`0x1ae8d5b20`, so the handoff activation bookkeeping ran.
No object-matched `_dispatch_mach_ipc_handoff_invoke`, child block, or
SpringBoard check-in return appeared during the 600-second window. The
bounded finding is an enqueued continuation still at the queue tail at
+16.66 seconds without a later observed invoke; it does not yet prove
queue starvation or why this continuation was not serviced.

`UI_CLASS16_REFRESH60_BKS_HANDOFF_DRAIN1` did not arm its callbacks and is
non-evidence. The corrected `...DRAIN2` run armed all 16 base sites and,
on the matched migration handoff, dynamically armed six queue-specific
push/wake/invoke/drain sites. `SpringBoard` reached check-in; the matched
`_dispatch_lane_push` took the exact lane `0x7788e0c000` and migration
continuation `0x7788c52260`. Before push, that lane already had head
`0x101701900` at `+0x68` and tail `0x101901400` at `+0x30`.
`_dispatch_lane_push` appends via a swap of `+0x30` and links the new
continuation from the old tail; its nonempty-queue branch can return
without a new wakeup. After push, the migration continuation was the new
tail, while the old head stayed `0x101701900` in every live sample through
+10.71 seconds. No matched lane/workloop wake, invoke, or drain occurred
after dynamic installation, and no object-matched handoff invoke, child
block, or SpringBoard check-in return appeared in the 600-second window.
This is bounded evidence of a **pre-existing nonempty lane whose head did
not advance in the sampled interval**. It does not yet identify the older
head item or establish whether a drainer was queued before the hooks armed.
The next cut is the head item's type/invoke chain and the lane's earlier
wake/drain state, rather than another migration enqueue probe.

`...HEAD1` stopped immediately after the matched enqueue and read the
lane's `do_next` chain in live backboardd context. It contained exactly
two older items before the migration continuation. `...HEAD2` confirmed
both older items are generic block continuations (`flags=0x114`) with
`dc_func=_dispatch_call_block_and_release`; their shared `+0x18` pointer
is the target queue, not a shared block context. `...HEAD3` then read the
distinct block contexts at continuation `+0x28`. Their block-header invoke
addresses resolve in this guest's BoardServices image to:

- head: `___55-[BSXPCServiceConnection _lock_activateNowOrWhenReady:]_block_invoke_2`
  (`0x18f8d818c`), which acquires the connection lock and tails into
  `_connection_consumeLock_runPendedEventsIfAppropriate`;
- next: `___55-[BSXPCServiceConnection _lock_activateNowOrWhenReady:]_block_invoke.294`
  (`0x18f8d68dc`), which runs the activation callback and calls
  `BSXPCServiceConnectionEventHandler connectionActivated:`;
- tail: the known migration child block
  `___73-[BSXPCServiceConnection _connection_handleMessage:fromPeer:withHandoff:]_block_invoke`
  (`0x18f8b9848`).

Thus the migration handoff is behind two **connection-activation** blocks
already queued on the same lane. The captured blocks had not executed when
the migration item was appended; the trace has not yet shown why the lane's
earlier activation work is not drained. The next probe should bracket the
first activation-block enqueue/wake and the lane's target workloop state
before the migration handoff arrives.

`UI_CLASS16_REFRESH60_BKS_ACTIVATION_QUEUE1` bracketed those earlier
pushes. The first pending-events block entered `_dispatch_lane_push` on an
**empty** activation lane (`head=tail=0`). That push took the lane wake
path, placed the activation lane on an initially empty target lane, and
woke the target lane. The first push returned with the pending-events
block as both activation-lane head and tail. The later
`connectionActivated:` block appended behind it; no second wake was
required for that nonempty lane. Both lanes remained nonempty in the
sampled state and neither lane invoke/drain was observed after the hooks
armed. The target lane's `do_targetq` points into libdispatch's root-queue
array (in this boot `0x1fa58d6c0`, unslid `0x1e6ef56c0`). Thus the first
activation block was **not** lost through a missing wake on either of the
two serial lanes. The next boundary is target-lane submission to that root
queue, followed by root worker wake/drain. This run was stopped after the
early enqueue/wake decision was captured; it did not establish a new
full-window absence of the later migration handoff.

`UI_CLASS16_REFRESH60_BKS_ACTIVATION_ROOT1` armed root push/wake/drain hooks
when that first activation block arrived; none matched the target root before
the manually stopped window. `...ACTIVATION_WAKE1` showed the target lane
waking but none of the later `__dispatch_queue_wakeup` target-push or
event-loop arms. `...BKS_WAKE_STATE1` then bracketed the earlier state
decision in the guest libdispatch function. On the target lane, the CAS
changed `dq_state` from `0x60002600007a01` to `0x6000a600007a01`: only
bit 39 changed. The guest's subsequent `tst` tested the state delta against
mask `1`, branched to the fallback because no low enqueue bit changed, found
no change in the `0x700000000` QoS field, and reached the return arm rather
than the root target-push arm. The target state already had low bit 0 set
before this wake. Open libdispatch labels bit 39 `DISPATCH_QUEUE_DIRTY` and
low bit 0 `DISPATCH_QUEUE_ENQUEUED`; the guest branch itself proves only that
this wake marked the target dirty and did **not** submit it again to root.
It does not establish whether an earlier root submission has a running,
blocked, or missing drainer. The next read-only target is the target lane's
existing enqueue/drain ownership and its earlier root submission, not a new
push from this wake. This boot was manually stopped after the branch was
captured; it is not a full-window absence claim.
The activation-gated snapshots already contain a useful ownership clue:
**before** the first activation block is pushed, the target lane has empty
head/tail fields but a set enqueue bit and nonzero low drain-owner bits. The
owner bits were `0x7100` in `...ACTIVATION_QUEUE1`, `0x7a00` in
`...WAKE_STATE1`, and `0x6200` in `...ROOT_HISTORY1`. Open libdispatch uses
those low bits as the drainer's Mach thread port; the guest's exact owner
must still be correlated to a live thread or an earlier invoke. These
snapshots support a lane that libdispatch already considers owned/enqueued,
but cannot yet distinguish an active, blocked, or stale owner. The probe now
decodes those fields at the same activation-gated snapshot, without adding
a broad boot-time hook.

`UI_CLASS16_REFRESH60_BKS_OWNER_FROZEN1` resolved that owner on an
activation-gated, read-only snapshot. The target lane's pre-push state was
`0x60002600017919`, with owner bits `0x17918`. Guest
`pthread_mach_thread_np` reads the current Mach thread port at
`pthread+0xf8`; the activation thread's port was `0x5913`, so it was not
the owner. A frozen backboardd task-thread walk found live XNU thread
`0xffffffe21c3f60c0` with pthread port `0x1791b`, whose masked owner bits
are exactly `0x17918`. Its saved user stack contains
`_dispatch_lane_serial_drain` / `_dispatch_lane_invoke` below
`__DMPerformMigration` → `-[DMConnection migrateCheckingNecessity:...]` →
`-[DMXPCConnection sendMessageSync:]` →
`_xpc_connection_send_message_with_reply_sync` → `_mach_msg2_trap`.
Thus the target lane **has an active drainer assigned**, and that drainer is
blocked waiting for a DataMigrator synchronous XPC reply. The activation
blocks and later migration handoff queue behind this already-running work;
the absent post-activation root push/invoke is expected. This establishes a
blocked owner, not stale ownership or failed root submission.

The same frozen snapshot contains `com.apple.datami` (PID 120). Its two
`-[DMXPCConnection _handleMessage:]` threads synchronously wait on serial
queue `0x1011eea00`. That queue's owner bits are `0x1420`, matching a live
DataMigrator worker's pthread Mach port `0x1423`. The worker is itself in
`_dispatch_sync_f_slow`, awaiting `__dispatch_main_q` at `0x1eb7a0dc0`.
The main queue owner bits are `0x100`, matching DataMigrator's main thread
port `0x103`. The main thread is blocked in an infinite
`dispatch_semaphore_wait` from executable return site `0x100a6d5cc`.
These queue identities were recovered read-only from the saved dispatch
wait frames and the queues' state words; they turn the earlier anonymous
server-side waits into this dependency chain:

```
backboardd target-lane owner: DMPerformMigration sync XPC
  -> DataMigrator XPC handlers: sync wait on queue 0x1011eea00
  -> that queue's worker: sync wait on DataMigrator main queue
  -> DataMigrator main thread: dispatch_semaphore_wait(DISPATCH_TIME_FOREVER)
```

The main-thread callback is more specific than a generic semaphore wait.
DataMigrator executable code at preferred `0x1000015bc` calls
`xpc_connection_send_barrier(connection, block)` and at `0x1000015c8`
waits on the semaphore. The block invoke at preferred `0x100001874`
tail-calls `dispatch_semaphore_signal` on that same captured semaphore.
Guest import pointers in the frozen process resolve those three APIs.
The XPC connection at `0x740f044000` points to a dispatch-Mach channel at
`0x740ec58320`; that channel's target is `0x740f038200`, whose target is
`0x1eb7a13c0`, rather than the main queue. In the same snapshot, that
intermediate target's head and tail both point to the dispatch-Mach channel;
its enqueue bit is set, but its drain-owner bits are zero. This is a
single-time state sample, not a bounded drain failure. The channel's send
references at `0x740f034418` have **the same `MACH_SEND_BARRIER` continuation
`0x101900680` as both head and tail**. Its copied block at `0x740ec88000`
has invoke `0x100a6d874` and captures the waiting semaphore
`0x740f05a260`. The continuation's vtable `do_invoke` resolves to guest
`__dispatch_mach_barrier_invoke` (`0x1ae8d5e14` unslid). Thus an older
*queued* send is not ahead of this barrier. The subsequent read-only live
probe `UI_CLASS16_REFRESH60_DM_BARRIER2` followed the same barrier pattern
for 600 seconds: **27/27 matched waits received a semaphore signal**,
25/27 had an exact item-keyed Mach invoke observed, and the longest
entry-to-signal interval was about 11 seconds. Neither of the two missing
invoke-hook hits lacked a signal. The frozen barrier was an in-flight
synchronization point, not evidence of a persistent blocker. The open
question moves back to backboardd's original `DMPerformMigration` call:
whether its synchronous request returns, and, if so, which later migration
phase (if any) fails to unwind. The DataMigrator/backboardd dependency
chain above remains a snapshot of one moment, not proof of a cycle.

The follow-up call/return probes on the same saved parent also show boot
variation. `UI_CLASS16_REFRESH60_DM_ROUNDTRIP1` armed 12 backboardd sites and
recorded `__DMPerformMigration` entry but no later `DMConnection` call or
return in 600 seconds. `UI_CLASS16_REFRESH60_DM_PRELUDE1` reached the
`DMEnvironment` first-check return (`0`) and post-preflight site, then did
not return from `+[DMConnection connection]` in 600 seconds.
`UI_CLASS16_REFRESH60_DM_CONN_INIT2` proved the same-thread class method
entered, `-[DMConnection init]` entered, its superclass init returned, and
it called `-[DMXPCConnection initWithServiceName:]`; that call did not return
within 600 seconds, before foreground-session targeting, `resume`, or the
migration RPC. `UI_CLASS16_REFRESH60_DM_XPC_INIT1` armed the initializer's
inner queue/XPC construction sites but reached only the top-level migration
entry in its 600-second window, so it is **non-evidence for the initializer's
inner call/return split**. The earlier frozen boot's sync-send stack remains
a separate, later state. These runs do not yet establish one persistent
non-returning call across boots.

`UI_CLASS16_REFRESH60_DM_XPC_INIT2` did reach that initializer. On one
backboardd thread, both dispatch-queue creations, `_xpc_init_services`,
`xpc_connection_create_mach_service`, target-queue assignment, event-handler
installation, and `-[DMXPCConnection initWithServiceName:]` return all
completed in milliseconds. `+[DMConnection connection]` did not return in
the subsequent 600-second window. Thus XPC connection construction is
**not a generally broken step**; this boot's remaining local split is
`-[DMConnection init]` after construction: foreground-session targeting,
then `resume`. The prior boot that stopped inside the initializer is kept
as a separate bounded observation, not merged into this one.

`UI_CLASS16_REFRESH60_DM_CONN_INIT3` then followed the enclosing
`-[DMConnection init]` on a fresh child: `initWithServiceName:` returned,
and the next call, `-[DMXPCConnection
targetForegroundUserSessionIfNecessary]`, entered but did not return in
600 seconds. No `resume`, class-method return, or migration RPC was reached
on that thread. Static code inside the pending method calls, in order,
`xpc_user_sessions_enabled`, optionally
`xpc_user_sessions_get_foreground_uid`, and then
`xpc_connection_set_target_user_session_uid`. The next live bracket must
distinguish those inner calls before assigning cause to user-session state.

`UI_CLASS16_REFRESH60_DM_FOREGROUND1` armed those sites but reached only
the earlier `+[DMConnection connection]` call, so it is non-evidence for
foreground targeting. To avoid repeatedly probing only one possible branch,
`UI_CLASS16_REFRESH60_DM_FULL_LADDER1` installed a full, read-only nested
ladder only after the matched backboardd `__DMPerformMigration` entry. In
that boot, all XPC connection-construction stages returned; user sessions
were enabled, foreground UID `501` was obtained and assigned, `resume`
returned, and `+[DMConnection connection]` returned. The next method,
`-[DMClientAPIController
_migrateWithConnection:checkNecessity:lastRelevantPlugin:testMigrationInfrastructureOnly:]`,
was called but did not return in 600 seconds. The later
`-[DMConnection migrateCheckingNecessity:...]` entry and synchronous send
were absent on that same thread. Thus the earlier construction and
foreground-targeting holds vary by boot and are not generally blocking;
this boot's first unreturned call is inside the client controller's migration
prelude or retry loop. Its static code queries
`connectionMigrationTimeIntervalToLastRetryDate` and
`connectionMigrationMaximumAttempts` before the first connection migration
call, which are the next live return edges.
`UI_CLASS16_REFRESH60_DM_FULL_LADDER2` dynamically armed those controller
sites but took an earlier path: `+[DMEnvironment sharedInstance]` returned,
then no first-environment-check return was observed at `0x25e8f031c` in the
600-second window. The selector stub is `deviceModeIsSharediPad`; its method
first calls `_MKBUserTypeDeviceMode` at `0x25e8f3420`. This boot therefore
does not exercise the controller sites and does not contradict the first
full-ladder boot. The next run brackets the MobileKeyBag call and return
without changing the saved parent or display fixture.
`UI_CLASS16_REFRESH60_DM_FULL_LADDER3` showed the MobileKeyBag call and
environment check both returning within milliseconds. It reached the
controller's retry-date calculation, but the next max-attempts site was not
observed during the full window. This **does not establish** a wait in the
intervening autorelease claim or release: the 70-site probe also missed a
controller call site while recording its immediately following return on the
same thread, proving that absence in this broad run is not reliable. A
smaller controller-only ladder now brackets the adjacent instructions and
requires matching call/return evidence before naming the next blocker.
`UI_CLASS16_REFRESH60_DM_CONTROLLER_SMALL1` armed 21 nearby sites on the
matched `__DMPerformMigration` entry, but this boot stopped earlier: the
first environment check returned `0`, then the call to `+[DMConnection
connection]` was observed with no matching return in its 600-second window.
No controller site was exercised. This is a new bounded observation of the
boot-varying construction path, not evidence that the controller's retry-date
step returned or hung on this child. The next useful sparse bracket is inside
`-[DMConnection init]` on a boot where the class call enters it.
`UI_CLASS16_REFRESH60_DM_CONN_INIT4` then recorded positive returns from
`-[DMXPCConnection resume]`, `-[DMConnection init]`, and
`+[DMConnection connection]` within milliseconds. Several earlier sites on
that same straight-line initializer path were silent despite being reported
as armed. Therefore, even 15 simultaneously armed guest breakpoints do not
provide dependable *negative* evidence here. The follow-up
`dm_migration_rolling_callbacks.py` enables only the current edge (plus its
enclosing return) and arms the next call/return from the preceding hit.
The earlier callback also keyed progress to LLDB's vCPU thread ID and
re-read the process name at every site; guest-thread migration or a failed
name read could hide a valid later hit. The rolling probe now keys later
sites to the `tpidr_el1` value captured on the backboardd entry. An absent
callback still needs a bounded window and, ideally, a live stack sample
before it is treated as a non-returning operation.
`UI_CLASS16_REFRESH60_DM_ROLLING1` kept only one current edge plus the
enclosing return armed. On the same backboardd thread it observed
`__DMPerformMigration` entry, the class connection call, `-[DMConnection
init]` entry, the `initWithServiceName:` call and return, then
`targetForegroundUserSessionIfNecessary` call. Neither its return nor the
enclosing class-method return appeared in the 600-second window. This is a
better **boot-specific** foreground-targeting bracket than the earlier
dense-hook runs, but its callback still used the vCPU/thread-ID filter noted
above. The inner libxpc call remains unnamed in this boot. The revised rolling
probe follows `xpc_user_sessions_enabled`, foreground UID lookup, and
target-session assignment within that method using `tpidr_el1` continuity.
`UI_CLASS16_REFRESH60_DM_ROLLING2` positively followed the same outer
sequence into `targetForegroundUserSessionIfNecessary`, observed
`xpc_user_sessions_enabled` return `1`, then observed the call to
`xpc_user_sessions_get_foreground_uid`. No return from that UID call was
logged in 600 seconds. This run still used the vCPU-ID filter and per-hit
process-name read, so the **missing** return remains provisional. Static
libxpc code shows the UID helper calls
`__xpc_user_sessions_info_routine`, which invokes `_xpc_interface_routine`
with request `0x388`. The next run uses `tpidr_el1` continuity and arms
those two libxpc entry/return edges only after the UID call.
`UI_CLASS16_REFRESH60_DM_ROLLING3`, using the guest-thread pointer rather
than the vCPU ID, has positively observed the full ordered foreground path:
`xpc_user_sessions_enabled` returned `1`, the foreground-UID helper entered
`__xpc_user_sessions_info_routine`, its `_xpc_interface_routine(0x388)`
returned, the UID helper returned, target-session assignment returned,
and `resume`, `-[DMConnection init]`, and `+[DMConnection connection]` all
returned. The controller then completed its date, retry-interval,
retry-date, autorelease, and max-attempts (`3`) steps and entered
`-[DMConnection migrateCheckingNecessity:...]`. This proves the earlier
apparent XPC-construction, foreground-UID, and controller-prelude stalls
are not general non-returning calls. The migration attempt's return remains
the open edge for this boot: it was not observed through the full 600-second
window, and SpringBoard still did not reach shell return. The rolling probe
has been extended to follow its `sendMessageSync:` call and XPC reply
boundary on the next boot.
`UI_CLASS16_REFRESH60_DM_ROLLING4` did so. On the same guest thread it
observed `-[DMConnection migrateCheckingNecessity:...]` entry, the call
into `-[DMXPCConnection sendMessageSync:]`, and the underlying synchronous
XPC send at `0x25e8f5798`. Its return at `0x25e8f579c` was not observed
within 600 seconds; neither was shell return. The request dictionary built
immediately before that call carries `msgID = 0` and `checkNecessity`.
Thus this boot's named wait is the migration RPC reply, after successful
connection construction and foreground-session targeting. This does not
establish whether the server received the request or whether its migration
work is slow. `dm_migration_server_receive_callbacks.py` is the next
read-only, client-triggered server-handler entry/return correlation.
`UI_CLASS16_REFRESH60_DM_SERVER_RECEIVE1` recorded the backboardd sync
send at t=1790887836.736 and a `com.apple.datamigrator`
`-[DMXPCConnection _handleMessage:]` entry at t=1790887836.745, about
9 ms later. The daemon logged many further generic handler entries, so its
receive path is active; no return was logged for the first server thread or
the client sync send during 600 seconds. This is **temporal server-side
correlation**, not yet proof that the first dictionary is the `msgID = 0`
migration request. `dm_migration_server_msgid_callbacks.py` now follows the
first handler's dictionary branch and a read-only
`xpc_dictionary_get_int64(dict, "msgID")` call/return on that exact XPC
dictionary pointer.
`UI_CLASS16_REFRESH60_DM_SERVER_MSGID1` closed that correlation on the next
boot: backboardd entered the migration sync send at t=1790888482.7283;
`com.apple.datamigrator` entered its first `-[DMXPCConnection
_handleMessage:]` at t=1790888482.7379, took the dictionary branch, and
read `msgID = 0` from that *same server dictionary pointer* at
t=1790888482.7467. The client request is also statically constructed with
`msgID = 0` and `checkNecessity`. This is strong request-path attribution,
though `msgID` alone is not a globally unique transaction identifier. The
next server bracket is the dictionary dispatch path, followed by the client
sync return. `UI_CLASS16_REFRESH60_DM_SERVER_DISPATCH1/2` did not score the
block-call site. The apparent `x0` block pointer at `0x25e8ef3e8` was a
pre-instruction register value, so it did **not** establish the stored block
field. Those two short boots were stopped as branch-coverage probes, not
treated as a blocked call. `DM_SERVER_DISPATCH3` reads the actual
`DMXPCConnection` field at `+0x30` and pre-arms both branch arms: the field
was null, so the matched request took the Objective-C fallback call at
`0x25e8ef40c`. The stub at `0x260035990` loads the selector
`handleMessage:`; the base `-[DMXPCConnection handleMessage:]` at
`0x25e8f5680` immediately returns, so the daemon is using an overriding
implementation. The fallback call's return at `0x25e8ef410` and
backboardd's sync-send return were both absent through the full 600-second
window. The next bracket belongs inside that daemon-side override, with
read-only thread/stack evidence before attributing a particular dependency.
`UI_CLASS16_REFRESH60_DM_SERVER_OVERRIDE1` matched that fallback to the
daemon's `msgID` lookup: its caller lives in the DataMigrator main executable
at static `0x100001b14`, within a large message-dispatch function beginning
at `0x100001ac0`. A 300-second window again saw neither the fallback return
nor backboardd's sync return. The `msgID = 0` jump-table arm begins at
`0x100001c78` and reads `checkNecessity`, `lastRelevantPlugin`, and
`testMigrationInfrastructureOnly`; it later calls the selector
`migrateCheckingNecessity:lastRelevantPlugin:testMigrationInfrastructureOnly:connection:migrationResultHandler:`
at static `0x100002a8c`. This selector is the next call/return bracket; its
entry is a static candidate, not yet a live hit.
`UI_CLASS16_REFRESH60_DM_SERVER_MIGRATE1` reached the live `msgID = 0`
arm, prepared the migration arguments, and called that selector at
`0x100002a8c` with `checkNecessity = 1`,
`lastRelevantPlugin = nil`, and `testMigrationInfrastructureOnly = 0`.
Neither its return at `0x100002a90` nor the daemon fallback or backboardd
sync-send return appeared within 600 seconds. Offline relative Objective-C
method-list decoding of the same DataMigrator executable maps the selector
to `-[DMMigrator migrateCheckingNecessity:...]` at `0x10000fe44`.
Its normal arm calls
`startMigrationAndOrBlockIfNecessaryWithClientInvocation:buildVersion:migrationStarterBlock:`
at `0x10000ffc8` (return `0x10000ffcc`). `dm_migration_server_inner_callbacks.py`
adds that inner call/return to the same matched-request ladder; the method
entry and that inner call are static candidates pending a live hit.
`UI_CLASS16_REFRESH60_DM_SERVER_INNER1` was unarmed because LLDB could not
resolve the wrapper module's callback; it is non-evidence. After fixing the
callback name and verifying all command lists,
`UI_CLASS16_REFRESH60_DM_SERVER_INNER2` hit the matched daemon's
`-[DMMigrator migrateCheckingNecessity:...]` entry and its normal arm,
then entered the `startMigrationAndOrBlockIfNecessary...` call at
`0x10000ffc8`. That call, its caller, the daemon fallback, and backboardd's
sync send all lacked returns through 600 seconds. Relative Objective-C
metadata maps the inner selector to `-[DMMigrationState
startMigrationAndOrBlockIfNecessaryWithClientInvocation:buildVersion:migrationStarterBlock:]`
at static `0x100008a98`. Its first lock call at `0x100008af0` resolves
to `_pthread_mutex_lock` after subtracting the frozen shared-cache slide.
`dm_migration_state_callbacks.py` now brackets that lock and later work;
the mutex was only a candidate. `UI_CLASS16_REFRESH60_DM_STATE_MUTEX1`
later showed `_pthread_mutex_lock` at `0x100008af0` returning at
`0x100008af4`, and `_dispatch_async` at `0x100008d74` returning at
`0x100008d78`, on the matched DataMigrator request. None of the state
method, daemon fallback, or backboardd synchronous send returned in the
600-second window. `dm_migration_state_tail_callbacks.py` brackets the code
after that async submission. In `UI_CLASS16_REFRESH60_DM_STATE_TAIL1`,
the mutex unlock at `0x100008d8c` returned, `isProgressHost` returned true,
and execution reached `0x100008e74` without its return at `0x100008e78`
yet. The imported target of that call is `_dispatch_sync` (resolved from
the captured Mach-O GOT pointer after subtracting the frozen cache slide).
Its queue is the `DMMigrationState + 0x98` queue created with label
`com.apple.datamigrator.migration`; the sync block's invoke at
`0x1000090d8` is an immediate `ret`. The matched request therefore waits
for prior work on that queue. The preceding `_dispatch_async` submits a
block whose invoke starts at `0x10000905c` and calls a captured migration
starter block. Whether that async work starts and returns is the next
request-specific split; a queue cycle is not yet proven. The full 600-second
tail window ended without the `_dispatch_sync` return at `0x100008e78`, the
state method return, the daemon fallback return, or backboardd's sync-send
return. `dm_migration_queue_work_callbacks.py` follows the preceding async
block by its captured pointer fields, accounting for `dispatch_async` copying
the stack block before invocation on another thread.
`UI_CLASS16_REFRESH60_DM_QUEUE_WORK1` reached the same matched msgID-0
request, submitted that async block to queue `0x1013fcd00`, returned from
`_dispatch_async`, and entered the `_dispatch_sync` barrier on the same
queue. The block-entry and callback breakpoints armed, but no entry matching
the copied block's captured pointer fields, no sync return, and no client
return appeared in the full 600-second window. The next queue-state probe
also logs the first few nonmatching block entries, so a copy-fingerprint
mismatch cannot be confused with no block invocation. It records queue
head/tail/state and exact-queue push/wake/invoke/drain at submission time.
`UI_CLASS16_REFRESH60_DM_QUEUE_STATE1` closed that ambiguity: at the
first `_dispatch_lane_push`, the queue was empty (`head = tail = 0`);
the exact item then reached queue wakeup, lane invoke, serial drain, and
the block invoke at `0x10000905c` on another daemon thread. The copied
block's address exactly equals the queue item's `+0x28` context, while
its captured pointer at `+0x28` differs from the original stack block;
the old fingerprint filter therefore labeled the hit `DM_ASYNC_BLOCK_OTHER`.
No callback-stage or client return was scored in the full window because
those hooks depended on that filter. The corrected lean probe keys the
block entry to the queue item's context pointer and brackets its first
helper call and migration-starter callback. There is no missing queue
wake/drain on this request.
`UI_CLASS16_REFRESH60_DM_BLOCK_INNER1` confirmed the context-pointer match:
the async block enters, its first helper call returns, and it invokes its
captured migration-starter block at `0x1000090a0`. That invocation did not
return at `0x1000090a4` in 600 seconds. Meanwhile the original daemon thread
entered `_dispatch_sync` on the same queue, now with `head = tail = 0`
because the worker owns the running block, and neither that sync call nor
backboardd's XPC send returned. Offline block construction at `0x10000ff90`
sets the starter invoke to `0x10001000c`, a trampoline into the local
Objective-C selector `_migrateWithTestMigrationInfrastructureOnly:buildVersion:lastBuildVersion:`.
The `DMMigrator` relative method list maps that selector to the method at
`0x100010020`. The next request-specific probe brackets that method and its
first nested call; the data-migration work inside the callback, rather than
queue wake/drain, is the active wait.
`UI_CLASS16_REFRESH60_DM_STARTER1` reached the exact starter-block
trampoline at `0x10001000c` and the `DMMigrator` migration method at
`0x100010020` on the queue worker's `tpidr_el1`. Its first nested
`_userCategoryWithEnvironment:` call at `0x100010094` returned, and the
method advanced through checkpoints `0x100010204`, `0x1000102f8`,
`0x100010454`, and `0x100010508`; no method/callback/XPC return appeared
in 600 seconds. The next static span builds the optional HID event queue:
`0x100010520` is a selector call, `0x100010548` creates the queue, and
`0x10001054c` is the post-create point. The following probe brackets those
calls and later coarse method stages. Reaching `0x100010508` alone does not
establish which of those subsequent calls failed to return.
`UI_CLASS16_REFRESH60_DM_STARTER_TAIL1` showed both HID-prep selector
(`0x100010520` → `0x100010524`) and HID queue creation
(`0x100010548` → `0x10001054c`) returning. The worker then reached
`0x100010608`, `0x10001068c`, and `0x100010740`, but no later coarse
checkpoint or callback/XPC return occurred in 600 seconds. Static code
between `0x100010740` and the next join at `0x100010794` calls
`setPreliminaryTasksCacheForUnitTesting:` at `0x100010748` and then
`_performMigrationWithPreliminaryTasks:pluginsProvider:pluginCategories:userCategory:buildVersion:lastBuildVersion:`
at `0x100010768`. The latter selector maps to `-[DMMigrator ...]` IMP
`0x10000db14`. The next probe brackets both call/return pairs and the
inner method entry. The `0x100010790` checkpoint can be skipped by a
branch, so its absence alone is not a stall verdict.
`UI_CLASS16_REFRESH60_DM_PERFORM1` hit the matched worker's
`setPreliminaryTasksCacheForUnitTesting:` call and return, then the
`_performMigrationWithPreliminaryTasks:...` call at `0x100010768` and
the target `DMMigrator` method entry at `0x10000db14`. Within that method,
checkpoints `0x10000dc98`, `0x10000dd60`, and `0x10000dfd0` fired on the
same daemon thread. Neither the inner method nor outer callback or XPC
send returned through 600 seconds. The next probe follows the method's
first item loop at `0x10000e010`, the task-ready stage at `0x10000e140`,
and the task callback call/return at `0x10000e178`/`0x10000e17c`.
The item-loop hook counts progress rather than assuming the first task is
stuck; there is not yet evidence of a deadlock or a particular bad plugin.
`UI_CLASS16_REFRESH60_DM_PERFORM_ITEM1` was unexercised for the inner method:
the first observed queue push belonged to the later sync item, after the
earlier async block had already begun. Its block-context filter rejected the
worker entry, so the task-loop breakpoints never armed. That 600-second
absence is a probe-coverage miss, not evidence that the item loop was skipped.
The lean retry matches the copied async block by its nested block's invoke
address, which is preserved across the stack-to-heap copy, before arming the
same inner-method sites. Static code at `0x10000dfd0` branches directly to
`0x10000e2bc` only when the prior result is zero; the prior live value was
`x0 = 2`, so the item-loop arm should execute on that earlier path.
`UI_CLASS16_REFRESH60_DM_PERFORM_ITEM2` used the corrected block matcher and
armed the inner method hooks. The matched worker reached item 1 at
`0x10000e010`, its task callback call at `0x10000e178` returned at
`0x10000e17c`, then item 2 entered at `0x10000e010` about 31 ms after the
first item. No third item, inner-method return, outer callback return, or
backboardd sync-send return appeared in the full 600-second window. The task
hooks in this run were one-shot, so this result places the bounded wait
somewhere inside item 2 but does not identify its nested call. The next probe
keeps item-stage and task call/return hooks active across both items, with
read-only bytes from each item and callback block.
`UI_CLASS16_REFRESH60_DM_PERFORM_ITEM3` corrected that interim reading:
item 2 reached all measured pre-task stages and its callback at
`0x10000e178` **returned** at `0x10000e17c` about 15.5 seconds later.
There are two items in this batch, so no third item was expected. No inner
method or backboardd client return appeared in the full 600-second window.
The next
useful cut is after the second callback: post-task bookkeeping
(`0x10000e180`-`0x10000e298`), the next-batch call at `0x10000e2b0`,
then method return. The item-2 callback itself is not a proven blocker.
`UI_CLASS16_REFRESH60_DM_PERFORM_POST1` showed both tasks' post-callback
bookkeeping reach the per-item finish call at `0x10000e284` and return at
`0x10000e288`. The loop test at `0x10000e298` ran twice; after item 2,
the next-batch call at `0x10000e2b0` returned zero at `0x10000e2b4`,
and the worker reached `0x10000e2bc`. The preliminary-task batch and its
drain are therefore finite on this child. The method continues into
concurrent plugin queue/executor setup after `0x10000e2bc`; the current
600-second window did not show the method or client XPC return. The next probe
brackets this setup rather than revisiting the two completed tasks.
`UI_CLASS16_REFRESH60_DM_PERFORM_PLUGIN1` showed the post-batch object
setup calls at `0x10000e2c8`, `0x10000e2d4`, `0x10000e2f0`,
`0x10000e308`, and `0x10000e338` all return. The worker reached
`0x10000e340`, `0x10000e400`, `0x10000e4d0`, and the plugin collection
count call at `0x10000e4e0`, which returned `54` at `0x10000e4e4`.
The loop setup at `0x10000e500` fired once; this is **not** a per-plugin
counter because subsequent iterations branch to `0x10000e504`. No loop
tail at `0x10000e5b4`, method return, or client XPC return appeared in the
full 600-second window, but that run cannot say which plugin was current.
`UI_CLASS16_REFRESH60_DM_PLUGIN_ITEM1` arms the per-plugin callback at
`0x10000e590` and its return at `0x10000e594`. Early in that run,
callbacks 1–3 returned and callback 4 entered, so the prior “first plugin
stall” interpretation was incorrect. In its full 600-second window,
callback 4 did not return, no fifth callback began, and the inner method
and backboardd XPC send remained in flight. This is a bounded outlier, not
proof of permanent deadlock. The next run dynamically arms a sparse
call-path ladder inside callback 4's block invoke at `0x10000e904`; it
should distinguish long plugin migration work from an earlier setup call.
`UI_CLASS16_REFRESH60_DM_PLUGIN4_INNER1` reached `0x10000ebc0` and no
later sparse checkpoint in 600 seconds. The first follow-up used a mistaken
return label at `0x10000ebc4`: disassembly shows that address is the call,
not its return. `UI_CLASS16_REFRESH60_DM_PLUGIN4_NAME1` read the callback-4
object name as `SystemAppMigrator.migrator` at the plugin deadline entry;
the same object pointer was passed into callback 4. Neither run established
which call failed to return.
`UI_CLASS16_REFRESH60_DM_PLUGIN4_CALL1` used corrected call/return sites.
At `0x10000ebc4`, `createRebootTimerIfAppropriateWithTimeIntervalBeforeReboot:pluginName:environment:`
returned at `0x10000ebc8` in about 2 ms. At `0x10000ec1c`, the
`SystemAppMigrator.migrator` object received
`performMigrationWithParameters:watchdogCoordinator:countOfAttempts:`;
`0x10000ec20` did not fire in the full 600-second window, nor did callback
4 or the backboardd synchronous call return. This names a bounded wait
inside that migration method, not a proven deadlock or plugin failure.
Earlier native Installation work under TCG needed multi-minute
installcoordination and `systemAppInstallGroup` waits (see
`dcp-state-acknowledgements.md` R10–R12). The next read-only cut is progress
inside the migration method and its MobileInstallation/installcoordination
path, including the plugin-wrapper return, before considering any timeout
change or bypass. `dm_migration_queue_work_callbacks.py` contains the
corrected sites and selector/object reads; no guest behavior was patched.
`UI_CLASS16_REFRESH60_DM_PLUGIN4_INNER2` traced the method through
`_performOneMigrationWithParameters:watchdogCoordinator:needsRetry:` and
into `DMWrapperProcessWatchdogCoordinator`'s
`callRunEventBlock:forPlugin:`. The call at `0x100013364` had not returned
at `0x100013368` by the end of its 600-second window.
`UI_CLASS16_REFRESH60_DM_PLUGIN4_RUNBLOCK1` then matched the coordinator's
run-event block to the callback-4 plugin object. Its first two synchronous
setup calls returned in milliseconds; the third entered at `0x100011eb8`.
The exact run-event block executed, created its
`com.apple.datamigrator.pluginCompletionSerialQueue`, and reached
`0x10001361c`, while the coordinator's third call remained in flight for
600 seconds. That probe had no later block checkpoints, so it did not
identify the long-running nested call. The next read-only run brackets
setup calls from `0x10001366c` onward and the later Objective-C calls at
`0x1000136bc` and `0x100013768`, keyed to the same block owner. Neither
MobileInstallation nor a specific installcoordination wait is attributed
until a selector and non-returning edge are observed.
`UI_CLASS16_REFRESH60_DM_PLUGIN4_RUNLATE1` verified all four setup call/return
pairs at `0x10001366c` through `0x1000136a8`, the ObjC call at
`0x1000136bc`, and later setup pairs through `0x100013740` returned.
The same matched run-event block entered the ObjC stub call at
`0x100013768`; its return site `0x10001376c`, block epilogue, coordinator
third-sync return, and outer migration returns were absent for the full
600-second window. This is the first bounded non-returning edge within the
run-event block. The selector slot at `0x10002fae0` pointed into the shared
cache and was not decoded by the live read, so the method name remains
unproven. The call's four argument positions and nearby strings suggest a
plugin launch, but the next read-only capture must identify its receiver,
path/identifier arguments, and downstream wrapper/installcoordination
progress before assigning that cause.
`UI_CLASS16_REFRESH60_DM_PLUGIN4_LAUNCHARG1` repeated the same call and
captured its arguments read-only. `x2` is an inline UTF-16 path beginning
`/System/Library/DataClassM...`; `x3` decodes as
`com.apple.MobileInstallation.SystemAppMigrator`; `x4` is the migration
parameters object and `x5` a completion block. This strongly matches the
four-argument `runPluginAtPath:withIdentifier:parameters:completion:` API
visible in the executable, though its optimized shared-cache selector at
`0x10002fae0` still did not decode from the live process. The bounded
return and a wrapper/installcoordination stack are needed before saying
where plugin startup spends its time.
`UI_CLASS16_REFRESH60_DM_PLUGIN4_LAUNCHFROZEN1` preserved a live guest and
walked the three relevant tasks through their saved task page tables. The
DataMigrator run-event thread is in
`-[DMMigrationPluginWrapperConnection runPluginAtPath:withIdentifier:parameters:completion:]`
→ `-[DMXPCConnection sendMessageSync:]` → Mach reply wait. This confirms
the selector inferred from the argument shape. The wrapper is alive (PID
179); its worker is in
`+[IXAppInstallCoordinator cancelCoordinatorForAppWithIdentity:withReason:client:error:]`
→ NSXPC synchronous reply wait. `installcoordinationd` is alive (PID 232):
one thread waits in `_dispatch_group_wait_slow` at the verified cleanup
return site (runtime `0x1048a247c`, static `0x10004a47c`), while other
workers at the first sample read files and create an IOPM assertion.
At a later read-only sample, the wrapper and DataMigrator waits remained,
but installcoordinationd workers had moved to
`_MobileInstallationICLRecordsUninstallForInstallCoordinationWithError`
and `_MobileInstallationInstallApp` synchronous calls. Its thread count
rose from 9 to 10. The group object found via the waiting thread was
`0x7432c9e760` at that second sample, with state `0x1fffffffd` at `+0x30`
(consistent with one outstanding item under the open libdispatch encoding).
That is evidence of ongoing cleanup work, not a completed migration or a
fixed worker stack. The next sample should compare the same group state and
the installcoordinationd worker calls before attributing a stall.
Further read-only samples kept the tracked group at `0x1fffffffd` and
the original cleanup wait at runtime `0x1048a247c`. The t3 sample found
`installd` (PID 154): its active worker was validating an executable bundle
through `MIExecutableBundle`. After another 45 seconds, that same worker was
inside `MIFilesystemScanner performScanWithError:` →
`MILaunchServicesDatabaseGatherer performGatherWithError:`, parsing an app's
Info.plist. A further 90-second run still had no group leave or wrapper
return, but the worker remained runnable (`wait_event=0`) at a different
Objective-C call site. The saved frame chain at that last interrupt did not
reconstruct the full MI stack, so it does not name a new phase. This is
evidence of MobileInstallation activity beneath the synchronous
installcoordination wait, with completion still open; the unchanged group
state alone is not a deadlock verdict. The saved task snapshots are
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_PLUGIN4_LAUNCHFROZEN1.{installcoordinationd,installd}.t3.json`
through `.t6.json` (available timepoints differ by process).

`UI_CLASS16_REFRESH60_DM_INSTALL_UNWIND4` used the exact known-good dev6
bootkc, device tree, trust cache, and CLASS16 60 Hz fixture. All five light
unwind hooks armed. Sixteen `IX_CANCEL_RETURN` hits occurred in a short burst,
but without entry pairing; no `DM_PERFORM_RETURN`, SpringBoard check-in return,
or shell pre-return occurred in the 1200-second window. At the held endpoint,
installcoordinationd no longer had the cleanup-group wait, installd was idle,
and a walk of the live process list found no migration plugin wrapper. The
DataMigrator task still had two threads in
`-[DMMigrationPluginWrapperConnection runPluginAtPath:withIdentifier:parameters:completion:]`
→ `-[DMXPCConnection sendMessageSync:]` → synchronous XPC reply wait.
Read-only user-space reads from those exact saved frames identified **two
distinct pending plugin RPCs**, rather than a continuation of the old
SystemAppMigrator call:

| DataMigrator thread | `runPluginAtPath:` argument | `pluginIdentifier` in its XPC request |
| --- | --- | --- |
| `0xffffffe3036320c0` | `/System/Library/DataClassMigrators/PassbookDataMigrator.migrator` | `com.apple.PassbookDataMigrator` |
| `0xffffffe3037ba890` | `/System/Library/DataClassMigrators/SpringBoard.migrator` | `com.apple.sbmigrator` |

The path objects were the preserved `x19` values in the
`runPluginAtPath:` frames (`0x79c2c5da40` and `0x79c2c64780`). The same
frames preserved separate XPC request dictionaries (`0x79c2c51c20` and
`0x79c2c50fc0`); each contained a `pluginIdentifier` key whose value decoded
to the identifier above. The synchronous calls also use distinct connection
objects. No live wrapper at the endpoint and two unanswered sends are a
bounded state, not proof that launchd failed to start either plugin or that
the same wrapper should answer both. The next cut is the launch/exit and
reply history for these two identifiers, especially the previously expensive
Passbook and SpringBoard migration paths. In `checkpoint-restore.md` R16–R17,
SpringBoard migration kept producing SplashBoard encodes across the native
600-second deadline, while Passbook migration waited on passd →
CoreLocation → fairplayd; those are comparison leads, not attribution for
this boot. The two earlier AMFI-aborting
controls used mismatched boot inputs, and a later healthy boot had zero
callback registrations; none is evidence for this migration boundary.

`UI_CLASS16_REFRESH60_DM_PLUGIN_LIFECYCLE1` then exercised both target rows
with all nine initial lifecycle sites armed. DataMigrator entered the
SpringBoard `runPluginAtPath:` request and its synchronous send, then did the
same for Passbook 88 seconds later. Neither send returned before the held
endpoint (about 188 and 100 seconds after the respective sends); no
`DM_PERFORM_RETURN` or SpringBoard check-in return fired. This is a bounded
wait, not a plugin hang verdict. The held guest has a live
`com.apple.migrationpluginwrapper` process (PID 749). Its saved threads show
both plugin paths running concurrently: one in SplashBoard's
`XBApplicationSnapshotImageGenerator` → `FBSSnapshot` →
`CARenderServerCreateSnapshots`, and another in
`PKPassLibrary migrateDataWithDidRestoreFromBackup:` → synchronous NSXPC
reply wait. The live `passd` process (PID 90) has a thread in CoreLocation;
`splashboardd` is also live. Thus neither request can be classified as
"wrapper never launched" or "wrapper exited without reply" at this
endpoint. The current evidence favors active plugin work, with completion
still open. These are single saved-stack samples, not proof of continued
progress or a specific downstream stall. Snapshot data:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_PLUGIN_LIFECYCLE1.{749,90,122,755}.json`;
trace: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_PLUGIN_LIFECYCLE1.lldb.log`.
The kernel wrapper-exit hook in this run compared the full process name to
`proc.p_comm`, which truncates it to `com.apple.migrat`; its silence is not
exit evidence. That filter is corrected in
`dm_plugin_lifecycle_callbacks.py` for a later run. The wrapper-side
`_handleMessage:` filter did not produce a target-identified hit, so this
run also did not directly measure reply send or wrapper exit history.

`UI_CLASS16_REFRESH60_DM_PLUGIN_PROGRESS1` repeated from the same saved
parent with the same boot inputs and display fixture, using nine verified,
lightweight progress hooks. Both target requests fired. The SpringBoard
request entered `sendMessageSync:` at t=1790927846.611; its wrapper entered
`XBApplicationSnapshotImageGenerator _generate` at t=1790927862.429 and
`CARenderServerCreateSnapshots` 24 ms later. Neither entry repeated through
the end of the 1600-second boot window. Passbook entered its synchronous
request at t=1790927909.064, then its wrapper entered
`PKPassLibrary migrateDataWithDidRestoreFromBackup:` twice on the same
thread/receiver within 6 ms. Neither DataMigrator sync call returned by the
cutoff, about 429 seconds after SpringBoard's send and 366 seconds after
Passbook's. The corrected kernel wrapper-exit hook fired for two earlier
wrappers (PIDs 168 and 512), providing a positive control; the target wrapper
(PID 734) was still live at cutoff.

Read-only held stacks at cutoff show the same substantive dependency frames
as the preceding lifecycle boot: the target wrapper has one thread beneath
SplashBoard → `FBSSnapshot` → `CARenderServerCreateSnapshots`, and another
under `PKPassLibrary migrateDataWithDidRestoreFromBackup:` → Foundation's
synchronous NSXPC reply wait. `passd` remains in a CoreLocation-triggered
sync send; DataMigrator has two threads at `sendMessageSync:`. The shared-cache
frame addresses normalize to the same static offsets across the different
slides, so this is a repeated stack shape, not an ASLR comparison error.
This is stronger evidence of a long plateau on two named dependencies, but
not proof of a permanent hang: the SpringBoard observation remains shorter
than the native 600-second plugin reboot allowance, and sparse entry hooks
cannot detect all work inside either call. The next useful cuts are the
CA snapshot request/reply and passd's specific CoreLocation request/reply,
plus a longer completion check if the native deadline is allowed to run.
Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_PLUGIN_PROGRESS1.lldb.log` and
saved task snapshots `.734.json`, `.90.json`, `.121.json`, `.78.json` in the
same `/tmp/dvm/UI_CLASS16_REFRESH60_DM_PLUGIN_PROGRESS1` prefix.

`UI_CLASS16_REFRESH60_DM_REPLY_EDGES1` followed those two dependencies with
14 verified read-only hooks from the same saved parent and unchanged CLASS16
60 Hz fixture. DataMigrator sent `com.apple.sbmigrator` at
t=1790955672.058. The wrapper entered SplashBoard snapshot generation 38.0 s
later, then `CARenderServerCreateSnapshots`, then a synchronous `mach_msg`
at t=1790955710.183. The request uses message ID 40224 (`0x9d20`,
`__XRenderDisplayContextListSurface` in `tools/re/carender_mig_ids.json`),
send+receive options 3, a 132-byte send and 52-byte reply, remote port
21267, local reply port 20483, and no Mach timeout. The paired Mach return,
CA render return, and snapshot-create return did not fire. DataMigrator sent
`com.apple.PassbookDataMigrator` at t=1790955733.962; neither plugin's
`sendMessageSync:` returned. The run ended with the **native** first panic
`userspace panic: datamigrator plugin SpringBoard.migrator hang`, about
602.4 s after the SpringBoard request and 564.3 s after the CA Mach call.
This confirms that the CA snapshot reply was still absent at the native
plugin reboot deadline. It does not establish whether the render server
received the request or which server-side operation withheld the reply; no
DCP cause follows from this client-side wait alone.
The older `UI_DISPLAY_PORT1` result in `docs/re/setup-launch-runtime.md`
also found CA render-server requests left unreceived after a brief initial
receive burst, including later display queries. That is a comparison lead,
not same-boot proof for this `0x9d20` request; the next server-side hook
must identify the receive port and message ID on this boot.

Static disassembly also identifies passd's CoreLocation call as
`getAuthorizationStatusForBundleID:orBundlePath:replyBlock:` at static
`0x1a783200c` (return `0x1a7832010`). That site did not fire in this
boot's gated trace, so there is no new Passbook reply verdict: the saved
passd wait may have entered before its hook became relevant, or this boot
may have taken a different call site. The next focused cut is the render
server's receive/dispatch/reply for message 40224, with the exact passd
CoreLocation request armed early enough to avoid a gating miss. Evidence:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_REPLY_EDGES1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_REPLY_EDGES1.probe.out`, and
`tools/re/dm_reply_edges_callbacks.py`. The panic-held child is retained;
it is not a migration-complete checkpoint.

`UI_CLASS16_REFRESH60_DM_CA_SERVER1` closed the client-to-server ambiguity
on another child of the same saved parent. Nine read-only sites armed before
boot. The CA render-server receive hook had thousands of successful internal
message controls. The SpringBoard migrator wrapper sent message 40224 at
t=1790961224.644; backboardd's CA server received that exact ID on its
service port at t=1790961224.651 and entered
`__XRenderDisplayContextListSurface` at t=1790961224.656. Thus the older
Phase-5 **receive blackout does not recur for this request**. The handler
and client Mach return remained unobserved for about 150 s. A deliberate
HMP pause for a live stack sample interrupted the LLDB session; later
hook silence from this boot must not be scored.

The paused, read-only backboardd sample (PID 72) moves the gap inside the
handler. Its CA server thread `0xffffffe49b9220c0` is parked in
`__psynch_mutexwait` on mutex `0x76d6d00018`:
`__XRenderDisplayContextListSurface` → `render_display_context_list` →
`CA::Render::post_notification` →
`CA::WindowServer::Server::render_display_context_list` → mutex lock.
The mutex is `CA::WindowServer::Server` object `0x76d6d00000` + `0x18`
(static lock call `0x1845a5b88`). Another backboardd thread,
`0xffffffe49ba79120`, is parked in `_kern_SwapWait` / IOMFB external
selector 6 under `CA::IOMobileFramebuffer::swap_wait` →
`IOMFBDisplay::finish_update` → `CA::WindowServer::Server::render_for_time`
→ vsync callback. `render_for_time` locks that **same** server object +
`0x18` at static `0x18440fd6c`; its early unlock exits the method, while
the remaining unlock at `0x184414994` lies after the active `finish_update`
call at `0x184411d38`. The saved `finish_update` frame carries server object
`0x76d6d00000` in its x25 spill. This identifies the swap-wait thread as
the holder preventing the snapshot handler from taking the server mutex.
The active kernel stack descends through IOMobileGraphicsFamily-DCP
`+0xa740` / `+0xa0c0` and IOMobileGraphicsFamily `+0x8cb4`; the exact
swap-completion condition is the next local target. This is **not** the
old Phase-8 `GetBlock`/TraceKit wait: the current same-boot user stack is
SwapWait, and the current request was received and dispatched.
Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_CA_SERVER1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_CA_SERVER1.backboardd-stacks.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_CA_SERVER1.backboardd-ca-thread.json`,
and `tools/re/dm_ca_server_receive_callbacks.py`.

An earlier-root-push history hook armed only at SpringBoard's service call
observed no root pushes before the activation callback, so it had no positive
control for prior submission. Moving that broad hook to boot attach caused a
600-second boot that never reached the shell-service or activation checkpoint;
that run is non-evidence for the lane. The broad history hook was removed
from the default probe to avoid perturbing subsequent boots.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_BKS_XPC_WRAP1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_XPC_DISPATCH1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_XPC_BRANCH1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_CHILD_PENDING1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_CHILD_ASYNC1.lldb.log`, and
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_HANDOFF_LIGHT3.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_HANDOFF_QUEUE3.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_HANDOFF_STATE1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_HANDOFF_DRAIN2.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_HANDOFF_HEAD1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_HANDOFF_HEAD3.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_ACTIVATION_QUEUE1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_ACTIVATION_ROOT1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_ACTIVATION_WAKE1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_WAKE_STATE1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_ROOT_HISTORY1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_ROOT_HISTORY2.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_OWNER_MATCH1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_OWNER_FROZEN1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_OWNER_FROZEN1.mem/backboardd-stacks.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_BKS_OWNER_FROZEN1.mem/process-stacks.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_BARRIER2.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_ROUNDTRIP1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_PRELUDE1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_CONN_INIT2.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_XPC_INIT1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_XPC_INIT2.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_CONN_INIT3.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_FOREGROUND1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_FULL_LADDER1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_FULL_LADDER2.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_FULL_LADDER3.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_CONTROLLER_SMALL1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_CONN_INIT4.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_ROLLING1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_ROLLING2.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_ROLLING3.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_ROLLING4.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_SERVER_RECEIVE1.lldb.log`,
`tools/re/datamigrator_barrier_callbacks.py`,
`tools/re/dm_perform_roundtrip_callbacks.py`,
`tools/re/dm_connection_init_callbacks.py`,
`tools/re/dm_xpc_init_callbacks.py`,
`tools/re/dm_foreground_session_callbacks.py`,
`tools/re/dm_migration_full_ladder_callbacks.py`,
`tools/re/dm_controller_prelude_callbacks.py`,
`tools/re/dm_migration_rolling_callbacks.py`,
`tools/re/dm_migration_server_receive_callbacks.py`,
`tools/re/dm_migration_server_msgid_callbacks.py`,
`tools/re/bks_server_receive_callbacks.py`, and
`tools/re/bks_handoff_queue_callbacks.py`, and
`tools/re/bks_handoff_drain_callbacks.py`, and
`tools/re/bks_handoff_head_callbacks.py`, and
`tools/re/bks_activation_queue_callbacks.py`.

Evidence: `/tmp/dvm/UI_COLOR2_TIMING1_CONSUMER1.lldb.log`,
`/tmp/dvm/probe/UI_COLOR2_TIMING1_CONSUMER1.stderr.log`,
`/tmp/dvm/UI_COLOR2_TIMING1_GATEBYTE1.lldb.log`,
`/tmp/dvm/probe/UI_COLOR2_TIMING1_GATEBYTE1.stderr.log`,
`/tmp/dvm/UI_COLOR2_ATTR_GATE1.lldb.log`,
`/tmp/dvm/UI_COLOR2_TIMING_LOOP1.lldb.log`,
`/tmp/dvm/UI_COLOR2_MODE_PATH1.lldb.log`,
`/tmp/dvm/UI_COLOR2_COLOR_WALK1.lldb.log`,
`/tmp/dvm/UI_COLOR2_COLOR_TREE1.lldb.log`,
`/tmp/dvm/UI_COLOR2_COLOR_MATCH1.lldb.log`, and
`/tmp/dvm/UI_COLOR2_COLORMODE_MIN1.lldb.log`, and
`/tmp/dvm/UI_COLOR2_POST_STORE1.lldb.log`, and
`/tmp/dvm/UI_COLOR2_DIGITAL_OUT1.lldb.log`, and
`/tmp/dvm/UI_COLOR2_SEL17_RETURN1.lldb.log`,
`/tmp/dvm/UI_COLOR2_SEL17_SHIM1.lldb.log`,
`/tmp/dvm/UI_COLOR2_SEL17_DISPATCH1.lldb.log`, and
`/tmp/dvm/UI_COLOR2_SEL17_OWNER1.lldb.log` with its QEMU model trace at
`/tmp/dvm/probe/UI_COLOR2_SEL17_OWNER1.stderr.log`, and
`/tmp/dvm/UI_COLOR2_A413_ERR1.lldb.log` with its model trace at
`/tmp/dvm/probe/UI_COLOR2_A413_ERR1.stderr.log`, and
`/tmp/dvm/UI_COLOR2_MODE_RESET1.lldb.log` with its model trace at
`/tmp/dvm/probe/UI_COLOR2_MODE_RESET1.stderr.log`, and
`/tmp/dvm/UI_COLOR2_MODE_RESET_CALLER1.lldb.log`, and the three pending-mode
logs above. All twenty-one bounded boots ended
with zero XNU panics and exited their QEMU guests. The failed nested-body
parse is recorded separately in `/tmp/dvm/UI_COLOR2_COLORMODE_DICT1.lldb.log`
and `/tmp/dvm/probe/UI_COLOR2_COLORMODE_DICT1.serial.log`.

### Direct callers and their gates

An exhaustive Capstone scan of the reconstructed larger-task `__TEXT` found
two direct branch-and-link references to `0x4288a60`: `0x428894c` and
`0x429c134`. A search for a plain 64-bit pointer to that VM address found
none; this establishes the direct callsites, without claiming that every
possible encoded or computed reference has been decoded.

The first callsite is inside an unnamed function beginning at `0x428865c`.
It is reached only when global byte `0x4984ec4` equals one. If
`self+0x5ca2` equals one, the function sends a no-input, four-byte-output
`D123` request. A returned status byte of one gates a controller method at
vtable offset `+0xbe0`; the next controller method at `+0xbc8` is then
checked. When that method returns nonzero, the function sends `D130` with an
eight-byte input: byte 0 is one, byte 1 is the `D123` status byte, and the
32-bit value at byte 2 is one or two according to the result from controller
slot `+0x100`. It calls the property publisher after this branch whether
`D130` was sent or skipped. If `self+0x5ca2` is not one, it skips both D123
and D130 and calls the publisher directly. The AP symbolication identifies
D130 as `UnifiedPipeline::update_ext_pipe_count(bool, bool, uint32_t)`; the
two flag meanings remain unknown.

The second callsite is inside `UnifiedPipeline::start_ios_gated()`, beginning
at `0x429b718` (the signature string is at `0x44958e3`). After its local
state gate, the first callback is `D107`; it then sends `D108`, `D109`,
`D110`, `D111`, `D112`, and `D120`, each with no input and a four-byte output.
The arithmetic on the `D100` FourCC base is what produces these tags. D107
through D112 replies control per-object setup and cleanup. The low bit of the
D120 reply is a hard gate: if clear, this caller takes its failure/cleanup
path and does not call the property publisher. If set, it makes further
controller/state checks and calls the publisher only when the global
controller object's `+0x130` method returns nonzero. This path does not send
D130 before publishing.

That distinction changes what a live trace must capture. A D130-centered
window tests the first caller, but misses `start_ios_gated()` entirely; a
D120-centered window is needed to see the second caller's preceding D107–D112
sequence and its publisher gate. The publisher's per-key null checks still
separate “publisher entered but no property object” from “publisher never
entered,” while each D126/D127/D128 ACK distinguishes transfer acceptance.

This is a DCP-task property path. It does not establish any relationship to
the separate firmware object's `self+0x60` buffer.

The local m1n1 tree confirms the AP callback shape. `DCPClient` starts endpoint
`0x37`; `DCPEndpoint` dispatches inbound RPCs through `DCPManager.handle_cb`,
which routes the property callbacks to `setDCPAVPropStart/Chunk/End`
(`proxyclient/m1n1/fw/dcp/{client.py,dcpep.py,manager.py}`). Those methods
accumulate and parse firmware-originated callback data; there is no AP command
that requests a property by name. `m1n1/fw/dcp/ipc.py` makes the IDs version
conditional: D122-D124 before 13.5, D126-D128 for the 13.5 generation, and
D127-D129 for 14.7. The iOS 27 H17P mapping remains the independent direct
result from its symbolicated kext: D126-D128.

The stock m1n1 manager parses the completed blob as `OSSerialize`, which is
not safe to assume for H17P: its D128 end path contains an
`OSUnserializeXML failed` error path. The current local m1n1 checkout has no
`TRACE_DCP_IOMFB_PROP_LOG` hook in `proxyclient/hv/trace_dcp.py` and no
`parse_prop_capture.py`. Earlier notes describing those as installed are
stale. A future raw capture must record callback bytes and ACKs before the
stock parser, with both D120 and D130 neighborhoods: D130 is conditional in
one caller and absent from `start_ios_gated()`.

Asahi Linux offers another data source on supported M1/M2 hardware. The DRM
connector debugfs files `ColorElements`, `TimingElements`,
`DisplayAttributes`, and `Transport` contain raw blobs for those four known
keys (`linux/drivers/gpu/drm/apple/connector.c:24-75,101-151`). Arbitrary
unknown keys are not retained there. This could provide a real-device
reference for the key and older binary format, but it is not an H17P capture.
The `asahi-fairydust-display` repo is a kernel installer plus an external-DP
resume patch; it does not implement this IOMFB property path.

The current darwin-vm baseline still records zero naturally generated
endpoint-0x37 callbacks, and the model does not execute the DCP RTKit task.
The callback script is a synthetic AP-handler probe, not evidence that the
static producer runs during the QEMU boot. The ordered sequence has now been
tested with the known-good dev6 bootkc, dtree, trust cache, and disk inputs.
With default model answers, the scripted `D107,D108,D109,D110,D111,D112,D120`
replies were `1,1,0,1,0,1,0`. This is recorded in
`/tmp/dvm/probe/DCP_START_IOS_GATED2.stderr.log`.

LLDB tracing then showed that D120's AP handler calls A475 through the wrapper
at runtime address `0xfffffff02a0cbdf4`; that wrapper sends a no-input,
four-byte-output A475 RPC and returns the low bit of its result. The current
model's default A475 result is zero, which directly explains the D120 zero.
As a positive control, `DCP_START_IOS_GATED7` supplied A475 output `01`; the
same ordered callback sequence then returned D120 output `01000000`
(`/tmp/dvm/probe/DCP_START_IOS_GATED7.stderr.log:38-46,362-369`). This proves
the AP-side dependency, but it is only a synthetic control: it does not prove
that one is the hardware-correct A475 answer, that firmware would continue
past D120, or that any property callback is produced.

Static firmware analysis identifies the next gate after a successful D120:
the provider retained by `AppleDCPDisplayService` must return nonzero from its
vtable method at `+0x130` before `start_ios_gated()` calls the property
publisher. P197 traces that receiver through the service's `start` argument
and corrects the earlier shorthand that called it a global controller; the
provider's dynamic class and slot implementation remain unknown. QEMU cannot
measure that return because it does not execute the DCP RTKit task. The next useful
bring-up input is therefore a real H17P/iOS 27 capture of the D126-D128
property transfers and their ACKs (all five properties, in publisher order),
or an independently justified firmware-side path that initializes the
controller and those properties. Do not treat A475=1 or fabricated property
blobs as a bring-up fix. A hardware trace is also needed to confirm that this
is the producer path selected on the target.

### DCPAV display-asleep gate before the VI property builder

The empty publisher dictionary is relevant only if its builder is entered.
At `0x40d0dd0`, the VI checks its `+0xab` reentrancy byte, initializes a
stack out-byte to one (`0x40d0df4`-`0x40d0df8`), then calls the DCPAVDevice
at `VI+0x58` through `vt+0x2e0` (PAC `0x50d1`, `0x40d0e50`-`0x40d0e84`)
with `(device, &out_byte)`. The `0xe00002bc` constant assigned to `w21` at
`0x40d0e60`-`0x40d0e64` is the *default return of `0x40d0dd0`*, not a call
argument. A nonzero child return retains that error and skips the builder.
If the child returns zero but leaves the out-byte one, the branch at
`0x40d0e90` returns zero without building. The same success-without-build
result follows a nonzero count in `VI+0x98` (`0x40d0e94`-`0x40d0e9c`,
`0x40d0fb0`). Only a zero return, cleared out-byte, and zero interface count
reach `vt+0x888`, then `vt+0x128` (PAC `0xea52`) at `0x40d0eec`;
the latter resolves to `0x413f790` in the fixups at `0x4957638`.

The DCPAVDevice virtual call is a trampoline at `0x40be110`: it loads the
controller at `device+0x158` and calls its `vt+0x338` (PAC `0x804d`). The
controller is created at `0x40c1650` through factory `0x40bd998`, using the
`DCPAVController` class descriptor at `0x4928180`, then stored at
`device+0x158` (`0x40c1654`). Its `vt+0x338` fixup at `0x4956470` points to
`0x40b8ad4`. That method's diagnostic string is `getDisplayAsleep`
(`0x44709a5`). On its direct path it copies `controller+0x84` to the out-byte
(`0x40b8b48`-`0x40b8b54`); its gated callback path does the same at
`0x40b8cc4`-`0x40b8cdc`. Thus the out-byte represents the controller's
display-asleep state. Methods at `0x40b8e90` and `0x40b90f4` explicitly set
and clear `controller+0x84` (`0x40b8eb8`, `0x40b9118`). Whether the target
runtime has cleared that byte when `0x40d0dd0` runs is unmeasured.
Their controller vtable slots are `+0x330` and `+0x328`, respectively
(`0x4956468`, `0x4956460` fixups); handlers at `0x40bdbdc` and
`0x40bdc28` forward to those slots through an object at `+0x6d8`.
This establishes reachable state
transitions but not which one occurs before a given publication attempt.

The firmware-side checkpoint order is therefore `0x40d0dd0` entry,
`getDisplayAsleep` return and out-byte, `VI+0x98` count, `0x413f790` entry,
then `0x411d210` ColorElement construction. An empty `VI+0x498` dictionary
does not imply that the first attempt reaches `0x413f790`; a populated
interface map can make the attempt return success without invoking it.
The two direct calls into `0x40d0dd0` are **alternate dispatch routes**, not
sequential checkpoints: `0x40d0bdc` calls it directly at `0x40d0c54` when
its workloop-gate test succeeds (`0x40d0c40`-`0x40d0c4c`), or schedules a
callback whose body calls it at `0x40d0fd0` otherwise (`0x40d0c60`-
`0x40d0cb4`). The open ordering question is which route runs relative to
interface insertion, not the order of `0x40d0c54` versus `0x40d0fd0`.

`VI+0x98` is allocated as an empty 16-byte-pair array at `0x40d8474`-
`0x40d8478`; `0x41bf928` counts `(end-begin)/16`. In
`addInterfaceGated` (`0x40d182c`), a passing interface `vt+0x78` check at
`0x40d1a2c` leads to an insert via `0x41bf73c` at `0x40d1a40`.
The existing-interface replacement path at `0x40d19d8`-`0x40d19e0`
removes the old key through `0x41bf9ac`. A successful insertion therefore
makes the map nonempty until a separate removal path runs. One route through
`0x40d9100` first checks
that the map is nonempty (`0x40d92b8`-`0x40d92c0`), then enumerates timing
and color (`0x40d93e8`, `0x40d9420`) and calls `vt+0x118` at `0x40d9474`.
That route cannot reach the builder through `0x40d0dd0` while the map remains
nonempty. Conversely, VI method `0x40ca5c0` checks that `+0x98` is empty
before calling the same `vt+0x118` at `0x40ca608`, making it a distinct
builder-reachable route if the display-asleep check also clears. Neither
route proves which state occurs on the target at runtime.
The direct in-gate caller at `0x40c4b64` forwards this method's status to an
object at `+0x90` with reason `0x11`; the other path schedules a callback that
invokes it at `0x40c4c60`. `0x40ca5c0` converts a nonzero `vt+0x118` result
to success (zero) and a zero result to `0xe00002bc` (`0x40ca610`-`0x40ca61c`).
That constant is a firmware-side status at this callsite; matching its numeric
value in an AP trace would not, by itself, prove the same function ran.
Scripted D126-D128 callbacks in QEMU exercise only the AP parser and registry.
The current QEMU model does not run this DCP task, so those callbacks cannot
measure any of these firmware-side checkpoints.

### CLASS16 DataMigration CA snapshot: retained fence-0 swap (2026-10-02)

`UI_CLASS16_REFRESH60_DM_SWAPWAIT1` reused the LS-seeded parent with the
patched BOOTKC, matching DTREE and `system.tc`, and the CLASS16 60 Hz fixture.
Seven read-only `dm_swapwait_boundary_callbacks.py` sites armed before the
run. On one backboardd thread (`tpidr_el1=0xffffffebe77b6890`), two
`_kern_SwapWait(fence=0, options=0x80000003)` calls returned
`0xe00002d5` in 10 and 6 ms. `_kern_SwapEnd` on the same object
`0x78f6cb8000` / connection `73731` returned zero in 12 ms. The subsequent
`_kern_SwapWait(fence=0, options=1)` entered at t=1790963318.381 and had no
recorded return for about 900 s. The device model answered one A407; it saw
no A408 or D594. This is not evidence of a missing D594 for a submitted A408.

At t=1790964218.714 the SpringBoard migration wrapper entered snapshot
generation; at t=1790964218.828 it sent CA MIG message 40224 (`0x9d20`).
An HMP pause then interrupted the LLDB batch. LLDB detached and QEMU briefly
resumed before a second HMP stop. Silence after that detach is not hook
evidence. The paused backboardd saved thread still waits on
`0xffffffeb0212c138`, exactly framebuffer `0xffffffeb02128000+0x4138`.
That head points to the single node `0xffffffedb3f8a000`: swap ID at `+0x3d4`
is 0, the word at `+0x3d8` is `0x80000007`, next at `+0xd38` is null, and
previous-link at `+0xd40` points back to the list head. The framebuffer's
`+0x4140` tail link points to `node+0xd38`. This is a linked, retained
fence-0 node, not an empty-list missed wake. Do not assign a semantic name to
`+0x3d8` from this one capture.

The same paused backboardd sample has a CA handler thread in the mutex wait
site at static `0x1845a5b88`, with mutex `0x78f6d04018`. The swap-wait
thread's render stack retains server object `0x78f6d04000`; both threads
therefore refer to the same `CA::WindowServer::Server` lock. This gives a
same-boot chain from the retained swap through the server mutex to the later
snapshot request, while leaving the exact pre-submit failure open.

Static iOS 27 kext code stores the swap ID into `node+0x3d4` at
`0xfffffff00a0c3744`; the `SwapEnd` path links that node into `fb+0x4138` at
`0xfffffff00a0c4500..0xa0c4514`. `_kern_SwapWait` walks the list by that ID
at `0xfffffff00a0bbf14..0xa0bbf30` and sleeps on `fb+0x4138`.
D594's `0xa0c58bc` removes a matching node in the known A408 completion
path; this boot never submitted A408. The next read-only bracket should
distinguish the post-link A407 branch from the A408 build/submit branch and
record the condition that leaves this node linked without a completable swap.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_SWAPWAIT1.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_SWAPWAIT1.stderr.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_SWAPWAIT1.backboardd.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_SWAPWAIT1.pending-list.json`, and the
read-only binary captures alongside them.

`UI_CLASS16_REFRESH60_DM_SWAPEND_SUBMIT1` then repeated the early path from
the same parent with 11 read-only sites, all proven armed. On the paired
backboardd thread, `SwapEnd` stored ID 0 at `node+0x3d4`, linked node
`0xffffffecce197000` at framebuffer `0xffffffecce014000+0x4138`, and the
post-link preparation call returned zero. The node already held
`0x80000007` at `+0x3d8`. At `0xa0c4564`, the byte at `fb+0x5870` was zero,
so `tbz bit 0` followed the second test at `0xa0c45fc`. There, the byte at
`fb+0x5df0` was also zero; the `tbz bit 0` at `0xa0c4608` branches to the A407
arm at `0xa0c4714`. `A407_BUILD` at `0xa0c477c` fired and returned zero;
`A408_BUILD` at `0xa0c4664` did not fire. `_kern_SwapEnd` returned zero and
the same thread entered `_kern_SwapWait(fence=0, options=1)`. The model log
has A407 only. This identifies the *observed branch*, not yet the meaning of
the `fb+0x5df0` bit or whether A407 has a separate retirement mechanism.
The next read-only comparison is where that bit is initialized/set, and the
completion contract for the A407 arm; do not manufacture D594 for an A408
that never occurred. Evidence:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_SWAPEND_SUBMIT1.lldb.log` and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_SWAPEND_SUBMIT1.stderr.log`.

Static D582 handling is an additional candidate retirement contract. Its
handler at `0xfffffff00a0da3ec` decodes the 12-byte packed callback and calls
`0xa0c5cac`, which calls `0xa0c5728` with the callback's swap ID. That helper
searches `fb+0x4138` by `node+0x3d4`, unlinks a matching node at
`0xa0c57d8..0xa0c5820`, and wakes sleepers on `fb+0x4138` at `0xa0c5844..0xa0c5850`.
The two runs above show no D582 or D594 in the model log. This proves that
neither observed retirement path ran; it does **not** yet assign D582 to A407
specifically. Next establish which callback/condition the A407 variant expects
before changing the model's completion behavior.

`UI_CLASS16_REFRESH60_DM_A407_RETIRE1` repeated the paired early path with 19
read-only hooks armed, including D582 and D594 handler entry, their remover
helpers, the head-of-line node unlink, and the `SwapWait` return. The same
backboardd thread linked swap-ID-0 node `0xffffffe3f3fb1000` to framebuffer
`0xffffffe228218000+0x4138`, selected A407 with both observed gate bytes
zero, received A407 RPC status zero, and returned successfully from
`_kern_SwapEnd`. It then entered `_kern_SwapWait(fence=0, options=1)`.
There was no D582 or D594 callback in the device-model log and no matched
handler, remover, or `SwapWait` return before the 420-second boot bound ended
(about 337 seconds after the wait entered). The two earlier quick-fail waits
with options `0x80000003` did return `0xe00002d5`; they are a separate path.

The frozen, read-only end sample still has `fb+0x4138` pointing to that **same**
node. Its ID remains 0, `+0x3d8` remains `0x80000007`, `next` is null,
`node+0xd40` points back to the list head, and `fb+0x4140` points to
`node+0xd38`. Thus this boot proves a retained node after a successful A407
RPC, not merely a missed callback hook. It does not prove A407 should cause
D582: the current model emits no A407 completion, and the current firmware
bundle has no literal `A407` or `D582` tag that connects the two. The older
m1n1 callback numbering is version-dependent; the current kext's D582 meaning
comes from its own handler, not that older table. The next question remains
what event the real A407 arm expects to retire this node.

A subsequent static firmware/AP-kext pass found a **third** retirement
mechanism: `batched_swap_complete_ap_gated` in the reconstructed DCP firmware
builds outbound FourCC D595 with a `0x2708`-byte batch payload
(`dcpfw` VM `0x04218f8c..0x04218fa8`). The current AP D595 handler at
`0xfffffff00a0db1a8` reads the batch count at payload `+0x400`, copies
swap IDs, and calls `0xa0c5c08`; that helper calls the known full-retirement
path `0xa0c58bc` once per ID. This identifies what D595 **can** do, but the
firmware call sites found so far do not link it to the A407 request. The
bounded A407 replay did not arm a D595 handler hook; its retained node proves
only that no retirement of swap ID 0 occurred by cutoff. A407's expected
completion ID remains unresolved.
The firmware batch producer is now also identified at
`0x0420daf0..0x0420de34`: a swap-object state check precedes copying the
active swap ID into the batch and incrementing `+0x2748`, which the D595
sender reads. That is a firmware-internal ID lineage, not an A407 request
lineage; no current-build branch seen so far assigns the A407 arm to D595.

The task-2 firmware disassembly gives a sharper **ID and field lineage**.
The `0x1d30`-input receive path at `0x04222d8c` calls vtable slot `+0x208`
(`0x04223060`) into `0x04215c50`; the `0xff4`-input path at
`0x04222af8` calls `+0x210` (`0x04222d00`) into `0x042147e4`. Those
sizes match the AP A407 and A408 RPCs. Correction: these handlers do
**not** create a swap object. Both search the existing `fb+0x3e30` list
by input ID `+0x98` using callback `0x04215bcc`, which compares each
object's `+0x3d4` and returns a pointer only on a match
(`0x04215bf4..0x04215c40`). They then copy input word `+0x17c` to the
*found* object's `+0x3ec` (A407 `0x04215e3c..0x04215e40`; A408
`0x042149c0..0x042149c4`). A lookup miss leaves the local object null
and takes an error branch before that copy.

The earlier `0x10`-input/`0x08`-output firmware receive path at
`0x04223134` reaches object allocation `0x04217080`; callback
`0x04217144` increments `fb+0x3e9c`, clamps the initial/wrapped ID to
**1**, writes the ID into both `object+0x3d4` and the first output word
(`0x04217158..0x0421717c`), then inserts the object into `fb+0x3e30`
(`0x04217180..0x04217198`). That size and ordering match the replay's
two A406 calls before A407. The QEMU model answered both A406s with
status zero and an all-zero output; the following A407 request's
sparse input shows `+0x98 == 0` as well as `+0x17c == 0`. Thus the
current replay has an **earlier candidate ID mismatch**: the firmware's
successful A406 allocation path returns a nonzero ID, while the model
returned zero. This does not prove how a real-firmware target would
handle this request, or exclude every possible alternate ID-0 insertion;
it does mean that the model's A407 success status cannot be used as proof
that firmware found or updated a matching object.

The direct writer inventory found no ID-0 constructor for that list:
`0x04217178` inserts the A406-allocated object with a nonzero ID;
`0x0420dc40` and `0x04292a5c` also assign IDs with the same
increment/clamp-to-1 rule, and the other direct `+0x3d4` store at
`0x042775b4` is a different configuration path without an observed
`fb+0x3e30` insertion. The list tail's only direct insertion write is
`0x04217198`; other direct tail writes initialize it or remove an
object. This does not exclude an indirect or copied ID-0 insertion.
If none exists, A407 takes its lookup-miss branch
`0x04215db4/0x04215dbc → 0x0421689c → 0x0421691c` and returns
`0x8000000a` at `0x04216f34`, before the `+0x3ec` write. A408 has the
same status on its parallel miss path. QEMU instead returned status
zero without running either handler. Thus ID lookup is the earliest
demonstrated model/firmware **contract difference**, while the actual
firmware result for an ID-0 request remains conditional on list content.

For a *matched* object, static firmware does show a conditional route
toward activation. A407 can call `0x04213980` with that object at
`0x04216d7c..0x04216d98` if the `w25/w21` branch permits it. That
routine's virtual `+0x5b0/+0x5b8` gates precede callback `0x04213e64`.
The callback rechecks the object's ID in `fb+0x3e30` and can call
`0x04214338` with the **same pointer**, moving it from `fb+0x3e30` to
`fb+0x3e20` (`0x04214298..0x04214414`). The first framebuffer vtable's
`+0x5b0` returns zero, so this route closes for that class; the second
delegates through an outer object and remains conditional. If the object
reaches the `+0x3e20` head, `0x0420c934..0x0420c944` can install it at
`fb+0x3e18` when active is null. Another class-specific active writer is
`0x04294ad8`, also sourced from `fb+0x3e20`. Those stores demonstrate
possible queue-to-active promotion, not that the stuck ID-0 request
reaches them.

The D595 producer reads active swap state at `0x0420daec..0x0420daf0`.
State 0 passes its first `0x80c0` and later `0x101` mask tests, but the
batch append at `0x0420ddec..0x0420de34` additionally requires virtual
`+0x378` true, or a nonzero batch count, or (with count zero) virtual
`+0x830` true, and count `<0x100`. It copies active `+0x3d4` to
`fb+0x48` and increments `fb+0x2748`, which the D595 sender reads. In
the first framebuffer vtable, `+0x378` and `+0x830` both return zero, so
state 0 alone cannot initiate an empty batch; the second vtable's gates
are stateful (`0x0429971c` and `0x042912f8`, the latter reading
`fb+0x42fa`). This is a producer-eligibility limit, not a decoded
A407 completion contract.

The AP-side A406 contract now narrows the **first QEMU-visible edge**.
Its wrapper at `0xfffffff00a0c8c50` issues A406 (`in=0x10`, `out=8`)
at `0xa0c8ccc..0xa0c8ce0`, then copies output word 0 from `sp+8`
to the nonnull caller pointer `x1` at `0xa0c8ce8..0xa0c8cec`; it
returns the mapped status from output word 1 at `0xa0c8cf0..0xa0c8cf4`.
Firmware A406 writes its newly allocated nonzero swap ID into both
object `+0x3d4` and output word 0 (`0x04217158..0x0421717c`), then
serializes status to output `+0x4` at `0x0422321c`. Its allocation
callback runs inline through `0x04033b20` (`blraaz x25` at
`0x04033b60`), rather than merely being scheduled for a later reply.

Both AP record-creation paths pass `record+0x98` as `x1` to virtual
slot `+0x598` (`0xa0bfbcc..0xa0bfbe0` and
`0xa0bfd34..0xa0bfd48`), then check the returned status alone at
`0xa0bfbe4` / `0xa0bfd4c`. They do not reject a zero ID. The
first immediately passes that record to slot `+0x928` and rereads
`+0x98` at `0xa0bfc7c`; the second passes it to slot `+0x5a0`
and rereads `+0x98` at `0xa0bfd88`. The `0xa0c366c` entry is a
forwarding thunk to virtual slot `+0x928`; the observed SwapEnd body
starts at `0xa0c36a8` and retains input `x1` as `x24` at
`0xa0c36d4`. Its `+0x98` word is
copied to the AP pending node `+0x3d4` at `0xa0c3730..0xa0c3744`.
The A407 call at `0xa0c4760..0xa0c477c` passes that same `x24`
record. Its wrapper copies the first `0x6e0` bytes to the A407
wire buffer at `0xa0c8da0..0xa0c8dac`, including `+0x98`.
Thus **within a given SwapEnd**, pending-node ID and A407 request ID
come from the same AP record field; this does not establish firmware
lookup success or completion.

**Live AP identity result (`UI_CLASS16_REFRESH60_DM_A406_USER2`).**
`tools/re/dm_a406_id_lineage_callbacks.py` verified all 21 kernel and
shared-cache breakpoint commands before the matched A407; the run
stopped at that A407, not at an unrelated proof line. The two
`0xa0bfbe0` / `0xa0bfd48` candidate record-creation sites did not
fire. Instead the A406 wrapper returned to base
IOMobileGraphicsFamily `0xfffffff00a0e5cdc`, where the selector-4
userclient path stored its local output ID through `[descriptor+0x48]`
at `0xa0e5ce4`. This kernel destination was distinct from the later
kernel SwapEnd record; direct pointer equality was the wrong test.

The userland bridge *did* hit with the process-name filter removed.
`_kern_SwapBegin` (`0x22a3911cc`) entered twice for the same IOMFB
object `0x73bb0d8000`. Both nested A406 replies were
`{id=0,status=0}`; selector-4 returned success at `0x22a391224`,
then `0x22a391254..0x22a391258` stored ID 0 at object `+0xb0`.
That field equals embedded record `(object+0x18)+0x98`.
`_kern_SwapEnd` (`0x22a391334`) later entered with the **same object**
and ID 0. Its `0x22a391350..0x22a391364` call passed the embedded
`0x6e0`-byte record to selector 5. On the matched kernel path,
the forwarding thunk `0xa0c366c` and body `0xa0c36d4` shared a
kernel record pointer; `0xa0c3730..0xa0c3748` copied its `+0x98=0`
to pending node `+0x3d4`, and A407 wrapper `0xa0c8d08` received
that record with ID 0. Five earlier `_virt_SwapBegin`/`_virt_SwapEnd`
pairs used other IOMFB objects and carried ID 2 at End. Thus the
observed stuck transaction is specifically on the kern path; the
userland and kernel record *addresses* differ because selector 5
transfers the record through the userclient.

This proves the AP-side lineage for this transaction:
`A406 output word 0 → selector-4 scalar output → same IOMFB object
+0xb0 / embedded record+0x98 → selector-5 SwapEnd record+0x98 →
pending node+0x3d4 and A407 request+0x98`.
The generic model zeroes A406's eight output bytes and reports
status zero (`qemu-sptm/hw/arm/darwin_iomfb.c`, `iomfb_class2`), while
successful firmware A406 allocates a nonzero object ID. The replay
therefore establishes a concrete QEMU A406 ID-contract mismatch. It
does not prove that correcting the ID alone will advance the
firmware-side queue or retire the AP node.

The object-keyed probe closed the identity edge. The smallest controlled
model experiment returns a monotonically assigned nonzero
32-bit ID at A406 output `+0x0`, zero status at `+0x4`, and retains
an ID marker for subsequent A407/A408 identity logging. That ID-contract
probe is now opt-in behind `DARWIN_DCP_IOMFB_A406_IDS=1` in
`qemu-sptm/hw/arm/darwin_iomfb.c`: it allocates IDs starting at 1,
packs them with `darwin_iomfb_a406_reply`, retains them for HIT/MISS
logging against later A407/A408 request `+0x98`; it does not emulate
the firmware swap object/list/lookup. It leaves A408
completion / D582 / D594 / D595 untouched so the boot isolates the ID
edge. `DARWIN_DCP_IOMFB_OUT` still wins when it overrides A406. This
tests ID lineage only: the later
`fb+0x3e30 → +0x3e20 → +0x3e18 → D595` gates and completion contract
remain separate. The CLASS16 60 Hz fixture remains unchanged.

**Opt-in ID-only replay (`UI_CLASS16_REFRESH60_DM_A406_IDS1`).** The
same saved parent and CLASS16 60 Hz fixture ran with
`DARWIN_DCP_IOMFB_A406_IDS=1`; all 23 read-only trace sites armed.
Compared with the zero-output control (two A406 calls, then A407 ID 0),
the model answered one A406 with `{id=1,status=0}` and retained an
ID-1 marker. The exact userland IOMFB object `0x7acccdd400` entered
`_kern_SwapBegin`, stored 1 at `+0xb0`, and later entered `_kern_SwapEnd`
with that field still 1. The matched kernel SwapEnd record `+0x98`,
pending node `+0x3d4`, and A407 request `+0x98` all carried 1; the
model's A407 marker lookup logged HIT. This is a QEMU-owned marker,
**not** a real-firmware `fb+0x3e30` lookup. Earlier `_virt_Swap*`
activity on other objects remains separate.

`_kern_SwapWait(fence=1, options=1)` entered on that same object and
had no matched return through the 300-second boot bound (about 215
seconds after wait entry). The only Wait returns were the two earlier
`fence=0, options=0x80000003` quick failures. The model logged no
A408 RPC or D582/D594/D595 completion in this window. Thus repairing
the measured ID mismatch is **not sufficient** for retirement under the
current QEMU model; the real firmware object activation and completion
contract remain unimplemented/unproven. This replay did not take an
end-window pending-list snapshot, so it establishes a still-open wait,
not the node's end-window linkage. Evidence:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A406_USER2.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A406_IDS1.lldb.log`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A406_IDS1.stderr.log`.

**Immediate post-A407 AP-state stop (`UI_CLASS16_REFRESH60_DM_A407_POST1`).**
The same saved parent, CLASS16 fixture, and opt-in nonzero A406 ID were used;
`DARWIN_DCP_IOMFB_COMPLETE=0` and scanout remained off. Four read-only
sites armed, and the stop regex matched only the ID-1 A407 return. The
matched `_kern_SwapEnd` object was `0x7d06c6a800`, kernel record
`0xffffffeac1ee9814`, framebuffer `0xffffffe72a1e0000`, and pending
node `0xffffffe72a284000`. Both the record and node held ID 1. Before
the A407 call and immediately after its status-zero return, the node was
the sole `fb+0x4138` head (`next=0`, `node+0xd40=fb+0x4138`,
`fb+0x4140=node+0xd38`). The sampled fields were unchanged:
`node+0x3d8=0x80000007`, `+0x3ec=0`, `+0x62a=0`, `+0x62b=0`, and
`fb+0x5870=0`, `+0x5df0=0`. This is an immediate return snapshot, not
an end-window node-retention claim.

The known AP removers do not require another observed node-state bit
before they can *find and unlink* this entry. D582's HOL helper
`0xa0c5728` reads `fb+0x4138` and compares each `node+0x3d4` with the
callback ID at `0xa0c5748..0xa0c5764`, then reaches list unlink at
`0xa0c57d8..0xa0c5820`. The full helper `0xa0c58bc` used by D594
and D595 likewise searches head/`+0x3d4` at `0xa0c592c..0xa0c5968`
and reaches unlink at `0xa0c5ac4..0xa0c5b0c`. Its later reads of
`node+0x3ec` and `+0x3d8` select ancillary work; the observed values
do not gate the final unlink. Thus this matched AP node already meets
the **local ID/list requirements** for a matching retirement callback.
The first AP-visible transition still absent in the prior 300-second
ID-1 replay is entry into any such matching callback/remover, not a
post-A407 write to `node+0x3d8` or the list links. This does **not**
assign D582, D594, or D595 to A407: the firmware-side
`fb+0x3e30 → +0x3e20 → +0x3e18` activation and producer gates remain
separate and unobserved on this QEMU model. Evidence:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_POST1.lldb.log` and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_POST1.stderr.log`.

**Current-build remover callers, narrowly inventoried.** A direct-branch
scan of this kext's `__text` (`0xfffffff00a0b2080..0xa0dc230`)
found the following callback-facing calls; the dispatch root is
`AppleDCPLinkServiceSoC::link_rpc_lookup` at `0xfffffff00917d328`
(the current-build D-table in this document, not a historical D590 map):

| Callback / AP entry | Required input and ID | Direct retirement chain |
| --- | --- | --- |
| D582 `0xa0da3ec` | `0x0c` bytes; ID at payload `+0` (`0xa0da484`), flags at `+4/+5/+9` | `0xa0da49c → 0xa0c5cac → 0xa0c5d30 → 0xa0c5728`; HOL ID search/unlink and wake at `0xa0c5844..0xa0c5850` |
| D594 `0xa0db034` | `0x730` bytes; ID at `+0` (`0xa0db12c`), optional info/timings as decoded in `surface-cache-and-completion.md` | `0xa0db144 → 0xa0c58bc`; full ID search/unlink and wake; measured A408 completion only |
| D595 `0xa0db1a8` | `0x2708`-byte batch input, `0x400`-byte ID-array output; count at input `+0x400` (`0xa0db26c`), per-entry IDs from input `+0` copied at `0xa0db304..0xa0db308` | `0xa0db360 → 0xa0c5c08 → 0xa0c5c80 → 0xa0c58bc` per ID; same full unlink/wake |

The direct callers of `0xa0c5728` additionally include
`0xa0c5518`, `0xa0c55f4`, and `0xa0c59cc` inside the completion
helpers; `0xa0c5d30` is D582's immediate wrapper. The full helper
also has local call `0xa0c7268` on a cancellation branch and
`0xa0c5c80` in the D595 batch loop. These local calls do not assign a
new D-series callback. The current-build D590 handler at `0xa0dac24`
has a `0x14`-byte input and directly calls `0xa0bb858` at
`0xa0daccc`, **not** either pending-node remover; the public D590
`swap_complete_ap_gated` stack from another build cannot supply this
build's callback number or ID layout. This direct-call inventory does
not rule out other indirect paths, but it covers the two known
`fb+0x4138` unlink helpers.

Firmware static now gives concrete *conditional* producers, documented
in `dcp-firmware-services.md`: D582 from a saved `fb+0x2750` HOL-ID
array at `0x0420de58..0x0420de88` (gated by `fb+0x274c` and
`fb+0x2764`), D595 from an active swap `+0x3d4` appended to the batch
at `0x0420ddec..0x0420de34` and sent at `0x04218f8c`, and D594
from caller-supplied ID at `0x0421c228..0x0421c2d8`. For a matched
A407 object, the first unproved transfer toward those producers is the
`w21/w25` gate at firmware `0x04216d7c..0x04216d9c` before
`0x04213980` queues it; active installation and each producer's own
gates remain later conditions. No current-build A407→specific-Dxxx
completion chain is established, and none of these firmware paths runs
inside the present QEMU IOMFB model. The smallest read-only
real-firmware object/ID-keyed probe is specified beside that gate in
`dcp-firmware-services.md`; no D582/D594/D595 emission is justified
from the QEMU ID-1 replay.

**A407 gate input meaning, static only.** `w25` is the A407 handler's
monotonic per-layer acceptance bit: it starts at one (`0x04216510`),
visits four layers selected by found-object `+0x3d8` (copied from A407
request `+0x14c` at `0x04215dcc`), and can clear at
`0x0421683c/0x04216990` for missing resolved surfaces, invalid source
rectangles, or passthrough geometry. `w25=w24` at `0x042169ac`.
`w21` is separately derived from framebuffer state and virtual results
at `0x042169ec..0x04216c58`; the branches forcing it to one log a
**swallowed swap** for external-display power/timing or unplugged
fast-update conditions (firmware strings `0x04489182`, `0x044891e6`,
`0x04489237`, `0x0448929f`). A zero `w21` can bypass the force-one
path at `0x04216a48` or follow virtual `+0x520` bit-one result at
`0x04216a68..0x04216a74`; later overrides can still set one.
Thus `0x04216d7c..0x04216d98` queues the found object only when
**`w25&1=1 && w21&1=0`**. Passing calls `0x04213980`, which schedules
callback `0x04213e64`; only after callback checks can `0x04214338`
move that *same object* from `fb+0x3e30` to `fb+0x3e20`, with later
conditional active installation at `0x0420c934..0x0420c944`.
If `w25=1` but `w21=1`, the handler can still return A407 status zero
(`0x04216d80..0x04216d88`) while its other arm schedules
`0x04213de4` to unlink/release the object from `fb+0x3e30`
(`0x04216db0..0x04216e0c`). Status zero therefore cannot stand in
for a gate or queue-transfer hit even on real firmware.
The observed QEMU A407(ID=1) does not execute this gate, so it cannot
show whether a normal real-firmware CLASS16 swap passes. The smallest
read-only real-firmware capture and A/B/C/D outcomes are in
`dcp-firmware-services.md`; neither this static path nor QEMU's
status-zero A407 assigns a completion FourCC.

The original sparse RPC #39 dump for `UI_CLASS16_REFRESH60_DM_A406_IDS1`
narrowed the request side: A407 input `+0x14c` is
`0x80000007` (slots 0–2 selected), but every `+0x9c+4*i` word is
zero. That excludes the firmware clear branch at
`0x04216694..0x04216698` requiring a *nonzero* word and null resolved
surface. The wrapper independently deserializes per-layer blobs at
`+0x6e0+0x22c*i`, controlled by bytes `+0x1d23+i`
(`0x04222e70..0x04222ef0`); those bytes and most blob rows were not
in that 64-row sparse print. On the swallow side,
virtual `+0x220` resolves to `0x04214428` for both framebuffer classes
and does not directly write the five source fields; one direct reset
of `fb+0x42bc/+0x42c4` is `0x0429c2a8`. No prior QEMU RPC reply is
proven to establish their real-firmware values.

`UI_CLASS16_REFRESH60_DM_A407_FULL1` then captured all `0x1d30` bytes
of the matched ID-1 A407 request with `DARWIN_DCP_IOMFB_DEBUG=1` and
stopped on the object-keyed A407 return. The full input is in
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_FULL1.stderr.log`, with an
exact reconstructed body at
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_FULL1.input.bin`. It confirms
`+0x98=1`, `+0x14c=0x80000007`, and all four `+0x9c+4*i` words zero.
The four flags at `+0x1d23..+0x1d26` are **`01 01 01 01`**; each
`0x22c`-byte blob at `+0x6e0+0x22c*i` is entirely `0xaa`. On the AP,
the A407 builder at `0xa0c8e10..0xa0c8e38` writes flag 1 when its
surface-array pointer is null and only serializes a nonnull pointer.
On firmware, flag 1 sets `x2[i]=0` at `0x04222e88..0x04222ef0`.
Thus the selected slots 0–2 carry **no surface**, rather than a
presented surface with bad geometry. The handler's zero per-layer word
and null resolved pointer take `0x04216694 → 0x042166a0 →
0x04216840`, bypassing both clear sites `0x0421683c/0x04216990`.
With an ID-1 lookup hit, this exact request yields `w25=1` at
`0x042169ac`; the next unknown is real-firmware `w21` at
`0x04216d7c`, not further AP surface construction. QEMU's status-zero
reply and retained-ID HIT still do not prove a firmware transfer or
assign any completion callback.

The minimal matched-object real-firmware stops at `0x042169b0` (after
`w25=w24`) and
`0x04216d7c`, with conditional clear/force-one follow-ups, are listed
in `dcp-firmware-services.md`; no model reply or completion change
follows from this artifact-only score.

Static follow-up on this same full request also found
`request+0x34f=0` and `+0x17c=0`. The former excludes the
`fb+0x42fd==1 && request+0x34f==1` fast-update swallow at
`0x04216b64..0x04216bb8`; the latter is copied to swap-object
`+0x3ec` at `0x04215e40`, excluding the later `+0x3ec==0x15`
override on the visible direct path. The remaining `w21` fork is
firmware framebuffer policy: `fb+0x42bc==0` forces swallow;
`+0x42bc!=0`, `+0x4305!=1`, and initial `w21=0` preserves zero;
other arms depend on `+0x42c4` and virtual `+0x520` (bit 0), with
`+0x42dc==2` additionally sourcing initial `w21` through virtual
`+0x820`. Firmware setup at `0x042862e8` can initialize
`+0x42bc/+0x42c4` to one; `0x0429c2a8` can clear them. Neither
gives their value at this A407. The exact branch table, external
display swallow diagnostics, and class-specific virtual targets are
in `dcp-firmware-services.md`. Thus the measured ID-1 request is
`w25`-pass-valued **if lookup succeeds**, but still cannot be called
transfer-eligible (`w21=0`) or swallowed from QEMU. The smallest
real-firmware discriminator is the matched object/ID, `w25` and
`w21` at `0x04216d7c`, plus whether that object enters
`0x04213980`; if swallowed, capture just the policy fork at
`0x04216a28..0x04216b4c`. No D-series completion is assigned or
emitted from this result.

The bounded AP cross-reference pass found no proven mirror of firmware
`fb+0x42bc/+0x4305/+0x42c4/+0x42dc` or QEMU reply that fixes
their value. The AP `self+0x430` external-display byte in
`iomfb-d575-external-cancel.md` is a **different object/offset**;
its semantic theme does not establish an identity bridge. Static
analysis of `w21` on the present QEMU model is therefore closed: the
model does not execute the task-2 A407 handler. The ready read-only
real-firmware discriminator is
`tools/re/dcp_a407_w21_gate_callbacks.py`, whose nine dynamically
installed sites key `request+0x98==object+0x3d4==1`, read
`w25/w21` at `0x04216d7c`, and report either same-object
`0x04213980` **transfer-scheduled** or `0x04216da0` **swallow**.
It requires a target exposing real DCP task-2 registers/memory and
cannot run inside this QEMU IOMFB stub. The first model-local edge
before `w21` is now an **opt-in experiment**:
`DARWIN_DCP_IOMFB_A406_OBJECTS=1` implies the existing
`A406_IDS` nonzero allocator, retains a minimal record keyed by the
returned ID, and logs A407 request `+0x98` HIT/MISS for an exact
`0x1d30`-byte body. `A406_IDS=1` alone preserves its previous ID-only
marker. Both A407 HIT and MISS still return the existing status-zero
model reply; firmware miss status `0x8000000a` is not synthesized.
`DARWIN_DCP_IOMFB_OUT` canned A406/A407 replies take precedence over
allocation/object lookup. Default-off object state is transient and
does not instantiate firmware `fb+0x3e30` or execute the `w21` gate.
The pure helper contract is tested in `test-darwin-iomfb-swap.c`.
Neither the real-firmware probe nor this QEMU object probe assigns or
emits D582/D594/D595, transfers a swap, or changes CLASS16.

**Live object-lookup check (`UI_CLASS16_REFRESH60_DM_A406_OBJECTS1`).**
The rebuilt QEMU ran one bounded child of the same saved
`ui-class16-refresh60-dm-plugin4-launchfrozen1.qcow2` parent with the
patched dev6 boot inputs and unchanged CLASS16 60 Hz fixture. The
new `A406_OBJECTS=1` opt-in was active (with `A406_IDS=1`), while
`COMPLETE=0` and `SCANOUT=0`. The existing six fixture callbacks were
unchanged. All 23 read-only lineage sites armed. Model RPC #38 A406
(`in=16/out=8`) logged `allocated id=1` and `object RETAIN id=1`;
RPC #39 A407 (`in=0x1d30/out=12`) carried `+0x98=1` and logged
`object lookup HIT id=1`. The same AP IOMFB object stored ID 1 at
`+0xb0`, reached `_kern_SwapEnd`, created pending-node ID 1, and entered
`_kern_SwapWait(fence=1,options=1)`. The condition watcher stopped on
the HIT, with no matched Wait return and no A408 or D-series swap
completion in the short window. The bound verifies list membership
and lookup **inside QEMU only**; transfer, firmware policy `w21`, and
retirement remain unmodeled. Logs:
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A406_OBJECTS1.stderr.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A406_OBJECTS1.lldb.log`,
`.probe.out`, and `.stop` for the same tag. Build:
`ninja -C qemu-sptm/build qemu-system-aarch64`; boot:
`tools/re/setup_gate_probe.sh UI_CLASS16_REFRESH60_DM_A406_OBJECTS1`
with the saved parent/BOOTKC/DTREE/TC, CLASS16 callback file,
`CALLBACKS=dm_a406_id_lineage_callbacks`, and a 420-second maximum.

**Opt-in post-HIT state/assumed queue brick.**
With `DARWIN_DCP_IOMFB_A406_OBJECTS=1`, a matched A407 now copies
request `+0x17c` into a minimal retained-object state field; the
firmware's corresponding conditional `object+0x3ec` write is at
`0x04215e3c..0x04215e40`. Objects mode alone reports the state copy
and leaves gate/transfer unmodeled. `DARWIN_DCP_IOMFB_A407_TRANSFER=1`
implies objects and nonzero A406 IDs, then marks the matched model
record queued and logs `TRANSFER_ASSUMED`. This is an explicit
**`w21=0` assumption**, not a measured firmware gate result or an
actual `fb+0x3e30→+0x3e20` list transfer. The measured CLASS16 A407
body has request `+0x17c=0`; that value is valid state-copy input.
MISS/invalid A407 requests leave retained state alone. A407 still
replies status 0 on both HIT and MISS, and canned
`DARWIN_DCP_IOMFB_OUT` answers take precedence. There is no completion
callback, pending-node retirement, SwapWait wake, producer path, or
CLASS16 fixture change in this opt-in brick. The affected QEMU target
built and all six `test-darwin-iomfb-swap` unit cases passed.

`UI_CLASS16_REFRESH60_DM_A407_TRANSFER1` exercised this opt-in in one
bounded child from the same saved parent and unchanged CLASS16 fixture.
`A407_TRANSFER=1` implicitly enabled objects/nonzero IDs;
`COMPLETE=0` and `SCANOUT=0`. All 23 AP lineage hooks armed. A406
RPC #38 allocated/retained ID 1, then A407 RPC #39 carried `+0x98=1`
and logged HIT, state copy `request+0x17c=0`, and
`TRANSFER_ASSUMED id=1 queued=1`. The same AP object entered
`_kern_SwapWait(fence=1,options=1)` without a matched return before
the watcher stopped on the marker. No A408 or D582/D594/D595 appeared
in that short window. This is a QEMU-only queued mark under an
unmeasured `w21=0` assumption; it does not demonstrate firmware queue
transfer or pending-swap retirement. Evidence:
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_TRANSFER1.stderr.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_TRANSFER1.lldb.log`,
`.probe.out`, and `.stop` under the same `/tmp/dvm/` tag. The build was
`ninja -C qemu-sptm/build qemu-system-aarch64
tests/unit/test-darwin-iomfb-swap`; the bounded boot used
`tools/re/setup_gate_probe.sh UI_CLASS16_REFRESH60_DM_A407_TRANSFER1`
with `CALLBACKS=dm_a406_id_lineage_callbacks` and a 240-second maximum.

**Completion identity after the queued mark remains unresolved.**
The current firmware has a direct but conditional queued-object D582
source: `0x04296d34/0x042975b4` load the `fb+0x3e20` object into `x20`;
`0x04297694..0x042976a0` require local `sp+0x5c==1`; and
`0x042976a4..0x042976d8` send a 12-byte D582 containing that object's
`+0x3d4` ID. A distinct D582 HOL path serializes saved `fb+0x2750`
IDs at `0x0420de58..0x0420de88`, without a proven A407 identity edge.
If an object is promoted to active `fb+0x3e18` at
`0x0420c934..0x0420c944`, D595 can append its ID at
`0x0420ddec..0x0420de34` and send the batch at `0x04218f8c`, while
branch-gated D594 callers at `0x0420da94..0x0420dab0` and
`0x0420e360..0x0420e378` can pass the active ID to `0x0421c20c`.
The CLASS16 state-0 request does not fix the D582 local flag, D595
virtual/count gates, or D594 branch. More than one current-build
producer therefore remains plausible for ID 1. The AP D582/D594/D595
removers being able to unlink `fb+0x4138` does **not** choose among
them. The transfer-only queue mark emits no D-series callback;
`SwapWait` cannot retire through that mark alone.

The narrow real-firmware discriminator is the first same-fb, ID-1
producer serialization: queued D582 at `0x042976a4` (`x20+0x3d4`,
`sp+0x5c`), saved HOL D582 at `0x0420de58` (`fb+0x2750` entry), D595
batch append at `0x0420ddec` (`[fb+0x3e18]+0x3d4`), or active-ID D594
calls at `0x0420da94/0x0420e36c`. This is a producer-only follow-up,
not an instruction to emit a candidate callback in QEMU.

**QEMU A/B completion hypotheses (`UI_CLASS16_REFRESH60_DM_A407_AB_*1`).**
`DARWIN_DCP_IOMFB_A407_COMPLETE=d582|d595|d594` is default-off and implies
the existing retained-object and `TRANSFER_ASSUMED` switches. D582 sends the
queued ID in this build's 12-byte packet (`id+0`, byte `+4=1`, unknown
auxiliary word `+5=0` as a hypothesis); D595 and D594 first log
`ACTIVE_ASSUMED` for that ID. D595 sends one ID and count 1 in the current
`0x2708`/`0x400` batch shape. D594 reuses the proven `0x730`-byte full-remover
payload as a control; it is **not** thereby assigned to A407. Each variant
uses the A407 caller's nested ack-0 slot, then replies status 0 to A407, to
avoid the separate-workloop inversion documented in
`display-completion-deadlock.md`. `DARWIN_DCP_IOMFB_COMPLETE=0` and scanout
stayed off; the CLASS16 fixture and saved parent were unchanged.

| Variant / tag | AP entry, ID 1 | AP remover, pending head ID 1 | `_kern_SwapWait(1,1)` |
| --- | --- | --- | --- |
| D582 / `UI_CLASS16_REFRESH60_DM_A407_AB_D5821` | `D582_ENTRY`, `0x0c` | `D582_REMOVER` | returned `0` in 6.0 ms |
| D595 / `UI_CLASS16_REFRESH60_DM_A407_AB_D5951` | `D595_ENTRY`, `0x2708`, count 1 | `FULL_REMOVER` | returned `0` in 8.2 ms |
| D594 / `UI_CLASS16_REFRESH60_DM_A407_AB_D5941` | `D594_ENTRY`, `0x730` | `FULL_REMOVER` | returned `0` in 4.0 ms |

For each tag, see `/tmp/dvm/probe/<tag>.stderr.log`,
`/tmp/dvm/<tag>.lldb.log`, and `/tmp/dvm/<tag>.probe.out`; all 28 read-only
hook sites armed. The remover was observed with the matching node at the
head, and Wait returned successfully; a separate post-unlink pointer sample
was not captured. All three are **AP-effective QEMU hypotheses**, so these
boots do not uniquify the firmware D-series producer. They also do not show
later migration or display progress: each stopped at the first ID-1 Wait
return. The producer-side real-firmware discriminator above remains required
to select a faithful callback for a future device model.

**One longer post-Wait boot (`UI_CLASS16_REFRESH60_DM_A407_POSTWAIT_D5822`,
2026-10-02).** The same parent, patched dev6 boot inputs, and unchanged
CLASS16 60 Hz fixture ran for 480 seconds with `A406_OBJECTS=1`,
`A407_TRANSFER=1`, `A407_COMPLETE=d582`, `COMPLETE=0`, and `SCANOUT=0`.
`dm_a407_postwait_callbacks.py` armed 33 swap/unwind sites. The model logged
one labeled `HYPOTHESIS` D582 send/reply per A407 HIT for IDs 1–4; the AP
reported four matching D582 remover hits with each ID at the pending head.
The original `_kern_SwapWait(1,1)` returned `0` after 6.9 ms. Subsequent
A406/SwapEnd/A407 calls carried IDs 2–4, and backboardd hit
`DM_XPC_SYNC_RETURN` about 1.24 seconds after the ID-1 Wait return. That
XPC return is unpaired with the original migration call. No A408,
`DM_PERFORM_RETURN`, SpringBoard check-in or shell return, or panic was
observed before the bound. Thus this hypothesis lets the guest perform
additional swaps after the formerly stuck Wait; migration completion and
the correct firmware completion FourCC remain unproven. Evidence:
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_POSTWAIT_D5822.stderr.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_POSTWAIT_D5822.lldb.log`, and
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_POSTWAIT_D5822.probe.out`.
`POSTWAIT_D5821` is excluded: LLDB failed to register the composite probe
before any swap breakpoint armed.

**Post-Wait migration boundary (`UI_CLASS16_REFRESH60_DM_A407_MIGTAIL_D5821`,
2026-10-02).** A 480-second child used the same saved parent, patched dev6
boot inputs, CLASS16 fixture, and only the default-off D582 hypothesis
(`A406_OBJECTS=1`, `A407_TRANSFER=1`, `A407_COMPLETE=d582`, A408
`COMPLETE=0`, `SCANOUT=0`). The ID-1 `_kern_SwapWait(1,1)` returned `0`
in 4.8 ms, followed by A407/D582 remover pairs through ID 5. The
earlier backboardd `DM_XPC_SYNC_RETURN` at `t=1790990106.126` came
*before* the owner thread entered `DMPerformMigration` at
`t=1790990106.167`; it was an unrelated sync return. That owner thread
(`0xffffffe0d94db830`) entered `DM_XPC_SYNC_CALL` at DataMigration static
`0x25e8f5798` (`t=1790990106.421`) and had no paired return before
freeze. No `DM_PERFORM_RETURN`, SpringBoard check-in/shell return, A408,
or panic appeared.

The frozen DataMigrator request was for
`/System/Library/DataClassMigrators/00LaunchServicesMigrator.migrator`
(exact XPC `pluginIdentifier=com.apple.-0LaunchServicesMigrator`), still
in `runPluginAtPath:...` → `sendMessageSync:`. A live wrapper (PID 192)
was in LaunchServices `__LSCopyServerStore` →
`___LAUNCH_SERVICES_IS_RESEEDING_ITS_DATABASE_AND_MAY_BLOCK__` →
synchronous NSXPC wait. `lsd` (PID 117) was alive with a worker in
`LSCreateRegistrationData` and `MIFilesystemScanner`/
`MILaunchServicesDatabaseGatherer` scanning; a second thread was in
bundle validation. This names the next **bounded** dependency as an LS
database reseed behind `00LaunchServicesMigrator`, rather than the
retired swap. The single frozen stack is not proof of a deadlock or lack
of scan progress. D582 still has no proven firmware-producer assignment.
Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_MIGTAIL_D5821.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_MIGTAIL_D5821.stderr.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_MIGTAIL_D5821.mem/migration-dependency.json`,
and `...mem/lsd-sparse.json` under the same tag.

**LS reseed bounded as advancing (`UI_CLASS16_REFRESH60_DM_LS_RESEED_D5822`,
2026-10-02).** The same D582-enabled CLASS16 parent/fixture was replayed
read-only for 650 seconds. ID-1 `_kern_SwapWait(1,1)` returned `0` in
8.8 ms; backboardd's matched `DM_XPC_SYNC_CALL` at static `0x25e8f5798`
remained open after `DM_PERFORM_ENTRY`. The live
`00LaunchServicesMigrator` wrapper entered `__LSCopyServerStore`
(`0x186df21c8`) and the blocking send (`0x186ef62b8`) without their
returns. `lsd` entered system scan (`0x186e75f0c`) and scan driver
(`0x186f81090`) before that wrapper send; both enclosing calls stayed
open, but registration callbacks returned continuously through item
**600**, the last sampled return only about 18 seconds before cutoff.
Thus this interval is **B: slow, progressing LS reseed**. The first open
server stage is scan driver; no individual registration was fixed in the
window. The earlier `lsd` blocking-handler hit preceded the wrapper's
send and is not a proven receive/reply pairing for it. Neither LS store
nor `DMPerformMigration` returned, and no panic occurred. D582 is still
a QEMU hypothesis, not a firmware callback assignment. Read-only probe
`tools/re/dm_ls_reseed_boundary_callbacks.py`; logs:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_RESEED_D5822.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_RESEED_D5822.probe.out`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LS_RESEED_D5822.stderr.log`.
The earlier `...LS_RESEED_D5821` run had an uninitialized process-name
slide in the probe and carries no LS verdict.

**LS rebuild reason on this saved parent (`UI_CLASS16_REFRESH60_DM_LS_REASON_D5823`,
2026-10-02): recovery file, not plain database-missing seed.** The same
D582-hypothesis CLASS16 setup stopped at the first builder reason. ID-1
SwapWait returned `0` in 6.95 ms. `lsd` read reason flags `0x5` from
the builder block at `0x186ec1e84`; the *same thread* entered system scan
(`0x186e75f0c`) and scan driver (`0x186f81090`) about six seconds later.
The code loads block `+0x28` at `0x186ec1ea0` and takes bit 2 through
`0x186ec1ec0 → 0x186ec1ef8..0x186ec1efc`, the named
`__LAUNCH_SERVICES_IS_REBUILDING_THE_DATABASE_BECAUSE_A_RECOVERY_FILE_EXISTED__`
arm. Bit 0 adds fresh creation; bit 3 (did-not-exist reason) is absent.
`__LSDatabaseCreateFromPersistentStore` calls
`__LSDatabaseRecoveryFileExists` (`0x186eb45b0`) and sets reason bit 2
at `0x186eb462c..0x186eb4630`. The persistent parent’s backing chain reaches
`ui-class16-refresh60-ccs-ls-seedprogress1.qcow2` through
`ui-class16-refresh60-dm-plugin4-launchfrozen1.qcow2`, where the ~1,060-item
scan returned but only the subsequent `syncWrite:` *entry* was observed.
That is consistent with, but does not establish, an interrupted prior
database sync. A later guest lsd path check (`...LS_PARENT_SEED_D5825`)
identified the existing marker as
`/private/var/mobile/Containers/Data/InternalDaemon/D7D6FEE4-CBF0-469A-B81A-1071B12A6174/Library/Caches/com.apple.LaunchServices.error`;
`com.apple.LaunchServices.syncInterrupted` and
`com.apple.launchservices.recreate` in the same cache were absent. The
recovery file's origin remains unproved. Its current-build create and
delete paths are `0x186eb4e00..0x186eb4e18` (`open`) and
`0x186eb53e4..0x186eb53eec` (`unlink`), respectively. The saved Data
volume is FileVault-locked under read-only host attach, so this check was
made from guest lsd without deleting or modifying LS files.
This short reason stop explains the full reseed but did not measure its
completion. The lsd handler peer PID 125 differs from the later wrapper's
PID 193, so the early handler was not their proven pair. D582 remained
a QEMU completion hypothesis. Read-only probe:
`tools/re/dm_ls_reseed_boundary_callbacks.py`; logs:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REASON_D5823.lldb.log` and
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REASON_D5823.probe.out`.

The corrected, condition-bounded recovery run
`UI_CLASS16_REFRESH60_DM_LS_PARENT_SEED_D5825` returned its scan driver
after 1,050+ registration returns, then returned from `syncWrite:`
(`0x186ec1ff4`), builder sync (`0x186ec1e6c`), and the migrator's
`__LSCopyServerStore` blocking send (`0x186ef62bc`). It stopped at the
last marker without a panic, preserving overlay
`/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825.qcow2`
as a candidate repaired parent. This proves finite recovery work and a
successful return from the previously unobserved write; a fresh child
must still show whether `.error` disappeared and `DMPerformMigration`
passes `00LaunchServicesMigrator`. The preceding `...D5824` boot used
the wrong callback fixture (zero timing rates), rebooted before LS,
and contributes no seed conclusion. Evidence:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_PARENT_SEED_D5825.lldb.log`.
On the fresh child `UI_CLASS16_REFRESH60_DM_LS_REPAIRED_CHILD_D5826`,
the same `.error` path returned `exists=0`; the first two migration
wrapper `__LSCopyServerStore` calls returned in ~0.19 s and ~0.03 s,
with no flags-`0x5` rebuild or system scan observed by then. The
saved overlay therefore clears the previous recovery-file LS gate.
The same child continued through 47 fast LS-store returns, then the
matched backboardd XPC send returned at `t=1790999956.738` and
`DMPerformMigration` returned on that thread at `t=1790999956.755`
(`x0=0`, about 1,194 s after entry). The probe stopped on that return;
SpringBoard check-in and shell tail are **not** established. The D582
retirement remains a QEMU hypothesis; this boot does not assign it as
the real firmware callback. Evidence:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_CHILD_D5826.lldb.log`.
The next child, `UI_CLASS16_REFRESH60_DM_LS_REPAIRED_SHELL_D5827`, uses
the same repaired parent and swap hypothesis. It armed the existing DM,
BKS check-in, and shell brackets before boot. ID-1 SwapWait returned
`0` in about 5 ms; SpringBoard then reached its migration check-in call
on the shell-service thread after all earlier endpoint and connection
setup calls returned. The check-in path is now measured across the
outer `DMPerformMigration` return instead of stopping at that return.
Probe: `tools/re/dm_repaired_ls_shell_callbacks.py`; log:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_SHELL_D5827.lldb.log`.
The matched backboardd `DMPerformMigration` returned `x0=0` at
`t=1791001551.963`. On the same SpringBoard shell thread that entered
check-in, the migration check-in call returned `x0=0` about 30 ms later;
`BKS_CHECKIN_RETURN` and `SHELL_SERVICE_RETURN` followed by
`t=1791001552.005`. The condition stop was the shell-service return,
with no panic or flags-`0x5` rebuild. Full shell-init tail, ADFL, and
pixels remain unmeasured. D582 is still a QEMU completion hypothesis.

`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_TAIL_D5828` continued from a new
child of the same repaired `...LS_PARENT_SEED_D5825.qcow2` with the
unchanged CLASS16 60 Hz fixture and D582 hypothesis (`A406_OBJECTS=1`,
`A407_TRANSFER=1`, `A407_COMPLETE=d582`; A408 completion and scanout
off). All 52 probe sites armed. ID-1 `_kern_SwapWait(1,1)` returned
`0` in ~6.2 ms, and matched `DMPerformMigration` returned `x0=0` at
`t=1791005038.213`; no flags-`0x5` rebuild marker fired. On the same
SpringBoard shell thread, migration check-in returned at `.260`,
BackBoardServices check-in at `.269`, and `_startSystemShellService`
at `.272`. All seven post-service tail sites then fired, followed by
`SHELL_PRE_RETURN` at `.679` and the caller-side
`SB_SYSTEM_APP_AFTER_SHELL` at `.685`, proving that
`_FBSystemShellInitialize` returned. SpringBoard entered
`applicationDidFinishLaunching` at `t=1791005039.355` on that same
thread. The probe stopped on ADFL entry; ADFL return, workspace,
CAContext, and pixels are not established. D582 remains a labeled
QEMU completion hypothesis, not an identified firmware callback.
Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_TAIL_D5828.lldb.log`
and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_TAIL_D5828.watch.log`.

`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_ADFL_D5829` used another fresh
child of the repaired D5825 parent with the same CLASS16 fixture and
D582 hypothesis; A408 completion and scanout stayed off. The 58
initial probe sites armed. ID-1 `SwapWait(1,1)` returned `0` in ~7.9 ms;
matched `DMPerformMigration` returned `x0=0`; and the caller-side
shell-init return fired before ADFL entry at `t=1791007927.630`.
The new read-only probe dynamically armed that ADFL call's actual
caller return address and `SBHomeScreenEntity init`'s caller return,
both keyed to SpringBoard thread `0xffffffe6cc33d120`. Within ADFL,
the workspace-create request fired at `t=1791008010.662`; the home
entity init returned a non-null object (`x0=0x7a0de8a1c0`) at `.681`.
After that return, `CAContext setLayer:` fired eight times, and the
workspace-execute-transition site fired twice. ADFL returned to its
caller on the same thread at `t=1791008028.444`, about 101 seconds
after entry. The stop required both ADFL return and non-null home init.
There was no flags-`0x5` LS rebuild marker or XNU panic. The observed
CA calls install layers; they do **not** prove a CA commit, scanout,
frame presentation, or pixels. D582 remains a QEMU completion
hypothesis, not a firmware FourCC assignment. Probe:
`tools/re/dm_repaired_ls_adfl_home_callbacks.py`; evidence:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_ADFL_D5829.lldb.log`
and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_ADFL_D5829.watch.log`.

`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_PRESENT_D5830` continued from a
fresh child of the repaired D5825 parent, keeping CLASS16 60 Hz and
`A406_OBJECTS=1`, `A407_TRANSFER=1`, `A407_COMPLETE=d582`, with A408
completion/scanout off. ID-1 Wait returned `0` (~6.2 ms), migration
returned `x0=0`, and shell/ADFL/Main-home returned again. The first
post-home SpringBoard `CA::Context::commit_transaction` entered at
`t=1791010063.172` and its paired caller return hit at
`t=1791010086.322`, ~23.15 s later. The first backboardd
`CA::WindowServer::Server::render_update` entered at
`t=1791010125.417` and returned at `.510`. `FB_SWAP_HANDOFF` initially
had null `x2`, then hit #6 at `t=1791010127.741` had a non-null
surface pointer (`x2=0x7c2ad303d0`, `x3=512`); later capped hits used
that pointer. Commit, render update, and surface handoff occurred in
order on one boot, but no transaction identity pairs the first commit
to that particular handoff. Only the first commit/render-update calls
had dynamic return hooks; entry logging capped at 16 per site.

The kernel `IOMFB_A408_BUILD` hook remained quiet and the model logged
no outbound A408 through the ~10-minute post-home condition bound.
A406/A407 and labeled D582-hypothesis traffic continued, but no A407
was paired to the non-null handoff. The after-home create-surface and
allocate-IOSurface hooks were quiet; this does not exclude a cached or
earlier surface. No XNU panic was reported. Thus the first-paint spine
has reached **commit → render update → non-null swap handoff**, while
**handoff → A408 (or another measured present route)** and actual
presentation remain open. No pixel/scanout claim follows from this
run. D582 remains a QEMU completion hypothesis, not firmware truth.
Read-only probe: `tools/re/dm_posthome_present_callbacks.py`; logs:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_PRESENT_D5830.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_PRESENT_D5830.stderr.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_PRESENT_D5830.probe.out`.

`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_BRANCH_D5831` used another fresh
child of D5825 with identical CLASS16/D582-hypothesis settings; A408
completion and scanout stayed off. The 73-site read-only probe selected
the first post-home non-null framebuffer handoff on backboardd thread
`0xffffffe230ebf060`: surface `0x75f11443d0`, IOMFB object
`0x75f1061400`, `x3=512`, at `t=1791013680.850`. The same thread and
object immediately reached SetLayer thunk `0x22a391ea0` (`+0xb38`
target `_kern_SwapSetLayer`, static `0x22a3918f4`) and public SwapEnd
thunk `0x22a391750` (`+0xc28` target `_kern_SwapEnd`, static
`0x22a391334`). The first matched End returned `0xe00002bd`
(`kIOReturnNoMemory`) at `t=1791013680.889`; the containing
`finish_update` returned next. The raw return row's `object_match=false`
is a probe-print bug: `x0` was the error code at return, while the saved
entry object matched the handoff; the script has been corrected for
future runs. Subsequent non-null handoffs and same-object End entries
recurred, but only the first End return was paired in this run.

The same boot's serial log reports 49 `mapper-disp0` inactive-DART
allocation failures, starting at line 1722: 759 pages, map length
`0xbdc000`, `iovmMapMemory` status `0xe00002bd`, and IOMFB's DMA-command
memory-descriptor failure. The serial message omits the surface pointer,
so its exact map object is not independently paired to the first
handoff; the identical error and immediate path are consistent. No
matched node-link/submit-gate (`0xa0c4518`, `0xa0c4564`, `0xa0c4608`),
A407 build (`0xa0c477c`), or A408 build (`0xa0c4664`) was observed
after the selected End, and the model logged no A408. Thus this natural
post-home transaction fails **at or within `_kern_SwapEnd`'s surface
mapping**, before the A407-versus-A408 branch. The next discriminator
is `dart-disp0` active state on the matched map attempt; prior `GATE154`
shows a different boot where it was active and the map/A408 path ran.
There is no presentation or pixel evidence here, and D582 remains a
QEMU completion hypothesis. Probe and logs:
`tools/re/dm_posthome_swap_branch_callbacks.py`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_BRANCH_D5831.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_BRANCH_D5831.serial.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_BRANCH_D5831.stderr.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_BRANCH_D5831.probe.out`.

`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5832B` repeated the same
D5825/CLASS16/D582-hypothesis profile with a narrow DART probe (A408
completion and scanout off). After `DMPerformMigration` returned and the
Main-home entity initialized, the first non-null handoff carried surface
`0x752ad18480` on IOMFB object `0x752b0bc000`. The matched `_kern_SwapEnd`
on thread `0xffffffe4e355c180` entered IOMMU `+0x550` at static
`0x9f22254` with length `0xbdc000`; that mapper was
`0xffffffe3fdf8c000`, whose provider was **`dart-disp0`** at
`0xffffffe231e0c000`. The same mapper and provider persisted through
the `+0x570`/`+0x608` vtable queries, `_iovmAlloc`'s active check, and the
map result. At map entry, mapper `+0x1e0=0` and provider
`+0x99a=0` (`+0x988=0xc0000065`); both vtable queries returned
0. The mapper requested active state (`x1=1`) at `0x9f1ee98`, but
the provider `+0x598` query at `0x9f1eef4` returned **0** at
`0x9f1eef8`. Execution reached the transition join `0x9f1f0a8`
without a hit at the DART power call `0x9f1f074` or the mapper-state
write `0x9f1f07c`. `_iovmAlloc` then logged an inactive-DART reject
for 759 pages, the paired IODMA return was `0xe00002bd`, and this
same-object `_kern_SwapEnd` returned `0xe00002bd`. This now directly
pairs the serial failure to the first post-home End rather than relying
only on timing. There was no matched A408 build or model A408.

Read-only HMP checks during this boot found the `dart-disp0` SID-0
enable register at `0x412300c00` equal to 0, with SID-0 TCR and TTBR
also zero; `dart-dcp`'s separate enable register was nonzero. The
matched mapper's SID field `+0x110` was **not** printed in this run, so
the SID-0 association remains the established pixel-DMA wiring, not a
new measured mapper-field value. The exact policy branch that skipped
`0x9f1f074` after the inactive query is still open; absence of a hit
before LLDB attachment cannot establish that power was never requested
earlier. No DART/model enable change follows from this result. Probe:
`tools/re/dm_posthome_dart_active_callbacks.py`; logs:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5832B.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5832B.serial.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5832B.probe.out`.

`UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5833` closed that guard on
another fresh D5825 child with the same env and fixture. The first
post-home non-null handoff (surface `0x7b0cd203b0`, IOMFB object
`0x7b0d0d5400`, thread `0xffffffecf19858f0`) again entered a paired
`_kern_SwapEnd` and mapped `0xbdc000` through `dart-disp0` via mapper
`0xffffffec0c053000`. This time the matched mapper's
`+0x110` **was read as SID 0**. Its `+0x120` configuration object's
word at `+8` was `0xe0000065` (bit 31 set); the DART provider's
`+0x988` flags were `0xc0000065` and active byte `+0x99a=0`.
The requested mapper state was `w20=1`, while the provider's
`+0x598` query returned `w25=0`. Prerequisite call at `0x9f1ef08`
returned 1. Execution then hit **all three same-thread sites**:
`0x9f1effc` (`tbz config+8 bit31`, not taken), `0x9f1f000`
(`eor w8,w20,w25`, producing bit 0 = 1), and `0x9f1f008`
(`b 0x9f1f0a8`, skipping the power call). Neither the DART power
method at `0x9f1f074` nor mapper-state write at `0x9f1f07c`
ran. The same `dart-disp0` mapper rejected the 759-page request as
inactive; matched `_kern_SwapEnd` returned `0xe00002bd`.

That is the **first exact branch explaining the failed map**: with
config bit 31 set, the active-state mismatch is joined without
powering DART. A prior, unpaired state request after migration returned
had already taken the same guard on this mapper; it did not activate
the provider before the home swap. The meaning of config bit 31 is
consistent with the existing manual-availability evidence, but its
full policy and the upstream event that should make `dart-disp0`
active remain unproven. QEMU's DART register model already records
enable/TCR/TTBR writes; this replay proves no power call at the
matched guest branch, not a model no-op. No forced enable or IOMFB
model change is justified. D582 is still a completion hypothesis;
no A408/presentation/pixels follow from this result. Probe:
`tools/re/dm_posthome_dart_active_callbacks.py`; logs:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5833.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5833.serial.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LS_REPAIRED_DART_D5833.probe.out`.

`UI_CLASS16_REFRESH60_DM_DART_ENABLE_D5834` bounded the upstream
activation path on another unchanged D5825/CLASS16/D582-hypothesis child.
The first post-home non-null handoff (`x2=0x7aa310c8b0`, IOMFB object
`0x7aa305a800`) reached the same-thread mapper `0xffffffe336064000`,
`dart-disp0` provider `0xffffffe502258000`, SID 0, length `0xbdc000`.
At that matched map and an earlier request on the same mapper, provider
`+0x99a` was 0, its `+0x598` query returned 0, requested state was 1,
and config `[[mapper+0x120]+8]=0xe0000065` took the
`0x9f1f008` skip-power branch. The matched map rejected 759 pages as
inactive and `_kern_SwapEnd` returned `0xe00002bd`. All provider-writer
hooks were armed before execution, but none hit for `dart-disp0` from
attach through this return. With `DARWIN_DART_DEBUG=1`, the model logged
no `dart-disp0` MMIO read or write (and no `ENABLE_STREAMS` write), while
`dart-dcp` did receive `0xc00` stream-enable writes. This rules out an
attempted display-DART enable that the QEMU register model ignored on
this boot; it does not identify the absent caller.

Static BOOTKC direct-store inventory finds two paths to provider
`+0x99a`: `AppleT8110DART` vtable `+0x580` at `0x9707c3c` writes at
`0x9707ce4` after its power helper succeeds and the caller's `x3` is
nonzero; vtable `+0x648` at `0x9708a98` writes its requested
availability (`x1`) at `0x9708ad8` when `+0x988` bit 31 selects the
manual-availability path. The IODART platform-function dispatcher
`0x9f1b560` can reach that `+0x648` method at `0x9f1b958..0x9f1b988`,
with `x1=([x21]!=0)`. Separately, the IOMFB `powerUpDART` route
`0xa0ba4b0` can reach its vtable `+0x8c0` at `0xa0ba5ec`, then
`0xa0ba678` calls the `IODARTMapper` vtable `+0x3a8` at
`0xa0ba78c`/`0xa0ba7d0`; that method (`0x9f26474`) invokes the mapper
state transition `0x9f1ee98` at `0x9f265b0`. On D5834 the observed
state transitions both skipped the `+0x580` power call. Whether the
IOMFB route made either request was not caller-paired, and the source
of the missing manual-availability request is still unknown.
GATE154 is the counterexample: the same `0xc0000065` flag class had
`+0x99a=1`, successful maps, and A408. Thus the first unmet prerequisite
on this lineage is **activation of `dart-disp0` before the home map**,
not a permanent bit-31 prohibition or a QEMU `ENABLE_STREAMS` no-op.
Only GATE154's documented result and surface events remain locally;
its old LLDB/serial traces were unavailable for a writer-by-writer replay.
The next read-only cut is the caller of the `0x9f1b944..0x9f1b988`
manual-availability dispatcher, paired with IOMFB `powerUpDART` entry
`0xa0ba4b0` on this display/mapper; no display-DART MMIO should be
invented before that request is identified.
No forced enable, fixture change, or model patch follows from this
read-only result. Probe:
`tools/re/dm_posthome_dart_active_callbacks.py`; logs:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_DART_ENABLE_D5834.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_DART_ENABLE_D5834.stderr.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_DART_ENABLE_D5834.serial.log`.

**D5835 caller slice (static, before runtime verdict).** In this BOOTKC,
the two direct calls to IOMFB `powerUpDART` (`0xa0ba4b0`) are
`DCPPowerManager::fn_0xa0` at `0xa0d667c` and the D572 AP callback
(`0xa0d9aa4`, input/output 4 bytes) at `0xa0d9b74`. The power-manager
path is inside its `set_power_state` code (`0xa0d6338`) and passes
`enable=0`, `dcp_power_off=1` at `0xa0d6670..0xa0d667c`, so this
particular call is **power-down**, not an activation candidate. The D572 path
indexes an IOMFB pointer at `0xa0d9b30..0xa0d9b60` and passes the low
bits of payload bytes 0 and 1 as the two `powerUpDART` arguments at
`0xa0d9b64..0xa0d9b74`. `powerUpDART`'s impl calls the display mapper's
vtable `+0x3a8`; the mapper's `setActive` selector is stored at `+0x1d8`
(`0x9f1dc6c..0x9f1dc78`) and its matching branch
`0x9f26530..0x9f265b0` reaches the state transition already measured
to skip provider power when active state is 0 with config bit 31 set.
There is also a power-manager vtable `+0x80` path at `0xa0d5db0`:
`0xa0d5e18..0xa0d5e24` calls IOMFB
`powerUpDART_v2_assertion` (`0xa0ba37c`) with enable derived from
`x3 != 0`. On its refcount transition, `0xa0ba460..0xa0ba488`
calls IOMFB vtable `+0x8c0` (`0xa0ba658`/`0xa0ba678`), so this is
another activation-capable caller even though it does not directly call
`0xa0ba4b0`. The D5835 hooks cover the vtable implementation, not the
v2 assertion entry; a quiet implementation hook would rule out its
activation transition, not every assertion entry.

The alternate provider-forwarding selector on that mapper is
`registerErrorHandler` at `+0xa70` (`0x9f1dc4c..0x9f1dc58`).
`IODARTMapper::fn_0x3a8` dispatches it at `0x9f2671c..0x9f2675c`
to the provider's `IODART::fn_0x3a8` (`0x9f1b560`); a narrower branch
at `0x9f1b944..0x9f1b988` can invoke `AppleT8110DART` vtable `+0x648`
and write `+0x99a`. This is a possible activation route, but the
`registerErrorHandler` selector and argument checks prevent treating
every platform-function call as an availability change. Live D5835
hooks distinguish this route from the power-manager/D572 `setActive`
route; none implies DCP firmware correctness in QEMU.

The first unchanged D5825/CLASS16 replay,
`UI_CLASS16_REFRESH60_DM_DART_CALLER_D5835`, armed all 48 DART sites
before execution and saw ID-1 `SwapWait` return plus
`DMPerformMigration` entry. It **did not reach the post-home non-null
handoff**: `SpringBoard.migrator` hit its native reboot deadline and
the guest panicked at 12:37 PDT. No D572, v2 implementation,
platform-function, provider-writer, or matched-map hook fired before
that panic. This is coverage, not evidence that no activation caller
would run before a home map. Logs:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_DART_CALLER_D5835.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_DART_CALLER_D5835.serial.log`,
and `.stderr.log`. A second read-only replay from the same parent was
started as `UI_CLASS16_REFRESH60_DM_DART_CALLER_D5835B`; keep its
result separate from the pre-home panic.

**D5835B reached the caller verdict.** The unchanged repeat passed
ID-1 `SwapWait`, matched `DM_PERFORM_RETURN` with `x0=0`, check-in,
shell-service return, and ADFL without panic, then paired the
post-home non-null surface `0x7b20d3c410` with IOMFB object
`0x7b20cb9400`, the same-thread `MAP_ENTRY`, and `_kern_SwapEnd`
return `0xe00002bd`. The map used mapper `0xffffffebb4215000`,
provider `dart-disp0` `0xffffffebb3ba6000`, SID 0, length
`0xbdc000`. Two `0x9f1ee98` state requests on that same mapper
(one earlier, one inside the matched map) requested 1 while provider
`+0x99a=0`; both provider queries returned 0 and both took
`0x9f1effc → 0x9f1f000 → 0x9f1f008` with config word
`0xe0000065`, skipping power. The matched mapper logged the inactive
759-page rejection before End returned. All 48 DART hooks were armed
before execution. **No** D572 handler, direct `powerUpDART` call,
v2 assertion's `+0x8c0` implementation, mapper `setActive` caller,
manual-availability platform-function branch, or either provider
`+0x99a` writer fired before that map. The model logged no
`dart-disp0` MMIO while `dart-dcp` had separate stream-enable writes.

Classification depends on the boundary: **A** for the instrumented
upstream activation call/transition (none ran for any instrumented
instance before this map), and **C** for the map-triggered state
request (it ran for the correct SID-0 mapper but the manual-availability
bit-31 path left it inactive). This rules out B, a wrong target among
the observed activation calls, and a guest enable write ignored by
QEMU. The first absent edge is an earlier activation request that would
set provider `+0x99a`/enable the display stream before map. D572 is a
specific candidate: its handler can request `powerUpDART(enable=1)`,
but the unchanged CLASS16 callback script has no D572 item and the
QEMU IOMFB model has no D572 emitter. The power-manager v2 assertion
is another candidate; its entry was not hooked, so a call that never
reached its `+0x8c0` activation transition is not excluded. Neither
candidate is proven to be the required
real-firmware event by this QEMU-only trace; do not synthesize either.
GATE154 proves the same flag class can have `+0x99a=1`, successful
power-up/map and A408, but its old logs do not identify which caller
supplied that state. D582 remains a completion hypothesis. No DART
enable/model, fixture, or A408 completion change was made. Logs:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_DART_CALLER_D5835B.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_DART_CALLER_D5835B.serial.log`,
and `.stderr.log`.

**D572 activation hypothesis (`UI_CLASS16_REFRESH60_DM_D572_HYP1`).**
`DARWIN_DCP_IOMFB_D572=1` is default-off in the QEMU IOMFB model.
With the unchanged CLASS16 callback file, it appends one D572 item after
the six setup callbacks, which start after A353. The packet has four input
bytes `01 00 00 00` and four output bytes: in this build's handler
`0xa0d9aa4`, `0xa0d9b64..0xa0d9b74` pass input byte-0 bit 0 as the
`powerUpDART` enable argument and byte-1 bit 0 as its power-off argument.
This is an **activation hypothesis**, not a claim that firmware sends D572
at this time. The model neither writes DART MMIO nor changes the D582
completion hypothesis, CLASS16 fixture, A408 completion, or scanout.
The first child logged callback #7 D572 after D128, sent and acknowledged
with status 0/output `00000000`. In the send-to-reply interval, the guest
wrote `dart-disp0` TCR/TTBR registers and `ENABLE_STREAMS` `0xc00=0x18`;
the baseline had no display-DART writes. `0x18` names streams 3 and 4,
not SID 0, so these writes alone do not establish the matched map's active
state. The late slide-resolved LLDB probe armed after this early callback;
its handler-entry hook cannot independently score that early invocation.
The matched post-home provider byte `+0x99a` and map/End verdict are scored
below. Logs:
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_D572_HYP1.stderr.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_D572_HYP1.lldb.log`.

The condition stop fired on the same-thread, same-IOMFB-object post-home
non-null handoff (`x2=0x7789140490`, `x3=512`) and `_kern_SwapEnd`
return. The live sequence was `DM_PERFORM_RETURN x0=0` → shell-service
return → ADFL entry → handoff → `dart-disp0` SID-0 map → End `x0=0`.
At the first two post-migration `0x9f1ee98` state requests and at the
matched map, the display provider had `+0x99a=1`; the map's two DART
precondition returns were `x0=0`, its `IODMA_MAP_RETURN` was `x0=0`,
and the matched End returned `0` instead of D5835B's `0xe00002bd`.
The model also recorded a later **guest** `dart-disp0` `0xc00=0x1`
write before A407 #61 (SID 0); the early D572 exchange itself wrote
`0xc00=0x18`. These are guest writes, not QEMU force-enables.

| D572 hypothesis score | D5721 evidence | Limit |
|---|---|---|
| Packet / AP reply | callback #7, 4-byte in/out, status 0 | Direct handler hook armed after this early exchange. |
| Power activity | `dart-disp0` TCR/TTBR and `0xc00=0x18` inside D572 exchange | `powerUpDART` entry not independently hooked at that early time. |
| Provider active | `+0x99a=1` on SID-0 state queries and matched map | No known-writer hook hit after arm; activation time was not directly captured. |
| Matched home map / End | DART map preconditions 0, `IODMA_MAP_RETURN=0`, `_kern_SwapEnd=0` | A408 build/model A408 did not occur before the End-return stop. |

The D5721 End-return stop also retained the **pre-return submit bracket**
for that exact handoff object/thread: node link `0xa0c4518` carried swap
ID 12 and `+0x3d8=1`; `SUBMIT_GATE_1` at `0xa0c4564` read
`fb+0x5870=0`; `SUBMIT_GATE_2` at `0xa0c4608` read `fb+0x5df0=0`;
`A407_BUILD` at `0xa0c477c` fired; model RPC #434 was A407 with
request `+0x98=12` and a retained-object HIT for 12. `A408_BUILD` at
`0xa0c4664` did not fire before matched End returned 0. The bit-0-clear
`fb+0x5df0` branch is the measured A407 selector in this call, after
the successful map.
The original stop happened at End return, so it captured that decision
but not any later presentation work.

**D572 submit repeat (`UI_CLASS16_REFRESH60_DM_D572_SUBMIT1`).** A fresh
child of D5825 with identical CLASS16/D572/D582 settings reached
`DM_PERFORM_RETURN x0=0`, ADFL, and the first post-home non-null handoff.
The handoff and `_kern_SwapEnd` used the same backboardd thread and
IOMFB object `0x76eac41400`. At the matched SID-0 map,
`dart-disp0+0x99a=1`, both DART precondition returns and
`IODMA_MAP_RETURN` were zero. The same End then linked node ID 12
(`+0x3d8=1`) at `0xa0c4518`; `fb+0x5870=0` at `0xa0c4564` allowed
the second gate, and `fb+0x5df0=0` at `0xa0c4608` took its
bit-0-clear A407 branch. `A407_BUILD` at `0xa0c477c` fired for the
same node/thread; QEMU RPC #830 was A407 with request `+0x98=12`
and logged its QEMU-local ID-12 lookup HIT. The paired End returned
`x0=0`. No matched `A408_BUILD` or model A408 fired before the
End-return stop. This repeats D5721's **mapped End → A407** choice;
the first deciding field after mapping is `fb+0x5df0` bit 0, observed
clear on both children. It does not identify what would set that bit
or establish any presentation. Logs:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_D572_SUBMIT1.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_D572_SUBMIT1.stderr.log`,
and `.serial.log`. The condition-bounded run ended at the matched
End return with zero XNU panics.

This is a **QEMU-positive D572 timing hypothesis** for the previously
missing display-DART activation, not proof that current DCP firmware
schedules D572 here. It removes the specific inactive-DART/NoMemory
block on this boot. It does not establish a submitted A408, completed
presentation, scanout, or pixels; D582 remains an independent
hypothetical swap retirement callback. The repeated post-map choice is
A407; the meaning and writer of the still-clear `fb+0x5df0` bit are
separate from this activation experiment.

**`fb+0x5df0` writer bound (`UI_CLASS16_REFRESH60_DM_FB5DF0_WRITER1`
and `UI_CLASS16_REFRESH60_DM_FB5DF0_EARLY1`).** In the current BOOTKC
DCP kext, `_kern_SwapEnd` saves `fb+0x5000` at `0xa0c388c`, reloads it
as `x26` at `0xa0c41ec`, and reads `x26+0xdf0` at `0xa0c45fc`:
this is the same `fb+0x5df0` byte read by the bit-0 `tbz` at
`0xa0c4608`. Set bit 0 falls through to the `+0x998` A408 call at
`0xa0c4664`; clear bit 0 branches to the `+0x990` A407 call at
`0xa0c477c`. This establishes a **submit-method selector**, not a
named power, surface, or presentation state. A direct-offset scan of
the DCP and base IOMFB kexts found this read but no direct store to
`+0x5df0`; aliases or bulk copies remain possible. Constructor stores
at `0xa0b52c8` clear `+0x5de0..+0x5def`, and `0xa0b537c` clears
`+0x5e00..+0x5e1f`; neither range includes `+0x5df0`. The nearby
`+0x5df2` store at `0xa0be9e4` is a distinct byte. The
`iomfb_disable_async_swap` boot argument writes `+0x5868`, not this
selector.

The early read-only control armed a one-byte write watch at the
constructor (`0xa0b52c4`, `fb=0xffffffe4e02d0000`) while the byte
was zero; after debugger handoff it re-armed the same address. There
was **no write hit** through the first pending-node link (ID 1, byte
still zero). The longer independent child armed at its first pending
node (`fb=0xffffffe10a468000`, ID 1, byte zero), recorded no write
through the post-home non-null handoff, and matched the same fb at
`SUBMIT_GATE_2`: `+0x5870=0`, `+0x5df0=0`, pending ID 14, A407 build,
no A408 build, and paired End `x0=0`. Thus the two windows cover
construction-to-first-swap and first-swap-to-mapped-home-End under the
same D5825/CLASS16/D572+D582 setup. They do not show a setter missed
by timing on this natural path. This is **default-clear A407 selection
on the measured boot**, not evidence that A407 presents pixels or that
A408 is required. GATE154 previously reached A408 with active DART,
but its old logs are unavailable, so its selector value and writer
cannot be compared. The next unproved present edge for this A407 arm
is firmware ID lookup and transfer (`0x04213980`, conditional
`fb+0x3e30` to `+0x3e20` queue, then `+0x3e18` active); QEMU's
`TRANSFER_ASSUMED` is not a firmware observation. No selector poke,
model change, A408 completion, or scanout was used. Evidence:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_FB5DF0_WRITER1.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_FB5DF0_WRITER1.stderr.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_FB5DF0_EARLY1.early-kernel-lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_FB5DF0_EARLY1.lldb.log`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_FB5DF0_EARLY1.stderr.log`.

**Mapped-home A407 wire (`UI_CLASS16_REFRESH60_DM_A407_MAPPED_FULL2`).**
A new D5825 child kept CLASS16 60 Hz, D572 and D582 hypotheses, A408
completion off, and scanout off. The matched backboardd non-null handoff
(`x2=0x766cd203d0`) reached the same IOMFB object's
`_kern_SwapEnd`: node ID 11, `fb+0x5870=0`, `fb+0x5df0=0`, A407 build,
and End return `x0=0`. No paired A408 build occurred. The model's
complete RPC #574 A407 input is saved as
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_MAPPED_FULL2.input.bin`
(`0x1d30` bytes; SHA-256
`f432a24da2ce0b7d7f43915ff2af72de204c74c9f3d169cd6733d5e35cdab756`),
with a compact field readout in the neighboring `.input.json` file.
This corrected child used the dev6 `system.tc`; an earlier attempted
`MAPPED_FULL1` child used the wrong trust cache, failed AMFI before
slide resolution, and provides no swap evidence.

The matched request has `+0x98=11`, selected mask `+0x14c=1`,
per-layer words `+0x9c..+0xa8 = {1,0,0,0}`, `+0x17c=0`, and
`+0x34f=0`. Its null flags at `+0x1d23..+0x1d26` are **`00 01 01 01`**.
Slot 0's `+0x6e0` blob is serialized, beginning with an `ARGB`
descriptor (1179×2556, stride 4864, allocation `0xbdc000`); slots
1–3 retain `0xaa` poison blobs. The raw 64-bit word at `+0xf90` is
`0x10000000000`; its relation to the matched `dart-disp0` SID-0 map
has **not** been separately measured. This differs materially from
pre-D572 `A407_FULL1` (selected slots 0–2, zero per-layer words,
`01 01 01 01` null flags, four poison blobs). A407 can carry this
mapped surface on the wire. Firmware would no longer take FULL1's
null-surface shortcut: with an ID-11 lookup hit, its slot-0 surface
and geometry checks can affect `w25` (`0x042166a4..0x04216990`),
then display policy still decides `w21` at `0x04216d7c`. Neither is
executed or measured by this QEMU model; A407 status zero is not a
transfer or presentation verdict.

QEMU logged local object HIT, `TRANSFER_ASSUMED id=11 queued=1`, and
one labeled D582 nested send/reply for ID 11. The probe stopped at
the paired End return, so it did **not** observe an ID-11
`_kern_SwapWait` return or a live AP unlink after that End; the earlier
ID-1 D582 Wait-wake result remains a separate control. On the present
side, the current model calls `iomfb_scanout()` only for A408
(`darwin_iomfb.c`, `h.name==0x41343038`), and its surface parser
requires the A408 `0xff4` body. Setting the existing scanout knob
would not consume this A407 `0x1d30` request. The first **QEMU-local**
missing present edge is therefore an A407-specific, validated
surface/DVA-to-scanout path, after the explicitly assumed transfer;
the first **real-firmware** unresolved edge is slot-0 `w25`, followed
by `w21`. No A407 scanout or completion code was added, and no buffer
update or pixels were claimed. Evidence:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_MAPPED_FULL2.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_MAPPED_FULL2.stderr.log`,
`.serial.log`, and the saved `.input.bin/.input.json`.

**Opt-in A407 primary scanout experiment.** The captured `MAPPED_FULL2`
`0x1d30` request is now a durable unit-test fixture at
`qemu-sptm/tests/unit/fixtures/a407-mapped-full2-input.bin` (the SHA-256
above). `darwin_iomfb_a407_surface()` accepts only a nonzero ID, selected
mask `+0x14c=1`, a nonzero slot-0 word at `+0x9c` with the other three
words zero, flags `+0x1d23..+0x1d26={0,1,1,1}`, and the bounded
single-primary ARGB descriptor. It takes the candidate DVA from `+0xf90`
and rejects missing DVA, invalid geometry/stride, or an allocation smaller
than the pixel span. This is a parser for the **measured A407 profile**, not
an implementation of firmware surface resolution or `w25/w21`.

`DARWIN_DCP_IOMFB_A407_SCANOUT=1` is default-off and requires
`A406_OBJECTS=1`. After an A407 retained-ID HIT, QEMU tries the parsed
candidate through `dart-disp0` SID 0 and copies pixels into host-owned
storage before the optional same-slot D582 hypothesis can retire the map.
The existing `DARWIN_DCP_IOMFB_SCANOUT=1` remains A408-only. No A408
completion, selector poke, or fixture change is involved; a firmware
transfer remains unmeasured.

**A407 host-buffer update (`UI_CLASS16_REFRESH60_DM_A407_SCANOUT_HYP3`).**
A fresh D5825 child used the same CLASS16 60 Hz file and
`A406_OBJECTS=1`, `A407_TRANSFER=1`, `A407_COMPLETE=d582`, `D572=1`,
`COMPLETE=0`, `SCANOUT=0`, plus only `A407_SCANOUT=1`. It also kept the
baseline `DARWIN_DCP_IOMFB_ECHO_MODE=1`. Two earlier HYP1/HYP2 children
mistyped that variable as `...MODE_ECHO`; they lacked the A413→A442
mode-echo control and suffered SpringBoard critical-process crashes
before A407, so they contribute no scanout evidence.

HYP3 returned `DMPerformMigration x0=0` and reached the same-object
non-null handoff → `_kern_SwapSetLayer` → `_kern_SwapEnd` chain. The
pending node and A407 build were ID 12 with `fb+0x5870=0`,
`fb+0x5df0=0`; paired End returned `x0=0`. Model RPC #434 A407 looked
up ID 12 (QEMU-local HIT), marked `TRANSFER_ASSUMED`, then accepted the
single-primary parser: 1179×2556, stride 4864, 12,432,384 pixel bytes,
candidate DVA `0x10000000000`. All SID-0 page translations and reads
succeeded, and `darwin_fb_present_bgra()` returned success before the
same-slot **hypothetical** D582 send/reply. The model logged
`HYPOTHESIS A407 presented 1179x2556 BGRA id=12 ... via
dart-disp0/SID0`. Earlier null-layer IDs 1–4 were rejected by the parser
without DMA. The condition stop was at End return; same-ID `SwapWait`
return, HMP screendump, and visible pixel content were not measured.
This is a QEMU host-framebuffer update on the observed A407 arm, **not**
proof that real firmware passes `w25/w21` or sends D572/D582. Evidence:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_SCANOUT_HYP3.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_SCANOUT_HYP3.stderr.log`,
and `.probe.out` (condition stop, zero panics).

**A407 visibility check (`UI_CLASS16_REFRESH60_DM_A407_VISIBLE1`).** A fresh
D5825/CLASS16 child kept the HYP3 stack exactly, including
`DARWIN_DCP_IOMFB_ECHO_MODE=1`, `A406_OBJECTS=1`, `A407_TRANSFER=1`,
`A407_COMPLETE=d582`, `D572=1`, `A407_SCANOUT=1`, `COMPLETE=0`, and
`SCANOUT=0`. `DMPerformMigration` returned `x0=0`; there was no panic. The
first `HYPOTHESIS A407 presented` was ID 6 just after ADFL entry, before a
home marker. The guest was frozen at that log and HMP `screendump` produced
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_VISIBLE1.png` (SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`):
1179×2556, all 3,013,524 RGB pixels `(0,0,0)`. Thus this early frame was
black, despite successful host presentation.

After resuming the **same child**, the next 1179×2556 present was A407 ID 13;
the model logged retained-object HIT, `TRANSFER_ASSUMED`, successful
`dart-disp0`/SID-0 present, and then the labeled D582 send/reply for ID 13.
An HMP stop and dump immediately afterward produced
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_VISIBLE1.id13.png` (SHA-256
`a2c2646a5f567a5070d62a027d605929fd39c55c50f957f36e90278e8df1c451`).
The 1179×2556 image has 41 RGB colors and 6,436 pixels with a channel above
8 (0.214%); the rest is nearly all black. Visible shapes are a faint `9:09`
and status icons at the top, an information glyph near the lower right, and
the home indicator at the bottom. This is **real, sparse UI-like content in
the QEMU framebuffer**, not a full home screen or a firmware-fidelity claim.
It also rules out an all-zero DMA source for ID 13; the matched geometry
copied recognizable content to the host buffer. ID-13 `_kern_SwapWait` return
was not observed: LLDB had detached at the first condition stop, and the
model's D582 reply alone is not a paired guest Wait result. Evidence:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_VISIBLE1.lldb.log` and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_VISIBLE1.stderr.log`.

**Later-frame richness and paired Wait (`UI_CLASS16_REFRESH60_DM_A407_RICH_WAIT1`).**
One fresh D5825 child kept the VISIBLE1 CLASS16 60 Hz, D572, D582,
`A406_OBJECTS`, `A407_TRANSFER`, `A407_SCANOUT`, and `ECHO_MODE=1` settings;
A408 completion/scanout remained off. LLDB stayed attached through the
home-entity return. The read-only
`tools/re/dm_a407_visibility_watch.py` took live HMP dumps after six
post-home `HYPOTHESIS A407 presented` lines, then stopped the guest. All
are 1179×2556; the threshold below is any RGB channel greater than 8:

| A407 ID | HMP dump under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_RICH_WAIT1.frames/` | Pixels above threshold | Paired `_kern_SwapWait` |
|---|---|---:|---|
| 14 | `id14.png` | 0% (black) | fence 14, `x0=0` |
| 15 | `id15.png` | 0.227% | fence 15, `x0=0` |
| 16, 17 | `id16.png`, `id17.png` | 0.232% each (same image hash) | fences 16, 17, `x0=0` |
| 18 | `id18.png` | **0.711%** | fence 18, `x0=0` |
| 19 | `id19.png` | 0.231% | fence 19, `x0=0` |

Every fence 14–19 had the same backboardd object
`0x7d3ccc8000` at Wait entry/return. For each ID both option values
`0x80000001` and `0x80000000` returned `0` within about 4–10 ms;
the ID-18 first Wait took 4.36 ms and its second 9.16 ms. The frozen
endpoint followed one more A407 present (ID 20); its
`frozen-last.png` has 0.308% pixels above threshold, and the paired
ID-20 `SwapWait(20,0x80000001)` returned `0` in 16.23 ms. The model
logged local HIT/present and hypothetical D582 reply for those IDs.
The `.frames.jsonl` artifact records each dump's SHA-256 and pixel count;
notable hashes are ID 18
`8349719808e43b7400eb33ca28f0a5ec2abac52a53d38b8d5d037c2570b8c86d`
and frozen ID 20
`f42aa185221dd26d2e5ac6f1ac4d9af66aa762d5fd9a246ba70f7ed716fe18e3`.
The boot stopped on `a407-visibility-frames=6` with zero XNU panics.

The images show progression beyond VISIBLE1's status-bar-only frame:
ID 18 has a **Charging / 100%** banner, swipe-up prompt, information
glyph, and home indicator; ID 20 has time/status icons and a swipe-up
prompt. The screen remains predominantly black, with **no wallpaper or
home icons** in the captured series. This is recognizable lock UI, not a
full home screen. Each presented A407 passed
`darwin_iomfb_a407_surface()` (`darwin_iomfb_swap.c:219`), which accepts
only selected slot 0 (`+0x14c=1`, `+0x9c` nonzero, `+0xa0/+0xa4/+0xa8`
zero, flags `00/01/01/01`). The accepted primary buffer therefore
contains these sparse visible pixels; the host did not drop selected
slots 1–3 on these frames. SpringBoard hit 11 `CA_CONTEXT_SET_LAYER`
markers after home, but this probe did not pair a commit or establish
whether wallpaper/home layers were ever composed into that primary
surface. That upstream scene/composition question remains; no A408,
gate-bit, or DART change is implied. All visible output and Wait returns
remain **QEMU-hypothesis-enabled**, not proof of real-firmware D572/D582
identity or `w21` transfer. Evidence: the tag's `.lldb.log`,
`probe/*.stderr.log`, `.frames.jsonl`, `.visibility-watch.log`, and PNGs.

Smallest read-only discriminator **on a target executing this exact
firmware**: stop at A407 lookup callback `0x04215bcc` and its return
`0x04215c4c`, keyed to the same thread/call. On entry capture framebuffer
`x0`, list address `x1 == fb+0x3e30`, requested ID `w2`, output-slot
address `x3`, and list head `*[x1]`; on return capture `w0` and the
saved slot's pointer, then read that object's `+0x3d4/+0x62b` if nonnull.
For requested ID 0, null output plus miss status means the first edge
fails *at lookup*; a nonnull same-ID object means proceed to the state
write/activation question. To correlate a real A406 allocation with that
hit, optionally record `x0` framebuffer, `x2` object, generated `w8`
and output `*[x1]` at `0x04217178..0x0421717c`; this is a subsequent
identity check, not required to decide whether ID 0 lookup succeeded.
Only after a matched object is proven should the same pointer/ID be
followed through `0x04214338` (source `fb+0x3e30`, destination
`fb+0x3e20`), active stores `0x0420c944/0x04294ad8`, producer
`0x0420daec`, and batch append `0x0420de34`. AP-only QEMU hooks cannot
observe these firmware-internal edges; D595 was not armed in the prior
replay and its absence there was not measured.

One early version of the callback logged `sp+0x5c` on the *caller* frame as
`rpc_output_0c`; that field is not the A407 wrapper's output buffer and must
not be interpreted as an RPC reply. The code no longer records it.
Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_RETIRE1.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_A407_RETIRE1.stderr.log`, and
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_A407_RETIRE1.pending-list.json`.

### LOCK_RICH1: lock sources exist, but the A407 primary remains sparse

`UI_CLASS16_REFRESH60_DM_LOCK_RICH1` used the same D5825 parent, CLASS16 60 Hz,
`ECHO_MODE=1`, A406 objects/A407 transfer, labeled D582 and D572 hypotheses,
and A407 scanout as `A407_RICH_WAIT1`; A408 completion and ordinary scanout
remained off. `tools/re/dm_lock_rich_callbacks.py` added read-only CoverSheet,
PosterBoard, and CA source markers to the prior post-home A407/Wait bracket.
The visibility watcher stopped after eight post-home presents; the probe
reported zero XNU panics. The eight HMP dumps (IDs 12–19) are in
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_RICH1.frames/`, with counts and hashes
in the tag's `.frames.jsonl`. ID 13 is a Charging/100% lock frame with only
0.590% of pixels above channel threshold 8 (SHA-256
`2505083498445105c32aa62f77d4e46fe172dddb8460b1e086938a7f3746a205`);
ID 19 shows status time and swipe-up text at 0.380%. Neither contains a
wallpaper or prominent clock. The matched backboardd A407 builds and both
`_kern_SwapWait(13, …)` returns are in the `.lldb.log`; ID 13's returns were
`x0=0` after 3.73 and 7.15 ms. The model's A407 presented/HIT and labeled
D582 lines are in `probe/<tag>.stderr.log`. This confirms the sparse result
on the already established one-slot A407 host-present path, not a firmware
completion identity or a paired SpringBoard-to-A407 transaction.

On the same timeline, SpringBoard initialized `CSProminentTimeView` at
`0x1b38d21ac`, updated its time string at `0x1b38bd310`, called
`CSCoverSheetViewController _updateDateTimeView` at `0x223e8b60c`, and made
post-home CA commit entries at `0x1843ff180` before ID 13. A read-only paused-memory
walk of the validated SpringBoard pmap root `0x1001c736000` found the retained
controller `0x78d1197000` → display `0x78d00a8c80` → `+0x1c8` time view
`0x78d00a8f00`. The time view's inherited `_textLabel` at `+0x1f0` is
`0x78d02b4e00`, whose ISA matches this build's `_UIAnimatingLabel` class
(`0x1e82e52d0` plus slide `0x1ac000`). Its `_wantsGlassMaterial` byte at
`+0x209` is 1; the observed renderer object is a
`CSGlassContentStyleRenderer`. Therefore the prominent time view and a native
label **were created and retained**. The first unclosed clock edge is
styling/rasterization of that content into CA, not construction or A407
selection. This matches the *shape* of the separate warm-boot native-label +
glass negative control in `lockscreen-poster-time.md`; it does not prove the
same exact renderer failure on this CLASS16 frame. The callback's LR return
sample for the glass-capability leaf reported `x0=0` despite the static
`0x1b38d67f0` body being `mov w0,#1; ret`, so that return value is not used
as a capability verdict.

Wallpaper remains a distinct fork. PosterBoard called
`PBUIWallpaperConfigurationManager wallpaperImageForVariant:wallpaperMode:`
at `0x201ee8d84` for variants 0 and 1; all four observed returns were nil.
This excludes a loaded **static image from those calls**, not a dynamic poster.
SpringBoard then called `CSCoverSheetViewController _updateWallpaper` at
`0x223e921d8` and `PBUIPosterLockViewController _updatePosterLayers` at
`0x201edef20` before the rich frame. The paused controller
`0x78d2740c00` retained a background host at `+0x520` = `0x78c6eabb80`;
its `_scene` at `+0x1b8` was `0x78c6dc4d00`, context ID at `+0x1a8`
`0xb25ca86a`. Foreground/floating host scenes were nil. Thus a background
scene **was attached**, but whether it produced non-black CA layers or those
layers were omitted/cleared before the A407 primary remains unmeasured.
Classification: absent view/host (A) is excluded for both time and wallpaper;
time-disabled policy (D) is excluded for the retained clock; wallpaper B vs C
and the clock's exact CA rendering edge remain open. The smallest next hook is
the background host's `setScene:` at `0x201ee0ee8` plus CA set-layer/commit
filtered to context `0xb25ca86a`, then its first server render update before
the matched A407 ID. That distinguishes a blank source context from lost
compositing without changing CLASS16, scanout, or guest state.

The paused object read followed a brief incidental continuation when a
read-only GDB attach detached; HMP re-paused the guest before sparse reads.
It is a **post-frame retained-state sample**, not an atomic snapshot at ID 13.
Artifacts: the tag's `.lldb.log`, `.visibility-watch.log`, `.frames.jsonl`,
`lock-state-final.json`, and `frames/id13.png` / `frames/id19.png`.

### LOCK_CA1: poster CA root reaches an attached scene; pixel contribution remains unmeasured

`UI_CLASS16_REFRESH60_DM_LOCK_CA1` kept the LOCK_RICH1 D5825 parent,
CLASS16 60 Hz, `ECHO_MODE=1`, A406 objects/A407 transfer, labeled D582 and
D572 hypotheses, and A407 scanout. A408 completion and ordinary scanout stayed
off. `tools/re/dm_lock_rich_callbacks.py` added read-only `setScene:`, CA
set-layer/commit, server render-update, and time-layer observations. The first
post-home watcher bound froze the guest immediately before the later render
sequence; the same paused child was reattached and resumed with the already
observed context ID seeded into the observer. No guest field was changed.
Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CA1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CA1.resume.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CA1.stderr.log`, and the tag's
`.frames.jsonl` / `frames/` directory.

On this boot, `MercuryPosterExtension` called `-[CAContext setLayer:]`
(`0x18451a138`) at epoch `1791094867.882` with non-null root layer
`0x7418d2dd80` for context **`0xb641f540`**. SpringBoard's
`PBUIPosterSceneLayerHostView setScene:` (`0x201ee0ee8`) entered at
`1791094870.460` with a non-null scene; the host retained the same context
ID before and after the call and the same scene after return. Its next
`_updatePosterLayers` sample confirmed the background host/scene/context.
This is an ID-matched extension-root-to-SpringBoard-host join, so the
background scene is more than a retained host. It does **not** establish
that the root drew non-black pixels or that backboardd used it in the
matched render update. The previous LOCK_RICH1 context `0xb25ca86a`
was from a different boot and was not reused as an identity assumption.

The same child's continuation recorded backboardd
`CA::Render::Server::render_update` (`0x184776174`) entry/return on the
handoff thread, followed by a non-null swap handoff and A407 build for ID 12
(`1791095364.875`). Subsequent same-object A407 builds and labeled model
presents continued; `_kern_SwapWait(14, 0x80000001)` returned `x0=0` in
4.2 ms. The ID-14 HMP frame is still Charging/status/swipe chrome on black:
0.762% of its 1179×2556 pixels have a channel above 8; SHA-256
`fa41e65bbb96662d1bd9c0dc32b352633fbcdbc920ca757945f29080268e3e5f`.
Later captured frames remained sparse (63 captures, IDs 12–74; maximum
0.762%). This rules out a transient late rich frame in that bound, not a
later eventual render. The A407 output is QEMU-hypothesis-enabled; its
`HIT`/assumed transfer and D582 do not prove real firmware `w21` or callback
identity.

A later `CSProminentTimeView` update (`0x1b38bd310`, epoch
`1791095412.335`) found both the time-view and native-label CA layers
not hidden; the label's `CA::Layer` contents pointer was non-null
(`0x788482b600`), while the parent time layer's contents was nil. Those
fields are a source-layer sample, not a raster/color sample: a text layer
may draw by delegate or style, and a non-null contents object may still
be transparent. CA transaction entries at `0x1843ff180` are global
(`retain_all_contexts` at `0x1843ff230`); their `x0` is not a poster
context ID, so the earlier trace's `context_id_candidate=0` must not be
interpreted as a per-context commit. The context-filtered
`CA::Render::Updater::update_handle` probe (`0x18444979c`, testing
`x2+0x10`) did not log the poster ID; that negative is coverage only
because cached/other render paths were not excluded.

**Classification ceiling:** neither blank source, lost-before-A407, nor
cleared is proven. The first missing measurement is whether backboardd
resolves/renders context `0xb641f540` with non-black output during the
render update that precedes one ID-keyed A407. The smallest next read-only
bracket is server context-ID resolution/attachment plus the context's
pre-composite surface or draw bounds, followed by the already armed
render-update/A407 ID. In parallel, sample the native time label's
rendered texture/bounds at that same update; its non-null `contents`
pointer alone does not classify the absent large clock. A non-black
context output missing from A407 would establish loss; black/empty output
would establish blank source; a non-black intermediate followed by a
black overwrite would establish clear. No wallpaper/pixel synthesis or
display-model change follows from the present evidence.

### LOCK_CTX1: poster accepted in an offscreen update, not yet joined to the display update (2026-10-04)

`UI_CLASS16_REFRESH60_DM_LOCK_CTX1` was one continuous child of the D5825
parent with the same CLASS16/ECHO_MODE, A406 objects, assumed A407 transfer,
labeled D582/D572, and A407-scanout settings as LOCK_CA1; A408 completion and
ordinary scanout remained off. `tools/re/dm_lock_rich_callbacks.py` bracketed
`CA::Render::Update::add_context` (`0x18443ffb8`) and its inclusion checks in
backboardd, in addition to the earlier scene, render-update, A407, and Wait
markers. The guest ran through three ID-matched post-home frames without a
freeze/resume cycle. Artifacts:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CTX1.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CTX1.stderr.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CTX1.frames.jsonl`, and
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CTX1.frames/id14.png`.

This boot's poster ID is **`0x6462aa96`** (`1684187798`), not LOCK_CA1's
`0xb641f540`: MercuryPosterExtension installed a non-null CA root at epoch
`1791097861.850`, and SpringBoard's `setScene:` retained that ID at
`1791097865.638`. At `1791097910.648` backboardd passed the same context
and root layer to `add_context`; both `allowed_to_include_context` and
`allowed_in_update` returned 1, and `add_included_context` ran. It did so
again at `1791097937.645`. The root size bytes at `+0x58` decode to
**589.5×1278 logical units**. The sampled root bytes at `+0x10` are zero;
they are not a texture or pixel sample and cannot establish a black poster.

The caller is decisive: the matched `add_context` LR is `0x1845a60d8`
before the shared-cache slide, immediately after the call at `0x1845a60d4`
inside `Server::render_display_context_list_to_target` (`0x1845a5cb4`).
That routine builds its own stack `Update` at `sp+0x230`. The two observed
poster inclusion updates were `0x108501ec0` and `0x108501e10`, whereas
the later `Server::render_update` entries before A407 IDs 12–14 used
**`0x108cffe48`**. Thus the positive inclusion checks are for an **offscreen
context-list request**; they do not prove the poster participated in the
display update. No poster-ID hit at `add_context` had the natural
`Server::render_for_time` callsite (`0x1844131f8`, or its no-transform
wrapper at `0x184410b50`) in the captured window. This is a measured
display-update attribution gap, not proof that the poster source is blank.

The display update at `0x184776174` returned before A407 IDs 12–14;
ID 14's `_kern_SwapWait` returned `x0=0` in 4.12 ms. Its 1179×2556
HMP image has 10,300 pixels (0.342%) with any channel above 8 and shows
status/swipe chrome on black, without wallpaper or large time; SHA-256
`b3f4d02b32943e3b8f8b8ce382989fc5bfd3ce70f78486fe3e1d09dde6cc4f5b`.
The three captured frames (IDs 12–14) remain sparse. The native time-label
layer was not hidden and its `contents` pointer became non-null before those
frames, but this run has no time-label `Render::Layer` identity or rendered
texture/bounds sample in backboardd.

**Next exact read-only cut:** on the same context ID, bracket
`Server::render_for_time`'s `add_context` calls at `0x184410b50` and
`0x1844131f8`, record Context ID, root and `Update*`, and compare that
`Update*` with `render_update`'s x1 for the following A407 ID. For poster
output, sample the root's prepared-layer result/draw bounds on that matched
display `Update` (`prepare_layer`'s zero-RGB decision `0x184433594`,
`add_context`'s prepared-result test `0x184440e60`), then pair the same
`Update*` with `Display::render_display` (`0x1844510f0`) and
`OGL::Renderer::render` (`0x18444b954`) before A407. For the large time, first map the
SpringBoard label layer to its backboardd Render::Layer/Context identity,
then sample its prepared bounds/texture in the same display update. Until
those samples exist, blank source versus lost-before-A407 versus cleared
remains unresolved. All presents and Wait returns remain QEMU-hypothesis-
enabled observations, not D572/D582 firmware identity or measured `w21`.

### LOCK_DISPLAY1: natural display bracket armed; poster join not exercised (2026-10-04)

`UI_CLASS16_REFRESH60_DM_LOCK_DISPLAY1` used the same D5825 parent,
CLASS16/ECHO_MODE and labeled D572/D582/A407-scanout settings as LOCK_CTX1,
with A408 completion/scanout off. `dm_lock_rich_callbacks.py` additionally
bracketed the direct `Server::render_for_time` add-context sites
`0x184410b50` / `0x1844131f8`, nested LayerHost inclusion in
`Updater::prepare_layer0`, prepared-result test `0x184440e60`, and display
`render_display` / OGL render. All 108 initial sites armed. The first
continuous bound reached `DMPerformMigration` return, ADFL and home, but
the A407-visibility watcher ended with zero captured frames. The guest was
kept frozen, then the same child was resumed under the same read-only hooks
for a 650-second post-home continuation; no fixture or model setting changed.
The continuation captured one A407 host present, ID 6, whose 1179×2556
HMP dump is uniformly black (zero pixels with a channel above 8, SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).
Later A407 IDs 7–10 were QEMU-local lookup HITs but did not match the
single-primary BGRA scanout profile. No matched post-present Wait return
was captured in the continuation. The boot was released after the bound.

Backboardd did execute `Server::render_update` with `Update*`
`0x107cffe48` and `0x107cfc990`; `Display::render_display`
(`0x1844510f0`) and `OGL::Renderer::render` (`0x18444b954`) received
those same respective pointers. This is a positive control for the
display-update hooks, not a poster contribution or an ID-6 transaction
pairing. Neither armed interval recorded a MercuryPosterExtension
`CAContext setLayer:` root or SpringBoard poster `setScene:`, so this boot
has **no measured poster context ID**. All direct/hosted context hooks
filter against per-boot poster IDs; their silence cannot classify this
boot's poster as absent from the display update. LOCK_CTX1's positive
offscreen context `0x6462aa96` belongs to a different child and must not
be substituted here. The native time label did update: the SpringBoard
label CALayer was non-hidden and had non-null `contents` by epoch
`1791101227.240`, but no server `Render::Layer` identity or prepared
texture/bounds was paired to either display `Update*`. Blank source,
loss-before-A407, and clear remain unresolved.

The next **first gate** is more precise than the direct `render_for_time`
callsite alone. In `Updater::prepare_layer0`, the embedded LayerHost at
`Render::Layer+0x210` supplies desired context ID at host `+0x1c` to
`LayerHost::retain_hosted_context` (call `0x18441ec84`, result
`0x18441ec88`). A non-null result can enter
`Update::add_included_context` at `0x18441ec9c` before the secure and
inclusion checks at `0x18441eccc` / `0x18441ecdc`; the accepted/rejected
branches are `0x18441ece4` / `0x18441ed14`. The read-only probe now logs
that host ID, returned Context ID and `Update*` even before a client poster
ID is known. On a boot with a positive `setScene:` ID, compare the returned
Context and `Update*` to the following display `render_update` x1 and A407;
only then sample `0x184440e60` prepared result and time-label server layer.
This is a probe plan and static path, not a measured exclusion on
LOCK_DISPLAY1.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_DISPLAY1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_DISPLAY1.followup.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_DISPLAY1.stderr.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_DISPLAY1_FOLLOW1.frames.jsonl`,
and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_DISPLAY1_FOLLOW1.frames/id6.png`.
All host presents remain QEMU-hypothesis-enabled; this run does not identify
real-firmware D572/D582 behavior or `w21`.

### LOCK_JOIN2: same-boot poster enters the display Update; pixels still unclassified (2026-10-04)

`UI_CLASS16_REFRESH60_DM_LOCK_JOIN2` used a fresh D5825 child and the same
CLASS16 60 Hz/ECHO_MODE, A406-objects, assumed A407 transfer, labeled
D582/D572, and A407-scanout stack as LOCK_CTX1. A408 completion and ordinary
scanout remained off. The 110 initial LLDB sites armed; a separate bounded
watcher (`tools/re/dm_lock_poster_join_watch.py`) waited for **both**
MercuryPosterExtension `CAContext setLayer:` and SpringBoard `setScene:`
before assigning a poster ID. The child ran continuously through
`DMPerformMigration` and home; there was no freeze between home and poster
installation. Artifacts are
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_JOIN2.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_JOIN2.stderr.log`, and
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_JOIN2.poster-join.json`.

This boot's Mercury root `0x7b18d41ec0` was installed in context
**`0x35fc28af`** at epoch `1791104318.303`; SpringBoard's `setScene:`
returned with that same context ID and a non-null scene at
`1791104321.581`. Mercury also installed another root/context
`0x3982ddc5`, which is not substituted for the SpringBoard scene ID.
Backboardd then passed Context `0x797ed82a00` (ID `0x35fc28af`)
through the nested LayerHost path: `add_included_context` at
`0x18441ec9c` on **`Update* 0x1058ffe48`** at `1791104322.036`,
`allowed_in_secure_update` returned 1 at `0x18441eccc`,
`allowed_to_include_context` returned 1 at `0x18441ecdc`, and the
accept arm `0x18441ece4` ran at `1791104322.046`. The same ID was
included/accepted again through `1791104337.259`; no matched reject was
logged. This is a positive **display-join** result for the poster, unlike
LOCK_CTX1's offscreen-only `Update*`.

The later display `Server::render_update` received x1
`0x1058ffe48`, and `Display::render_display` / `OGL::Renderer::render`
received that same `Update*` (for example `1791104366.886` /
`1791104366.889` / `1791104366.893`, with render-update return at
`1791104368.476`).
The non-null framebuffer handoff followed at `1791104368.504`; the matched
A407 build carried ID 13 at `1791104368.559`. Thus accepted poster context
and the render/present path share the display `Update*` before this A407.
`Update*` identity and acceptance do **not** prove the poster generated
opaque/non-black draw commands in that render pass, or that the A407 slot-0
buffer contains them. The ID-13 HMP dump is uniformly black (0 pixels with
any channel above 8; SHA-256
`8fe76bea023fd530242b26cce284fe6f00277c7ec0d6d1f386d8c188f661dfdf`)
at `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_JOIN2.poster-id13.png`.
The held later frame shows sparse time/status/swipe chrome on black:
9,676/3,013,524 pixels (0.321%) above that threshold, SHA-256
`93c13009a858856655d9ad06ad3756082d454bf54191cd4bef64c9014522a372`
at `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_JOIN2.frozen.png`.
Neither image gives a pre-composite poster-pixel sample, so **blank source
versus loss/clear after acceptance remains open**. The earlier
“poster absent from display update” fork is closed for this boot.

The prominent time label again had a non-hidden client CALayer with
non-null `contents` by `1791104329.565`, but no matching backboardd
`Render::Layer` or texture/bounds was identified. The first precise
missing measurement is inside `Updater::prepare_layer0` after the accepted
hosted branch `0x18441ece4`: x21 is its `Render::Layer`, x19 its stack
`LocalState0`, and x23 the same `Update*`; sample prepared bounds/contents
there and follow that layer's draw/texture result into OGL before an
ID-matched A407. For the clock, correlate the client CALayer through
`-[CALayer _copyRenderLayer:layerFlags:commitFlags:]` at `0x1844c0268`
(source x0, new Render::Layer x21 after `0x1844c02e0`) to the server
layer's prepared result on that `Update*`. Client `contents` alone remains
insufficient. `dm_lock_rich_callbacks.py` now preserves the static hosted
return breakpoint and reads the resolved Context from the LayerHost's first
word; that correction was made **after** the initial LLDB install, so this
run's decisive evidence is the independent include/allow/accept hits, not
the early return sample. A same-child post-freeze reattach produced no
usable callback hits; it adds no content classification.

D572, D582, and A407 scanout remain labeled QEMU hypotheses. This run
neither identifies their real-firmware callback contracts nor proves
wallpaper or large-clock pixels reached the display.

### LOCK_DRAW2: Mercury root without SpringBoard host attachment (2026-10-04)

`UI_CLASS16_REFRESH60_DM_LOCK_DRAW2` was one continuous fresh D5825 child
with the unchanged CLASS16/ECHO and labeled D572/D582/A407-scanout stack.
`dm_lock_rich_callbacks.py` armed the corrected hosted resolve, the
post-accept `prepare_layer0` state, and OGL layer-node identity sites.
`DMPerformMigration` returned `x0=0`, home completed, and
MercuryPosterExtension installed a non-null CA root `0x7869129d00` for this
boot's context **`0xbc7d5c3f`** at `1791136624.072`. SpringBoard continued
CA commits and prominent time-layer copies, but there was **no**
`PBUIPosterLockViewController _updatePosterLayers` (`0x201edef20`) or
`PBUIPosterSceneLayerHostView setScene:` (`0x201ee0ee8`) before the
post-home watcher bound. Its result was
`poster-join-no-positive-context-posthome-bound`; no scene ID was assigned
and no accepted poster `LayerNode` could be scored. This is a host-attachment
**coverage miss**, not a reversal of LOCK_JOIN2's positive display join or
evidence of blank prepared pixels. The only logged host present before that
bound was A407 ID 6, which preceded a joined poster context.

The time label's CALayer did enter `_copyRenderLayer:` (`0x1844c0268`): the
client render layer was allocated at `0x1844c02e0`, and the source-contents
call returned non-null `501402995712` at `0x1844c061c`. The enclosing time
layer's source-contents result was null. This advances the time path through
client render-layer construction, but neither the cross-process server layer
identity nor a prepared texture/draw is established. The static
`Updater::prepare_layer0` post-accept sites now bracket `0x18441f658`,
`0x18441fdf8`, `0x18441fe80`, hidden cull `0x18441feac`, and
`0x18441ff28`; OGL's layer stores its `Render::LayerNode*` at `+0x10`
(`0x184466304`), permitting a keyed OGL prepare/render test on a positive
scene boot. `0x184440e60` is a different add-context path and its silence
cannot classify the nested poster layer.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_DRAW2.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_DRAW2.stderr.log`, and
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_DRAW2.poster-join.json`. The child
was released with HMP `quit` after the bounded freeze. No DCP/model/fixture
or guest state was changed.

### LOCK_DRAW4: corrected probe, bounded poster-attachment miss (2026-10-04)

`UI_CLASS16_REFRESH60_DM_LOCK_DRAW4` was a fresh, continuous D5825/CLASS16
child with the unchanged ECHO, objects/transfer, D582, D572, and A407-scanout
hypothesis stack (A408 completion and generic scanout off). DRAW3 had stopped
at an early breakpoint because its thin LLDB module imported
`dm_lock_rich_callbacks.py` only as Python, leaving the underlying scripted
breakpoint callback unresolved. `dm_lock_draw_callbacks.py` now also registers
that module with LLDB's `command script import` before installing its 27
selected rich sites. DRAW3's detached/reattached interval is not a content
sample; DRAW4 armed the corrected callbacks from boot and recorded live
SpringBoard/BKS hits, so this control error did not affect DRAW4.

DRAW4 reached `DMPerformMigration` return (`1791141471.571`) and home
completion. Backboardd's hosted-resolve call/return at `0x18441ec84/88`
fired for eight unrelated hosts on a display `Update*`, validating the
server-side probe. The time label's client CALayer entered
`_copyRenderLayer:` at `0x1844c0268`; its label source-contents result at
`0x1844c061c` was non-null (`526520410112`), while the enclosing time
layer's was null. This is client serialization, not a server texture or draw.
The model also logged later A407 presents, but no same-boot
MercuryPosterExtension non-null `setLayer:` root, SpringBoard
`PBUIPosterSceneLayerHostView setScene:`, or `_updatePosterLayers` appeared
before the **1,000-second post-home** watcher bound. The watcher recorded
`poster-join-no-positive-context-posthome-bound` with empty Mercury and scene
ID sets. There is therefore no poster `Render::LayerNode*` to follow into
OGL and no attributable A407/HMP frame. This is an attachment **coverage
miss**, not evidence for blank, prepared-not-drawn, lost, or cleared pixels;
JOIN2's positive display join remains valid for its own boot.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_DRAW4.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_DRAW4.stderr.log`, and
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_DRAW4.poster-join.json`. The
condition-frozen child was released with HMP `quit`. Next scoreable evidence
still requires one boot's Mercury root and SpringBoard scene ID to match,
followed by accept → prepared LayerNode → node-keyed OGL draw on the same
display `Update*` → A407. The client time-layer copy must be joined to its
backboardd layer separately. No model, fixture, or guest state was changed.

### LOCK_ATTACH1: scene-null attachment gate on a continuous child (2026-10-04)

`UI_CLASS16_REFRESH60_DM_LOCK_ATTACH1` kept the D5825 parent, CLASS16/ECHO,
and labeled D572/D582/A407-scanout hypothesis stack fixed. The read-only
`tools/re/dm_lock_attach_callbacks.py` imported the corrected DRAW4 probe
from boot and added SpringBoard poster-controller and scene-update sites.
`DMPerformMigration` returned (`1791144897.800`), ADFL/home completed, and
backboardd's hosted-resolve call/return fired for eight unrelated hosts.
There was no panic. The continuous watcher ended at the same 1,000-second
post-home bound as DRAW4 with empty Mercury-root and SpringBoard scene-ID
sets (`poster-join-no-positive-context-posthome-bound`). It did not capture
an attributable A407/HMP frame. JOIN2's accepted display `Update*` remains
valid for JOIN2 only.

This run identifies an earlier, **observed attachment skip** rather than a
draw result. One SpringBoard `PBUIPosterViewController` (receiver
`0x7b2cd79200`, decimal `529033302528`) entered
`_updatePosterScenesForReasons:updater:completion:` at `0x201ed190c`
19 times before home. Every visit loaded `_scene == 0` at
`0x201ed1960`, took `cbz` at `0x201ed1964` to `0x201ed1a0c`, and
skipped the scene-present arm at `0x201ed1968`. The same receiver got
`setActivelyRequired:` with requested values 1 then 0 at `0x201ed15a0`;
the intervening scene updates still took the null branch. No
`PBUIPosterLockViewController _updatePosterLayers` (`0x201edef20`),
`PBUIPosterSceneLayerHostView setScene:` (`0x201ee0ee8`), or Mercury
non-null `CAContext setLayer:` was observed in the post-home bound. The
19 null decisions are direct branch evidence; the later hook silence alone
does not prove that MercuryPosterExtension never launched or that no scene
could arrive after the cutoff. It also says nothing about blank, lost,
cleared, or prepared-but-not-drawn pixels.

The current PaperBoardUI binary supplies a narrow next input bracket.
`PBUIPosterViewController updateConfiguration:`'s block starts at
`0x201f37e68`; `0x201f37e98` tests whether its controller lookup is
non-null. Its rendering-role scene lookup returns to `0x201f37ed8`
(`x20`), and `0x201f37fdc..0x201f37fe4` passes `x22` (the `_scene`
field address) and `x20` to a strong-store helper. The `_scene` ivar
offset symbol is at `0x26c1242ec`. `dm_lock_attach_callbacks.py` now
contains these **next-run** sites; they were added after LOCK_ATTACH1
froze and are not claimed as live hits here. A fresh read-only child can
distinguish no configuration block, nil controller, nil scene lookup, and
scene stored but later cleared. An extension-process launch marker remains
needed to distinguish extension absence from extension alive without a root.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_ATTACH1.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_ATTACH1.stderr.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_ATTACH1.poster-join.json`, and
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_ATTACH1.probe.out`. The frozen child
was released with HMP `quit`. There was no guest patch, model edit, parent
repair, or fixture change. D572, D582, and A407 scanout remain QEMU
hypotheses.

#### PLUGIN209 — shell/migration activity with protocol-1 input helper; no gesture score

P209 used the same durable P182 disk parent, the V12 trust cache, and the
explicit V12 ramdisk. It reached DataMigrator/SystemApp activity and the
SpringBoard shell-service and BKS check-in calls; the observed SystemApp
`CLIENT_SYNC_RETURN` occurred, but no BKS/check-in or shell-service return was
captured before this bounded setup stop. The primary display mode trace again
found the ID-1 `ColorElements` query returning `0xe00002bc` and power-mode
lookup `-1`; the separate ID-6 display accepted mode 0. No accepted lock
dismissal, Home entity, post-transition render, or scanout verdict was
recorded.

Although QEMU was given the V12 ramdisk path, the guest launched
`DVM_INPUT_START version=6` and reached `DVM_INPUT_READY protocol=1`, not the
DVMI2 V12 helper. The host sent 41 readiness pings and got no ACKs; it sent no
touch/key event, and `dispatched=0`. P209 is therefore an input-helper/setup
miss, not evidence about the gesture or unlock path. The child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin209-p182-v12-home-display-join3.qcow2`
(SHA-256 `831ab24001cfbda2cb79366142c5469a9f96d15a0dd87bbb76f4b4ec24a46068`)
passed `qemu-img check -U`; records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN209_P182_V12_HOME_DISPLAY_JOIN3/`.
The next input attempt must start from a disk state that demonstrably launches
the V12 helper (as P206 did) and require `DVMI2R R` before input. No model,
fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582
remain hypotheses.

### LOCK_CONFIG3: `_scene` resolved and stored; display draw still unscored (2026-10-04)

`UI_CLASS16_REFRESH60_DM_LOCK_CONFIG3` was a continuous child of the repaired
D5825 parent with the same CLASS16/ECHO/D572/D582/A407-scanout hypotheses.
The corrected `dm_lock_attach_callbacks.py` sites were armed from boot, and
the hidden-cull breakpoints were excluded after importing the rich callback
module. This closes the earlier CONFIG1/2 coverage loss at the unrelated
hidden-cull site.

The `updateConfiguration:` writer ran: block `0x201f37e68`, non-null
controller at `0x201f37e98`, scene candidate `0x794222bc00` (decimal
`520800615424`) in `x20` after lookup at `0x201f37edc`, then a strong-store
call at `0x201f37fdc` with `scene_before=0` and `_scene` field address
`0x7940dbd618` (decimal `520779191832`) in `x22`. The same controller later took
the `_updatePosterScenes` non-null/present arm at `0x201ed1968` six times;
all 19 null-arm hits at `0x201ed1a0c` preceded the store. This is outcome E:
`_scene` was populated, not a remaining nil-controller/scene/store failure.

Mercury launch and scene attachment were also positively observed on this
boot. MercuryPosterExtension supplied a non-null root for context
`0xe6e42442` (`3873711170`), and SpringBoard `setScene:` returned with that
same context ID and a non-null scene. The continuous watcher then captured
A407 ID 12, but its PNG was uniformly black (0.0% non-black). This does not
classify poster preparation or draw: the server-side poster accept/prepared
chain was not recorded for that ID.

Review found why CONFIG3 could not score that next edge. The hosted-resolve
filter compared the LayerHost's local `+0x1c` value (118) with the poster
CAContext ID before the resolver returned the actual Context*. That dropped
all resolver rows after the first eight unrelated controls, including a
potential poster match. `dm_lock_rich_callbacks.py` now keeps those later
calls paired and filters the return using the resolved context ID. The
poster watcher can now require `SERVER_HOSTED_POSTER_ACCEPT` for this boot's
joined ID before capturing a subsequent A407.

The follow-up child `UI_CLASS16_REFRESH60_DM_LOCK_CONFIG5` used the same
parent, patched bootkc/device-tree/trust-cache bundle, and hypothesis stack,
but did not reach the post-home window. At ten minutes its matched
`BKS_MIGRATION_CHECKIN_CALL` at `0x19a6a36a0` still had no return at
`0x19a6a36a4`; there were no panics and no poster/config events. It was
stopped as a pre-home no-progress bound, so this is not an attachment or draw
verdict. Its poster watcher reported detachment after the manual stop, which
must not be interpreted as a context miss.

Artifacts: CONFIG3 `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG3.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG3.poster-join.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG3.poster-id12.png`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG3.stderr.log`; CONFIG5
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG5.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG5.probe.out`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG5.serial.log`. The
remaining display question is same-boot resolution/accept → LayerNode/OGL →
A407; D572, D582, and A407 scanout remain hypotheses.

### LOCK_CONFIG6: corrected resolver run stopped before home (2026-10-04)

`UI_CLASS16_REFRESH60_DM_LOCK_CONFIG6` was a fresh continuous child of the
same D5825 parent, with the exact CONFIG3 boot bundle and CLASS16/ECHO,
D572/D582/A407-scanout hypothesis settings. The revised resolver callback
and `--require-display-accept` watcher were armed from boot. This child
entered the matched SpringBoard migration check-in at `0x19a6a36a0` but had
not returned at `0x19a6a36a4` after a 600-second bound. `DMPerformMigration`
did not return, home was not reached, and no poster config writer, scene,
Mercury root, or hosted display accept was observed. There were no XNU
panics. The run was frozen at the bound and QEMU was released; the watcher's
`poster-join-coverage-lost-lldb-detached` is a consequence of that stop, not
a poster-attachment result.

This repeats CONFIG5's pre-home coverage loss. It does not reopen or
contradict CONFIG3: on CONFIG3 the controller and scene lookup were
non-null, `_scene` was strongly stored, Mercury attached the same context,
and `_updatePosterScenes` later took its present arm. The first nil in the
CONFIG3 population chain is therefore closed for that boot. The corrected
post-join resolver/accept path remains unscored because CONFIG3 used the
earlier resolver filter and CONFIG5/6 never reached home. A successful
post-home child with the corrected filter is still required before drawing
conclusions about poster prepare/OGL or its A407 frame.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG6.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG6.poster-join.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG6.stop`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG6.probe.out`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG6.serial.log` /
`stderr.log`. No model, parent, fixture, or guest state was changed.

### LOCK_CHECKIN2: migration check-in stops at queued BKS continuation (2026-10-04)

One fresh D5825/CLASS16 child used the same ECHO, D572, D582, and A407
scanout hypotheses as CONFIG3–7; A408 completion and scanout stayed off.
The composed read-only probe armed the BKS receive/handoff bracket, the
DataMigrator entry-triggered ladder, the corrected poster callbacks, and
hidden-cull exclusion from boot. ID-1 `SwapWait(1,1)` returned `x0=0` in
4.9 ms. SpringBoard entered its migration check-in at `0x19acc76a0`; there
was no return at `0x19acc76a4` by the stop.

Backboardd did receive and configure the connection. The migration listener
and `configureConnection` returned in ~33 ms and ~17 ms. The server then
took the child-activation immediate path: it enqueued child block
`0x105586268`, performed the handoff reply, and pushed continuation
`0x74f2dd12c0` onto lane `0x74f31a6c80`. The live queue was
`0x103a01280 → 0x103a012c0 → 0x74f2dd12c0`; the first two items were already
ahead of the migration continuation. That order remained unchanged at
+3.37 s, +12.57 s, and +54.01 s after handoff. During the same bound, no
matched lane wake/invoke/serial-drain or `SERVER_BS_CHILD_BLOCK_INVOKE` hit,
and neither the concrete migration check-in handler nor the SpringBoard
client return fired. This names the first open edge as **servicing the queued
child continuation on a lane with two older items ahead**. It does not yet
explain why the older items are not drained.

The backboardd `DMPerformMigration` entry fired on thread pointer
`0xffffffdfa8733830` and reached environment/device-mode returns. The BKS
server handoff used thread pointer `0xffffffdfa8735120`, so this DM entry is
not proven to serve that exact check-in; its entry-triggered ladder did not
observe `DM_PERFORM_RETURN`. A separate XPC-return line was not paired to
that entry and is not counted. The child stopped before
home, `_scene`, Mercury attachment, display accept, or the corrected
accept→prepare/OGL→A407 half; those remain unscored, not failures. No XNU
panic was reported. The probe reported a frozen guest, but its HMP socket
refused a connection after the driver exited; the qcow2 child and logs remain
available, while a live paused guest is not confirmed.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN2.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN2.stop`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN2.slide.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN2.probe.out`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN2.serial.log` /
`stderr.log`. The first `LOCK_CHECKIN1` attempt was an unscored LLDB callback
registration failure during early boot. D572, D582, and A407 scanout remain
QEMU hypotheses; no model, fixture, parent, or guest state was changed.

### LOCK_CONFIG7: another pre-home check-in bound (2026-10-04)

`UI_CLASS16_REFRESH60_DM_LOCK_CONFIG7` repeated the CONFIG6 setup on a fresh
child of the same D5825 parent, with `dm_lock_attach_callbacks` armed from
boot, the corrected resolver filter, and the watcher requiring this boot's
display accept. The matched SpringBoard migration check-in entered at
`0x19f6c76a0` but did not return at `0x19f6c76a4` within 600 seconds. There
was no `DMPerformMigration` return, home completion, configuration-writer
hit, Mercury root, scene, or display-accept event; the probe reported no
XNU panic. The child was stopped at the bound and QEMU released. As in
CONFIG5/6, the watcher's detach result is only a consequence of the bounded
stop. This attempt provides no poster or draw verdict and does not alter
CONFIG3's measured `_scene` store and matching Mercury/SpringBoard context.

CONFIG5–7 show that this parent/fixture can repeatedly miss the post-home
window at the migration check-in, despite CONFIG3 having reached it. The
corrected resolver and accept→prepare/OGL path therefore remain unvalidated
on a successful post-home run. Do not interpret silence in these three
children as a poster rejection or blank draw.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG7.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG7.poster-join.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG7.stop`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG7.probe.out`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CONFIG7.serial.log` /
`stderr.log`. No model, parent, fixture, or guest state was changed.

### LOCK_CHECKIN3: queue-head identities and active-owner limit (2026-10-04)

The fresh `UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN3` child failed before LLDB
could resolve a cache-backed EL0 PC: AMFI rejected the system dyld cache
under CT policy 0, and launchd panicked when libSystem could not be loaded.
Thus CHECKIN3 has no check-in, lane, or poster evidence; this is a setup
coverage miss, not a new migration result.

Decoding the retained CHECKIN2 handoff snapshot gives the two queued items:
`0x103a01280` invokes static `0x18f8d818c`, the
`BSXPCServiceConnection _lock_activateNowOrWhenReady:` pending-events block;
`0x103a012c0` invokes `0x18f8d68dc`, the connection-activated callback.
Both are `0x114` generic block continuations; their item `+0x18` queue
field was `0x74f2df45f0`. The migration continuation `0x74f2dd12c0` was
appended third on lane `0x74f31a6c80`. That lane's target
`0x74f31a5d00` sampled `dq_state=0x6000a60001cb25`
(enqueued=1, dirty=1, owner bits `0x1cb24`). This identifies pending
activation work and an already assigned drainer, rather than a lane that
never received its initial activation wake.

The best explanation for the unchanging queue is still the separate frozen
owner trace `UI_CLASS16_REFRESH60_BKS_OWNER_FROZEN1`: there, the target
owner's port matched a backboardd thread whose saved stack was executing
`__DMPerformMigration` inside the dispatch serial drain and blocked in a
synchronous DataMigrator XPC send ending at `_mach_msg2_trap`. That run also
found the DataMigrator worker synchronously waiting on its main queue, with
the main thread at a semaphore wait. This supports a migration/XPC
head-of-line block as the reason pending activation/check-in blocks cannot
run, but CHECKIN2's owner bits are `0x1cb24`, not `0x17918`; the
same-boot owner-to-stack identity is not proven. Do not claim that the two
pending activation blocks themselves are blocked on XPC; the evidence puts
the wait in the active drainer ahead of their invocation.

The composed probe's queue decoder now logs invoke PCs, continuation flags,
and target-lane state in `DM_LOCK_CHECKIN_QUEUE_IDENTITY`. CHECKIN3 never
reached it. The next discriminator is a cache-accepted fresh boot and a
same-boot mapping from the target owner bits to the live thread stack. Home,
poster acceptance, and draw remain unscored on the CHECKIN2/3 lineages.

### LOCK_CHECKIN5: same-boot owner-stack match (2026-10-04)

CHECKIN5 fixed the AMFI setup miss by using the known cache-accepted
`/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`; the guest mapped its
dyld cache and the probe attached. At the migration handoff, target
`dq_state=0x6000a600002a21`, owner bits `0x2a20`. In the same frozen child,
backboardd pthread port `0x2a23` on thread `0xffffffec684958f0` matches
those owner bits. Its saved frames pass through dispatch serial drain and
the `__DMPerformMigration` → DataMigration synchronous XPC path, ending at
the Mach-message wait. This closes the owner→thread→stack identity gap and
supports HOL in the active drainer, rather than a missing initial wake.

The outer handoff queue sampled empty by +82 ms, but no child-block invoke,
BKS check-in return, or `DMPerformMigration` return was observed before the
freeze. The only logged `DM_XPC_SYNC_RETURN` was at
`t=1791164536.900`, before the observed `DM_PERFORM_ENTRY` at
`t=1791164536.961`, so it is not proof that this check-in's migration call
replied. In the same snapshot, DataMigrator's main thread port `0x103` was
at a semaphore wait and worker port `0x2703` was in dispatch-sync with
DataMigrator/XPC frames. That reproduces the previously seen main-queue /
semaphore dependency shape; exact pairing of that semaphore to this
check-in request remains open. No home or poster/display attribution was
scored on this stopped child.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN5.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN5.mem/process-stacks.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN5.mem/ram/`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN5.slide.json`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN5.serial.log` /
`stderr.log`. D582 remains a QEMU hypothesis; the CLASS16 fixture and
swap-completion settings were unchanged.

### LOCK_CHECKIN6: same-request DataMigrator wait and missing reply edge (2026-10-04)

The fresh `UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6` child used the repaired
D5825 parent, CLASS16 60 Hz fixture, unchanged D572/D582 hypothesis stack,
and cache-accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`.
The 900-second condition bound ended with no panic or shell progress.

The matched backboardd synchronous migration send entered at
`0x269af5798` on thread `0xffffffec9228b830` at `t=1791165886.189`
(connection `0x7772c76d00`, message `0x77731a07e0`). DataMigrator received
the corresponding server message at `0x269aef374` on thread
`0xffffffec92220950`; its `msgID` lookup selected the msgID-0 arm, then
the same thread entered the migration delegate and
`startMigrationAndOrBlockIfNecessary...` at `0x102b4bfc8`. The frozen
thread remained inside dispatch-sync on queue
`com.apple.datamigrator.migration`. No start-migration return, migration
return, reply-block call, server fallback return, backboardd sync return,
`DMPerformMigration` return, or BKS check-in return followed.

The frozen backboardd snapshot contains the same send-owner thread
`0xffffffec9228b830`; its saved frames retain the sync-send return chain
`0x269af579c` -> `0x269af4bac` -> `0x269af25b8` -> `0x269af03fc`. This confirms
that the captured client request was still waiting when the two
DataMigrator threads were sampled.

In the same frozen DataMigrator process, the main thread was blocked in
`xpc_connection_send_barrier`'s semaphore wait. Its saved semaphore and
connection registers (`x20=0x7c0b0663a0`, `x22=0x7c0b04ca00`) exactly match
barrier 9, whose item `0x103901340` remained in the dispatch-Mach send
references and whose semaphore had no observed signal before freeze. This
is a same-boot pairing of the request worker's blocked migration path and
the main thread's concrete outstanding barrier. The barrier's causal link
to this specific message, and a deadlock cycle, are **not** yet proved; the
worker queue resolves as the DataMigrator migration queue rather than a
demonstrated main-queue sync. The earliest missing response edge remains
after msgID-0 dispatch enters migration work and before the migration
routine/reply returns. DataMigrator did receive and run the handler, so
“handler never scheduled” is excluded.

No check-in return or home was reached on this child, so corrected
poster-accept / prepare / OGL scoring remains out of scope. Evidence is in
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6.mem/process-stacks-datamigrator.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6.mem/process-stacks.json`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6.mem/ram/`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_CHECKIN6.serial.log` /
`stderr.log`. The narrow read-only composition is
`tools/re/dm_lock_checkin_reply_callbacks.py`; D572/D582 remain QEMU
hypotheses and no display/model behavior changed.

#### LOCK_CHECKIN6 offline migration-queue owner decode

The saved RAM chunks resolve the exact queue passed in the matched worker's
saved `x0`: `0x10348e2e0`, labeled `com.apple.datamigrator.migration`.
At freeze its head was `0x16d461ff0`, tail `0x16d605fd0`, and state
`0x60002500000e01`; the linked chain was
`0x16d461ff0 → 0x16d691ff0 → 0x16d4edf90 → 0x16d605fd0`. The head
`0x16d461ff0` resides in the blocked handler thread's stack range; its
`+0x40` word is runtime `0x102b450d8`, which is executable offset `0x90d8`
from the recorded image base and matches the no-op block invoke passed to
the sync. This strongly identifies the head continuation as the waiting
sync item itself: there is no older queued item ahead of it in this list.
The three following items are recorded queue contents, not assigned as the
blocker.

The queue state's owner token `0xe00` matches the `0xe03` thread-port-shaped
field at `cthread_self+0x18` for DataMigrator thread `0xffffffec92687830`.
The same field on DataMigrator main is `0x103`, matching its separately
identified thread port, which anchors this owner-field interpretation. The
owner thread's saved frame `0x102b4f76c`, relative to executable base
`0x102b3c000`, is offset `0x1376c`, the return site after call `0x13768`.
Prior same-build RE identifies that call as
`runPluginAtPath:withIdentifier:parameters:completion:` / the plugin-launch
XPC path. CHECKIN6 did not capture the call arguments, so it does not newly
identify the plugin name from this snapshot.

This closes the immediate wait target: the matched msgID-0 handler's
`dispatch_sync` is waiting for its serial queue's active owner to finish;
that owner is still inside the plugin-launch call at `0x13768` (no frame past
`0x1376c`). This is an active plugin/XPC wait edge (classification C), not a
missing queue wake or an uninvoked queue head. Prior same-build traces went
deeper into `DMMigrationPluginWrapperConnection`'s synchronous send and
SystemAppMigrator/installcoordination work, but those helper-side details
were not paired to CHECKIN6. The main-thread barrier 9 remains a separate
outstanding wait: neither this queue snapshot nor the owner stack links its
item `0x103901340` to the plugin call or establishes a deadlock cycle. The
next precise edge is the plugin-launch XPC reply/return at `0x1376c`, paired
with its wrapper-side request. No check-in return or home occurred; poster
and draw remain out of scope.

This was an offline read-only decode of the preserved CHECKIN6 RAM and
DataMigrator process page-table root; no VM was resumed or modified.

#### LOCK_PLUGIN4: matched plugin launch identity; wrapper return still open

On `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN4`, a fresh D5825 child with the
known cache-accepted `bp-patched-dev6/system.tc` and fixed CLASS16/D572/D582
hypothesis setup, the matched msgID-0 migration path reached the live
`runPluginAtPath:withIdentifier:parameters:completion:` call at executable
offset `0x13768`. The arguments were:

| Argument | Captured value |
| --- | --- |
| path | `/System/Library/DataClassMigrators/SystemAppMigrator.migrator` |
| identifier | `com.apple.MobileInstallation.SystemAppMigrator` |
| parameters | `0x75270449a0` |
| completion | `0x16f7959e0` |

At `t=1791170980.978`, owner `tpidr_el1=0xffffffe1fe743060` entered this
call. At `t=1791170980.989`, the same owner entered the wrapper's synchronous
send at `0x25e8f1f88`; request `0x7526c58360` carried the same plugin
identifier. This is the live argument-to-wrapper-request pairing that was
missing from CHECKIN6.

Neither the matching `CLIENT_SYNC_RETURN` nor the DataMigrator return at
`0x1376c` appeared in the next ~107 seconds. The wrapper connection emitted
generic XPC traffic and DataMigrator received a `CLIENT_WRAPPER_MESSAGE` on
the same receiver, but no event carried enough identity to call it the reply
to this request. Thus the plugin identity and outstanding synchronous send
are established; the actual wrapper reply/return edge is not.

The probe stopped on `DM_START_MIGRATION_RETURN` at `t=1791171088.056` on
`tpidr_el1=0xffffffe1fe807830`, a different owner from the plugin call. Treat
that as an unpaired stop condition only. No matched migration return, reply
block, backboardd sync return, or check-in return was recorded. Barrier 9
remains unlinked and no deadlock is proven. A separate
`MergeBuddyProvisioningResponse.migrator` call appeared on another owner and
must not be conflated with SystemAppMigrator.

Earlier in the same boot, ContainerMigrator, 00LaunchServicesMigrator, and
MSUDataMigrator each had a wrapper sync return followed by `0x1376c` in about
1.5–3.9 seconds. That contrast makes SystemAppMigrator the first measured
long-running candidate on this run, not a proven permanent hang. The next
minimal cut is a SystemApp-ID-scoped wrapper receive/reply or `CLIENT_SYNC_RETURN`
and `0x1376c` stop, followed only then by the matched msgID-0 reply/check-in
ladder. Logs: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN4.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN4.events/`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN4.serial.log` /
`stderr.log`. `tools/re/dm_lock_checkin_plugin_callbacks.py` now writes
identifier-scoped success markers so future stops can target this exact
plugin instead of an unrelated migration return. Poster/draw remain out of
scope; D572/D582 remain QEMU hypotheses.

#### LOCK_PLUGIN5/6: no target request, so no reply verdict

The SystemAppMigrator-scoped follow-up used fresh children of the repaired
D5825 parent, the known cache-accepted `bp-patched-dev6/system.tc`, and the
same CLASS16/D572/D582 hypothesis stack. Its stop conditions targeted only
`WRAPPER_XPC_REPLY`, `CLIENT_SYNC_RETURN`, and `0x1376c` for
`com.apple.MobileInstallation.SystemAppMigrator`; the unpaired
`DM_START_MIGRATION_RETURN` was excluded.

Neither run reached the target. PLUGIN5 ended after launchd reported a
critical SpringBoard crash and the subsequent shutdown hit
`Halt/Restart Timed Out`. PLUGIN6 also reported a critical SpringBoard crash
before any matched DataMigrator start or plugin callback; the guest was
released during shutdown. The trust cache was loaded/accepted, but these
children provide no wrapper or helper-stack evidence. No SystemApp request
was live to freeze, so installcoordinationd/installd were not sampled.

The latest positive evidence remains PLUGIN4: the same DataMigrator owner
entered `runPluginAtPath:` for SystemAppMigrator at `0x13768`, then its
`sendMessageSync` at `0x25e8f1f88` with a request containing the same
identifier. Neither `CLIENT_SYNC_RETURN` nor `0x1376c` appeared for ~107 s;
generic wrapper traffic did not identify a reply. The stop was an unrelated
thread's `DM_START_MIGRATION_RETURN`, so it was only a cutoff. The wrapper
reply remains unresolved and this does not prove a permanent hang or
deadlock.

The probe's ID-scoped success markers now allow a future run to stop only on
the matching wrapper reply, client sync return, or `0x1376c`. If the request
remains open through a valid long bound, the next useful sample is a same-boot
wrapper receive/reply identity plus wrapper/installcoordinationd/installd
stacks. Logs: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN5.*`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN6.*`, and their
`/tmp/dvm/probe/` serial/stderr files. Poster/draw remain out of scope;
D572/D582 remain QEMU hypotheses.

#### LOCK_PLUGIN7: SpringBoard crash before the SystemApp request window

PLUGIN7 reused the PLUGIN4 parent, cache-accepted
`bp-patched-dev6/system.tc`, CLASS16, and D572/D582 hypothesis environment.
The trust cache was validated and sent to launchd. The guest reached `Early
boot complete`, but no matched migration start, `runPluginAtPath:` at
`0x13768`, or SystemApp-scoped callback occurred before launchd logged
`rebooting due to critical process crashes: SpringBoard` at
20:55:08.102627. This is a boot-coverage miss only; PLUGIN4 remains the
high-water mark for the wrapper-reply question.

The first explicit serial error immediately before the restart was
`_process_matches_constraint: error: Requirement not satisfied: Constraint
not matched`. The postmortem found zero XNU panics and no SpringBoard
`OS_REASON`/process exit payload, so the nearby constraint message is not
causally assigned. `docs/re/springboard-crash.md` records a prior
SpringBoard `OS_REASON` for missing `/usr/lib/libobjc-trampolines.dylib` on
the boot image, but this run did not capture that reason; do not treat it as
PLUGIN7's diagnosis. No guest image or state was changed.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN7.postmortem.txt`,
`.threads.txt`, `.ramscan.txt`, `.lldb.log`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN7.serial.log` /
`stderr.log`. If another attempt is made, first restore a PLUGIN4-class live
SystemApp request; only then sample its wrapper reply or helper stacks.
Poster/draw remain parked and D572/D582 remain QEMU hypotheses.

#### LOCK_PLUGIN8: SystemApp request window restored; scoped reply still open

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8` reused the D5825 parent,
`bp-patched-dev6/system.tc`, and the unchanged CLASS16/D572/D582 hypothesis
environment. Unlike PLUGIN5–7, its launch file imported
`dm_lock_checkin_plugin_callbacks.py` from boot, so this is valid coverage of
the request window. The guest passed Early boot and reached the matched
DataMigrator plugin sequence; the earlier PLUGIN5–7 callback-module mismatch
is corrected here, but it does not explain their SpringBoard crashes.

At `t=1791174134.8907661`, owner
`tpidr_el1=0xffffffecf3b30180` entered `runPluginAtPath:` at `0x13768` for
`/System/Library/DataClassMigrators/SystemAppMigrator.migrator`, identifier
`com.apple.MobileInstallation.SystemAppMigrator`. At
`t=1791174134.903908`, the same owner entered `CLIENT_SYNC_CALL` with request
`0x7b3b044ba0` and the same identifier. This restores the PLUGIN4-class live
request on a covered child.

The 1200-second boot watch expired without a SystemApp-ID-scoped
`WRAPPER_XPC_REPLY`, `CLIENT_SYNC_RETURN`, or `0x1376c` return marker. The
matched request had remained open for about 7 minutes 48 seconds at the final
HMP stop. Generic activity for other migration plugins and a later
`DM_START_MIGRATION_RETURN` on owner `0xffffffecf391d120` are not paired to
this request. The serial contained no SpringBoard critical-crash line, and
the probe summary reported zero XNU panics; therefore this run is neither a
SpringBoard failure diagnosis nor a wrapper-reply result. It establishes
restored request coverage and a longer unanswered interval than PLUGIN4's
~107 seconds, without proving a permanent hang or deadlock.

The guest was left frozen at the bounded stop. Artifacts:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.events/`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.watch.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.probe.out`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.serial.log` /
`stderr.log`. The wrapper reply remains unresolved; any helper-stack follow-up
must stay keyed to this live SystemApp request. Poster/draw remain parked and
D572/D582 remain QEMU hypotheses.

#### LOCK_PLUGIN8 same-freeze stack sample: client wait confirmed, peer still unpaired

The preserved PLUGIN8 guest was sampled from its saved RAM image while the
source VM remained paused. The DataMigrator process is PID 120
(`com.apple.datami`); its thread `0xffffffecf3b30180` is the exact
`tpidr_el1` recorded at the SystemApp `CLIENT_SYNC_CALL`. The saved user stack
remains in the XPC/Mach wait path and still has frames at `0x27af5179c`
(inner DM sync return site), `0x27af4df8c` (`CLIENT_SYNC_RETURN`),
and `0x10243776c` (`runPluginAtPath:` return). The saved process page table
still maps request `0x7b3b044ba0`; its entry event identifies
`com.apple.MobileInstallation.SystemAppMigrator`. This is same-request
evidence that the client call remained open at the freeze, not a completed
reply.

The helper snapshots are contemporaneous but not request-keyed. PID 169
(`com.apple.migrat`) has worker threads `0xffffffecf3775120` and
`0xffffffecf3637060` in XPC/Mach wait stacks. Shortly after the SystemApp
call, owner `0xffffffecf3775120` emitted `WRAPPER_XPC_SEND` with connection
`0x7cd1098000` and message `0x7cd0c100c0`; the decoded event had no
SystemApp identifier. The saved message contains `msgID`, `pid`, and an
Apple-account benefactor key, so it is not assigned to SystemAppMigrator.
At `t=1791174135.516`, a `CLIENT_WRAPPER_MESSAGE` also arrived with `x0`
equal to the SystemApp receiver `0x7b3ac74c80` captured at
`runPluginAtPath:`. Its fields were not decoded and it did not produce the
scoped sync return; treat it as same-receiver traffic, not proof of the
awaited reply.
PID 203 (`installcoordinat`) and PID 172 (`installd`) also had active
service/install worker stacks in this freeze, but no connection, message, or
plugin-ID link ties those workers to request `0x7b3b044ba0`. This is
concurrent helper activity, not evidence that either service owns the
outstanding send or that a deadlock exists.

The first unresolved edge is the wrapper-side peer identity and reply. A
decisive follow-up must preserve the matched wrapper connection/message
through remote service receive and reply, then correlate that reply with the
SystemApp `CLIENT_SYNC_RETURN` and `0x1376c`. Process-only stacks cannot name
the peer. No migration/check-in return or home was observed; poster/draw
remain out of scope. RAM and stack artifacts are
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.mem/ram/` and
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.mem/process-stacks.json`.

#### LOCK_PLUGIN8 message-object follow-up: receiver matched, reply not paired

Read-only decoding of the paused PLUGIN8 RAM resolves the
`CLIENT_WRAPPER_MESSAGE` at `t=1791174135.516374`: `x0=0x7b3ac74c80` matches
the receiver from the SystemAppMigrator `runPluginAtPath:` call at
`t=1791174134.890766`; `x1` resolves to `handleMessage:`. Message
`0x7b3ac2c900` points at an inner `CPX@` payload at `+0xb8` containing
`msgID` and `checkNecessity`. Treat this as an inbound message on the same
per-call receiver, not as the terminal sync reply: the matched client request
`0x7b3b044ba0` remained blocked, with no SystemApp-scoped
`CLIENT_SYNC_RETURN` or `0x1376c` before the freeze.

The same-build completed ContainerMigrator trace offers only a protocol
comparison: its wrapper send carrying `migrationResult` preceded its client
sync return. No corresponding request-linked SystemApp result/reply is in
PLUGIN8. The nearby `WRAPPER_XPC_SEND` (`conn=0x7cd1098000`,
`message=0x7cd0c100c0`) decodes as Apple-account traffic without a SystemApp
identifier and remains unrelated. Process stacks for `com.apple.migrat`,
installcoordinationd, and installd are still not tied to the matched request.

The snapshot closes receiver identity for this inbound callback, but not the
XPC/Mach peer identity or the remote receive/reply lineage. The next useful
capture is the underlying transport endpoint/message identity for this
receiver and its corresponding remote receive and reply, paired to
`CLIENT_SYNC_RETURN` / `0x1376c`. This remains an open reply path, not a
hang/deadlock finding. Evidence: the PLUGIN8 LLDB log and saved RAM under
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.*`.

#### LOCK_PLUGIN8 transport follow-up: frozen attach did not recover endpoint

A bounded LLDB follow-up attached to the paused PLUGIN8 guest and armed raw
wrapper XPC send/receive/reply capture. QEMU's GDB stub selected a vCPU/kernel
context, not the saved DataMigrator user thread; reading request
`0x7b3b044ba0` and receiver `0x7b3ac74c80` from that context returned no user
bytes. The endpoint at the already-entered `CLIENT_SYNC_CALL` could not be
recovered retrospectively.

After a bounded resume, raw wrapper sends appeared with connection candidate
`0x7cd1199b80` and opaque dictionary field `f=0xbbc7a38efc9dc91f`. The
observed sends were not tied by plugin ID, receiver, or message identity to
SystemApp request `0x7b3b044ba0`. Generic `DM_START_MIGRATION_RETURN` and
`DM_CLIENT_SYNC_RETURN` hits were on other `tpidr_el1` values than the matched
owner `0xffffffecf3b30180`; neither closes this sync. The guest was paused
again. There is still no remote receive/reply classification and no
hang/deadlock claim.

The probe now logs bounded raw bytes for the `x0` endpoint candidate at
wrapper send/receive sites. The candidate's field layout and service/port
identity remain unproven. The next decisive run must start before the
SystemApp sync call and preserve: call-time `x0..x8` plus request/receiver;
Mach send header destination and reply ports plus message ID; matching
wrapper-side receive and reply; and same-owner `CLIENT_SYNC_RETURN` /
`0x1376c`. Use the Mach port/message tuple to join processes, and do not
promote same-receiver `checkNecessity` traffic to a terminal result without
that link. Poster/draw remain parked until check-in and home.
The bounded LLDB console excerpt is preserved at
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN8.followup.stdout.log`.

#### LOCK_PLUGIN11/12: call-time Mach tuple captured; cross-process reply remains unpaired

The PLUGIN8 attach limitation required fresh children. PLUGIN9 stopped on an
earlier plugin return and PLUGIN10 hit a callback `UnboundLocalError`; neither
is transport evidence. Fresh PLUGIN11 reached the matched SystemApp call, but
its then-loaded `mach_msg2` logger read the vector pointer as though it were a
Mach header. PLUGIN11 remains useful for call identity only; its parsed port
fields are discarded.

PLUGIN12 reused the D5825 parent, accepted
`/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, CLASS16, and the same
D572/D582/A407-scanout hypothesis stack. Its 600-second port-capture window
started before the SystemApp sync. At `0x13768`, DataMigrator owner
`0xffffffe545d6f830` entered
`/System/Library/DataClassMigrators/SystemAppMigrator.migrator` with ID
`com.apple.MobileInstallation.SystemAppMigrator`; the following
`CLIENT_SYNC_CALL` at runtime `0x27a54f88` had request `0x7548c2c7e0`,
receiver `0x7549070e40`, and registers:

```text
x0=0x7548c482c0 x1=0x211f72488 x2=0x7548c2c7e0 x3=0x16d467d30
x4=0x2001880e8 x5=0x8001376c06020004 x6=0x4210123 x7=0x16d46b0e0
x8=0x7549070e40
```

At the call-time `mach_msg2_trap` site (static `0x237ccfccc`, runtime
`0x25392bccc`), the packed arguments decoded with the existing
`lsd_ipc_trace_callbacks.py` ABI helper show a 508-byte message carrying the
SystemApp `pluginIdentifier`: destination name `0x7803`, local reply name
`0x2e03`, voucher name `0x170f`, message ID `0x40000000`. The same owner then
entered receive on name `0x2e03` with a 16 KiB receive size. A preceding
52-byte send to name `0x5503` used message ID `0x77303074`; it is recorded as
another call-time packet, not assigned as the plugin request.

A same-receiver `handleMessage:` callback arrived at `t=1791182950.161` on
receiver `0x7549070e40`, about 0.73 seconds after the sync call. It was not
paired to a Mach reply or shown to be terminal. No same-owner
`CLIENT_SYNC_RETURN` or `0x1376c` was observed through the 600-second bound.
No remote receive/reply was proven for this request. Other-process traffic
included the same numeric port names, but Mach names belong to each task's
IPC namespace; equal numbers across processes do not identify the same right.
Those rows are collisions, not peer evidence. The bounded run stopped with
no panic and no hang/deadlock conclusion.

The source probe now decodes packed `mach_msg2` arguments and scopes numeric
port-name matches to a process; the PLUGIN12 runtime predated that final
scope correction, so its cross-process numeric matches must not be used.
The next exact edge is resolving the caller's destination and reply names to
their kernel IPC right identities (or capturing equivalent request identity
at the XPC/Mach transport boundary), then matching the peer receive/reply to
the same request/message and same-owner return. Evidence:
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN12.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN12.events/`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN12.probe.out`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN12.serial.log`.
The child is `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin12.qcow2`.
Poster/draw remain parked until check-in returns and home completes.

#### LOCK_PLUGIN13: current-kernel IPC name-to-object boundary identified; request join still open

The paused-attach route did not provide the old call-time user registers, and
the PLUGIN12 QEMU process was no longer paused when checked (`info status`
reported running). Do not mine that child for the old call-time port mapping.
The fresh-child requirement remains for a request-keyed capture.

An offline pass over the exact extracted `firmware/bootkc` kernel
(`/tmp/dvm/bootkc-kernel/com.apple.kernel`, Darwin 27.0.0 / xnu-13432.2.10,
T8140) now identifies the relevant kernel conversion boundary. The stripped
kernel has no usable internal symbol names, so these names are assigned by
matching the disassembly and call dataflow to the XNU routines, not by a
kernel symbol table:

* `mach_msg2_trap` at static `0xfffffff00aaa3b38` reaches the send helper at
  `0xfffffff00aaa3650`; that helper calls the copyin routine at
  `0xfffffff00aa8bc1c` (`+0x20000000` for this project's established runtime
  kernel slide).
* The `0xfffffff00aa8bc1c` prologue preserves the six-argument shape of
  `ipc_kmsg_copyin_from_user`: kernel message, send context, sender
  `ipc_space`, map, priority, options pointer. This is where the outgoing
  user header is copied into a kernel message and its rights are resolved.
  The source-level resolution sequence is `ipc_kmsg_copyin_header` →
  `ipc_kmsg_copyin_header_rights` → `ipc_entry_lookup(space, name)` →
  `ipc_right_copyin`, which leaves the kernel message holding the destination
  `ipc_port` object. These addresses are local to this bootkc; the precise
  inner lookup instruction in the stripped binary is not yet assigned.
* The receive-side boundary is visible in the same bootkc disassembly:
  `mach_msg2_trap` at `0xfffffff00aaa402c` calls
  `0xfffffff00aa8f990` with the current task's IPC space, receive name, and
  output slot. The callee preserves x1 (name) and x2 (output pointer), calls
  its lookup helper, then stores the resolved object through that output
  pointer on success. This matches the `ipc_mqueue_copyin(space, rcv_name,
  &object)` role. The receive name is therefore also task-local; the
  cross-task join must compare the resolved kernel object (`ipc_port*`), not
  `0x7803` or `0x2e03` against another process's numeric name.

This identifies the exact *kind* of kernel edge that maps names to shared
objects, but does not yet prove that the destination right behind DataMigrator
name `0x7803` is the same right received by a particular peer, nor that the
reply right from name `0x2e03` returned a terminal result. A matching next
capture must start before the SystemApp call and retain, for each task, the
`ipc_space` plus name at copyin; sample the send-side resolved destination
`ipc_port*` and the peer-side resolved receive object; then join by that
pointer and message ID `0x40000000`/SystemApp identity. For the reply, compare
the reply port object to the DataMigrator receive object and require the same
owner's `CLIENT_SYNC_RETURN` / `0x1376c`.

One PLUGIN12 event file contains `success.DM_CLIENT_SYNC_RETURN.json`, but its
`tpidr_el1` is `0xffffffe5459ac950`, not the SystemApp owner
`0xffffffe545d6f830`. It is an unrelated return and is explicitly excluded.
Thus the result remains: task-local tuple proven; kernel conversion boundary
identified statically; shared right, peer receive/reply, and same-owner return
still unproven. The 600-second open interval is not a hang/deadlock result.
Poster/draw remain parked. Evidence: exact-kernel disassembly in
`/tmp/dvm/mach2-disasm.txt`, `/tmp/dvm/send-helper-disasm.txt`, and
`/tmp/dvm/ipc-copyin-disasm.txt`; PLUGIN12 event
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN12.events/success.DM_CLIENT_SYNC_RETURN.json`.

#### LOCK_PLUGIN14: name-to-entry helper isolated; fresh dynamic capture blocked

A closer pass on this bootkc identifies the generic table lookup at static
`0xfffffff00aa82188`. Its entry is `x0=ipc_space`, `w1=mach_port_name`; it
loads the space table through `space+0x48`, indexes with `name>>8` and 0x18-byte
entries, validates the generation/right bits at entry `+8`, and returns the
entry pointer in `x0` (or zero for an invalid name). The caller's following
field access resolves the entry's object. This matches the `ipc_entry_lookup`
role and is the exact name→entry site; it still requires a runtime sample to
show the object for DataMigrator's particular `0x7803` / `0x2e03` names.

The dynamic callback was extended at two bounded points: after send copyin at
`mach_msg2_trap` return PC `0xfffffff00aaa3900` (`x23` is the kernel message;
the inline kernel header at `kmsg+0x40` has 64-bit remote/local object fields
at `+8` / `+0x10` and message ID at `+0x20`), and after receive-name resolution
at `0xfffffff00aa8fa18` (`x22` resolved object, `x21` task-local name). It
arms only after this SystemApp `CLIENT_SYNC_CALL`, checks message ID
`0x40000000` plus the SystemApp identifier in the body, then conditions peer
receive and reply-send sampling on the resolved object pointer. Non-inline
signed headers are skipped; failed LLDB predicates disable their kernel
breakpoints instead of widening into an IPC survey. Its send-side event is
`IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS`, not a peer match: it includes the
DataMigrator task PID/owner and its local destination/reply names alongside
the resolved object candidates. A peer receive is joined only by exact
resolved-object equality and records the peer PID/program plus its separate
local receive name. The reply send is filtered on the original resolved reply
object; the final closure condition remains same-owner
`CLIENT_SYNC_RETURN` / `0x1376c`. The bounded capture has a 600-second window.
This probe has not run on a live matching request.

`qemu-img info` verifies the D5825 chain as D5825 →
`ui-class16-refresh60-dm-plugin4-launchfrozen1.qcow2` →
`ui-class16-refresh60-ccs-ls-seedprogress1.qcow2`. The final image is missing;
the intermediate plugin4 overlay exists, so D5825 is the correct documented
lineage but cannot open without its lower backing layer. Exact-name searches
of `/tmp/dvm`, `~/dvm-artifacts`, and Spotlight found no replacement. The
checkpoint's only recorded location is under `/tmp/dvm`, a wipe-prone scratch
tree; the particular cleanup event is unknown. The
generic persistent-parent rebuild would create a different guest state and
is not a valid substitute. Recreate this checkpoint only from its original
ancestor by rerunning `CCS_LS_SEEDPROGRESS1` with
`tools/re/ccs_ls_seed_progress_callbacks.py`; that ancestor is not present in
the current artifact set, so there is no runnable rebuild command now. Once
the verified pre-seed parent and original boot inputs are restored, the
recreation command shape is:

```sh
PARENT=<verified-original-pre-seed-parent.qcow2> \\
CALLBACKS=ccs_ls_seed_progress_callbacks \\
PROBE_STOP_LLDB_REGEX=LSD_SEED_SYNCWRITE_CALL \\
KEEP_GUEST=1 SECS=1800 STALL_SECS=1800 \\
tools/re/setup_gate_probe.sh UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS1
```

This template cannot establish equivalence by filename alone; it requires the
original ancestor and run inputs. The generic persistent-parent rebuild is
not a substitute.

#### LOCK_PLUGIN15 recovery replay and same-lineage IPC attempt

The earlier “ancestor not present” status was corrected by the saved launch
record: the exact pre-seed parent exists at
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/disk.qcow2`. The original
bootkc, trust cache, and device tree are the sibling
`/Users/steve/dvm-artifacts/bp-patched-dev6/{bootkc,system.tc,system.dtree}`.
The lost seed qcow2 itself was not recovered. Instead, its seed-progress
operation was replayed from that original parent using the recorded CLASS16
60 Hz callback, reconstructed from the checked-in color/timing fixtures. The
ColorElements callback prefix matched the retained CLASS16-probe prefix; the
timing portion used the recorded 21,000 Hz horizontal and 60 Hz vertical
fixed-point rates. This is a replayed checkpoint at the same measured boundary,
not a byte-for-byte restoration claim.

Tag `UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS1` stopped on the emitted JSON
event for `LSD_SEED_SYNCWRITE_CALL`, after 1,061 registration returns and
natural `LSD_SCAN_DRIVER_RETURN`, `LSD_SYSTEM_SCAN_RETURN`, and
`LSD_ENUM_RETURN`. The write's return was not observed. The seed qcow2 is 336
MiB, passes `qemu-img check`, and has SHA-256
`004f1685acf94d2264fd6c5d4e5bc0d0bd65ac5fbde1be0d6a1b5771d0b617ab`. A
byte-identical durable copy is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-ccs-ls-seedprogress1-replay-20261005.qcow2`.
After `qemu-img info --backing-chain` showed the complete chain, the existing
plugin4 overlay was metadata-repointed to that durable copy. D5825 now resolves
through plugin4 → replayed seed → original boot1 parent and passes
`qemu-img check`; this makes the documented lineage runnable while retaining
the explicit replay-vs-original-binary distinction.

The fresh child `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN15_IPC1` booted that D5825
chain with `bp-patched-dev6/system.tc`, the same CLASS16/D572/D582 settings,
and `dm_lock_checkin_plugin_callbacks` armed from boot. It reached a new,
matched SystemAppMigrator sequence: owner `tpidr_el1=0xffffffe026249120`,
request `0x78b3038d80`, receiver `0x78b3074e00`, and
`CLIENT_SYNC_CALL` for `com.apple.MobileInstallation.SystemAppMigrator`. The
captured 508-byte Mach send carried message ID `0x40000000`, destination name
`0x6c03`, and reply name `0x200b`. A preceding send to `0x7503` with ID
`0x77303074` is unrelated.

The kernel hooks were reported ready at runtime `0xfffffff02aaa3900`
(`KERNEL_KMSG_COPYIN_RETURN`) and `0xfffffff02aa8fa18`
(`KERNEL_MQUEUE_COPYIN_RESOLVED`), but neither hit in this capture. Thus the
new boot closes the media/launch-coverage gate and revalidates the SystemApp
call, but it does **not** close the cross-process identity edge:
`IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS`, object-equal peer receive/reply, and
same-owner `CLIENT_SYNC_RETURN` / `0x1376c` are absent. The same-receiver
`handleMessage:` traffic remains intermediate. The matched sync was still
open after about 661 seconds; no hang/deadlock is inferred. The immediate next
probe refinement is to prove why the post-copyin kernel breakpoints do not
fire after the measured `mach_msg2` call, or place an equivalent resolved-port
sample on the live path. Poster/draw remain parked and D572/D582 remain QEMU
hypotheses.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS1.lldb.log`,
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS1.serial.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN15_IPC1.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN15_IPC1.events/`, and
`/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN15_IPC1.serial.log`.
No matching DVM QEMU process was present to release. The probe update remains
unrun on a live matched request: send-side resolved object candidates,
object-equal peer receive/reply, and same-owner return are all still open.
The 600-second unanswered sync is not a hang/deadlock finding.

#### LOCK_PLUGIN29: live `mach_msg2` object sample; peer delivery remains open

The D5825 replay lineage and PLUGIN4-style boot inputs were runnable again;
the seed is the replayed 2026-10-05 image, not the lost byte-identical file.
Fresh child `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN29_IPC_REPLY` revalidated the
SystemAppMigrator `CLIENT_SYNC_CALL`. Its owner was
`0xffffffeaf20ee0c0`, request `0x7619038c60`, receiver `0x7619060e20`, and
same-owner `mach_msg2` carried message ID `0x40000000`, task-local
destination `0x4a0f`, and reply name `0x2013`.

The static/runtime mapping is now measured on this bootkc: `mach_msg2_trap`
starts at `0xfffffff00aaa3b38`; its copyin helper call is at static
`0xfffffff00aaa3f50` and the post-copyin return sample is
`0xfffffff00aaa3f54` after adding the kernel slide `0x20000000`. At that
runtime PC, `x21` is the kmsg. Its header at `kmsg+0x40` contains remote and
local object pointers at header `+8` / `+0x10` (kmsg `+0x48` / `+0x50`) and
message ID at header `+0x1c` (kmsg `+0x5c`). The live
`IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS` event recorded destination object
`0xffffffeaf1f83430` and reply object `0xffffffeaf1fd6470`, size 516, ID
`0x40000000`, and the same owner/request correlation. The OOL body did not
contain the plugin identifier; the call-time owner/request plus message ID
are the correlation, not an inline-body string match.

This explains the PLUGIN15 breakpoint miss: static `0xfffffff00aaa3900` is
not the post-copyin return in the live `mach_msg2_trap` path (it belongs to
the adjacent/older trap path); the measured send sample is `…3f54`. The
PLUGIN15 receive-helper breakpoint `0xfffffff00aa8fa18` was also not the site
that produced the live object sample. In the current `mach_msg2_trap`, the
helper is called at `0xfffffff00aaa402c`; after it returns,
`0xfffffff00aaa4040` exposes the
resolved receive object in `x22` and local name in `x21`. That site hit for
the **same DataMigrator task's reply object**. It proves receive-right
resolution, not that a message was dequeued or that a peer received the
SystemApp request. Names `0x4a0f` and `0x2013` remain task-local.

The original PLUGIN29 window had no object-equal destination receive, no
reply-destination send, and no same-owner `CLIENT_SYNC_RETURN` /
`0x1376c` before its 1200-second bound. Those are bounded negative
observations on the instrumented `mach_msg2` path, not proof that no peer or
reply exists. The initial static interpretation of
`0xfffffff00aaa40bc` as a post-`ipc_mqueue_receive` result was corrected by
PLUGIN31/32 below: it is a secondary object-copyin result before the outgoing
send. The PLUGIN29 late attach did not establish peer-receive coverage. No
peer join or terminal reply is established by PLUGIN29/30; unanswered time
is not a hang/deadlock result.

Evidence: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN29_IPC_REPLY.lldb.log`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN29_IPC_REPLY.events/`,
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN29_IPC_REPLY.probe.out`, and
`/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN29_IPC_FOLLOWUP2.lldb.log`.
The follow-up child was left paused. D572/D582 remain QEMU hypotheses; poster
and draw work remain parked.

### PLUGIN30 peer-reply capture (legacy DEQUEUE tag; no dequeue proven)

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN30_IPC_DEQUEUE` reran the PLUGIN29 transport capture from a fresh child of the replay-backed D5825 chain (`dm-ls-parent-seed-d5825` → `dm-plugin4-launchfrozen1` → `ccs-ls-seedprogress1-replay-20261005`). The replay is still not byte-identical to the deleted original seed. The run used `bp-patched-dev6/system.tc`, the unchanged CLASS16/D572/D582 settings, and a 1500-second bound. No late attach was scored.

The probe armed cross-task receive-right (`0xfffffff02aaa4040`) and a second candidate site (`0xfffffff02aaa40bc`) without LLDB expression conditions. The old DEQUEUE label was incorrect: PLUGIN31/32 disassembly shows this is a secondary object-copyin result before send, not dequeue. The reply-send callback was object-filtered, and same-owner sync/`0x1376c` remained separately keyed.

This child revalidated SystemApp `CLIENT_SYNC_CALL`: owner `0xffffffe503396890`, request `0x74fb01cf60`, path `/System/Library/DataClassMigrators/SystemAppMigrator.migrator`, identifier `com.apple.MobileInstallation.SystemAppMigrator`. At `mach_msg2_trap` post-copyin `0xfffffff02aaa3f54`, msgID `0x40000000` resolved destination candidate `0xffffffe2534d1030` and reply candidate `0xffffffe252ce86a0`.

The only shared-object receive-right resolve was `0xfffffff02aaa4040` in `com.apple.datamigrator`, matching the **reply candidate** `0xffffffe252ce86a0` on the sending owner. It was not a peer dequeue or terminal result. The PLUGIN30 `0xfffffff02aaa40bc` breakpoint did not record a dequeue; its site meaning is corrected below. Keep the peer/reply edge unresolved; unanswered time is not a hang/deadlock result.

Result: send-side object identity is proven again; peer delivery/reply remains open. Do not assign the unpaired `handleMessage:` or other plugin returns to this request. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN30_IPC_DEQUEUE.lldb.log`, `.events/`, `.probe.out`, `.slide.json`, and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN30_IPC_DEQUEUE.{serial,stderr}.log`. The guest was left frozen at the bounded stop. D572/D582 remain QEMU hypotheses; poster/draw remain parked.


### PLUGIN31/32: `+0x40bc` is pre-send secondary copyin, not dequeue

Fresh replay-D5825 children (`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN31_IPC_40BC` and `...PLUGIN32_IPC_40BC_HEADER`) repeated the matched SystemAppMigrator send with the existing CLASS16/D572/D582 hypothesis stack and cache-accepted `system.tc`. The callback was same-owner keyed after `IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS`; each run stopped on the first `+0x40bc` hit. These are point samples, not a long unanswered-request interval.

The live send candidates and same-owner `+0x40bc` sample were:

- PLUGIN31 owner `0xffffffe2298f5120`, request `0x7d34c2cd20`; destination object `0xffffffe31086d0c0`, reply object `0xffffffe3106fe110`. At `+0x40bc`, `x0=0`, `x22=0xffffffe3106fe110` (reply object), `x23=0` before the current instruction, and `x8=0xffffffe31086d0c0` (destination candidate).
- PLUGIN32 owner `0xffffffdf0b2118f0`, request `0x7bcac60ea0`; destination object `0xffffffdd407d4cd0`, reply object `0xffffffdd4068c2b0`. At `+0x40bc`, `x0=0`, `x22=0xffffffdd4068c2b0` (reply object), `x23=0` before the current instruction, `x8=0xffffffdd407d4cd0` (destination candidate), and `x24=0x6107`.

The bootkc disassembly resolves the meaning. At static `0xfffffff00aaa40b0`, the function calls the secondary copyin helper; `+0x40b4` loads its output into `x8`, `+0x40b8` tests `w0`, and `+0x40bc` is `csel x23,x8,xzr,eq`. Thus the breakpoint sees `x23` before the select executes. The earlier `+0x4040` hit resolves the sending task's reply receive right into `x22`; it does not mean a message was dequeued. In PLUGIN32, reading `x8+0x40` as a kmsg header produced a zero-looking structure, but that interpretation is invalid: `x8` is the secondary copyin output, not a kmsg.

This is outside the requested A–D dequeue classification: `+0x40bc` is not the receive success edge. It proves a successful secondary copyin on the matched same-owner path and confirms the reply object is the current `x22`; it does not show a peer receive, reply send, or sync return. The outgoing-send helper call is at static `0xfffffff00aaa3f50` (target `0xfffffff00aa8dd58`), with its return-status branch at `+0x3f54`. `+0x4160` is instead the call to `ipc_mqueue_receive`; `+0x4164` is its post-call branch, and `+0x4178..+0x417c` calls `mach_msg_receive_results` and captures its result. None of those trap boundaries alone is the queue-dequeue success edge. The normal queued receive handoff is the `ith_kmsg` store at `0xfffffff00aa8f44c`, after the selected queue link is removed at `0xfffffff00aa8f3a4..0xfffffff00aa8f3c4`.

The probe now labels this sample `KERNEL_SECONDARY_COPYIN_RESULT` / `IPC_SAME_OWNER_SECONDARY_COPYIN_RESULT` and no longer decodes `x8` as a kmsg. The PLUGIN33 follow-up below carries the send-helper object candidate into the queue-store probe; it does not promote that candidate to peer evidence.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN31_IPC_40BC.{lldb.log,events,probe.out,slide.json}`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN31_IPC_40BC.{serial,stderr}.log`, and corresponding `PLUGIN32_IPC_40BC_HEADER` files. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

### PLUGIN33: matched send reaches reply receive; queue handoff PC identified

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN33_IPC_MQUEUE` used a fresh child of the replay-backed D5825 parent (`/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825.qcow2`), the accepted `bp-patched-dev6/system.tc`, and the same CLASS16/D572/D582 hypothesis settings. The child revalidated the matched SystemAppMigrator request: owner `0xffffffe0db58a0c0`, request `0x7cd30404e0`, plugin ID `com.apple.MobileInstallation.SystemAppMigrator`.

At the send-helper call `0xfffffff02aaa3f50`, the 516-byte kernel kmsg was ID `0x40000000`, kmsg `0xffffffde29f18300`, destination object candidate `0xffffffdff6da2fb0`, and reply object candidate `0xffffffdff6be4df0`. The same owner then entered the `ipc_mqueue_receive` call at `0xfffffff02aaa4160` with `x22=0xffffffdff6be4df0` and `x0=0xffffffdff6be4e00` (the receive waitq argument derived from that object). This is the request's reply-receive call, not a dequeue result. The direct `+0x3f54` status sample was not captured for this 516-byte message; the earlier `+0x3f54` result (`x0=0`) belonged to a separate 68-byte control message with ID `0x77303074`. No `+0x4164` receive return, `+0x417c` receive-results return, target-matched kmsg handoff, or same-owner `CLIENT_SYNC_RETURN`/`0x1376c` was logged before the guest paused at a global `+0x3f54` stop on a different owner. That cutoff is not evidence that the target receive never completed.

Static bootkc disassembly identifies the normal queued success handoff: `ipc_mqueue_receive` is called from `mach_msg2_trap+0x4160`; on the queue path it removes the selected entry at `0xfffffff00aa8f3a4..0xfffffff00aa8f3c4`, then PAC-stores the selected kmsg pointer to the receiving thread's `ith_kmsg` field (`thread+0x3b0`) at `0xfffffff00aa8f44c`. The store is the useful dequeue-success probe, because it follows queue unlink and carries the kmsg candidate in `x20`. Alternate `ith_kmsg` stores at `0xfffffff00aa8e638` and `0xfffffff00aa8eca0` remain direct-delivery candidates and require the same header/object check. The trap's `+0x4164` and `+0x417c` sites are result/continuation boundaries, not the actual unlink/store.

The probe now seeds an explicitly labeled send-helper-call object candidate from the post-copyin kmsg, so later receive/store probes can compare kernel object identity even if the helper-return callback misses. A send-call candidate or entry to the reply wait does not prove peer delivery, terminal reply, or sync completion. The guest was stopped after the bounded capture; no hang/deadlock conclusion is made. Logs: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN33_IPC_MQUEUE.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN33_IPC_MQUEUE.events/`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN33_IPC_MQUEUE.{serial,stderr}.log`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

### PLUGIN34–36: SystemApp request handoff proven; terminal reply remains open

Fresh replay-D5825 children used the accepted `bp-patched-dev6/system.tc`, the unchanged CLASS16/D572/D582 settings, and `dm_lock_checkin_plugin_kernelpath_callbacks` armed from boot. PLUGIN34 (`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN34_IPC_ITHKMSG`) produced a positive queue-handoff match at runtime `0xfffffff02aa8f44c` (static `0xfffffff00aa8f44c`). The send-helper candidate was the matched SystemAppMigrator request: owner `0xffffffec41638950`, request `0x77eec24a80`, msgID `0x40000000`, 516 bytes, destination object `0xffffffec41974190`, reply object `0xffffffec4158c850`. At the store, `x20=0xffffffea74695d00` decoded to that same request and its remote object matched the destination candidate. The receiver was `com.apple.migrationpluginwrapper`, thread `0xffffffec41625120`; the kmsg was assigned to `ith_kmsg` (`thread+0x3b0`). This closes actual request delivery into the wrapper; the send candidate alone did not establish delivery.

PLUGIN36 (`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN36_IPC_REPLY`) repeated the matched handoff: owner `0xffffffe21e5c3060`, request `0x79d103c180`, destination candidate `0xffffffe4ce3c5ae0`, reply candidate `0xffffffe4ce727b80`; the 516-byte ID `0x40000000` kmsg was stored at `+0xf44c` in `com.apple.migrationpluginwrapper`, receiver thread `0xffffffe21e4ab060`. The same owner resolved and entered `ipc_mqueue_receive` on reply object `0xffffffe4ce727b80`. No reply-object `ith_kmsg` store, reply-destination copyin candidate, or SystemApp-scoped `PLUGIN_SYNC_RETURN` / `0x1376c` was recorded before the capture ended. PLUGIN35 was probe-control-inconclusive after the same request/receive path. PLUGIN36 was ended deliberately after the VM paused in debug without a matching stop marker; this is not evidence of receive failure or a permanent wait.


### PLUGIN38: matched wrapper XPC dispatch; app-level reply edge still open

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN38_WRAPPER_REPLY` used a fresh replay-D5825 child, `bp-patched-dev6/system.tc`, and the existing CLASS16/D572/D582 settings. The matched SystemAppMigrator call was on owner `0xffffffdedb7da890`, request `0x73bac104e0`, identifier `com.apple.MobileInstallation.SystemAppMigrator`. At runtime `0xfffffff02aa8f44c`, the 516-byte `0x40000000` kmsg was assigned to `ith_kmsg` on the `com.apple.migrationpluginwrapper` receiver thread `0xffffffdedb7920c0`; its remote object `0xffffffdedb571390` matched the send-side destination candidate. This repeats and preserves PLUGIN34's request-delivery proof.

The new userspace bracket then saw the same wrapper thread cross shared libxpc's Mach-event boundary on connection candidate `0x7de2c44780` at `t=1791236136.827` and `.832`, about 9–14 ms after the `ith_kmsg` store. The second event's raw payload at `0x10312b5f0` contains `com.apple.MobileInstallation.SystemAppMigrator`. At `t=1791236136.837`, the same thread and connection entered the XPC event-handler boundary (`pc=0x19da17a44`, `lr=0x19da19f5c`; `x1=0x7de3020000`, `x2=x3=0x2903`). This closes the edge through wrapper-side XPC dispatch for a payload carrying the target plugin identifier. It does **not** identify the wrapper's registered application callback or prove that migration work began; these shared libxpc sites are dispatch boundaries.

Reply generation remains unclosed. For roughly 355 seconds after the matched wrapper handoff, there was no object-matched send to the captured reply object, no reply-object `ith_kmsg` handoff to DataMigrator, and no same-owner `CLIENT_SYNC_RETURN` / `0x1376c`. Instrumentation ended when LLDB stopped at the global DataMigrator plugin-return PC for an unrelated `TCCAccessRequest` owner and exited; the rest of the 1200-second guest window therefore has no probe coverage. The guest itself reached the 1200-second bound and was left frozen with zero XNU panics, but that later uninstrumented interval is not reply evidence. Treat this as a bounded observation ending at loss of instrumentation, not proof that the wrapper never replied, and make no hang/deadlock claim.

The next discriminator is to follow the wrapper's registered event callback from this same connection/event to its first app-level message handler, carrying the matched request or decoded plugin ID, then continue to the reply-object send. Keep the reply-object identity filter and same-owner return. The probe was adjusted to omit the global `runPluginAtPath` return breakpoint, which interrupted this run on an unrelated owner; the scoped sync-return marker remains. That adjustment passed syntax validation but has not yet been boot-validated. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN38_WRAPPER_REPLY.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN38_WRAPPER_REPLY.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN38_WRAPPER_REPLY.probe.out`, and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN38_WRAPPER_REPLY.{serial,stderr}.log`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

### PLUGIN42/43: registered wrapper callback reached; terminal reply still open

PLUGIN42 (`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN42_WRAPPER_HANDLER`) revalidated the SystemAppMigrator `CLIENT_SYNC_CALL` after the global `runPluginAtPath` return breakpoint was removed. Its wrapper-event candidate was sibling traffic without the target plugin identity, so it is not callback attribution. PLUGIN43 (`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN43_WRAPPER_HANDLER`) is the boot-validated capture: the probe remained attached through the matched request and did not stop on unrelated plugin returns.

In PLUGIN43, the matched SystemApp send was owner `0xffffffee0c5418f0`, request `0x73ab0443c0`, msgID `0x40000000`, 516 bytes. The resolved destination/reply objects were `0xffffffee0c2bf280` / `0xffffffee0c5b6d70`; at runtime `0xfffffff02aa8f44c`, the destination-object-matched kmsg was stored to `ith_kmsg` on wrapper thread/TPIDR `0xffffffee0c13f830`. On that same thread, connection `0x73ad038780` reached the libxpc event-handler boundary at `0x1878a3a44` for event `0x73acc40000`. The registered callback was recovered through the connection's `+0x20` handler block and the block's `+0x10` invoke pointer: block `0x73acc5a2e0`, invoke `0x265bdf44c`. That invoke ran on the matched wrapper receiver thread shortly after the event-handler entry. This names the registered callback address and proves it ran for the matched wrapper event; it does not resolve a source symbol or, by itself, prove migration work or a reply.

The callback was followed by `WRAPPER_XPC_SEND` on the same wrapper thread/connection (`0x73ad038780`, message `0x73ad0184e0`). The send's decoded XPC fields were empty, so it is callback-to-send progression, not a reply-object match. A later DataMigrator `CLIENT_WRAPPER_MESSAGE_RAW` entry on receiver context `0xffffffee0c5418f0` carried identifier `com.apple.MobileInstallation.SystemAppMigrator` (message `0x73ab048700`, handler PC `0x265be20cc`). This is the first plugin-ID-attributed client handler in this capture, but its payload/result semantics are not decoded; do not call it the terminal migration result. Other later `migrationResult` sends and sync returns belonged to other plugin owners and are excluded.

The bounded, attached observation continued for about 294 seconds after the matched callback without a reply-object-matched send, reply-object `ith_kmsg` handoff to the matched DataMigrator waiter, or same-owner `CLIENT_SYNC_RETURN` / `0x1376c`. This is a bounded open-reply result, not evidence of a permanent hang or deadlock. The next cut is to decode the callback's outbound message and the DataMigrator `handleMessage` payload, then retain the resolved reply-object equality check through send, handoff, and same-owner return. No poster/draw work or display-model changes were made; D572/D582 remain QEMU hypotheses.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN43_WRAPPER_HANDLER.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN43_WRAPPER_HANDLER.events/`, and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN43_WRAPPER_HANDLER.{serial,stderr}.log`; PLUGIN42 coverage details are in its corresponding `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN42_WRAPPER_HANDLER.*` logs.

### PLUGIN44: matched callback send and client handler captured; semantics remain undecoded

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN44_WRAPPER_DECODE` used a fresh replay-D5825 child with the accepted `bp-patched-dev6/system.tc`, unchanged CLASS16/D572/D582 settings, and the wrapper callback/reply probe armed from boot. The matched SystemAppMigrator sync was owner `0xffffffde1b8b3060`, request `0x741b03c420`, msgID `0x40000000`, 516 bytes. Post-copyin resolved the DataMigrator send's destination object as `0xffffffdfe76c55d0` and reply object as `0xffffffdfe770bee0`. A destination-object-matched kmsg was stored to wrapper `ith_kmsg` at runtime `0xfffffff02aa8f44c` on receiver TPIDR `0xffffffde1b593060`.

On that same wrapper connection (`0x7c06c48780`) and receiver thread, the registered callback path issued `WRAPPER_XPC_SEND` for message `0x7c06c30180` at about `1791246874.095`. Its captured XPC fields were empty. Scanning the raw message as UTF-16 found `MergeBuddyProvisioningResponse.migrator`; no `migrationResult`, `checkNecessity`, or `SystemAppMigrator` string was present. The JSON's `destination_object` and `reply_object` fields are the original request's comparison candidates, **not** a resolved destination for this outbound message; therefore this capture does not establish equality to the reply object. The send is callback-associated by connection/thread, but its payload string does not identify a SystemApp terminal result. Full bytes and bounded pointer graph are preserved in `progress.MATCHED_WRAPPER_XPC_SEND_DECODE.001.json`.

About 0.10 seconds later, DataMigrator's `handleMessage` boundary (`0x272e320cc`) captured message `0x741ac3c600` on receiver context owned by the matched request's task, with the SystemAppMigrator identifier. Its raw body contains the literal fields `msgID` and `checkNecessity`, but no `migrationResult`; this classifies the observed handler message as the intermediate necessity check, not the terminal result. The raw 0x400-byte snapshot also contains pointer candidates at `+0x60` and `+0x70`, while the bounded graph walk only follows pointer-shaped values through `+0x38`; other nested payload semantics remain unknown. Plugin identity in receiver context alone does not make a message terminal.

The attached observation continued for a 600-second bound after the matched SystemApp sync window was established. The object-filtered probe had no `IPC_REPLY_DEST_COPYIN_CANDIDATE`, reply-object `ith_kmsg` handoff, or same-owner `CLIENT_SYNC_RETURN` / `0x1376c`. This is a bounded absence, not a hang/deadlock finding. The exact next cut is to decode the outbound message's nested objects (including the `+0x60/+0x70` candidates) and correlate its actual send destination through the kernel object-equality hooks; retain the same-owner return marker. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN44_WRAPPER_DECODE.lldb.log`, `.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN44_WRAPPER_DECODE.probe.out`, and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN44_WRAPPER_DECODE.{serial,stderr}.log`. The guest was left frozen; no panic was recorded. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

### PLUGIN46: nested callback payload and outbound Mach destination candidate

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN46_SEND_DEST` used a fresh child of the replay-backed D5825 parent, accepted `bp-patched-dev6/system.tc`, and the unchanged CLASS16/D572/D582/A407-scanout hypothesis setup. The child revalidated SystemAppMigrator request `0x785303cc60` on owner `0xffffffdfe8b260c0`, msgID `0x40000000`, size 516. Its destination/reply objects were `0xffffffde1b777790` / `0xffffffde1b7f4190`; the destination-matched kmsg reached wrapper `ith_kmsg` at `0xfffffff02aa8f44c` on wrapper TPIDR `0xffffffdfe7eaf830`.

On that wrapper thread and connection `0x75f0c4c640`, callback-associated XPC send `0x75f102c1e0` was captured at `t=1791249762.699411`. The bounded graph included the tagged `+0x60` candidate and object references at `+0x68` / `+0x70`; nested nodes contain `MSUDataMigrator` and a `msgID` key. The graph did not contain `MergeBuddyProvisioningResponse.migrator`, `SystemAppMigrator`, `migrationResult`, or `checkNecessity`. The XPC dictionary decoder still returns no fields, so this is nested-string/pointer evidence, not a fully decoded protocol object.

The first same-thread kernel send-helper sample followed about 62 ms later. Its `x0` kmsg `0xffffffe0cd75f200` has a coherent 88-byte header, Mach message ID `0x10000000`, destination `ipc_object*` `0xffffffde1b79ff70`, and null local object. Its compact body carries the `CPX@` envelope with `pid` and `msgID` keys. That destination is not the matched SystemApp reply object (`0xffffffde1b7f4190`), so this sample is not the terminal reply. The time/thread and payload-shape join makes it the leading kernel-send candidate for the callback XPC send, but no transaction identifier directly equates XPC pointer `0x75f102c1e0` to that kmsg; preserve that limitation. Later samples on the reused wrapper thread were other messages and are not attributed to this callback send. The normalized pair is preserved at `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN46_SEND_DEST.events/analysis.PLUGIN46_WRAPPER_XPC_TO_MACH_SEND_CANDIDATE.json`.

The 600-second post-request bound produced no object-equal send to the original reply object, no reply-object `ith_kmsg` handoff, and no same-owner `CLIENT_SYNC_RETURN` / `0x1376c`. This remains a bounded open-reply result, not a hang/deadlock claim. The next narrow edge is to join the observed outbound destination object `0xffffffde1b79ff70` to a receiving task's `ith_kmsg`, while keeping the original reply-object filter and same-owner return armed; do not promote same-thread later sends to this message. Tag artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN46_SEND_DEST.{lldb.log,events,probe.out,slide.json}` and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN46_SEND_DEST.{serial,stderr}.log`. The guest was left frozen with zero XNU panics. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

### PLUGIN47: matched outbound destination reaches DataMigrator `ith_kmsg`

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN47_DEST_PEER` used a fresh replay-D5825 child from `ui-class16-refresh60-dm-ls-parent-seed-d5825.qcow2`, accepted `system.tc`, and unchanged CLASS16/D572/D582 settings. It revalidated the SystemAppMigrator sync (request `0x7818c38660`, owner TPIDR `0xffffffe2b8698950`, msgID `0x40000000`) and its resolved destination/reply objects (`0xffffffe482ab4c40` / `0xffffffe48327f820`).

The matched wrapper callback ran on TPIDR `0xffffffe2b8624950`, connection `0x786ec60780`. About 22 ms after its XPC send, the same thread's send-helper sample produced kmsg `0xffffffe39d908a00`: size 88, Mach ID `0x10000000`, destination object `0xffffffe482aaec50`, and null local object. This candidate destination is not the SystemApp reply object. At `0xfffffff02aa8f44c`, the actual queue-removal store placed that same kmsg into `ith_kmsg` on a distinct DataMigrator thread, TPIDR `0xffffffe2b87ce0c0`; program label was `com.apple.datamigrator`. The receiver-side header matched destination object, message ID, size, and the entire captured body byte-for-byte. Thus the recipient of this outbound kmsg candidate is identified. The XPC-send-to-kmsg association remains a same-thread/connection, short-time, and payload-shape correlation, without a direct transaction ID.

The kernel reply-object send/handoff filters remained active through their configured 900-second SystemApp Mach window. `SYSTEMAPP_MACH_WINDOW_CLOSED` recorded its bounded closure; no send to `0xffffffe48327f820` or reply-object `ith_kmsg` handoff appeared in that window. The same-owner `CLIENT_SYNC_RETURN` / `0x1376c` marker remained armed until the manually requested stop about 935 seconds after the matched send and did not fire. This is not a hang/deadlock result and does not show whether the DataMigrator message advances the SystemApp result. Next is to follow the receiving DataMigrator thread from the proven `ith_kmsg` assignment into its userspace handler, while retaining the original reply-object and same-owner return filters. The probe reported re-freezing the guest at the bound with zero XNU panics, but the monitor socket refused a post-run HMP reconnect and the QEMU process was no longer available for inspection. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN47_DEST_PEER.{lldb.log,events,probe.out,slide.json}` and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN47_DEST_PEER.{serial,stderr}.log`.

### PLUGIN51: SystemApp callback's secondary DataMigrator message reaches `handleMessage:` as `checkNecessity`

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN51_DM_HANDLER` used a fresh child of the replay-backed D5825 lineage, the accepted `bp-patched-dev6/system.tc`, and the same CLASS16/D572/D582 hypothesis settings. The matched SystemAppMigrator sync was owner `0xffffffec9a4ba0c0`, request `0x7722c50720`, msgID `0x40000000`, 508-byte user request. Its call-time Mach tuple had task-local destination name `0x5b03` and reply receive name `0x2a13`; those names were not treated as cross-task identities.

The callback-context candidate carrying `com.apple.MobileInstallation.SystemAppMigrator` was on wrapper TPIDR `0xffffffec9a393830` (connection `0x7df8c78780`, event `0x7df9010000`, registered invoke `0x27a5c744c`). Its secondary send candidate was an 88-byte kmsg (`0xffffffe9e8ac8700`) with Mach ID `0x10000000`, destination object `0xffffffe9024e8220`, and null local object. At `0xfffffff02aa8f44c`, DataMigrator TPIDR `0xffffffec9a2cf060` received that exact kmsg: destination object, header, size, ID, and full message body matched. The event-to-kmsg association remains a same-thread callback candidate rather than a transaction-ID join; the kernel send-to-`ith_kmsg` delivery itself is exact.

On that receiving DataMigrator thread, `WRAPPER_XPC_RECEIVE` and `CLIENT_WRAPPER_MESSAGE` reached `handleMessage:` at `0x27a5ca0cc` with message `0x7722c54500`. The bounded body decode contains `msgID` and `checkNecessity`, with no `migrationResult` or SystemAppMigrator string. This is an intermediate necessity-check message, not the terminal result. Other 88-byte secondary notifications in the same child were also delivered to DataMigrator; they do not change this classification. A separate `migrationResult` XPC send belonged to another wrapper thread/plugin context and is excluded from this SystemApp request.

This child did not produce `IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS`, so it did not capture the original request's reply `ipc_object*`; the reply-object equality watch therefore could not score this secondary destination against the original reply object. The same-owner `CLIENT_SYNC_RETURN` marker was armed, but no SystemAppMigrator return was recorded before the capture was deliberately ended at a debug pause. That cutoff is not evidence of a hang or permanent absence of a reply. The first userspace handler is now named; the next missing edge is same-child resolution of reply name `0x2a13` to its kernel object (or the corrected post-copyin sample for this owner), then retaining object-equal reply-send/handoff and SystemApp-scoped return watches.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN51_DM_HANDLER.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN51_DM_HANDLER.events/`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN51_DM_HANDLER.{serial,stderr}.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin51-dm-handler.qcow2`. The VM was ended through HMP after the matched handler sample; no panic or guest failure was inferred. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

### PLUGIN52: post-copyin reply identity restored; terminal reply still open

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN52_REPLY_RESOLVE` used a fresh child of the replay-backed D5825 lineage with `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, unchanged CLASS16/D572/D582/A407-scanout settings, and the wrapper-reply/kernelpath probes armed from boot. This run fixes PLUGIN51's instrumentation miss: callback-send hooks had already populated `KERNEL_BREAKPOINTS`, while `_kernel_arm()` treated any nonempty map as proof that the `mach_msg2` owner prologue was installed. It now checks specifically for `KERNEL_MACH2_OWNER_PROLOGUE`, allowing the SystemApp owner hook to be installed even when unrelated callback breakpoints exist. The live `KERNEL_MACH2_OWNER_ENTRY` and post-copyin events in this child validate that fix.

The matched SystemAppMigrator user send was request `0x7b0ec34060`, owner TPIDR `0xffffffdff06dc180`, plugin `com.apple.MobileInstallation.SystemAppMigrator`, msgID `0x40000000`, 508-byte user message. Its task-local names were destination `0x7903` and reply `0x2603`; names are labels only. At runtime `0xfffffff02aaa3f54` (static `0xfffffff00aaa3f54`), same-owner post-copyin produced a coherent 516-byte kernel kmsg `0xffffffdff01c1100`. The user-send header matched the owner/request/message-ID capture, and the kmsg resolved destination/reply objects to `0xffffffde2390cdf0` / `0xffffffde2399af20`. `IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS` therefore fired in this child, closing the PLUGIN51 gap; the reply object was no longer unscored.

The send helper returned `x0=0` at `…3f54`. The same owner then resolved local reply name `0x4000` at `…4040` to object `0xffffffde2399af20` and entered `ipc_mqueue_receive` at `…4160` on that object. This proves the matched caller waited on the resolved reply right; the helper's zero return does not prove remote delivery or completion. The original request was independently stored to wrapper `ith_kmsg` at `…f44c` with destination-object equality. The wrapper's later 88-byte, msgID `0x10000000` send was handed to DataMigrator on a different object (`0xffffffde2390cd60`), and its `handleMessage:` payload was `msgID`/`checkNecessity`, not `migrationResult`; it is intermediate traffic, not the terminal response. A later 56-byte wrapper-thread kmsg (Mach ID `5403`) also targeted `0xffffffde238d8c40`, not the original reply object.

The continuous probe ran to its 1,200-second cap and left the guest frozen. It recorded no send/handoff to reply object `0xffffffde2399af20` and no same-owner SystemApp `CLIENT_SYNC_RETURN` / `0x1376c`. Other migrator return events are excluded by identifier/request; a reused TPIDR alone is not a match. This is a bounded no-reply observation with reply identity known, not a hang/deadlock finding. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN52_REPLY_RESOLVE.lldb.log`, `.events/`, `.probe.out`, `.slide.json`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN52_REPLY_RESOLVE.{serial,stderr}.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin52-reply-resolve.qcow2`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN52 freeze follow-up: requester remains on reply waitq; producer side narrowed

The still-running frozen child was sampled read-only through HMP/GDB. The matched owner thread `0xffffffdff06dc180` has `thread+0x28 = 0xffffffde2399af30`, equal to the reply object's `+0x10` waitq; the queue's receiver field at `0xffffffde2399af60` points back to the same owner thread. This corroborates that the caller remained associated with the resolved reply receive queue at the freeze. It says nothing about the remote service's current stack or whether it had attempted a reply.

The producer side is now narrowly identified at the protocol boundary: the service that received the original synchronous request, `com.apple.migrationpluginwrapper`, must provide the response that makes DataMigrator's `sendMessageSync` complete. The likely trigger is completion/result from the launched SystemAppMigrator work. The matched DataMigrator `DM_RUN_PLUGIN_CALL_13768` recorded completion block `0x16dab19e0`; no matched `DM_RUN_PLUGIN_RETURN_1376C` occurred. The wrapper's `checkNecessity` message (88-byte kmsg to `0xffffffde2390cd60`) is a separate side request, and its later 56-byte send to `0xffffffde238d8c40` also does not target the reply object. A `migrationResult` observed for another plugin is not evidence for this request. Thus the missing edge is **SystemAppMigrator completion/result → wrapper response on the original sync transaction → send to reply object `0xffffffde2399af20`**. That is the producer contract to test, not a finding that the wrapper or plugin failed or stalled at a particular internal site.

At the 1,200-second bound there was no object-equal send/handoff to the reply object or same-owner sync return. The freeze snapshot's saved wrapper receiver thread `0xffffffdff06f4180` had a different `thread+0x28` value (`0xffffffde238d2e10`), which was not readable in this HMP view; it cannot be used to name a wrapper wait or plugin dependency. The partial RAM thread scan contained unrelated work and is not attributed to this request. This remains a bounded no-reply observation, not a hang/deadlock conclusion. Snapshot files are `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN52_REPLY_RESOLVE_FREEZE.{threads.txt,ramscan.txt,postmortem.txt}`; the live frozen guest was read through `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN52_REPLY_RESOLVE.sock`.


#### PLUGIN52 same-child completion follow-up: debugger coverage failed; no completion verdict

A follow-up was attempted on the still-running PLUGIN52 QEMU child (PID 77364; monitor `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN52_REPLY_RESOLVE.sock`) through GDB port 1279. The matched event supplies the concrete block identity: SystemAppMigrator completion block `0x16dab19e0`, whose recorded invoke pointer is `0x102473bd8`; the plugin-call return is `0x10247376c`, the same-owner sync return is `0x260e99f8c`, and the reply object remains `0xffffffde2399af20`. These exact invoke/return and reply-object locations were armed in an attach follow-up, but the attach did not produce a continuous usable event window.

The reconnect encountered stops at kernel IPC hook PCs (`0xfffffff02aaa4040` and `0xfffffff02aaa3f54`). A broad follow-up stalled; a narrower temporary callback was then interrupted, and LLDB crashed in GDB-remote register unwinding (`GDBRemoteRegisterContext`). No new completion-invoke, completion-return, `0x1376c`, reply-object-send, or reply-object `ith_kmsg` event was recorded. This is a debugger coverage failure, **not** a bounded no-hit result for the plugin, and it does not support “never tried,” hang, or deadlock claims. The QEMU child remains available/paused, but its current attach state is not a clean observation window.

Next usable cut: a fresh replay-D5825 child with the completion invoke armed from the start-call event by reading the captured block's `+0x10` invoke pointer, filtering invocation on `x0 == that exact block`, and recording its return at the invocation LR. Keep the request's resolved reply-object send/handoff and same-owner `CLIENT_SYNC_RETURN` filters active through the same continuous window. Temporary attach scripts were under `/tmp/dm_plugin52_*_followup*`; they are not the durable probe. No display, model, or completion-emission behavior changed. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN54: dynamic completion hook covered the matched block; no invoke in 2,100 seconds

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN54_COMPLETION` used a fresh replay-D5825 child, accepted `system.tc`, and the fixed CLASS16/D572/D582 environment. At the matched `DM_RUN_PLUGIN_CALL_13768`, SystemAppMigrator request `0x7964c542a0` (owner TPIDR `0xffffffe94d53d120`) supplied completion block `0x16f8f59e0`; reading `block+0x10` yielded invoke `0x1005a3bd8`. The probe dynamically armed that address and accepted the hit only when `x0` equaled the exact block. LLDB's command-list proof confirms the callback was installed. The matching plugin return at `0x1005a376c` was armed per this call, with invoke owner tracked separately from call owner.

The same child's post-copyin event resolved the 516-byte msgID `0x40000000` request to destination object `0xffffffe5b67fe500` and reply object `0xffffffe5b6728fa0`; the same owner entered `ipc_mqueue_receive` on the reply object. The reply-object send/handoff watch and SystemApp-scoped return watch stayed active. At the 2,100-second matched window close there was no exact-block invoke, completion return, `0x1376c` plugin return, reply-object send/handoff, or same-owner sync return. LLDB did not crash or detach, and no instrumentation-stop event was recorded. Thus the useful result is **no completion invoke within this covered bound**. It narrows the next edge to whatever schedules the captured completion block, but does not show that invocation can never occur, explain why it had not occurred, or prove a hang/deadlock.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN54_COMPLETION.{lldb.log,events/,slide.json,watch.log}` and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin54-completion.qcow2`; serial/stderr are under `/tmp/dvm/probe/`. The replay seed is durable but not byte-identical to the lost original. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN55: static owner of the completion callback; response boundary hooks ready

Static analysis of the PLUGIN54-matched DataMigrator executable and the same build's `DataMigration.framework` narrows the immediate scheduler. In `/tmp/dvm/datamigrator-unslid.macho` (image base `0x100000000`), the `__DMPerformMigration` launch path builds block B on its stack at `sp+0x10`: the invoke field is signed at `0x1000136f4..0x100013700` to `0x100013bd8`, and the call at `0x100013768` passes B as `x5` to `runPluginAtPath:withIdentifier:parameters:completion:`. Thus B is a completion argument to the wrapper method, not a callback issued by the Mach receive path directly.

The framework implementation is `-[DMMigrationPluginWrapperConnection runPluginAtPath:withIdentifier:parameters:completion:]` at unslid `0x25e8f1a10`. Its request path calls the synchronous plugin-send helper at `0x25e8f1f88`; the return boundary is `0x25e8f1f8c`. After that response returns, the method accesses the `migrationResult` key (cstring at `0x25e8f77b7`, referenced at `0x25e8f1fa8..0x25e8f1fb4`) and later invokes its local result block through `blraa` at `0x25e8f2068` (`x22` is the local block and `x24` is the result argument). This makes the wrapper's synchronous response—and its `migrationResult` value—the necessary upstream event before the callback path can progress. Exact capture identity between that local block and this call's B still needs live confirmation; the new probe records B's invoke pointer at method entry, the sync-return result, and the local block/result invocation without stopping execution.

Once B itself is invoked, its body at `0x100013bd8` creates another block with invoke `0x100013cc8` and submits it using a two-argument queue/block call at `0x100013c94`, with the queue captured from `B+0x20`. The executable also creates a serial queue named `com.apple.datamigrator.pluginCompletionSerialQueue` at `0x100013468`; the exact identity between that created queue and the captured pointer has not been independently resolved. Either way, this queue submission is **downstream of B's invoke**; it is not the missing scheduler for the currently quiet B. The current evidence therefore places the first unresolved edge at the wrapper's synchronous plugin result/reply, before B invocation. The terminal SystemApp result that would satisfy that request remains the likely trigger, but the 2,100-second no-invoke window does not prove which side failed to produce it.

The durable `tools/re/dm_lock_checkin_wrapper_reply_callbacks.py` probe now has non-stopping hooks at the wrapper method entry, sync-return boundary, and local result-block invoke. It filters the DataMigrator-side method to `com.apple.MobileInstallation.SystemAppMigrator`, logs B and its `+0x10` invoke, response bytes/graph, and whether the local block graph contains B or its invoke. The existing dynamic exact-B invoke/return, reply-object send/handoff, and same-owner sync-return watches remain the close criteria. PLUGIN54's QEMU/LLDB had already exited by this analysis, so these new response-boundary hooks are **implemented but not boot-validated**; this is static call-graph progress, not a new live scheduler hit. Do not infer hang, permanent noncompletion, or firmware behavior. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

Static artifacts: `/tmp/datamigrator-otool.asm`, `/tmp/dm_run_plugin_disass_current.txt`, `/tmp/dm_handle_message_disass_current.txt`, and extracted framework `/tmp/dm-plugin-static/DataMigration`. The next live discriminator is same-call B at `0x13768` → wrapper sync return `0x25e8f1f8c` → local result callback `0x25e8f2068` → exact B invoke `*(B+0x10)`; retain the existing reply-object and same-owner return filters throughout.

#### PLUGIN56–57: wrapper response boundary live-validated, but not reached

On `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN56_LIVE_RESPONSE1`, a fresh replay child reached the SystemAppMigrator wrapper entry with B `0x16b8f99e0` / invoke `0x1046b7bd8`; the same DataMigrator owner resolved reply object `0xffffffe3aa8fd810` and entered `ipc_mqueue_receive` on it. The wrapper post-send/result hooks were armed, but the kernel object-matching window was only the default 600 seconds on this first attempt, so this run is preliminary for full reply-object coverage. LLDB stayed healthy and the 2,400-second boot cap had zero kernel panics.

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN57_SYNC2400` repeated the capture with the reply-object window explicitly extended to 2,700 seconds. For request `0x758b02cea0` / owner TPIDR `0xffffffdee58020c0`, the plugin call and wrapper method entry both carried B `0x16f6259e0`, whose `+0x10` invoke was `0x100873bd8`. Kernel post-copyin resolved reply object `0xffffffdfcb49baf0`; the same owner entered receive on it, and the corresponding request kmsg was handed to the wrapper. The object-equality send/handoff watch ran through the full 2,700-second interval and closed on its configured bound.

No hit occurred at the slid wrapper sync-return boundary `0x271f3df8c`, local result-block callback `0x271f3e068`, exact-B invoke `0x100873bd8`, reply-object send/`ith_kmsg`, or same-owner SystemApp return. The probe completed its 3,000-second cap with LLDB attached until the planned freeze and zero kernel panics. This is **A in-bound**: the wrapper sync response had not returned, so `migrationResult`, local-block-versus-B identity, and B invocation remain unscored. It narrows the missing event to the response path that would satisfy the known reply object, but does not name its producer and is not evidence of deadlock or permanent noncompletion. Earlier generic `CLIENT_SYNC_RETURN` samples for other plugin identifiers are excluded. The first run's 600-second kernel subwindow and the corrected 2,700-second run are kept distinct. Logs: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN56_LIVE_RESPONSE1.{lldb.log,events/,watch.log}`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN56_LIVE_RESPONSE1.{serial,stderr}.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN57_SYNC2400.{lldb.log,events/,watch.log}`, and matching probe logs. Child: `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin57-sync2400.qcow2`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN59: post-delivery wrapper thread and producer snapshot

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN59_PRODUCER` used a fresh replay-D5825 child with accepted `system.tc`, the same CLASS16/D572/D582 hypothesis settings, and a 900-second attached SystemApp window. Request `0x7bef040c60` / DataMigrator owner `0xffffffeaccaac950` formed B `0x16bd219e0`, whose dynamic invoke was `0x104203bd8`; resolved destination/reply objects were `0xffffffe9e62eefb0` / `0xffffffe9e617f790`. The SystemApp request kmsg was handed to `com.apple.migrationpluginwrapper` on TPIDR `0xffffffeacd4f18f0`; its registered callback at `0x26e19344c` ran on that same thread with the matched request handoff attached. This closes request delivery and callback execution for this child.

The saved process table labels that thread's process PID 162 as `com.apple.migrat` (truncated kernel process name); LLDB's per-event `progname` identifies it as `com.apple.migrationpluginwrapper`. The saved PC/LR `0x247579734` / `0x247579720` were initially mistaken for a Mach-trap location. Symbolication with this boot's dyld slide (`0xf8a4000`) identifies them as `___open_nocancel + 8` / `_open$NOCANCEL + 40` in `libsystem_kernel`, not a Mach send or receive. The captured call registers are `x0=0x16dd2707e`, `x1=0x01100000`, `x2=0`; translating `x0` through this process's saved 16-KiB page tables yields `/System/Library/AppPlaceholders/Tips.app`. In the local SDK, `x1` decodes as `O_DIRECTORY | O_CLOEXEC`. Thus the snapshot caught the callback thread opening the Tips placeholder directory while walking bundle metadata, not waiting on an IPC peer. Its frames support that operation: `___opendir2` → `__CFIterateDirectory` / bundle creation → `_MICreateCFBundleEnforcingInfoPlistSize` / `_MILoadInfoPlistWithError` → `MIGlobalConfiguration` bundle-ID map enumeration → `-[DMXPCConnection _handleMessage:]` → libxpc event handling. The captured state does not include the syscall result, so it does not establish whether the open was slow, blocked, or subsequently returned.

DataMigrator's matched owner was also present in PID 120 with reply-receive frames above the wrapper sync path (`0x26e195f8c → 0x10420376c`). During the covered interval there was no wrapper sync return, local result callback, exact-B invoke, reply-object send/`ith_kmsg`, or same-owner SystemApp return. The saved wrapper-thread operation is a directory open, so this snapshot has no Mach endpoint or peer to join at that point. The next useful cut is a fresh, continuously attached sample of this callback's `open$NOCANCEL` return and subsequent bundle-scan/result path, while retaining the known DM reply-object and same-owner return watches. The targeted scan found no installcoordinationd or installd candidate in this capture, which remains only a sampling limit. No claim of permanent noncompletion or firmware callback identity follows from this snapshot. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN59_PRODUCER.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN59_PRODUCER/events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN59_PRODUCER/process-stacks.json`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN59_PRODUCER/ram/`, and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN59_PRODUCER.{serial,stderr}.log`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN60: open-return probe did not reach the matched callback gate

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1` booted a fresh qcow2 child from `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825.qcow2` with accepted `system.tc` and the same CLASS16/D572/D582 knobs. LLDB attached from boot and kept the existing SystemApp call, resolved reply-object, wrapper delivery/callback, sync-return, and result hooks armed. The added `_open$NOCANCEL` observer is dynamically armed only after a positive matched SystemApp callback, records AppPlaceholders path/flags on that callback's TPIDR, and installs a same-thread return breakpoint at the captured LR.

At the 2,400-second bound, the child had no BKS check-in, DataMigrator migration entry/return, matched SystemApp sync call, or wrapper callback. The dynamic open observer therefore never armed: open return and post-scan/result path are **unscored**, and the PLUGIN59 Tips open remains unresolved. The guest continued emitting serial output, reached Early Boot but not shell, and had zero XNU panics. There were 51 APFS checksum-corruption reports, but this run does not establish that they caused the missing migration window. The final QEMU CPU PC was `0xfffffff02ab11eb8`; no causal symbol assignment is made. This is a boot-coverage miss, not a no-open/no-reply finding.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1.lldb.cmd`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1/launch.json`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1.serial.log`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN60_OPENRET1.stderr.log`, and `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin60-openret1.qcow2`. Next is to restore a child that reaches the matched SystemApp callback, then score open return → bundle-scan/result while retaining the original reply-object and same-owner return filters. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN61: corrected launch still missed the matched callback

This fresh child was created directly from `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825.qcow2`; its backing chain passed `qemu-img check` before and after the run. Comparison with PLUGIN59 identified the OPENRET1 launch delta: OPENRET1 lacked `-S` and used `-serial file:` instead of PLUGIN59's paused start and logged probe-UART chardev. PLUGIN61 restored those settings, used the same accepted `system.tc`, CLASS16/D572/D582 knobs and boot arguments, attached LLDB before continuing the guest, and installed the SystemApp/reply/open hooks first.

The run was stopped after about 11 minutes because no BKS check-in, DataMigrator entry, SystemApp `CLIENT_SYNC_CALL`, or wrapper callback had appeared. Serial had reached Early Boot. There were 131 lines matching explicit APFS corruption/checksum-error text, compared with 22 in PLUGIN59's captured serial log. The new child and launch setup are verified, but that APFS difference is only a coverage/lineage signal; it does not establish the cause. CPU0 was stopped at `0xfffffff00709a610` without assigning a symbol. Consequently, the callback-scoped Tips open hook never armed; there is no open-return or reply verdict.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN61_OPENRET2.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN61_OPENRET2.lldb.cmd`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN61_OPENRET2/launch.json`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN61_OPENRET2.events/`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN61_OPENRET2.serial.log`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN61_OPENRET2.stderr.log`, and `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin61-openret2.qcow2`. This remains a pre-request coverage miss, not an open/reply finding. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN63: replay2 lineage reaches DataMigrator, not the matched SystemApp callback

To restore a D5825-class backing chain without silently substituting another parent, I replayed the seed-progress stop from durable `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/disk.qcow2` and its sibling bootkc, trust cache, and device tree. `UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS2_REPLAY` reached the exact `LSD_SEED_SYNCWRITE_CALL` boundary after 1,061 registrations; the scan driver, system scan, and enumeration had returned. The seed-write call's return was not observed, so this is not labeled seed-complete or byte-identical to the lost checkpoint. Its durable copy is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-ccs-ls-seedprogress2-replay-20261006.qcow2` (SHA-256 `421d40e1d3c082036ae661dd57c232b00a31cf1cd51e713dbb00690a3e6c3c9d`).

I copied the old plugin4 launch-frozen delta to `/tmp/dvm/data-seed/ui-class16-refresh60-dm-plugin4-launchfrozen1-replay2.qcow2`, rebased that copy onto replay2, and created `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2`. The old D5825 parent path was not overwritten. `qemu-img info --backing-chain` shows the complete replay2 → durable boot1 chain, and `qemu-img check` passed for all new layers and the final child.

PLUGIN62 is excluded as a trust-cache setup failure: it accidentally used the generic fallback cache, producing a launchd `libSystem.B.dylib` load failure before DSC resolution. Corrected PLUGIN63 explicitly used `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, the restored PLUGIN59 paused-start/UART launch shape, and the unchanged CLASS16/D572/D582 stack. LLDB/probes were armed before guest continuation. By the configured 1,800-second bound, this child had reached Early Boot, DataMigrator migration/sync traffic, ContainerMigrator (which returned), and `00LaunchServicesMigrator`. The latest event remained LaunchServices. No SystemAppMigrator entry or registered callback, Tips open-return event, BKS check-in return, `DM_PERFORM_RETURN`, reply-object handoff, or same-owner SystemApp return was recorded. The callback-scoped open hook therefore did not arm. This is a bounded gate miss after partial migration progress, not a Tips-open, reply, or permanent-stall verdict; zero XNU panics were reported and LLDB stayed attached through the bound.

Using the same literal serial regex `obj_checksum_verify:7152:` on saved logs gives 880 lines in PLUGIN63, 45 in PLUGIN61, and 11 in PLUGIN59. Earlier broader corruption-line counts 131/22 use a different filter. The increase is a lineage/coverage signal, not causal proof. Logs: `/tmp/dvm/UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS2_REPLAY.{lldb.log,events/,probe.out,launch.json}`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS2_REPLAY.{serial,stderr}.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN63_RESEED_OPENRET1_TCFIX.{lldb.log,lldb.cmd,events/,probe.out,launch.json}`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN63_RESEED_OPENRET1_TCFIX.{serial,stderr}.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin63-reseed-openret1-tcfix.qcow2`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN64: replay2 passes the callback gate; Tips open remains unobserved

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN64_REPLAY2_OPENRET2` used child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin64-replay2-openret2.qcow2` backed by `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2`, accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, and the PLUGIN59 paused-start/probe-UART launch setup. The fixed hypothesis stack stayed in place (`A406_OBJECTS=1`, `A407_TRANSFER=1`, `A407_COMPLETE=d582`, `D572=1`, `A407_SCANOUT=1`); A408 completion/scanout remained off. Bound: 3,600 seconds.

This boot clears the prior LaunchServices gate: ContainerMigrator returned; matched `00LaunchServicesMigrator` sync returned at `1791318993.389`, about 1,412.8 seconds after its call; MSUDataMigrator returned; then SystemAppMigrator entered at `1791319003.956`. Its matched sync request was `0x7b950383c0` on owner TPIDR `0xffffffe7d5bcc180`. The registered wrapper callback ran at `1791319004.388` on receiver TPIDR `0xffffffe7d5891120` with the SystemAppMigrator identifier in the event. The callback-scoped `_open$NOCANCEL` observer was dynamically armed at `0x257839734`; neither `SYSTEMAPP_APPPLACEHOLDERS_OPEN_CALL` nor `SYSTEMAPP_APPPLACEHOLDERS_OPEN_RETURN` appeared through the bound. This is a gate-positive **no matching Tips/AppPlaceholders open observed** result, not an open failure or return verdict.

The same owner entered `ipc_mqueue_receive` on its reply path at `1791319004.079`, and a 516-byte msgID `0x40000000` request reached the wrapper's `ith_kmsg` handoff at `1791319004.306`. The object-equality Mach matcher unexpectedly expired on its 600-second default at `1791319603.987`; reply-object send/handoff evidence is limited to that initial interval and is not scored for the rest of the boot. The same-owner lifecycle hook recorded no SystemApp `CLIENT_SYNC_RETURN` / `0x1376c`, `DM_PERFORM_RETURN`, or BKS check-in return by the one-hour bound. The latest plugin lifecycle was MobileSlideShow's return at `1791319623.716`, not a SystemApp return. LLDB remained attached, there were zero XNU panics, the guest was re-frozen at the bound, and `qemu-img check` found no errors.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN64_REPLAY2_OPENRET2.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN64_REPLAY2_OPENRET2.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN64_REPLAY2_OPENRET2.watch.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN64_REPLAY2_OPENRET2/launch.json`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN64_REPLAY2_OPENRET2.{serial,stderr}.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin64-replay2-openret2.qcow2`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN65: callback-thread activity resumes after long Mach receives; no reply-object join

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN65_NEXTEDGE1` used a fresh child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin65-nextedge1.qcow2` backed by replay2 parent `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2`, accepted `system.tc`, and the same CLASS16/D572/D582 hypothesis setup. The probe was externally stopped at about 3,660 seconds after launch and left the guest paused; LLDB remained attached through the stop. Serial had zero XNU panics, and `qemu-img check -U` reported no errors.

The matched `SystemAppMigrator.migrator` call entered at `1791323386.443` on owner TPIDR `0xffffffeb3685e0c0`, with completion block `0x16b7a19e0` and invoke target `0x104783bd8`. The registered wrapper callback was observed at `1791323540.877` on TPIDR `0xffffffeb36c5e890`. The first post-callback operation captured on that TPIDR was a successful 80-byte Mach message (ID `0x10000000`, body fields `pid` and `msgID`) to task-local port `0x4203`. Later decoded RPC payloads included `getMigrationServiceWithReply:`, `cleanupModelForDataMigrationForRestoreType:reply:`, and `dataMigrationWillFinishWithReply:`. Two `mach_msg2` calls on this TPIDR returned `x0=0` after about 1,117 and 1,121 seconds; one was receive-only and the other used a send-and-receive vector. The callback context therefore showed extended waits followed by activity; it was not continuously parked at one observed wait PC.

A 68-byte Mach message with ID `0x20000000` and a `migrationResult` body was sent at `1791324270.801`; its trap returned `x0=0`. The recorded destination was task-local port `0x4303`, with no resolved kernel object. This boot emitted no `IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS`; consequently the original DM reply object was unavailable for the object-equality matcher. No same-owner `CLIENT_SYNC_RETURN`, SystemApp plugin return at `0x1376c`, completion-block invoke/return, `DM_PERFORM_RETURN`, or BKS check-in return was recorded through the run stop. The configured Mach observation window was 4,200 seconds, avoiding PLUGIN64's 600-second timeout; the actual post-entry observation ended with the run at about 2,145 seconds. No reply-object absence or hang conclusion follows without a resolved object.

The open observer recorded 14 calls on the matched TPIDR, but path reads were empty, flags were `0`, and the captured first argument was a small integer rather than a readable path. Raw return values included small positive values and `-1`; the syscall site/argument decode is therefore not validated. This does not classify Tips/AppPlaceholders behavior. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN65_NEXTEDGE1.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN65_NEXTEDGE1.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN65_NEXTEDGE1.watch.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN65_NEXTEDGE1/launch.json`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN65_NEXTEDGE1.{serial,stderr}.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin65-nextedge1.qcow2`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.


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

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN69_MIGRESULT_JOIN2` used a fresh child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin69-migresult-join2.qcow2` backed directly by `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2`. It used the accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, PLUGIN59's `-S`/probe-UART startup shape, the CLASS16/D572/D582 settings, and explicit `DARWIN_DCP_IOMFB_COMPLETE=0` / `DARWIN_DCP_IOMFB_SCANOUT=0`. The probe was imported before the first continue. The original PLUGIN67 launch manifest was unavailable, so this retry's launch settings were reconstructed from the saved replay2 manifest and PLUGIN67 startup record; no parent or fixture was substituted.

At the 3,600-second bound, the child had completed Early Boot but emitted no migration-probe events: no `DM_CLIENT_MIGRATE_ENTRY`, SystemApp `CLIENT_SYNC_CALL`, resolved request/reply pin, wrapper handoff, or `migrationResult` candidate. LLDB remained attached. The literal serial count for `obj_checksum_verify:7152:` was 1,184 versus 1,158 in PLUGIN68, then plateaued. This is the second consecutive pre-DM coverage miss with a small increase in that APFS signal, not an IPC result or causal APFS diagnosis. PLUGIN67 remains the last request-scoped result: its immutable reply pin held, its 939 callback-thread sends were unequal to that pin, and the other-owner `migrationResult` XPC events had no kernel-destination join.

Following the documented PLUGIN63 recovery shape, a new seed-progress replay was run from durable `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/disk.qcow2` with sibling `bootkc`, `system.dtree`, and accepted `system.tc`. `UI_CLASS16_REFRESH60_CCS_LS_SEEDPROGRESS3_REPLAY` used `ccs_ls_seed_progress_callbacks`, the `LSD_SEED_SYNCWRITE_CALL` stop, and an 1,800-second bound. It reached the `syncWrite:` call after 1,061 registration entries; `LSD_SCAN_DRIVER_RETURN`, `LSD_SYSTEM_SCAN_RETURN`, and `LSD_ENUM_RETURN` were recorded, but no `syncWrite:` return/seed-complete event was observed. This is a new replay checkpoint, not a byte-identical recovery and not proof the write completed. Seed `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-ccs-ls-seedprogress3-replay-20261006.qcow2` has SHA-256 `7d7603a6ee5c52dc6e16568a12360a0f86625edd08d28be3cc1ce45f38e8f9d7`; the `/tmp/dvm/data-seed/` copy has the same hash. A copied plugin4 layer was rebased onto this seed as `/tmp/dvm/data-seed/ui-class16-refresh60-dm-plugin4-launchfrozen1-replay3.qcow2`, then the copied D5825-class parent was rebased as `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay3.qcow2`. Replay2 parents were left untouched, and `qemu-img check` passed on the seed and both rebased layers.

#### PLUGIN70: replay3 validation still missed the request gate

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN70_MIGRESULT_JOIN3` used a fresh child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin70-migresult-join3.qcow2` from the new replay3 parent above, the accepted `system.tc`, and the same PLUGIN59 startup/CLASS16/D572/D582 settings. The current pinned-reply/migrationResult candidate probe was installed from reset. At the 3,600-second bound, Early Boot was complete but the event directory remained empty: no DM entry, SystemApp call, reply pin, wrapper delivery, or `migrationResult` sender candidate. LLDB stayed attached and the guest was host-paused; `qemu-img check -U` reported no errors. The literal `obj_checksum_verify:7152:` count was 1,150, lower than PLUGIN69's 1,184; therefore this run does not meet a further “rising APFS count” trigger. It still fails to validate the migrationResult-to-reply join, and it does not invalidate PLUGIN67's pin or 939 unequal callback-thread sends. Do not infer reply/no-reply, hang, or deadlock from this pre-request miss.

Artifacts: PLUGIN69 launch manifest `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN69_MIGRESULT_JOIN2.launch.json`, LLDB log `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN69_MIGRESULT_JOIN2.lldb.log`, events `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN69_MIGRESULT_JOIN2.events/`, serial `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN69_MIGRESULT_JOIN2.serial.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin69-migresult-join2.qcow2`; seed3 and PLUGIN70 artifacts are under the paths stated above and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN70_MIGRESULT_JOIN3.*`. No model, fixture, D572/D582 hypothesis, or completion setting changed. Poster/draw remain parked.


#### PLUGIN71: coverage-light replay3 A/B still misses the DM gate

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN71_GATE_LIGHT_A1` is the coverage-light arm against PLUGIN70's full-probe control. It used fresh child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin71-gate-light-a1.qcow2`, backed by `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay3.qcow2`, accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, and PLUGIN70's same boot args, CLASS16/D572/D582 settings, and `DARWIN_DCP_IOMFB_ECHO_MODE=1`. Only two auto-continue LLDB callbacks were armed initially: `DM_CLIENT_MIGRATE_ENTRY` at runtime `0x25f040af8` and DataMigrator `CLIENT_RUN_ENTRY` at `0x25f03da10`; the sync call/return sites would have been added only after a SystemAppMigrator path match. Callback proof and the `ready.gate-light.json` event were present. The dynamic SystemApp hooks never armed because no matching run entry occurred.

The full-probe PLUGIN70 and this lean child both ran to the 3,600-second bound without `DM_CLIENT_MIGRATE_ENTRY`, SystemApp `CLIENT_RUN_ENTRY`/`CLIENT_SYNC_CALL`, request pin, wrapper handoff, or `migrationResult` event. PLUGIN71 had no panic/reboot and LLDB remained attached; its only event was probe readiness. Serial contained 47 launchd records, matching PLUGIN70's normalized launchd sequence. The final launchd records were the same missing WebBookmarks and webinspectord executable messages; after those, the serial tail was repeated IOMFB power-status traffic without another launchd milestone. APFS `obj_checksum_verify:7152:` count ended at 1,216 (PLUGIN70: 1,150; PLUGIN69: 1,184; request-positive PLUGIN64: 1,080 and PLUGIN67: 1,253). These counts overlap and do not identify a cause. The A/B therefore does not support broad reply-probe tax as the explanation for the pre-DM miss; it does not prove an image failure or explain which launch/job prerequisite is absent.

Launch manifests for PLUGIN70 and PLUGIN71 match in TC, bootkc, dtree, ramdisk, boot args, QEMU/fixture arguments, and hypothesis environment; only tag-derived paths and GDB port differ. PLUGIN64's manifest omitted `DARWIN_DCP_IOMFB_ECHO_MODE`, but PLUGIN67's saved stderr explicitly reports “A413-to-A442 block-0x41 mode echo enabled,” and PLUGIN67 is request-positive. Thus ECHO mode is not unique to the misses. PLUGIN67's `launch.json` itself is unavailable, so do not claim a byte-for-byte manifest comparison to it. The meaningful remaining setup delta is the replay3 backing lineage: seedprogress3 was captured at `LSD_SEED_SYNCWRITE_CALL` without observing that call return, then rebased as D5825 replay3; PLUGIN67 used replay2.

This is still a DM-gate/lineage finding only. The migrationResult→pinned-reply join was not exercised and PLUGIN67 remains the latest request-scoped IPC evidence. Next discriminating run should be a **coverage-light child of the existing request-positive PLUGIN67 overlay** (not another replay3 child): if it re-enters DM/SystemApp, arm the pinned-reply join only after the gate; if it misses, compare that boot's first launchd/job divergence before another seed replay. Do not infer reply, hang, or deadlock from PLUGIN68–71.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN71_GATE_LIGHT_A1.launch.json`, `.launch.sh`, `.lldb.cmd`, `.lldb.log`, `.events/`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN71_GATE_LIGHT_A1.serial.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin71-gate-light-a1.qcow2`. The guest was host-paused after the bound; `qemu-img check -U` found no errors. The new probe `tools/re/dm_gate_light_callbacks.py` passed `py_compile`; launch-script syntax and `git diff --check` passed. D572/D582 remain QEMU hypotheses; poster/draw remain parked.


#### PLUGIN72: PLUGIN67-overlay child still missed the DM gate; reply join was never armed

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN72_GATE_LIGHT_JOIN1` used a fresh qcow2 child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin72-gate-light-join1.qcow2` backed directly by the request-positive `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin67-reply-pin1.qcow2`. `qemu-img info --backing-chain` verified the chain through replay2 D5825/plugin4 and durable seedprogress2; the source overlay passed `qemu-img check -U` before boot. The child also passed `qemu-img check -U` after the run. It used accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, PLUGIN59's `-S`/probe-UART launch shape reconstructed from PLUGIN71, CLASS16/D572/D582/ECHO settings, and A408 completion/scanout off. PLUGIN67's exact launch manifest remains unavailable, so this is a direct PLUGIN67-disk child with the documented launch settings, not a byte-for-byte launch-manifest replay.

Only the two coverage-light callbacks were present at reset: backboardd `DM_CLIENT_MIGRATE_ENTRY` (`0x25f040af8`) and DataMigrator `CLIENT_RUN_ENTRY` (`0x25f03da10`). `DVM_GATE_LIGHT_ARM_REPLY_AFTER_SYSTEMAPP=1` requested deferred activation. `ready.gate-light.json` confirms only those two initial breakpoints. The reply/object matcher was designed to import/install at the exact `SystemAppMigrator` `CLIENT_RUN_ENTRY`, after the identifier gate and before its sync call. Since that gate did not fire, the matcher did not arm and its boot behavior remains unvalidated.

The child ran for just over 3,600 seconds with LLDB attached and QEMU running, then was paused through HMP. The event directory contains only `ready.gate-light.json`: no `DM_CLIENT_MIGRATE_ENTRY`, SystemApp path/sync entry, reply pin, wrapper handoff, or `migrationResult` event. Serial contains no SpringBoard/DataMigrator/SystemApp marker and no XNU panic/reboot. `obj_checksum_verify:7152:` occurred 159 times; this is a lineage/coverage observation, not a cause.

The normalized launchd stream reached 47 records and the same two missing-executable jobs seen in request-positive PLUGIN67 and replay3 PLUGIN71, followed by IOMFB power/RPC output. The first visible post-Early-Boot divergence is their order and timing: PLUGIN67 logged WebBookmarks at +150 s then webinspectord at +158 s; PLUGIN71 logged them at +220 s and +241 s; PLUGIN72 logged webinspectord at +169 s, but WebBookmarks only at +1,960 s. Both jobs are present in PLUGIN72 (the WebBookmarks line is interleaved with IOMFB serial text), so this is a timing/order divergence rather than proof a job was absent. This direct child of the PLUGIN67 overlay weakens replay3 seed lineage as the sole explanation for recent gate misses, but the delayed launchd job, lower checksum count (159 versus PLUGIN67 1,253 and PLUGIN71 1,216), and missing PLUGIN67 launch manifest leave overlay-state versus host/runtime drift unresolved. Do not infer an IPC result or start another seed replay from this evidence alone.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN72_GATE_LIGHT_JOIN1.{launch.json,launch.sh,lldb.cmd,lldb.log,stderr.log}`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN72_GATE_LIGHT_JOIN1.events/`, serial `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN72_GATE_LIGHT_JOIN1.serial.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin72-gate-light-join1.qcow2`. The paused VM and child are preserved. `dm_gate_light_callbacks.py` and related wrapper/kernel probes passed `py_compile`; launch syntax and `git diff --check` passed; `qemu-img check -U` reported no errors. PLUGIN67 remains the last request-scoped reply-pin result. No reply/no-reply, hang, or deadlock conclusion follows from PLUGIN72; D572/D582 remain QEMU hypotheses and poster/draw remain parked.

#### PLUGIN73: replay2 pre-request control also misses the migration gate

To separate a PLUGIN67-overlay cold-boot epoch from a pre-request boot problem, `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN73_REPLAY2_GATE_LIGHT_JOIN2` booted a fresh child directly from the underlying replay2 D5825 parent, `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2`; it did not use the PLUGIN67 overlay or replay3 and did not reseed. The child is `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin73-replay2-gate-light-join2.qcow2`. The parent chain through replay2/plugin4 and durable seedprogress2 was verified with `qemu-img info --backing-chain`; parent and child passed `qemu-img check -U` before launch and the child passed again after the stop.

The launch reconstructed PLUGIN72's saved launch shape because PLUGIN67's manifest is absent: accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, `-S` and probe-UART, the same CLASS16/D572/D582/ECHO settings, and A408 completion/scanout off. The coverage-light probe was imported before continue and installed only `DM_CLIENT_MIGRATE_ENTRY` (`0x25f040af8`) and DataMigrator `CLIENT_RUN_ENTRY` (`0x25f03da10`). `DVM_GATE_LIGHT_ARM_REPLY_AFTER_SYSTEMAPP=1` requested the pinned matcher only after a positive SystemAppMigrator entry. It never armed: the sole event through the stop was `ready.gate-light.json`. There was no DM migration entry, SystemApp run/sync entry, reply pin, wrapper handoff, or `migrationResult` candidate. Thus the deferred join remains unvalidated and there is no IPC result.

After Early Boot at guest serial time `12:12:48`, launchd logged WebBookmarks at `12:15:15` (+147 s) and webinspectord at `12:15:17` (+149 s), then remained at 47 normalized launchd records through the observation window. This order/timing resembles PLUGIN67's early pair (+150/+158 s) and PLUGIN71 (+220/+241 s), unlike PLUGIN72's webinspectord-first / WebBookmarks-at-+1,960 s sequence. The serial tail continued IOMFB power-status traffic, but launchd time did not advance past `12:15:17`; no new service/job identity was captured. The first observable divergence from request-positive PLUGIN67 is therefore after the shared 47-record launchd prefix: PLUGIN67's event stream later entered DataMigrator and SystemAppMigrator, while this child never hit the migration callbacks. The gate-light probes do not identify the service or dependency responsible for that gap.

The QEMU remained running and LLDB attached for about 1 h 52 min before HMP `stop`; no panic or critical-process reboot appeared. The literal `obj_checksum_verify:7152:` count was 1,323 and had plateaued. This is descriptive, not causal. PLUGIN72's PLUGIN67-overlay child and this replay2-parent control both missed the gate, so a post-request PLUGIN67 disk epoch may contribute to its cold-child miss but is not a sufficient explanation for all misses. Since PLUGIN64 previously reached SystemApp from replay2, the present result is a nondeterministic/host-or-boot progression discrepancy after the observed common launchd prefix; no specific missing job is proven. Do not infer that the reply join failed, or that migration can never run. No seed replay is justified by this result alone.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN73_REPLAY2_GATE_LIGHT_JOIN2.{launch.json,launch.sh,lldb.cmd,qemu.out}`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN73_REPLAY2_GATE_LIGHT_JOIN2.events/`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN73_REPLAY2_GATE_LIGHT_JOIN2.serial.log`, and `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin73-replay2-gate-light-join2.qcow2`. The current deferred matcher was not invoked. Gate/light and wrapper-reply probes pass `py_compile`, the launch script passes `bash -n`, and `git diff --check`/`qemu-img check -U` pass. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN73 frozen-process cut: DataMigrator is absent at the miss snapshot

After the gate-light bound, the same PLUGIN73 VM was left paused and inspected read-only through HMP. A complete 12-GiB physical RAM dump covered `0x10000000000` through `0x102fffffffff` in `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN73_REPLAY2_GATE_LIGHT_JOIN2.ram/`; `warm_boot_postmortem.py` translated saved thread state against that still-paused VM. The process snapshot identifies SpringBoard PID 35, backboardd PID 72, launchd PID 1, installd PID 187, installcoordinationd (`p_comm` `installcoordinat`) PID 335, and `com.apple.migrationpluginwrapper` PID 198 (confirmed from its in-task dyld program-name pointer). No `DataMigrator`, `DataMigrat`, `datamigrator`, or `com.apple.DataM` process-name candidate was found. This supports that `com.apple.datamigrator` was not resident at the snapshot; absence at one instant cannot distinguish “never launched” from an earlier exit.

The saved stacks do not identify a DataMigrator/check-in handler. Four launchd threads share wait event `0xfffffff027011654` and frames through `0x1c42fef50` and `0x100f99740`; this wait has no proven DataMigrator or migration-job association and is not a deadlock finding. backboardd and SpringBoard were alive, as were the wrapper, installd, and installcoordinationd, so the first missing observable edge remains DataMigrator launch/activation and its expected backboardd `DM_CLIENT_MIGRATE_ENTRY` (`0x25f040af8`), not an earlier launchd-prefix record. PLUGIN64/67 provide the positive contrast: DM entry at `1791317538.519` / `1791333533.657`, followed by SystemAppMigrator at `1791319003.952` / `1791334991.787`. PLUGIN73 emitted neither gate event after the shared WebBookmarks→webinspectord prefix. The trigger that should launch/activate DataMigrator remains unnamed.

This closes only process presence at the snapshot: it does not prove launchd never attempted a spawn or that DataMigrator exited. The next useful retry is one replay2 child with the existing two gate-light callbacks plus a narrow launchd process-launch/exec observation filtered to the DataMigrator executable; retain `DM_CLIENT_MIGRATE_ENTRY` and `CLIENT_RUN_ENTRY` as gates and defer the reply matcher until SystemApp. The launch-attempt hook must be implemented and validated before that retry; another gate-only long boot would not discriminate this edge. The PLUGIN73 guest remains paused, and no guest state was written during inspection.

#### PLUGIN74–75: replay2 again reaches SystemApp; deferred join setup needed live-call seeding

Both fresh children used the unchanged replay2 parent, accepted `system.tc`, and the same paused-start/UART/CLASS16/D572/D582 launch shape. The launchd spawn observer was present from boot. PLUGIN74 reached backboardd `DM_CLIENT_MIGRATE_ENTRY` at `1791409850.335`, then SystemApp `CLIENT_RUN_ENTRY` at `1791410465.256` and `CLIENT_SYNC_CALL` at `1791410465.339`. The deferred wrapper-reply callback then raised `NameError: handoff_match is not defined` (and an adjacent path referenced an unset `connection`); LLDB coverage ended there, so no request-object result was scored. PLUGIN75 corrected those names and again hit DM entry, SystemApp entry, and sync call (`1791410667.866`, `1791411459.139`, and `1791411459.235`). The callback filter was extended to include SystemAppMigrator, but the dynamic lifecycle breakpoint had been installed at `CLIENT_RUN_ENTRY` only after that entry was already executing. Its owner context was consequently absent from the composed lifecycle probe, so `CLIENT_SYNC_CALL` did not arm the post-copyin resolver. PLUGIN75's wrapper XPC candidates are intermediate and do not resolve the original reply object.

These are positive gate retries, not replies or no-reply findings. The current gate-light code now seeds the already-live SystemApp owner/path/receiver/completion into the deferred transport tables after probe installation, so the upcoming sync callback can arm the kernel object resolver. The fresh validation run is PLUGIN76, tag `UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN76_REPLAY2_REPLYJOIN_SEED1`, a child of the same replay2 parent. Its decisive event is still `IPC_RESOLVED_SYSTEMAPP_SEND_OBJECTS`; only that call-time event may pin a reply object, after which object-equal sends and same-owner sync return are scored. PLUGIN74/75 do not establish reply equality, timeout, hang, or deadlock. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN78: replay2 gate restored; activation trigger remains unnamed; migrationResult destinations differ from the pinned reply

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN78_REPLAY2_REPLYJOIN_SEED3` used a fresh child of the existing replay2 D5825 parent (`/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin78-replay2-replyjoin-seed3.qcow2`), accepted `system.tc`, and the established CLASS16/D572/D582/ECHO launch shape. No reseed or model change was made. This targeted follow-up to PLUGIN73 reached backboardd `DM_CLIENT_MIGRATE_ENTRY` at `1791413668.129`, then DataMigrator's SystemAppMigrator entry at `1791414383.147` and `CLIENT_SYNC_CALL` at `1791414383.235`, about 715 seconds after DM entry. The shared 47-record launchd prefix was not a sufficient prerequisite to prevent the gate: a same-lineage child passed the later DM/SystemApp edge.

The launchd observer recorded ordinary `posix_spawn`/`posix_spawnp` samples, ending its generic sample set at `/usr/libexec/xpcroleaccountd`, but no DataMigrator-specific spawn/exec event before the positive gate. This does not name the activation trigger: DataMigrator may have started through another mechanism or outside the sampled call path. PLUGIN73's frozen process list supports only that DataMigrator was not resident at that snapshot. It does not prove launchd never attempted a spawn, and the four launchd wait stacks remain unassociated. The first missing observable edge on PLUGIN73 remains DataMigrator activation/DM migration entry after the common prefix; the upstream cause is still unknown rather than a named absent service.

The deferred matcher validated on the positive gate. SystemApp request `0x77010487e0`, owner TPIDR `0xffffffe02aaf6890`, resolved destination object `0xffffffe02adff3a0` and original reply object `0xffffffe02afabf70`. The 516-byte msgID `0x40000000` request reached `com.apple.migrationpluginwrapper` with destination-object equality. The DataMigrator owner then entered `ipc_mqueue_receive` on the pinned reply object.

Three captured 76-byte, msgID `0x20000000` kernel sends contained a `migrationResult` body. XPC-to-kernel candidate links use sender TPIDR, a short time interval, and matching body key; there is no transaction identifier. The destinations were `0xffffffe02cadbd30` (sender `0xffffffe02c5a0180`, XPC/send at `1791414401.671` / `1791414401.720`), `0xffffffe02ba38cd0` (`0xffffffe02af1e0c0`, `1791414406.897` / `1791414406.913`), and `0xffffffe02c55afb0` (`0xffffffe02c5ca0c0`, `1791414415.341` / `1791414415.361`). All differ from original reply object `0xffffffe02afabf70`; none closes the SystemApp sync.

The guest is paused at a debugger stop before the configured 4,800-second bound. Through the last recorded wrapper event at `1791414528.929`, no send/handoff to the pinned reply, same-owner `CLIENT_SYNC_RETURN`/`0x1376c`, plugin return, or `DM_PERFORM_RETURN` was recorded. This is a bounded partial window, not a no-reply, hang, or deadlock verdict. PLUGIN78 establishes a positive replay2 gate and the unequal destinations above; it does not explain why PLUGIN73 missed DataMigrator. The next discriminator is a reliable launchd/service activation trace that records the actual DataMigrator start mechanism, not another unguided gate boot.

Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN78_REPLAY2_REPLYJOIN_SEED3.{launch.json,launch.sh,lldb.cmd,lldb.log,watch.log}`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN78_REPLAY2_REPLYJOIN_SEED3.events/`, serial `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN78_REPLAY2_REPLYJOIN_SEED3.serial.log`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin78-replay2-replyjoin-seed3.qcow2`. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN79: launchd XPC-service activation path observed on a gate-positive child

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN79_ACTIVATION1` used a fresh child of the existing replay2 parent, with the same accepted `system.tc`, paused-start/UART launch shape, CLASS16/D572/D582 settings, and A408 completion/scanout disabled. No seed-progress replay or model change was made. The observer was armed at slide `0x1cf9c000`; its `posix_spawn`, `posix_spawnp`, `execve`, DM-entry, and SystemApp-entry callback proofs were live. Unlike PLUGIN78's path-only observer, this version read bounded spawn `argv`/matching environment fields and retained caller PCs.

At `1791418712.016007`, launchd PID 1 called `posix_spawnp` for `/usr/libexec/xpcproxy` with `argv=["xpcproxy", "com.apple.datamigrator"]`; the nested `posix_spawn` for the same path/label followed at `1791418712.024323`. Both returned `x0=0` at `1791418712.072183` / `1791418712.067056` and wrote child PID 122. The launchd caller stack for the outer call began `0x1027a8598 → 0x1cb86efc0 → 0x1cb859700` (the nested spawn began `0x1027a14dc → 0x1027a8598`). This establishes a launchd-managed XPC service activation request, not merely an executable-path guess.

At `1791418712.203852`, the resulting `xpcproxy` task called `posix_spawn` for `/System/Library/PrivateFrameworks/DataMigration.framework/XPCServices/com.apple.datamigrator.xpc/com.apple.datamigrator`; its argv was that executable path and its environment included `XPC_SERVICE_NAME=com.apple.datamigrator`. The caller PCs were `0x102e40cbc → 0x19d4235b8`. The observer was stopped on backboardd `DM_CLIENT_MIGRATE_ENTRY` at `1791418755.717777`, about 43.5 seconds after the service-spawn sequence. No callback record for the inner xpcproxy spawn's return was present before that condition stop, so that inner spawn's completion and DataMigrator's own `CLIENT_RUN_ENTRY` remain unscored. The positive trace names the activation chain and job/service label on a hit; it does not establish why PLUGIN73 did not reach the gate.

For a miss boot, the remaining discriminator is whether this `com.apple.datamigrator` xpcproxy request is absent, or present but the inner spawn fails/does not reach the service entry. PLUGIN73's late frozen process table only says DataMigrator was not resident at that instant; it cannot answer that earlier question. No miss-path failure, hang, or reply result is claimed. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN79_ACTIVATION1.{lldb.log,slide.json,watch.log}`, its `.events/`, serial/stderr under `/tmp/dvm/probe/`, and child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin79-activation1.qcow2`. `py_compile`, setup-script syntax, `git diff --check`, and `qemu-img check -U` passed. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN80: replay2 activation chain reaches SystemApp; inner spawn return remains unscored

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN80_ACTIVATION2` was a fresh child of the unchanged replay2 D5825 parent, using the accepted `system.tc`, the PLUGIN59 launch shape, CLASS16/D572/D582, and the coverage-light activation probe from boot. The child reproduced the PLUGIN79 service-demand path: launchd issued `posix_spawnp` for `/usr/libexec/xpcproxy` with argv `xpcproxy com.apple.datamigrator` at `1791420606.823433`; its nested `posix_spawn` returned `x0=0` and child PID 121 at `1791420606.878711`. At `1791420607.005887`, that xpcproxy called `posix_spawn` for `/System/Library/PrivateFrameworks/DataMigration.framework/XPCServices/com.apple.datamigrator.xpc/com.apple.datamigrator`, with `XPC_SERVICE_NAME=com.apple.datamigrator`.

The dynamically armed inner-spawn return breakpoint at `0x102fad944` (LLDB BP 8) did not emit a return event. Its callback currently requires the same LLDB thread ID and TPIDR as the call and silently ignores mismatches, so the missing event is not evidence that the spawn failed or remained blocked. The later DataMigrator client entry establishes that the service path progressed far enough for the `com.apple.datamigrator` process to run: backboardd `DM_CLIENT_MIGRATE_ENTRY` fired at `1791420645.138169`, and SystemAppMigrator `CLIENT_RUN_ENTRY` fired at `1791421586.061417` (path `/System/Library/DataClassMigrators/SystemAppMigrator.migrator`, completion block `0x16ff259e0`). The second gate arrived about 940.9 seconds after the backboardd entry; `GATE_ACTIVATION_CHAIN_COMPLETE` stopped the child with both required entries observed.

This is a positive activation control, not a miss-side classification. It proves the hit-side chain through the SystemApp run entry on this child, but does not identify which stage failed on PLUGIN73. It also leaves the inner spawn return itself unscored; the next useful improvement is to report return-breakpoint hits with actual-vs-expected thread/TPIDR before filtering, then compare the full chain on a covered miss. No reply matcher was armed, and no reply, hang, or deadlock result follows. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN80_ACTIVATION2.*`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN80_ACTIVATION2.events/`, and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN80_ACTIVATION2.serial.log`. `qemu-img check -U` found no image errors. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN81–82: two full-bound replay2 hits; no miss-side stage classification

Both fresh children used the unchanged replay2 D5825 parent, accepted `system.tc`, and the established CLASS16/D572/D582/ECHO launch profile with the gate-light observer. Each ran for the requested 6,720-second bound; neither reproduced a pre-DM miss. PLUGIN81 (`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN81_ACTIVATION3`) recorded launchd's `xpcproxy com.apple.datamigrator` request and a successful outer return (`x0=0`, child PID 153), then the framework service spawn at `1791423533.587`. It reached backboardd `DM_CLIENT_MIGRATE_ENTRY` at `1791423572.554` and SystemAppMigrator `CLIENT_RUN_ENTRY` at `1791424328.826` (about 756 seconds after DM entry), plus `CLIENT_SYNC_CALL`. The inner framework-spawn return was not captured by the first diagnostic revision.

PLUGIN82 (`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN82_ACTIVATION4`) reached the same gates: launchd requested `xpcproxy com.apple.datamigrator` at `1791430866.743`; its return matched saved SP and process context despite an LLDB thread-ID change, returning `x0=0` and child PID 122. The xpcproxy framework-executable spawn followed at `1791430866.932` with `XPC_SERVICE_NAME=com.apple.datamigrator`; backboardd migration entry arrived at `1791430905.169`, and SystemAppMigrator `CLIENT_RUN_ENTRY` at `1791431716.733` (about 812 seconds later). The run completed its full bound with zero panics and no shell. The shared inner-return PC emitted unrelated candidates, including a `fairplayd.H2` hit and later wrapper-service traffic; their saved SP/TPIDR did not match the captured xpcproxy call. These are visible nonmatches, not evidence of inner-spawn failure. The probe did not record a matching inner return in either run.

Across PLUGIN81 and PLUGIN82, the observed result is **two bounded hits, zero misses**. PLUGIN73's first absent activation stage therefore remains unknown. No reply matcher ran, and these runs establish no reply/no-reply, hang, or deadlock result. After PLUGIN82, the return matcher was tightened to use saved SP plus TPIDR as the call-context key; LLDB thread ID and process name remain diagnostics because the latter can be null at a shared return PC. That source revision has not yet been boot-validated. Artifacts are under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN81_ACTIVATION3.*` and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN82_ACTIVATION4.*`; P82's child passed `qemu-img check -U`. No seed, model, fixture, A408, or completion changes were made. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN86: outer spawn return matched; inner return remains unscored; pinned reply join completed for 7,200 seconds

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN86_ACT_RETURN_REPLY4` was a fresh child of the unchanged replay2 D5825 parent, using the accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, the established CLASS16/D572/D582/ECHO launch shape, and current gate-light plus deferred reply-join callbacks from boot. It reached `DM_CLIENT_MIGRATE_ENTRY` and SystemAppMigrator `CLIENT_RUN_ENTRY` (`1791472684.813025`), followed by the matched `CLIENT_SYNC_CALL` at `1791472684.901463` on owner TPIDR `0xffffffe2e53f3060`, request `0x7c5ec408a0`. The deferred join armed from that same live SystemApp entry and did not reuse any prior boot's port object.

The SP+TPIDR return matcher positively matched the launchd outer `xpcproxy com.apple.datamigrator` spawn returns, including `x0=0` and child PID 122; one return crossed an LLDB thread-ID change, confirming that thread ID is diagnostic rather than part of the match key. The inner DataMigration framework spawn call was captured at `1791472011.1233659`, targeting `.../XPCServices/com.apple.datamigrator.xpc/com.apple.datamigrator`, with `XPC_SERVICE_NAME=com.apple.datamigrator`, saved SP `0x16b1db040`, and TPIDR `0xffffffe2e56a5120`. Its shared return PC emitted a configd candidate at `1791477263.866025`, but actual SP `0x107386460` and TPIDR `0xffffffe2e52a3060` matched neither saved value. Three other candidates belonged to a distinct spawn context and likewise failed that context's saved SP/TPIDR comparison. P86 validates outer-return matching and exposes inner shared-PC candidates, but does **not** validate an inner framework-spawn return or show that spawn failed.

For the matched SystemApp request, copied-in send objects resolved to destination `0xffffffe200854220` and pinned reply `0xffffffe2004fbaf0`. The original 516-byte msgID `0x40000000` request was handed to `com.apple.migrationpluginwrapper` at `ith_kmsg` with destination-object equality. During the complete configured Mach window, from the matched sync call through `SYSTEMAPP_MACH_WINDOW_CLOSED` (`window_seconds=7200.0`, close time `1791479884.935322`), the log recorded 3,340 SystemApp Mach-transport samples and 20 resolved kernel-send candidates. Five candidate kmsgs contained a `migrationResult` body; their destination objects, in time order, were `0xffffffe2004a3a60`, `0xffffffe200726d70`, `0xffffffe2004a3a60`, `0xffffffe200726d70`, and `0xffffffe200842080`. Every destination compared unequal to the pinned reply. The XPC-to-kernel association remains based on sender/window/body evidence without a transaction ID, so these are observed candidate sends, not a proven terminal result for this request.

No `CLIENT_SYNC_RETURN`, SystemApp `0x1376c`/plugin return, `DM_PERFORM_RETURN`, or BKS check-in return was recorded before the explicit 7,200-second window close. This is a bounded no-return observation for that covered interval, not proof of a hang or permanent noncompletion. The probe stopped on `SYSTEMAPP_7200S_WINDOW_COMPLETE`, left the guest frozen, reported zero panics, and `qemu-img check -U` found no image errors. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN86_ACT_RETURN_REPLY4.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN86_ACT_RETURN_REPLY4.events/`, `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN86_ACT_RETURN_REPLY4.serial.log`, and `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin86-act-return-reply4.qcow2`. No seed, model, fixture, A408, or completion changes were made. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN87: non-monotonic-time panic before SystemApp; reply watch unscored

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN87_ANY_REPLY_DEST1` was a fresh child of the unchanged replay2 D5825 parent, using the accepted `system.tc`, CLASS16/D572/D582 launch profile, and deferred reply instrumentation. The coverage-light gate recorded backboardd `DM_CLIENT_MIGRATE_ENTRY` at `1791480772.355`; DataMigrator `CLIENT_RUN_ENTRY` for SystemAppMigrator never appeared before the guest failed. There was no `CLIENT_SYNC_CALL`, request/reply-object pin, wrapper handoff, migrationResult candidate, or 7,200-second SystemApp window. The deferred plugin/transport module therefore did not load, and its new all-sender destination watch and migrationResult-destination receiver joins were not runtime-validated on this child.

The first XNU panic was `Non-monotonic time: last_dispatch at 0x38d9c1ef, ctime 0x38d9a797 @sched_prim.c:815`; the serial capture then repeated the panic into nested-panic handling. The probe stopped on the nested-panic limit and left QEMU frozen. This is a boot/coverage failure, not an IPC result and not evidence about SystemApp completion. No reply object existed to test, so no send equality or receiver identity was scored. Do not infer hang, deadlock, or no-reply.

The child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin87-any-reply-dest1.qcow2` is backed directly by `/tmp/dvm/data-seed/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2`; `qemu-img check -U` reported no errors. Artifacts are `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN87_ANY_REPLY_DEST1.{lldb.log,probe.out,stop}` and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN87_ANY_REPLY_DEST1.events/`, plus `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN87_ANY_REPLY_DEST1.{serial.log,stderr.log}`. The gate, plugin, and wrapper probes pass `py_compile`; `setup_gate_probe.sh` passes `bash -n`; `git diff --check` passes. The P87 runtime modification remains unvalidated because the SystemApp gate was not reached. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN88: full any-send reply-object window; migrationResult destinations resolve to other peers

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN88_ANY_REPLY_DEST2` used a fresh replay2 child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin88-any-reply-dest2.qcow2`, accepted `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, and the unchanged CLASS16/D572/D582/ECHO profile. The coverage-light gate reached `DM_CLIENT_MIGRATE_ENTRY` at `1791482002.72402`, then SystemAppMigrator `CLIENT_RUN_ENTRY` at `1791483496.787873`. The deferred reply probe loaded at `1791483496.902039` for request `0x79a7040a20`, owner TPIDR `0xffffffe9e8b2b060`; post-copyin resolution pinned original reply object `0xffffffeaceb9ace0` (key `0xffeaceb9ace0`) and request destination `0xffffffeacebf18a0`. The request's 516-byte msgID `0x40000000` kmsg was delivered to `com.apple.migrationpluginwrapper` with destination-object equality. The owner then entered `ipc_mqueue_receive` on the pinned reply object.

The immutable any-destination matcher was ready at the global post-copyin Mach helper return `0xfffffff02aaa3f54` (`0xfffffff00aaa3f54` static). It compared resolved kernel destination objects from all observed hits at that send-helper site against the pinned reply key; no `IPC_ANY_SEND_DEST_PINNED_REPLY_OBJECT` event occurred before the explicit `SYSTEMAPP_MACH_WINDOW_CLOSED` marker at `1791490696.9259899` (`window_seconds=7200`). Thus the result is zero equal destinations on the watched `mach_msg2` post-copyin helper-return path for the full matched window. This is a bounded path-scoped negative, not a claim about unobserved IPC mechanisms or permanent noncompletion.

Four `IPC_MIGRATION_RESULT_KERNEL_SEND_CANDIDATE` records carried the `migrationResult` key; their resolved destination objects were `0xffffffeacef73b80`, `0xffffffeacee4d780` (twice), and `0xffffffeacef1fdc0`, all unequal to the pin. The XPC-to-kernel correlation for these records remains a candidate join where it relies on same-thread/time/body shape rather than a transaction ID. Receiver-side `ith_kmsg` evidence identifies the peers as follows:

* `0xffffffeacef73b80` was observed at a `tccd` receive, but that dequeued message had ID `0x46`, size 44, and a different body; this names the receiving service for the object, not delivery of the recorded `migrationResult` message.
* `0xffffffeacee4d780` received an exact 76-byte ID `0x20000000` `migrationResult` body in `com.apple.migrationpluginwrapper` (candidate 7; receiver TPIDR `0xffffffe9e8a758f0`). The object also appeared in an earlier same-shaped send candidate, so the exact receiver event is not assigned to that earlier send absent a transaction ID.
* `0xffffffeacef1fdc0` received an exact 76-byte ID `0x20000000` body in `com.apple.migrationpluginwrapper` (receiver TPIDR `0xffffffe9e8de0180`).

At the close, a paused 12-GiB RAM capture and thread scan found the matched owner thread at GPA `0x1005ba17060`, with `thread+0x28 = 0xffffffeaceb9acf0`, exactly the pinned reply object's `+0x10` wait queue. Its saved-state flavor was `0x15`, with saved PC `0x24e817cd4`, LR `0x24e81b308`, and SP `0x16b691590`; the thread continuation was `0xfffffff02aa8f658`. This confirms the request owner remained on the pinned reply wait at the sampled close. The thread scan found no waiter keyed to the three `migrationResult` destination-object wait queues. The postmortem process-name scan yielded no candidates for the requested DataMigrator/wrapper names, so no separate wrapper-process stack is claimed.

No same-owner `CLIENT_SYNC_RETURN`, SystemApp plugin return/`0x1376c`, `DM_PERFORM_RETURN`, or BKS check-in return fired in the full window. The guest was left frozen; the child passed `qemu-img check -U`. Artifacts are under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN88_ANY_REPLY_DEST2.*`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN88_ANY_REPLY_DEST2.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN88_ANY_REPLY_DEST2/ram/`, and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN88_ANY_REPLY_DEST2.{serial.log,stderr.log}`. The runtime `migrationResult`→kernel-send and all-destination watcher were exercised on this hit. This bounded open reply is not a hang/deadlock finding. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN89: broadened pinned-reply destination coverage; wrapper post-result sends remain non-pin

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN89_REPLY_PATH3` used a fresh replay2 child, the accepted `system.tc`, and the established CLASS16/D572/D582/ECHO launch profile. The deferred matcher reached the live SystemApp request `0x77f9038900` at `1791502373.196268` (SystemApp run entry `1791502373.092740`) on owner TPIDR `0xffffffe1e6d3f830`. Post-copyin resolution pinned reply object `0xffffffde5090faf0` (key `0xffde5090faf0`) and request destination `0xffffffde5082bd30`. The 516-byte msgID `0x40000000` request was delivered to `com.apple.migrationpluginwrapper` with destination-object equality; the owner entered `ipc_mqueue_receive` on the pinned reply object's wait queue `0xffffffde5090fb00`.

The probe added independent receive-side destination checks at `ith_kmsg` queue-store `0xfffffff02aa8f44c`, direct-store A `0xfffffff02aa8e638`, and direct-store B `0xfffffff02aa8eca0`, alongside the existing global post-copyin helper-return check at `0xfffffff02aaa3f54`. At the explicit `SYSTEMAPP_MACH_WINDOW_CLOSED` event (`1791509573.194928`, `window_seconds=7200`), the coverage snapshot reported 142,675 post-copyin return hits: 33,367 readable kmsgs and 109,308 unreadable candidates. No readable post-copyin destination matched the pinned reply. The queue-store callback ran 151,012 times and the direct-store-B callback 26,372 times; direct-store A had zero hits. Across those exercised `ith_kmsg` stores, reply-match counters were zero. This establishes a bounded negative for these armed helper-return and receive-store paths, not for every possible kernel send mechanism.

The exact 76-byte ID `0x20000000` `migrationResult` receives were observed at the wrapper on the destination objects resolved from their sends (three exact body/header joins in the success events). The earlier post-result send hook joined solely by TPIDR and time, so its six records need process-identity correction: the first was still labeled `com.apple.migrationpluginwrapper` and sent ID/size `1016/44` to `0xffffffde505d9ed0` about 800 seconds after the exact receive; the following five were labeled `amsengagementd` on the reused TPIDR, with IDs/sizes `1999646836/68`, `79/32`, `268435456/724`, `3240/48`, and `5403/56`. All six resolved destinations differed from the pin, but only the first remains a wrapper-send candidate. Its receiving peer was not matched in the saved events. In particular, the latest `5403/56` destination `0xffffffde50a86080` belongs to the `amsengagementd`-labeled candidate and cannot be used as the wrapper's next edge. No candidate is a transaction-ID-proven reply.

At close, the 12-GiB frozen-RAM thread scan found a thread whose waitq field (`thread+0x28`) was `0xffffffde5090fb00`, exactly the pinned reply object's `+0x10` waitq. The request's live owner was already observed receiving on that same waitq at `1791502373.444219`; the scan confirms a waiter remained at the frozen close, though the scan alone does not provide a user-process attribution. The postmortem process-name scan recovered one backboardd candidate but no `com.apple.migrationpluginwrapper` or `com.apple.datamigrator` candidate, so no wrapper end-stack is claimed. No `CLIENT_SYNC_RETURN`, plugin return/`0x1376c`, `DM_PERFORM_RETURN`, or BKS check-in return fired before the 7,200-second close. The guest was left frozen; `qemu-img check -U` passed. Artifacts: `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN89_REPLY_PATH3.lldb.log`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN89_REPLY_PATH3.events/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN89_REPLY_PATH3.mem/`, `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN89_REPLY_PATH3.threads.txt`, and `/tmp/dvm/probe/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN89_REPLY_PATH3.{serial.log,stderr.log}`. This is bounded QEMU-hypothesis evidence, not a hang/deadlock or firmware-fidelity claim; poster/draw remain parked.

#### PLUGIN90: receive-store follow-up; post-result sender context was reused

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN90_POST_MR_PEER1` used a fresh child of the unchanged replay2 parent, accepted `system.tc`, and the established CLASS16/D572/D582/ECHO launch profile. It reached SystemAppMigrator request `0x7b5b030a20` on owner TPIDR `0xffffffe97edaf060`. The 516-byte msgID `0x40000000` send resolved destination object `0xffffffe7b199f4c0` and reply object `0xffffffe7b1b20e80`; the request was handed to `com.apple.migrationpluginwrapper`, and the caller received on the pinned reply wait queue `0xffffffe7b1b20e90`.

The matched 7,200-second window closed normally with zero panics. The deferred module listed the global post-copyin return at `0xfffffff02aaa3f54`, but its all-sender option was **off**: the close snapshot records zero callback hits, zero readable kmsgs, and zero bad candidates. Therefore P90 adds no global `…3f54` any-destination result; P89 remains the preceding run with a live `…3f54` counter. The independent receive-store hooks did run: queue store `0xfffffff02aa8f44c` had 100,672 hits, direct-store A `0xfffffff02aa8e638` had zero, and direct-store B `0xfffffff02aa8eca0` had 8,725. None stored a kmsg whose destination object equaled the pinned reply. This is a strong bounded negative for those receive-store sites only.

Two exact 76-byte msgID `0x20000000` `migrationResult` kmsgs were received by `com.apple.migrationpluginwrapper`, with exact destination object, header, and body matches. Both destinations differed from the original reply object. The receiver context recorded on the second path reused TPIDR `0xffffffe97ecbb830`. A later same-TPIDR send candidate was emitted 1,972.9 seconds after that receive and was labeled `UserEventAgent`; its 112-byte XPC event-registration message was received by `nesessionmanager` with exact header/body equality. This identifies the receiver for that later message, but the elapsed interval and changed program identity invalidate its attribution as a wrapper continuation. No P90 post-migrationResult wrapper outbound peer is therefore named. The final candidate destination `0xffffffe7b1f00a00` is not the pinned reply `0xffffffe7b1b20e80`.

The expected protocol producer is the service that received the original synchronous request, `com.apple.migrationpluginwrapper`: its response must be sent to the request's pinned local/reply right for DataMigrator's `sendMessageSync` to complete. Static client-side framework analysis places the caller's synchronous send at `0x25e8f1f88`, its return at `0x25e8f1f8c`, and the subsequent local `migrationResult` result-block call at `0x25e8f2068`; these are client-side boundaries, **not** the service-side instruction that sends the reply. The likely trigger is SystemAppMigrator completion/result, but P90 does not identify the exact server-side producer PC or prove that it ran.

At close, the RAM thread scan found a thread whose `thread+0x28` waitq was `0xffffffe7b1b20e90`, the pin's `+0x10` wait queue. The scan did not map that physical thread row to a guest process; no DataMigrator or wrapper end-stack is assigned. No same-owner sync/plugin return, `DM_PERFORM_RETURN`, or BKS check-in return was recorded. Artifacts are under `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN90_POST_MR_PEER1.*` and `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN90_POST_MR_PEER1.events/`; the close-time thread list is `/tmp/dvm/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN90_POST_MR_PEER1.threads.txt`. `qemu-img check -U` passed. The probe source was subsequently corrected to enable the all-sender watch when the deferred module loads and to require the receiver process label to remain unchanged for post-result sender candidates. Its time window now defaults to 7,200 seconds so P89's slower wrapper-labeled candidate is retained; these changes were not loaded in P90 and remain unvalidated. This remains a bounded result, not a hang/deadlock or permanent no-reply finding. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN91B: SystemApp gate reached; deferred join failed before request instrumentation

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN91B_POST_MR_PEER2` used the replay2 parent, accepted `system.tc`, and the established CLASS16/D572/D582/ECHO launch profile. Launchd's `xpcproxy com.apple.datamigrator` spawn returned zero, and `DM_CLIENT_MIGRATE_ENTRY` fired at `1791522355.230`. SystemAppMigrator reached `CLIENT_RUN_ENTRY` at `1791525277.890` (about 2,922.7 seconds later) with completion block `0x16b0219e0`; this restores the activation gate for this boot. The deferred reply module loaded its breakpoints, but its arm callback then raised `NameError: process is not defined` while reading the live completion block. The emitted all-sender-watch status was also `enabled=false`: the flag was set before `kernelpath.install()`, which reset it. LLDB exited at the callback boundary, so no same-boot request-object pin, `migrationResult` destination comparison, post-receive peer, or sync-return observation is valid for P91B. This is a probe-arm failure, not an IPC result. Source now gets the process from the live frame and enables `TRACE_ANY_PINNED_REPLY_DEST` after install; those fixes require a fresh boot. P91B child `/tmp/dvm/data-seed/ui-class16-refresh60-dm-lock-plugin91b-post-mr-peer2.qcow2` passed `qemu-img check -U`. D572/D582 remain QEMU hypotheses; no hang/deadlock or presentation claim follows.

#### PLUGIN92: temporary run artifacts and replay2 working parent unavailable

P92 did not produce a scoreable result in the available workspace. `/tmp/dvm` is absent, no P92 QEMU/LLDB process remains, and no P92 log, event directory, or child image can be read. The run therefore has no verifiable `GATE_LIGHT_REPLY_JOIN_ARMED`, `GATE_LIGHT_PINNED_REPLY_SEND_WATCH_READY enabled=true`, or nonzero post-copyin `…3f54` counters. Do not count P92 as a gate hit, miss, peer result, or reply-path window.

The replay2 D5825 parent used by P88–P92 is also absent. Durable `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-ccs-ls-seedprogress2-replay-20261006.qcow2` remains and passes `qemu-img check -U` (SHA-256 `421d40e1d3c082036ae661dd57c232b00a31cf1cd51e713dbb00690a3e6c3c9d`), but the intermediate `ui-class16-refresh60-dm-plugin4-launchfrozen1-replay2.qcow2` delta and final `ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2.qcow2` were only under `/tmp/dvm/data-seed/`. The documented replay2 parent was built by rebasing that plugin4 launch-frozen delta onto seedprogress2. Booting directly from seedprogress2 would change the lineage, so no substitute child was launched.

The deferred-arm source fixes remain in `tools/re/dm_gate_light_callbacks.py`: obtain the process from the live frame, install the reply module, then enable the global pinned-destination matcher and emit the ready marker. `py_compile` and `git diff --check` pass, but these fixes are not runtime-validated. P89 remains the last corrected post-result attribution: only its first post-`migrationResult` send was wrapper-labeled (destination `0xffffffde505d9ed0`, receiver not recovered); later reused-TPIDR sends belong to `amsengagementd`. P90's `…3f54` watch was disabled, and P91B failed during deferred arming. P90–P92 add no terminal-reply or no-reply result. Resume after recovering the replay2 parent, then require the armed/enabled markers and live helper counters before interpreting any zero-equality tally. D572/D582 remain hypotheses; poster/draw remain parked.


#### PLUGIN93–95: durable replay2b reconstruction after `/tmp` loss

After the `/tmp/dvm` wipe, the durable seedprogress2 input was replayed rather than substituted. Seedprogress2 is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-ccs-ls-seedprogress2-replay-20261006.qcow2` (SHA-256 `421d40e1d3c082036ae661dd57c232b00a31cf1cd51e713dbb00690a3e6c3c9d`). PLUGIN93 reproduced a plugin4-class launch-frozen layer, saved durably as `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-plugin4-launchfrozen-replay2b.qcow2` (SHA-256 `822ed6a83bc94a1fe24dfdfe5a0d371893149ca540be318ec3d6c8667af60414`). PLUGIN94's first stop matched a setup/proof line rather than a runtime callback and is excluded. PLUGIN94B reached DataMigrator migration work but ended in a non-monotonic-time XNU panic before a return; its saved partial layer is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-ls-partial-panic-plugin94b-replay2b.qcow2` (SHA-256 `260926a25618cf964c98e9f452c6104ed3ab0dc740f9eafbc18ac34f26ddaebd`). It is retained as an explicit intermediate, not represented as a successful parent.

PLUGIN95 continued from that partial layer and did reach a successful matched DataMigrator round trip: `DM_PERFORM_ENTRY` at `1791564319.80788`, `DM_XPC_SYNC_CALL` at `1791564319.859047`, `DM_XPC_SYNC_RETURN` at `1791564926.613344`, and same-thread `DM_PERFORM_RETURN` at `1791564926.6305962`, with `x0=0`; zero XNU panics. The configured runtime stop accepted `DM_PERFORM_RETURN` as the completion boundary; the separate `LSD_BUILDER_SYNC_RETURN` label did not fire. The durable D5825-class parent is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2b.qcow2` (SHA-256 `7d9a1cb431c998a1cdabf47bf401385ba47a7c6f25eb20aeb62dcc15595fe3ea`). Its chain is parent → PLUGIN94B partial panic → PLUGIN93 plugin4 replay2b → durable seedprogress2 → boot1 disk. Each new layer passed `qemu-img check -U`, and `qemu-img info --backing-chain` confirmed the chain. This is a documented replay2b recovery lineage, not a byte-identical restoration of the lost historical plugin4/replay2 files. No IPC or poster/draw result follows from rebuilding it.

PLUGIN96 ARM1 is excluded: its success-stop list included `GATE_LIGHT_REPLY_JOIN_ARMED`, which would stop before the reply window; it was terminated before the migration gate. ARM2 restarted from a fresh child of the durable replay2b parent, used the corrected deferred arm, and stopped after the positive same-owner reply return at about 1,850 seconds rather than waiting out the 7,200-second timeout. This is an early success close, not a full-bound negative.

#### PLUGIN96 ARM2: pinned reply delivered and SystemApp sync returned

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN96_REPLY_JOIN_ARM2` used a fresh child of the durable replay2b parent, accepted `system.tc`, and the D5825/CLASS16/ECHO/D572/D582/A407 scanout profile. The live SystemApp gate armed the reply module. `GATE_LIGHT_PINNED_REPLY_SEND_WATCH_READY` reported `enabled=true` at runtime `0xfffffff028ab3f54`; `GATE_LIGHT_REPLY_JOIN_ARMED` identified SystemAppMigrator and owner TPIDR `0xffffffede6bfc950`. The matched request was `0x74c50587e0`. Post-copyin object resolution followed by the wrapper `ith_kmsg` delivery pinned request destination `0xffffffecffc579d0` and original reply object `0xffffffecffda9540`.

The wrapper received exact 76-byte `migrationResult` kmsgs. For this request, an outbound `migrationResult` XPC candidate on wrapper TPIDR `0xffffffede6f7b830` was followed by a 76-byte msgID `0x20000000` kmsg at `KERNEL_ITH_KMSG_DIRECT_STORE_B` (`0xfffffff02aa8eca0`). Its resolved remote object was exactly the pinned reply (`0xffffffecffda9540`), with local object zero; the receive event also matched the candidate's exact header/body. The waiting SystemAppMigrator owner then reached `CLIENT_SYNC_RETURN` at `0x27c901f8c` on the same TPIDR, about 28 ms after the handoff. This closes the matched wrapper reply → pinned right → same-owner return chain.

The all-sender post-copyin matcher emitted no equal-destination event before this direct-store handoff; therefore the positive reply was established by the receive-side resolved-object match, not by `…3f54`. Four process-scoped post-result outbound candidates were recorded and receiver joins identified `cfprefsd` and `notifyd`; their destinations differed from the pin. No unrelated reused-TPIDR `amsengagementd` traffic was assigned to the wrapper. Because the matched sync returned, the guest was stopped intentionally at approximately 1,850 seconds; the configured 7,200-second interval did not complete and no full-window absence claim is made. `DM_PERFORM_RETURN` and BKS check-in return were not observed before stop.

The successful child is durable at `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin96-reply-join-arm2-success.qcow2` (SHA-256 `99bd22d814215ffd5dccffe82f7e541a0ea5638de2ffe10211e4025911dab298`), backed by the replay2b D5825 parent; `qemu-img check -U` passed. Event/log evidence is preserved under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/PLUGIN96_REPLY_JOIN_ARM2/`. The first ARM1 attempt is unscored. No model, fixture, seed, A408, or completion changes were made. D572/D582 remain QEMU hypotheses; poster/draw remain parked.

#### PLUGIN97: same-boot sync → DM perform → BKS check-in → shell-service return

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN97_POST_SYNC_LADDER1` is a fresh child of the durable replay2b D5825-class parent `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2b.qcow2`, with accepted `system.tc` and the established CLASS16/D572/D582/ECHO profile. This is the labeled replay2b recovery lineage, not the lost byte-identical replay2 chain. The gate recorded backboardd `DM_PERFORM_ENTRY` at `1791568044.209604` on TPIDR `0xffffffe9b19a8950`; SpringBoard shell-service and migration-check-in calls followed at `1791568047.189052` and `1791568047.214302`, both on TPIDR `0xffffffe9b1bbc180`. SystemAppMigrator entered at `1791568067.990362` with completion block `0x16d6099e0`, then issued request `0x748f0445a0` at `1791568068.070929` on owner TPIDR `0xffffffe9b195c180`. Deferred reply-arm readiness and object resolution were observed; the request destination `0xffffffe9b2a31c90` reached the wrapper by object match, and reply object `0xffffffe9b26de350` was pinned for this boot.

The broad LLDB session hit a kernel trace stop and exited before return coverage. That interval is a probe-coverage gap, not a negative result. QEMU remained live, so a narrow return-only probe attached to the same boot and seeded from captured call events. It logged a same-owner `CLIENT_SYNC_RETURN` site event at `1791569460.971211` (owner TPIDR `0xffffffe9b195c180`, request field `0x748f0445a0`, `x0=0x748ec35400`), 0.376 seconds before `DM_PERFORM_RETURN`. The PC is shared and had repeated hits; treat this as an owner/request/time-associated return-site event, not an independent re-proof of P96's direct-store reply handoff or an exact completion-block identity.

The same-boot ladder then closed: backboardd `DM_PERFORM_RETURN` at `1791569461.347405` had `x0=0`, elapsed 1417.138 seconds from entry, and matched entry TPIDR and LLDB thread. SpringBoard `BKS_MIGRATION_CHECKIN_RETURN` followed at `1791569461.382015` with `x0=0`, matching migration-check-in TPIDR and call state. `BKS_CHECKIN_RETURN` followed at `1791569461.389929`, then `SHELL_SERVICE_RETURN` / `POST_SYNC_SHELL_SERVICE_RETURN` at `1791569461.393836`; those returns matched the BKS call's TPIDR/thread. This closes the requested post-sync migration and shell-service ladder on one boot. The run-plugin return at `0x1376c` and exact completion-block B invoke were not captured by the return-only reattach and remain unscored. Shell-service return does not prove that the full home UI appeared.

The child is preserved at `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin97-post-sync-ladder1.qcow2` (SHA-256 `3f45626dcda4a853f4bc41d160da12f899c6d0b77fa81c54f4e62c3536e3e0c4`) with events and logs under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/PLUGIN97_POST_SYNC_LADDER1/`. The durable child and source passed `qemu-img check -U`; the copy hash matches. The guest was manually paused after the shell success event because the configured success label did not stop the watcher automatically. No model, fixture, A408, or scanout change was made. D572/D582 remain hypotheses; poster/draw remain parked.

#### PLUGIN98: shell returned; SpringBoard entered poster-scene settings, but home/present was not established

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN98_POST_SHELL_UI1` was a fresh child of the durable replay2b D5825-class parent, with accepted `system.tc`, CLASS16/ECHO, and the established D572/D582/A407 hypothesis profile; A408 remained off. The same-boot ladder recorded `DM_PERFORM_RETURN x0=0` at `1791570567.617846`, `BKS_MIGRATION_CHECKIN_RETURN x0=0` at `1791570567.651104`, `BKS_CHECKIN_RETURN` at `1791570567.659599`, and `SHELL_SERVICE_RETURN` at `1791570567.663093`. The BKS/check-in/shell returns shared the SpringBoard caller TPIDR. Shell return is not home evidence.

The first dynamic post-shell hook import used a stale module slide of zero and is excluded. On the same live child, the arm was corrected to read slide `0x1b824000` from `ready.gate-light.json`; the lock/home hooks became ready at `1791570690.029012`, 122 seconds after shell return. This leaves an initial coverage gap. The later trace showed workspace create/transition callbacks at `1791570712.666420`–`1791570712.680818`, but not a completed home surface. `PBUIPosterViewController` initially took the null-scene branch repeatedly; then `updateConfiguration:` saw a non-null controller, resolved candidate scene `0x772de68e00` at `1791571214.463598`, and stored it at `1791571215.581394` into `_scene` field `0x772d1b5c18` (previously zero). Follow-up `_updatePosterScenes` samples took the non-null present arm and `willUpdateSettings` ran. This proves scene population and settings-path progress, not a display join or rendered pixels. CA `setLayer:` context callbacks occurred; no saved `POSTER_HOST_SET_CONTEXT_SCENE` or display-render hit followed.

HMP screendumps at shell return and later workspace/scene/debug-stop samples all remained 1179×2556 with zero pixels whose maximum RGB channel exceeded 8; each had SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. No `SB_ADFL_ENTRY`, `SB_HOME_ENTITY_INIT_ENTRY/RETURN`, `DM_ADFL_HOME_COMPLETE`, or A407-present marker was recorded before coverage ended. Do not call this a no-home-by-bound result: the watcher was configured for 1,800 seconds after shell, but two LLDB remote-register-unwinder crashes (`GDBRemoteRegisterContext`) interrupted the trace well before that bound. A paused CPU snapshot was at runtime PC `0x19fc43e88` (unslid `0x18443e88`, near the `SERVER_HOSTED_ALLOWED_UPDATE` hook at `0x18443e80`); process identity was not recovered, so this is a location clue rather than a proven stall. A smaller register-only callback set was armed afterward, but no hit was recorded before another debugger stop.

The recovery child is durable at `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin98-post-shell-ui1.qcow2` (SHA-256 `ec67e862bd92c736be335862fb69e63ac8572420d56464b8429a14ab4e4c6d0a`) with launch, event, frame, serial, and LLDB records under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/PLUGIN98_POST_SHELL_UI1/`. Source and durable child passed `qemu-img check -U`, and hashes agree. The durable `RESULT.md` and `observed-ui-events.jsonl` preserve event samples and coverage limits. No model, fixture, A408, or scanout change was made. D572/D582 remain hypotheses.

#### PLUGIN99: shell returned; configured post-shell observer failed to arm

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN99_POST_SHELL_HOME2` is a fresh durable child of the replay2b D5825-class parent. The existing gate-light and post-sync ladder reached `DM_PERFORM_RETURN` (`x0=0`), BKS migration/check-in returns (`x0=0` where recorded), and `SHELL_SERVICE_RETURN` on the matching SpringBoard check-in thread. SpringBoard ADFL entry was observed twice shortly afterward. The compact post-shell observer did **not** arm: `_arm_post_shell_ui` imported the configured module but then looked it up under the old fixed name `dm_post_shell_ui_callbacks`; the log records `POST_SHELL_UI_ARM_FAILED module-not-registered`. Therefore no home-entity, workspace, poster-scene, or display-present silence from this run is a UI verdict.

HMP screendumps at shell return and 0, 120, 240, and 360 seconds afterward were 1179×2556 and uniformly black (`nonblack_gt8=0`; identical SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). A manual sample at about eight minutes was also fully black. This records the visible output at those sample times, not whether SpringBoard later built a home or lock scene. The guest was ended cleanly after the six-minute post-shell sample because the observer-arm failure made a longer uninstrumented wait low-value.

The arm path is corrected in `tools/re/dm_post_sync_ladder_callbacks.py`: it now explicitly loads the configured callback module under its configured Python name, which is the name used by `SetScriptCallbackFunction`. This source fix is syntax-checked but still requires a fresh boot to validate `POST_SHELL_HOME_MIN_READY` after shell return. The child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin99-post-shell-home2.qcow2` (SHA-256 `b820414a5de7425c97b06e270d5d456c8991dcf7e565fd64f59f05aae9e358e3`) passed `qemu-img check -U`; its backing chain begins at the durable replay2b parent. Logs and frames are preserved in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/PLUGIN99_POST_SHELL_HOME2/`. No unlock input was sent; D572/D582 remain hypotheses.

#### PLUGIN100: black post-shell frames; callback module was not LLDB-registered

P100 is a fresh durable replay2b child. SystemApp sync returned; SpringBoard BKS migration/check-in and shell-service returns were observed. No `DM_PERFORM_RETURN` event was recorded in this run. The shell-return UI module wrote a READY line, but the import was Python `importlib` only. A local LLDB check showed that this does not register `SetScriptCallbackFunction` callbacks (the breakpoint command list remained empty); therefore P100's lack of home/workspace/poster callbacks is a coverage failure, not a UI-path negative.

HMP frames at shell return and +0 through +961 seconds were all 1179×2556 and uniformly black (zero pixels over the configured nonblack threshold; identical hash `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). These samples establish only the captured output. The guest was cleanly ended after +961 seconds because callback registration had not been proven. The source now uses LLDB `command script import` for the configured module name, and the compact module checks every breakpoint's `breakpoint command list` before emitting READY. Both fixes are syntax-checked; the combined callback proof still needs a fresh-boot validation.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin100-post-shell-home-armfix1.qcow2`, SHA-256 `732a710d69f140b7dfc91278c79d64f771a9debfd5d11ea8546f4dee1bd7a900`, passed `qemu-img check -U` and is backed by the replay2b D5825-class parent. Logs and frames are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/PLUGIN100_POST_SHELL_HOME_ARMFIX1/`. No touch/unlock was sent. D572/D582 remain hypotheses; no model, fixture, A408, or scanout changes were made.

#### PLUGIN101: shell return, home entity, and poster scene are live; framebuffer stays black

Fresh durable child `ui-class16-refresh60-dm-lock-plugin101-post-shell-home-cbproof1.qcow2` was based on the replay2b D5825-class parent and booted with accepted `system.tc`, CLASS16, and the established D572/D582/ECHO/A407 hypothesis profile; A408 stayed off. The shell ladder returned successfully: `DM_PERFORM_RETURN` and BKS migration/check-in/shell-service returns are recorded in the matching P101 event files. The post-shell compact module emitted `POST_SHELL_HOME_MIN_READY` only after `breakpoint command list` showed the callback and all seven breakpoints resolved one location at live slide `0x1f440000`.

This run closes several SpringBoard lifecycle gaps. `SB_ADFL_ENTRY` hit twice, workspace create/execute-transition callbacks ran, and `SB_HOME_ENTITY_INIT_ENTRY` returned a non-null object twice (`SB_HOME_ENTITY_INIT_ENTRY_RETURN`, around `1791575032.491` and `1791575074.211`). `POSTER_HOST_SET_CONTEXT_SCENE` then ran repeatedly with scene/context value `8939545518` (`0x214d6a3ae`) beginning at `1791575088.296`. These are SpringBoard lifecycle and scene-attachment facts; they do not prove a visible home or a display-presented poster.

HMP screendumps at shell return, home-entity return, and post-shell 0/120-second samples were each 1179×2556 and uniformly black (`nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The child was cleanly quit at the end of collection; it passed `qemu-img check -U`, with no XNU panic. P101 did not install the separate post-home CA/backboardd render/swap observer, so it cannot distinguish missing composition from a rendered-but-unpresented surface. The next run adds that read-only cut. No display model, fixture, scanout, A408, SpringBoard, or CLASS16 behavior was changed, and no touch/unlock input was attempted.

The child SHA-256 is `370c922eb1e88b71c6c51059ad596d888f7f71a73c2eedb7af02941d602bbcdb`; the recovery bundle, including event JSON, LLDB/probe logs, serial/stderr, and frames, is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/PLUGIN101_POST_SHELL_HOME_CBPROOF1/`. D572/D582 remain hypotheses. Home rendering and visible output remain open.

#### PLUGIN102: shell/check-in clear and SpringBoard render submission, but black framebuffer

P102 used a fresh child of durable replay2b D5825-class parent `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2b.qcow2`, with accepted `system.tc`, CLASS16, and the established D572/D582/ECHO/A407 hypothesis profile; A408 completion/scanout stayed off. SpringBoard's migration check-in returned `x0=0` at `1791575919.4722528`, followed by `BKS_CHECKIN_RETURN` and `SHELL_SERVICE_RETURN` on the matching TPIDR. Although `DM_PERFORM_RETURN` was armed, no matched return event was recorded in P102, so this run closes only the check-in/shell edge.

The post-shell callbacks were runtime-validated (`POST_SHELL_HOME_MIN_READY`, seven sites; `POSTHOME_PRESENT_READY`, seven sites, one location each). SpringBoard ADFL ran, workspace transitions followed, and home-entity initialization returned non-null objects twice. `POSTER_HOST_SET_CONTEXT_SCENE` attached scene/context `0x2006623ae` nine times. CA commit ran sixteen times; backboardd entered `Server::render_update`, created 1179×2556 surfaces, received non-null IOSurface allocation returns, and reached `fb_swap_set_layer`/framebuffer handoff. The IOMFB A408-build observer also ran; this is not a DCP correctness verdict or proof that a frame was displayed.

All eleven HMP captures through 600 seconds after shell return, including samples paired with home-entity, CA, render-update, surface-allocation, and swap markers, were uniformly black (`nonblack_gt8=0`; SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). P102 did not measure IOSurface pixels: the initial capture hook used `x2` at the later handoff, but static call-site analysis identifies `x3` at `fb_swap_set_layer` as the IOSurface argument. The hook was corrected to sample non-null `x3` at that verified entry; the fresh P103 run validates it. P102 was intentionally stopped after the stable-black interval, not after a guest failure. No touch/unlock input or model, fixture, A408, SpringBoard, or CLASS16 change was made.

P102 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin102-post-shell-render1.qcow2` (SHA-256 `b431bbfc64f568b18cf0cfce8dcbca08803747425da1aec41599edb69f2feeff`) passed `qemu-img check -U`; logs, events, and frames are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN102_POST_SHELL_RENDER1/`. D572/D582/A407 remain hypotheses; visible home and unlock are still open.

#### PLUGIN103: captured lock surface is sparse; HMP output remains black

P103 is a fresh sibling child of the durable replay2b D5825-class parent, with the same accepted `system.tc`, CLASS16, and D572/D582/ECHO/A407 hypothesis profile; A408 stayed off. The matched migration path closed in this boot: `DM_PERFORM_RETURN x0=0` at `1791577079.134561`, BKS migration/check-in return `x0=0` at `1791577079.163752`, and shell-service return at `1791577079.174076`. Post-shell hooks armed at live slide `0x10e84000`; SpringBoard returned a non-null home entity and committed CA state. Backboardd then rendered a 1179×2556 surface and reached `IOMFBDisplay::fb_swap_set_layer` at `0x1952f638c` (static `0x18447238c`) with non-null IOSurface in `x3`.

The corrected read-only surface callback captured IOSurface `0x74f5115a80` at `1791577400.315679`, immediately before the handoff sequence, from base `0x7bb5c00000`, geometry 1179×2556, stride 4864, format `BGRA` (`pixel_format=0x42475241`). The saved bytes hash is `b10ffee88a1fe8ae1b93c6c1201737b76202733a871c1dde4330f80c8e5e1583`; decoding with the existing IOSurface converter shows a black background containing only small status time/charging indicators, info glyph, and home indicator. Stats: 6,808 nonblack pixels of 3,013,524 (0.226%), peak RGB 255. No wallpaper or large clock appears in this submitted surface. A full-size capture earlier in the same run was effectively black (peak RGB 1); a third half-size candidate could not be read and is excluded.

HMP captures paired to shell return, home-entity return, CA commit, render-update, allocation, set-layer, and handoff remained entirely black (`nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). A manual HMP screendump after the IOSurface sample had the same result. Thus the CA/backboardd surface itself is sparse, and its sparse pixels do not appear in the framebuffer dump at the adjacent swap. P103 did not capture an A407 present marker, so do not assign the downstream loss to a specific DCP/A407 gate. `IOMFB_A408_BUILD` was observed only as a build callback; no A408 force or firmware-correctness inference follows. The run was intentionally stopped after the decisive capture and 361 seconds post-shell; no XNU panic occurred.

P103 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin103-post-shell-pixels1.qcow2` (SHA-256 `5f6282a1e9bb80670d3958b7ea309f4b3382f8578f8cde6ec85ad7a3a8a79b99`) passed `qemu-img check -U`. Recovery bundle: `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN103_POST_SHELL_PIXELS1/`; decoded sample PNG `events/iosurface-02-correct.png`, source event/raw `events/posthome-iosurface-02.json` and `events/iosurface-002-0x74f5115a80-hit2.raw`. No touch input or model, fixture, A408, SpringBoard, or CLASS16 changes were made. Next is a normal swipe through the supported native input path, then a fresh observation of SpringBoard's resulting surface. D572/D582 remain hypotheses.

#### PLUGIN106B: compositor surface confirms sparse lock pixels; input-enabled child is next

P106B is a continuous replay2b child with the shell/check-in and SpringBoard home callbacks active. `DM_PERFORM_RETURN`, BKS migration/check-in return, shell-service return, home-entity return, workspace requests, CA commit, backboardd render update, and IOSurface-backed `fb_swap_set_layer` were observed. The read-only post-shell sampler ran for 1500 seconds and ended on its explicit instrumentation-stop marker. HMP screendumps stayed black (all `nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`), with no A407-present marker.

The captured 1179×2556 BGRA IOSurface sample contains sparse lock-screen chrome (time/status indicators, info glyph, and home indicator) on black: 6,769 nonblack pixels of 3,013,524 (0.2247%), peak RGB 255. This confirms real compositor pixels, but not wallpaper, a complete lock scene, scanout, or an unlocked/home frame. The stopped child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin106b-post-shell-surface-trace1.qcow2` (SHA-256 `ebb356479c0cd0d56494aa5ebea4c4d0d8c21a59c224f2a1b8ff876be0f1cb75`) passed `qemu-img check -U`; its records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN106B_POST_SHELL_SURFACE_TRACE1/`. The ordinary cold child did not enable native input. The follow-up arms the documented launchd-only input-helper bootstrap and samples compositor surfaces after a normal swipe; no scanout forcing or synthetic pixels are used. D572/D582 remain hypotheses.


#### PLUGIN105B: shell return; home lifecycle and poster scene ran; visible output remained black

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN105B_POST_SHELL_SWIPE2` is a fresh child of the durable replay2b D5825-class parent with accepted `system.tc`, CLASS16, and the existing D572/D582/ECHO/A407 hypothesis profile; A408 remained disabled. On this boot `DM_PERFORM_RETURN` was recorded with `x0=1` at `1791579181.4131682`; SpringBoard migration/check-in and shell-service return followed, with the shell service returning `x0=0x1fb729c28` on the matched BKS check-in thread at `1791579350.172986`. Do not normalize the nonzero DM return to success; the BKS/check-in/shell callbacks themselves returned.

After shell, SpringBoard ADFL ran, home-entity initialization returned non-null objects at `1791578840.116829` and `1791578946.967806` (later returns at `1791579024.487548` and `1791579081.921638`), workspace execute-transition callbacks ran, and poster host/context scene callbacks attached scene/context value `0x20782a3ae`. An ordinary DVMINPUT1 legacy upward swipe and Home-button event were submitted and acknowledged by the relay; these acknowledgements establish input delivery only. CA commits and backboardd `Server::render_update` ran. Backboardd allocated a non-null 1179×2556 IOSurface at `1791579117.145526`, and the surface-create return matched it.

HMP samples from shell return through post-shell +842 seconds were uniformly black at 1179×2556 (`nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The first 16 `FB_SWAP_SET_LAYER_ENTRY` samples had `x3=0`; the callback disabled itself at that fixed cap. P105 therefore does not establish that all later swaps lacked a surface. The dynamic `SERVER_RENDER_UPDATE_RETURN` hook also did not fire. RAM/thread postmortem scans are preserved but do not identify a named app-level blocker or prove DCP/firmware behavior. No A407 present marker was recorded. This is home-lifecycle progress with black captured output and incomplete swap coverage, not visible home/unlock success or a diagnosis of D572/D582.

The read-only next-cut probe now logs up to 256 set-layer/handoff hits and records register/header data, and adds display-render, OGL, finish-update, finish-surface, finish-swap, and dynamic return hooks. It passed Python/shell syntax checks before P106 but is not yet runtime-validated. Preserve first non-null surface bytes and pair their content stats with the same render/update and HMP sample; if traced sources remain black, identify the earliest CA layer/update that lacks home content. Do not force A408 or paint pixels into scanout.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin105b-post-shell-swipe2.qcow2` has SHA-256 `fc1dcee45e15a80481e332c60c8db7d17d90f9671222ca9612db6f864c59c61d` and passed `qemu-img check -U`. The recovery bundle, including copied relay/home-input and thread/RAM postmortem records, is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN105B_POST_SHELL_SWIPE2/`. No model, fixture, SpringBoard, CLASS16, A408, or scanout changes were made. Visible home and unlock remain open.


#### PLUGIN105B: home lifecycle and poster scene ran, but HMP remained black

`UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN105B_POST_SHELL_SWIPE2` is a fresh child of the durable replay2b D5825-class parent with accepted `system.tc`, CLASS16, and the existing D572/D582/ECHO/A407 hypothesis profile; A408 remained disabled. `DM_PERFORM_RETURN` was recorded with `x0=1` at `1791579181.4131682`; SpringBoard migration/check-in and shell-service returns followed, with shell `x0=0x1fb729c28` on the matched BKS check-in thread at `1791579350.172986`. Preserve the nonzero DM return rather than describing it as success.

SpringBoard ADFL ran; home-entity initialization returned non-null objects at `1791578840.116829` and `1791578946.967806`, with later returns at `1791579024.487548` and `1791579081.921638`. Workspace transitions ran and poster host/context scene callbacks attached scene/context `0x20782a3ae`. CA commits and backboardd `Server::render_update` ran; backboardd allocated a non-null 1179×2556 IOSurface at `1791579117.145526`. Ordinary DVMINPUT1 legacy upward-swipe and Home-button relay events were acknowledged, which proves input submission only.

HMP samples through post-shell +842 seconds were uniformly black (`nonblack_gt8=0`, 1179×2556, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The first 16 `FB_SWAP_SET_LAYER_ENTRY` callbacks had `x3=0`, then the fixed callback cap disabled that hook. Therefore P105 says nothing about later swap surfaces. The dynamic `SERVER_RENDER_UPDATE_RETURN` hook also did not fire, and no A407 marker was captured. Thread/RAM postmortem files are preserved but identify no app-level blocker. This is home-lifecycle progress with black captures and incomplete later-swap coverage, not visible-home/unlock success and not a D572/D582/A407 diagnosis.

The next read-only probe increases set-layer/handoff coverage to 256 hits and adds render-display, OGL, finish-update, finish-surface, finish-swap, and dynamic return hooks. It passed syntax checks but still needs boot validation. Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin105b-post-shell-swipe2.qcow2` has SHA-256 `fc1dcee45e15a80481e332c60c8db7d17d90f9671222ca9612db6f864c59c61d` and passed `qemu-img check -U`. Its recovery bundle, including input and postmortem records, is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN105B_POST_SHELL_SWIPE2/`. No model, fixture, SpringBoard, CLASS16, A408, or scanout change was made. Visible home and unlock remain open.

#### PLUGIN107: shell returned and home entity initialized; v6 input reader never became ready

P107 is a fresh child of the durable replay2b D5825-class parent. On the same boot, `DM_PERFORM_RETURN` returned `x0=0`, followed by successful BKS migration/check-in returns and `SHELL_SERVICE_RETURN` at guest time `1791582295.085`. The post-shell observer armed at slide `0xedc000`; SpringBoard ran ADFL twice, returned a non-null home entity, and issued a workspace-create request. This is UI lifecycle progress, not visible home or unlock. The only HMP samples available were black, and the post-home CA/swap observer recorded no rendered-surface or A407 presentation result before the run was stopped.

The launchd-only input bootstrap itself worked: it started `/usr/local/libexec/dvm-input` PID 20. That disk's v6 helper emitted `DVM_INPUT_START` and `DVM_INPUT_INITIALIZING`, but no `DVM_INPUT_READY` or ACK during the observed interval. The QEMU bridge expects DVMI2 records and replies (`DVMI2R`/`DVMI2A`), while v6 speaks `DVMINPUT1` plus `DVM_INPUT_READY`/`DVM_INPUT_ACK`; therefore the host status remained not-ready and queued transport did not establish a guest input. The run was stopped as an input coverage miss, not as an unlock or display verdict. Its durable child is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin107-native-unlock1.qcow2` (SHA-256 `4496244f94a27dce585192fa437ecae510913247fb220def262bfc912a91e612`); the full bundle is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN107_NATIVE_UNLOCK1/`.

To remove that protocol mismatch without changing QEMU or the display model, the DVMI2 IOKit helper v12 was installed through the restore ramdisk into a separate child of the same replay2b parent. The helper install marker `DVM_HID_INSTALL_DONE` was observed; the installation child is sealed at `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/input-v12-install-replay2b-20261009/disk.qcow2`, and its source/TC/ramdisk are under `/Users/steve/dvm-artifacts/bp-patched-dev6/input-v12-native/`. P108 is the first normal boot of that modified lineage; until its DVMI2 readiness, touch ACK, and resulting compositor surface are observed, it supplies no input or unlock verdict. D572/D582 remain hypotheses; no A408 forcing or pixel synthesis was used.

#### PLUGIN112: shell return, lock UI, and acknowledged ordinary input; Home remains unproven

P112 was a fresh child of the durable v6-input-install child on the replay2b D5825-class lineage, using CLASS16 and the established D572/D582/A407 hypothesis profile with A408 disabled. The shell/home entity and workspace paths ran. The native v6 `DVMINPUT1` helper became ready, and ordinary upward swipe and Home events were acknowledged and reached BackBoard/SpringBoard event and hit-test callbacks. Those ACKs prove event submission, not that SpringBoard unlocked.

The IOSurface captured after input shows lock-screen chrome on black: time/status, battery, the Chinese swipe-up prompt, home indicator, and info glyph. It has no wallpaper, passcode keypad, or Home icons. A second attempt repeated an upward swipe after the Home event, but the post-input captures remained sparse; the guest was stopped by clean host HMP quit, with no guest panic. P112 used the direct `T` stream, whose 21 motion points are submitted serially; it may stretch the gesture and is not a reliable test of the guest's buffered Recap path. Therefore P112 is an input-delivery and visible-lock-state observation, not an unlock-failure or home verdict.

The next bounded attempt uses one buffered native `R` Recap swipe after the same-boot input-helper and post-shell compositor observers are ready, then examines a new read-only IOSurface capture. If that reveals passcode UI, interaction pauses for user entry; no passcode is synthesized. Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin112-home-unlock1.qcow2` (SHA-256 `5d60f913709b73f5758d1fed79afc7f7578da33d4013b3aac280fa657eac9a4c`) passed `qemu-img check -U`; event captures and logs are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN112_HOME_UNLOCK1/`. No model, fixture, SpringBoard, CLASS16, A408, or scanout changes were made. D572/D582 remain hypotheses.

#### PLUGIN119: home entity returned; legacy input helper did not become ready

P119 is a fresh child of durable P113 on the replay2b D5825-class lineage. The same boot returned through DataMigrator, BKS migration/check-in, and shell service. The combined post-shell module armed at the live slide, and the compact observer recorded ADFL, workspace transitions, six non-null home-entity returns, and 32 CA commits. It did not record poster-scene attachment or a backboardd `Server::render_update`/A407 event. These are lifecycle callbacks, not visible Home evidence.

Launchd started legacy `/usr/local/libexec/dvm-input` v6 as PID 20. During the 40-minute observed run its serial output remained at `DVM_INPUT_START` and `DVM_INPUT_INITIALIZING`; no ready/error/ACK was emitted and no gesture was sent. This is an input-helper startup coverage miss, not an unlock result. HMP dumps were uniform black, but do not substitute for IOSurface pixels. The post-input capture marker was never created, so there is no post-gesture IOSurface verdict. P119 had no XNU panic and was stopped deliberately.

The post-shell callback fix in `dm_post_shell_home_unlock_render_callbacks.py` was runtime-validated: it command-imported the compact home module, and the non-null home-entity readiness gate fired. The next run switches to the already installed DVMI2 helper and QEMU status path. P119 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin119-home-unlock1.qcow2` (SHA-256 `bd299c598e26114997e112ac06b0df5da61493aad027661792aecbd6b0c753ed`) passed `qemu-img check -U`; its backing parent is P113. Records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN119_HOME_UNLOCK1/`. No passcode, model, fixture, A408, or scanout action was performed; D572/D582 remain hypotheses.

#### PLUGIN120: DVMI2 helper rejected because its CDHash was absent from the boot trust cache

P120 used a fresh child of the durable v12 input-install image and armed the home/unlock observer, but the launcher supplied `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc`, which has 3,938 entries and does not contain the installed helper's CDHash `5135a276753e8b656e05651c91c20f4927a14c1e`. The guest serial log repeatedly reports `/usr/local/libexec/dvm-input` as ad-hoc signed, unsuitable for CT policy 0, then rejected by an enforcing launch constraint (19 observed attempts). The QEMU DVMI2 status consequently has no guest PID/ready announcement, no ACKs, and transitions to lost state after host ping timeouts. The guarded trigger remained behind its readiness gate and sent no gesture. P120 was stopped as input setup failure before the matched SystemApp sync/check-in had completed; no home or unlock verdict follows from it.

The exact helper is durable at `/Users/steve/dvm-artifacts/bp-patched-dev6/input-v12-native/dvm-input`; its one-entry helper cache is `helper.tc`. A derived cache was built by merging that entry into the accepted dev6 system cache: `/Users/steve/dvm-artifacts/bp-patched-dev6/input-v12-native/system-plus-dev6-input.tc`, SHA-256 `d60beb27dd6fc3726f205164842de245b9f4f6a2fa5e8bc40a95351a52fb7266`. It contains 3,939 unique entries, and the helper CDHash was checked in the merged entries. Its hash matches the already-existing v12 `system.tc`; no guest image or QEMU model was modified. Use this TC on the next fresh v12-install child, require an explicit DVMI2 `R` state before the one guarded swipe, and retain the passcode stop rule. P120's child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin120-dvmi2-home-unlock1.qcow2` (SHA-256 `4d1acec8529113dfe4f834fd40327459330f092a23efe36f017c0ea559e4cec4`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN120_DVMI2_HOME_UNLOCK1/`. No input was sent and D572/D582 remain hypotheses.

#### PLUGIN122: helper-bootstrap miss; PLUGIN123: Home input did not change the black capture

P122 used the durable P121 child and merged input-v12 trust cache with `INPUT_BOOTSTRAP=1`. Its manually started helper remained in state `I`; it never announced `R`, so the gate correctly sent no input. This is a launch configuration miss, not a screen or gesture verdict. P122's durable child SHA-256 is `e3474dabb25db5eeeaa078339615e079b2e8db82d13bd0f7ae36f8fa95083300`, with its record under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN122_DVMI2_HOME_UNLOCK2/`.

P123 restored P121's automatic helper startup (`INPUT_BOOTSTRAP=0`); DVMI2 reached `R` as PID 85. After same-boot SpringBoard home-entity return and render-observer readiness, one ordinary Home key and one upward swipe were ACKed/dispatched. All gesture before/after screenshots and the final screenshot were identical black 1179×2556 frames (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The corrected `can_update` callback dereferenced display+`0x330` and observed active-shaped bytes `0101000000010000`; this establishes the sampled display state only. SpringBoard ran ADFL, workspace/home-entity initialization, poster host/context-scene calls, and 32 CA commit entries. No post-home backboardd render-update/display/OGL/swap or IOSurface callback fired; no A408-build hit or DVMI2 present/input-to-present sample was recorded. SpringBoard input/hit-test was not instrumented, so the touch's arrival at UIKit is unknown. The trigger initially missed its readiness marker due to a wrong string/tail scan; it was restarted after correcting that bug, and the result here is from the fully gated input attempt.

This moves the open edge to **SpringBoard input delivery and CA-to-backboardd update**, not helper readiness or shell/home-entity creation. It does not establish a D572/D582 or scanout defect, and no passcode was entered. P123 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin123-dvmi2-autolaunch-home-unlock3.qcow2` (SHA-256 `f2d54dee5940d067d4be0f228157ae4e3a700769c71b074bc329a071de8030e`) passed `qemu-img check -U`; the full log, frame, and event bundle is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN123_DVMI2_AUTOLAUNCH_HOME_UNLOCK3/`. The next boot should arm `dm_unlock_input_callbacks` before the one gated gesture and retain CA/backboardd hooks to locate the first missing update. No model, fixture, SpringBoard, CLASS16, A408, or scanout modifications were made; D572/D582 remain hypotheses.

#### PLUGIN128: Home and swipe reached SpringBoard; captured output remained black

P128 is a fresh child of durable P127 on the replay2b D5825-class lineage. It used the accepted merged DVMI2 trust cache, CLASS16, and the established D572/D582/ECHO/A407 hypothesis profile; A408 remained off. `DM_PERFORM_RETURN` was recorded with `x0=1`; the SpringBoard migration check-in returned `x0=0`, followed by BKS check-in and shell-service returns on the matched shell thread. Preserve the nonzero DM result rather than labeling the whole ladder successful.

The compact callbacks armed at the live slide and recorded ADFL, workspace creation/transitions, ten non-null home-entity returns, and fifteen `POSTER_HOST_SET_CONTEXT_SCENE` callbacks for context `0x204aee48e`. DVMI2 reached ready state. The Home key was ACKed and followed by workspace/home-entity activity; one upward swipe was ACKed and dispatched without failed records. Its SpringBoard path is directly observed: `UI_EVENT`, `_UISystemGestureWindow` hit-test, returned `UISystemGestureView`, and `SPRINGBOARD_HID_EVENT`. This proves delivery to SpringBoard's system-gesture view, not an unlock transition. HMP frames before Home, after Home, after swipe, and at the end of the 300-second observation were the same uniformly black 1179×2556 image (SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The compact P128 observer did not arm CA/backboardd/IOSurface pixel hooks, so this run has no compositor-pixel verdict. No passcode was entered.

P128 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin128-min-home-unlock1.qcow2` (SHA-256 `cf1946fd14e786557f544f2fa0505a690f840e1d0bea3e150b58edff4c2fb04b`) passed `qemu-img check -U`; the recovery bundle is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN128_MIN_HOME_UNLOCK1/`. The next cut combines the runtime-validated SpringBoard input callbacks with the existing read-only IOSurface sampler, opens pixel capture only after the swipe ACK, and compares that surface with the same-boot screen capture. The black HMP result alone does not identify an A407/D572/D582 fault. No model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN130 / PLUGIN131: corrected observer setup; home entity returned later than the old gate

P130 is setup-only. It booted a child of durable P128, but inherited `INPUT_BOOTSTRAP=1` and the default `dm_post_shell_home_render_callbacks` module instead of the gated unlock-render observer. The trigger was stopped before sending input. Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin130-home-render-retry1.qcow2` (SHA-256 `5b0fe2a2046186a17985a87c7bec37d094d12ac808c85e4133e269576501b27b`) passed `qemu-img check -U`; records are in `recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN130_HOME_RENDER_RETRY1/`.

P131 corrects the setup: fresh child of durable P128, accepted input trust cache, `INPUT_BOOTSTRAP=0`, DVMI2 helper `R`, and `dm_post_shell_home_unlock_render_callbacks` with `surface_capture_gated=true`. The shell ladder returned and the observer armed. The Home key was ACKed with no input errors; the HMP frame remained uniformly black (SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). No non-null home entity appeared during the trigger's 180-second post-key gate, so it correctly withheld the swipe. A non-null `SB_HOME_ENTITY_INIT_ENTRY_RETURN` did appear at `1791614769.679`, about 343 seconds after the Home command and during the later observation interval. The helper had already cached `home_entity_after_key=false`; therefore P131 did not swipe, and it produced no post-input IOSurface event. This is a late-home timing result, not an unlock test. The next run extends the entity wait to 600 seconds before deciding whether to swipe.

P131 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin131-home-unlock-render1.qcow2` (SHA-256 `b5ba0911db77dd10a2dd4c1adedc356b7bb82488238ffb1a0611bd1cb7d6ad17`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN131_HOME_UNLOCK_RENDER1/`. No passcode or text was entered, and no model, fixture, SpringBoard, CLASS16, A408, or scanout change was made.

#### PLUGIN132: delayed Home entity and swipe reach SpringBoard, but no visible unlock

P132 is a fresh child of durable P128 using the merged input trust cache, `INPUT_BOOTSTRAP=0`, DVMI2, and `dm_post_shell_home_unlock_render_callbacks`. The Home key was ACKed. A non-null `SB_HOME_ENTITY_INIT_ENTRY_RETURN` arrived at `1791615274.582`, about 333 seconds later, so the helper waited through the delayed entity and sent one ordinary upward swipe. The swipe was ACKed and dispatched without failures. SpringBoard then logged `UI_EVENT`; `_UISystemGestureWindow` hit testing returned `UISystemGestureView`; and `SPRINGBOARD_HID_EVENT` ran. This closes the input-delivery gap through the system-gesture view, but does not prove that the lock screen accepted or completed its dismissal.

The HMP frame before and after the swipe remained uniformly black at 1179×2556 (SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). During the bounded 300-second post-input observation, CA commit entry ran 32 times and one commit return was recorded, but the armed backboardd render-update/display/OGL/swap callbacks did not log a hit; no gated IOSurface event was produced. The framebuffer therefore supplies no visible unlock/home result, and the trace has not yet separated gesture recognition from the post-CA display handoff. No passcode was entered.

P132 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin132-lockscreen-swipe1.qcow2` (SHA-256 `610d0298e8abd5d3ca6ad9c51fc7ad80e1775df422954a266dcce71d698a037e`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN132_LOCKSCREEN_SWIPE1/`. Next, instrument the cover-sheet dismissal and home-gesture recognizer paths plus the lock-screen transition result, while retaining the post-gesture CA/render/surface hooks. No model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN133: SpringBoard receives the pan, but no visible frame follows

P133 is a fresh child of durable P132 on the same replay2b/D5825-class parent chain, with the merged DVMI2 helper trust cache, CLASS16, the existing D572/D582/ECHO/A407 hypothesis profile, and A408 disabled. The matched shell-service return was recorded. A Home command was acknowledged; SpringBoard later returned a non-null home entity at `1791616716.021`, about 319 seconds after shell return. One upward swipe was then acknowledged with no helper dispatch failures.

The swipe's `UI_EVENT` reached SpringBoard, and `_UISystemGestureWindow` hit testing at `(294.5, 1225)` returned `UISystemGestureView`. SpringBoard's `SBFluidSwitcherScreenEdgePanGestureRecognizer` and `SBCoverSheetScreenEdgePanGestureRecognizer` each received `touchesBegan`; the cover-sheet pan also received `touchesMoved`. A `SBLockScreenManager lockScreenViewControllerWillPerformTransitionToFinalValue:forUserGesture:withVelocity:animated:` callback ran with `x2=0`, `x3=0`, `d0=d1=0`, and `x5=1`. That callback has not been tied to the swipe; its `forUserGesture` argument was zero. No armed `gestureRecognizerShouldBegin`, home-gesture interaction handler, or cover-sheet dismiss begin/change callback fired. Thus input reached recognizer touch delivery, but the trace does not show the pan being accepted or an unlock transition.

Poster host/context-scene callbacks followed, and SpringBoard emitted 42 CA commit entries. None of the armed backboardd render-update, display/OGL, swap, or IOSurface callbacks fired. Every periodic and post-input HMP capture remained uniform black at 1179×2556 (`nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The input helper reported `home-and-swipe-acknowledged`, `home_entity_after_key=true`, `swipe.ok=true`, and `frame_changed=false`. This is neither visible Home/unlock nor a display-path diagnosis; the next cut records UIKit recognizer state/should-begin on the two observed screen-edge recognizers and pairs it with the existing transition and compositor observers.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin133-lock-gesture1.qcow2` (SHA-256 `5415b4d4950aa79c9e7b0febada0f84858f1eb28111b0f77d841dd1e89db1ce9`) passed `qemu-img check -U`. Full evidence is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN133_LOCK_GESTURE1/`. No passcode was entered and no model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN134: the screen-edge recognizers enter UIKit Failed state

P134 is a fresh durable child of P133 with the accepted DVMI2 input profile, `INPUT_BOOTSTRAP=0`, and the combined read-only input/gesture/render observers. The Home key and one upward swipe were acknowledged and dispatched; the home entity was non-null. At UIKit `UIGestureRecognizer setState:` (`0x18491ff3c`, runtime `0x190dc3f3c`), three calls set `x2=5`: `SBCoverSheetScreenEdgePanGestureRecognizer` at `1791617757.445894`, `SBFluidSwitcherScreenEdgePanGestureRecognizer` at `1791617757.578570`, and the cover-sheet recognizer again at `1791617757.921956`. The SDK enum identifies value 5 as `UIGestureRecognizerStateFailed`. No UIKit `_shouldBegin`/driver-state update or SpringBoard home/dismiss `shouldBegin`/action callback was observed. The recognizers therefore fail before acceptance, but this run does not identify why UIKit failed them.

The HMP frame stayed uniformly black (SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`); no render-update/display/OGL/swap or IOSurface event followed. P134 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin134-gesture-state1.qcow2` (SHA-256 `6dabcef60e9762d0385d3e428174972f5a4e1dacdacd2999b337d744ff60e7bd`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN134_GESTURE_STATE1/`. The next discriminator captures the Fluid recognizer's `setState:` caller and the screen-edge grabber-zone predicate, while moving the swipe start point closer to the bottom edge to test the geometry hypothesis. No passcode or model/fixture/SpringBoard/CLASS16/A408/scanout change was made; D572/D582 remain hypotheses.


#### PLUGIN141: bottom-edge dismissal transition succeeds, but display stays black

P141 is a fresh child of durable P134 using the accepted DVMI2 helper trust cache, `INPUT_BOOTSTRAP=0`, and the existing read-only lock/input/render observers. The first swipe began too far above the bottom edge and failed the screen-edge recognizers. A second buffered swipe on the same live child used `(589,2540)→(589,500)`, 600 ms hold, and 32 steps; all 35 input records were acknowledged, 33 were dispatched, and none failed. SpringBoard's `SBCoverSheetScreenEdgePanGestureRecognizer` and system-gesture delegate both returned `shouldBegin = true`; Cover Sheet dismissal began and changed, the pan ended, and `LOCKSCREEN_GESTURE_TRANSITION` recorded `forUserGesture=1` with velocity `0.2715`. A subsequent home-entity initializer returned non-null. This is direct evidence that the user-gesture dismissal path ran, beyond the earlier recognizer-failure cases.

The screen did not become visible: HMP frames remained the same uniform-black 1179×2556 image (`d312521c…f42b9`). SpringBoard's CA commit entry ran 58 times, but the armed backboardd render-update, render-display, OGL, swap, and IOSurface callbacks recorded zero events; DVMI2 reported zero presents and zero input-to-present samples. Therefore this boot closes gesture recognition/dismissal and home-entity creation, while leaving the CA client-to-server handoff and visible presentation unclassified. It does not establish an A407/D572/D582 defect.

The durable child is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin141-gesture-center600ms1.qcow2` (SHA-256 `8c88db5fdcad9a14b103e635cb137ce02500cdfef004a7f797890153d46cab44`); `qemu-img check -U` passed. Full capture is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN141_GESTURE_CENTER600MS1/`, including `events/bottom-center-swipe.json`. The next cut traces the read-only QuartzCore `0x9c4x` Mach send/receive family alongside the existing post-shell compositor observers, to determine whether SpringBoard sends a transaction and whether backboardd receives it. No passcode was entered; no model, fixture, SpringBoard, CLASS16, A408, or scanout change was made.


#### PLUGIN142: CA server receives post-home transactions; display path stays quiet

P142 is a fresh durable child of P134 on the replay2b D5825-class lineage, with the accepted input trust cache and unchanged CLASS16/D572/D582/ECHO/A407 hypothesis profile; A408 remained off. The shell/check-in ladder cleared, SpringBoard returned non-null home entities, and the home/swipe helper completed its 600-second post-input observation. Its bottom-center swipe `(589,2540)→(589,500)` was acknowledged and dispatched without failures. A second pause-aware retry dispatched 31 records with zero failures. Neither changed the HMP frame: it remained uniformly black at 1179×2556 (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). P142 did not record the accepted Cover Sheet dismissal callbacks seen on P141; its one lock-transition callback had `x2=0`, `x3=0`, and zero velocity, so do not treat it as user-gesture acceptance.

The opt-in QuartzCore trace was armed with global Mach traps disabled. SpringBoard recorded 68 CA commit entries and the client send hook recorded 16 `0x9c44`/`0x9c45` send-wrapper events (13/3). Backboardd recorded its bounded first 32 receive events, including nontrivial `0x9c44` and `0x9c45` transactions. This establishes CA server receive-side activity in the observation window; task-local port names were not used as a cross-process identity join. The post-home callbacks were runtime-armed, but no `Server::render_update`, `render_display`, OGL, finish-update, framebuffer swap, or IOSurface pixel callback fired. DVMI2 reported zero presents and zero input-to-present samples. Thus the first open edge is after CA server receive and before the display render/swap path; this run does not identify whether scheduling/eligibility prevented `render_for_time` or another handoff is missing.

The durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin142-ca-handoff1.qcow2` has SHA-256 `947e900c389721292e48341aee39666d33a85442232303ab84031dec6a17b729` and passed `qemu-img check -U`. Records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN142_CA_HANDOFF1/`. The next probe should observe the display scheduler, `can_update`, and `render_for_time` entry/decision/short-skip/full-path sites alongside the existing read-only render hooks. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN147: natural render exits before Server update after Home input

P147 used a fresh child of P134, the accepted merged DVMI2 trust cache, CLASS16, and the existing D572/D582/ECHO/A407 hypothesis profile; A408 remained off. The migration/check-in/shell ladder returned. SpringBoard produced a non-null home entity after a Home key. A bottom-center upward swipe `(589,2540)→(589,500)` sent and ACKed 34 records, dispatched 32 with no failures; a second bottom-edge retry dispatched 22 of 23 records, also with no failures. The captured lock transition had `forUserGesture=0`, so P147 does not establish an accepted unlock gesture. HMP remained uniformly black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`), with zero DVMI2 presents/input-to-present samples and no IOSurface event through 900 seconds.

Backboardd's post-home scheduler observer recorded at least 2,048 `render_for_time` full-path iterations. V178/V248 returned 0; VF8 and V170 returned 1. No later V1B8/compare/outer update, Server render, OGL, swap, or surface path was recorded. The V170 observer sampled after the call and treated stack-like x19 as a display receiver; those receiver fields are invalid. After P147 closed, the source gained a read-only pre-V170 breakpoint sampling call-site x0 (display) and x1 Update, including the predicate state bytes and update flags. That hook passed syntax checks but P147 ran the prior module and did not validate it. This is a renderer-gate result, not a lock dismissal or DCP/A407 correctness result.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin147-render-gate1.qcow2` has SHA-256 `e932a63396e2ad65c7d9ec8d319befed4562ef24acb63cda1775d35591cb985e` and passed `qemu-img check -U`. Full records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN147_RENDER_GATE1/`. No passcode or model, fixture, SpringBoard, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN149: Home entity is present, but active QuartzCore display ignores natural updates

P149 used a fresh child of the durable P147 render-gate child with the accepted DVMI2 trust cache and the existing CLASS16/D572/D582/ECHO/A407 hypothesis profile; A408 remained disabled. SpringBoard returned a non-null Home entity and the helper acknowledged Home/swipe inputs, but every HMP capture remained uniformly black (1179×2556, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).

The active QuartzCore display receiver was `0x793ecf8000`, with state object `0x7521fe4000`. Seventy sampled `render_for_time` decisions entered the full path; before V170 they all had update flags `0x80010` (mask `0xa` clear), state bytes 4/5 = `0/1`, and mode byte `+0x2bb=0xff`. `ignore_update_p` returned 1 for every sample. This validates the call-site receiver/predicate read on P149 and places the observed skip before Server render and surface creation.

The power-state callbacks hit a different object: receiver `0x793ed08000` (display ID 2), state `0x7521fe8000`. Those are `+0x8000` and `+0x4000` from the active V170 display/state, so their state 5 transitions must not be attributed to the active render display. The active-state watch reported no writes before LLDB stopped at breakpoint 81 (`0x188e772e8`, `CAN_UPDATE_OUTER_AFTER_INNER`). Subsequent HMP samples continued, but compositor callback coverage did not. Next trace the active display's state-byte writer and the natural mutator branch feeding bit 22; omit the unrelated `can_update` breakpoint and raw watchpoints that ended the previous LLDB session.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin149-posthotplug-branch1.qcow2` passed `qemu-img check -U`; evidence is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN149_POSTHOTPLUG_BRANCH1/`. This remains a QuartzCore result; it does not establish DCP firmware behavior or D572/D582 correctness.

#### PLUGIN150: first-run setup is requested, but no Home entity appears

P150 is a fresh child of the durable P149 post-hotplug parent, with the accepted input trust cache, CLASS16, the existing D572/D582/ECHO/A407 hypothesis profile, A408 disabled, and the DVMI2 helper enabled. The post-shell ladder returned and SpringBoard entered ADFL twice, at `1791630627.774` and `1791632007.995`. A read-only setup-progress trace caught the second call to `updateInSetupMode`: the call returned `x0=1`, and `SB_SETUP_SET_REASON_ENTRY` received `x2=1`. This establishes that the setup-required branch was selected; it does not establish that Setup Assistant launched.

The Home key was acknowledged immediately after shell return. No non-null `SB_HOME_ENTITY_INIT_ENTRY_RETURN` was observed during the 2,406-second post-shell capture. After the configured 600-second wait, the helper also sent one ordinary upward swipe despite the missing Home entity; both inputs were delivered without helper dispatch errors. Neither changed the HMP frame: all captures remained uniform black at 1179×2556 (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`), and DVMI2 recorded zero presents and zero input-to-present samples. This is not a successful Home or unlock result.

P150's setup-progress and activation observers were installed late: the setup-progress observer began after the first ADFL entry, and the activation observer began about 628 seconds after the second entry. Their quiet rows therefore cannot exclude earlier activation activity. The ADFL exit observer also produced no event after its late install. P151 moves setup/activation/epilogue installation to shell time and withholds the swipe unless a non-null Home entity appears. Static setup-path context is in `docs/re/setup-launch-runtime.md`; this run does not identify a missing app or a display/firmware defect.

The durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin150-natural-mutator1.qcow2` has SHA-256 `dfbb8cbdd32464437549532873c208d182ad163525eeabdd58d5577ce77b6ecb` and passed `qemu-img check -U`. The evidence bundle is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN150_NATURAL_MUTATOR1/`. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN151: input-bootstrap trust-cache mismatch (coverage only)

P151 stopped before the UI experiment because it used `system.tc` while enabling `INPUT_BOOTSTRAP=1`. AMFI rejected `/usr/local/libexec/dvm-input` under that cache's launch constraint, so DVMI2 never became ready and no input was sent. This is a launch-configuration error, not a SpringBoard result. The child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin151-home-gated1.qcow2` (SHA-256 `17ab58ca5f9f5c29a933a1ebffbb4fab57172577b99248727e7aa84c9e43496f`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN151_HOME_GATED1/`. The corrected P152 setup reuses P150's merged `system-plus-dev6-input.tc` with bootstrap disabled; no model or guest image change was made.

#### UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN152_HOME_GATED1: short swipe failed; no rendered surface followed

P152 used a fresh child of durable P149 with the merged DVMI2 trust cache, `INPUT_BOOTSTRAP=0`, and read-only post-shell UI/render observers. The shell/check-in ladder returned. SpringBoard reported setup mode (`updateInSetupMode` returned 1), then a non-null Home-entity initialization return; workspace creation/execution and poster-context attachment also occurred. These are SpringBoard state markers, not proof of a visible Home screen.

The helper ACKed Home and one short upward swipe `(589,2450)→(589,650)` (12 steps, 80 ms). SpringBoard received the input through `UISystemGestureView`, but both `SBFluidSwitcherScreenEdgePanGestureRecognizer` and `SBCoverSheetScreenEdgePanGestureRecognizer` entered UIKit Failed state (5). Since this swipe started above the bottom-edge profile accepted on P141, P152 does not classify a dismissal failure. HMP stayed uniformly black at 1179×2556 (`nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). CA/FB swap bookkeeping occurred, but there was no `Server::render_update`, display/OGL render, finish-update surface, or IOSurface pixel sample. No rendered Home/lock frame is evidenced in the display path.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin152-home-gated1.qcow2` (SHA-256 `66415b93381af50a03c90ff25754af23000d6b2f1dbc4b924b5568a85ca436c6`) passed `qemu-img check -U`; evidence is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN152_HOME_GATED1/`. P153 repeats P141's bottom-center `(589,2540)→(589,500)`, 600 ms/32-step gesture on a fresh P152 child and enables the natural active-display scheduler/mutator trace. No passcode was entered; no model, fixture, SpringBoard, CLASS16, A408, or scanout change was made. D572/D582 remain hypotheses.

#### PLUGIN153: Home/swipe input acknowledged; SpringBoard scene startup and display remain incomplete

P153 used a fresh child of the durable P152 child, with the merged DVMI2 trust cache, input bootstrap disabled, and the same read-only shell, setup, Home, gesture, and active-display scheduler observers. The shell/check-in ladder returned. SpringBoard's ADFL path called `updateInSetupMode`, returned `x0=1`, and set reason `x2=1`. No non-null Home entity was returned. The armed return-to-lock, Setup application lookup/activation, `SBToAppsWorkspaceTransaction`, and setup-exit callbacks emitted no hit. The separate `finalizeStartupAfterScenesDidConnect:` entry also stayed quiet in the covered interval. The observed SpringBoard state is therefore Setup-required with no Home entity; this is not an unlock result.

After the original instrumented input sequence, the reattached debugger repeatedly stopped on `SIGTRAP` in user code. Detaching it restored a running guest; that debugger behavior is a coverage disturbance, not a SpringBoard crash. With LLDB detached, a final Home key and the exact P141 bottom-center swipe `(589,2540)→(589,500)` (600 ms, 32 steps) were both acknowledged and dispatched without errors. The input helper recorded `frame_changed=false` for the Home key and no presented frame after the swipe. All HMP frames stayed uniformly black at 1179×2556 (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The scheduler trace reached at least 4,096 `render_for_time` full-path samples; V178 returned decimal 250 (`0xfa`) and no later V248/V218/VF8/V170 or server-render/surface event was recorded. This is a QuartzCore branch observation, not a DCP firmware conclusion.

P153 was stopped deliberately after about 736 seconds of virtual time to preserve a clean run; this is not the configured 7,200-second bound. Its durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin153-bottom-edge-home1.qcow2` (SHA-256 `3f66ab894ea9548dae748352c43d18758baf357fdef5b36318346ccd77c022b7`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN153_BOTTOM_EDGE_HOME1/`. The next cut observes SpringBoard's `application:configurationForConnectingSceneSession:options:`, `_completeStartupAfterMainSceneConnect:`, and scene-finalization entries from the shell-time callback arm, to split “no main-scene configuration request” from “configuration returned but the main scene never completed.” No passcode, model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN154: scene/home lifecycle observed, but the input helper used the wrong trust cache

P154 was a fresh child of the P152 lineage and reached scene finalization and a non-null Home-entity return. It used `system.tc`, which did not authorize the installed DVMI2 helper; the host stopped at its explicit helper-rejection gate. No gesture was sent, so P154 adds SpringBoard lifecycle coverage only. Its record is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN154_SCENE_GATE1/`; it is not an input or unlock result.

#### PLUGIN155: SpringBoard renders lock chrome into IOSurfaces; HMP scanout remains black

P155 is a fresh child of durable P152 using the accepted merged input trust cache, `INPUT_BOOTSTRAP=0`, and the shell-time setup/activation/render observers. `SHELL_SERVICE_RETURN` was followed by `updateInSetupMode` selecting reason 1, a non-null SpringBoard Home-entity return, three scene-configuration return paths, startup finalization, and poster-scene attachment. This establishes that SpringBoard reached these setup and scene checkpoints; it does not show a visible Home screen.

The corrected pixel result is that backboardd did render lock-screen chrome. It entered `Server::render_update`, allocated 1179×2556 IOSurfaces, rendered, and handed surfaces off through the framebuffer swap path. Three captured IOSurfaces contained 6,243, 9,736, and 6,984 nonblack pixels; the middle capture had 8,954 pixels above RGB 32 and visibly contains status/time, charging indicators, the Chinese swipe instruction, info glyph, and home indicator on black. In contrast, every HMP screendump remained uniformly black (1179×2556; SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). P155 therefore proves nonblack lock content in the rendered surface but not that it reached the captured scanout. Raw surfaces and capture metadata are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN155_HOME_TOUCH1/events/`.

After the Home entity appeared, P155 sent one bottom-center swipe `(589,2540)→(589,500)` (600 ms, 32 samples). The helper ACKed all 23 sent records, but 11 motion records were coalesced and only 22 were reported dispatched; there were no dispatch failures. SpringBoard's gesture transition reported `forUserGesture=0`, so this gesture is not a successful unlock. The trace does not yet distinguish the transport's lost motion history from SpringBoard gesture policy. P141 remains the accepted-gesture reference: 35 records ACKed, 33 dispatched, no coalescing, with `forUserGesture=1`; it did not produce visible scanout either.

The setup activation trace captured 24 `SB_SETUP_APPLICATION_LOOKUP_ENTRY` hits and three `SB_HANDLE_SETUP_EXITED` hits, but no workspace activation or `SBToAppsWorkspaceTransaction` setup-app return. The old fixed return breakpoint was at another call site's lookup return and therefore cannot say whether these Buddy lookups returned nil. The next capture arms a per-call dynamic return at the entry's saved LR and records x0/isa, while serializing each swipe motion until HID dispatch. These are narrow read-only probes; no passcode, model, fixture, SpringBoard, CLASS16, A408, or scanout change was made. D572/D582 remain hypotheses.

P155 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin155-home-touch1.qcow2` has SHA-256 `00c1f7d813346d9cea73227decdde3297648be08c1f340f876e09cd6d82f0992` and passed `qemu-img check -U`. Its recovery bundle is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN155_HOME_TOUCH1/`.

#### PLUGIN161: Home entity absent in the observer-rich input window

P161 used a fresh child of durable P158 with the merged DVMI2 trust cache. The shell-service return was observed and the Home key was acknowledged, but SpringBoard did not return a non-null Home entity during the configured 180-second wait. The host then delivered two bottom-edge swipes and a later Home retry. The sampled framebuffer remained uniformly black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). Optional CA-handoff, render-scheduler, hotplug, and mutator traces were enabled; LLDB later detached at a render-scheduler breakpoint, so post-detach input has delivery evidence only. This run does not establish gesture acceptance or a visible Home/unlock result. The next control uses the observer-light profile with the explicit unlock-render wrapper so gesture callbacks are installed.

The durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin161-unlock-double1.qcow2` (SHA-256 `f818357564be5a2193c822ec93b292d7f5acbc1395fa154528802e5039a57e10`) passed `qemu-img check -U`; its bundle is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN161_UNLOCK_DOUBLE1/`. No passcode, model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.


#### PLUGIN167: Home entity returns; sampled natural display update is ignored

P167 used a fresh child of durable P164, the merged DVMI2 trust cache, and the post-shell Home/gesture/present callbacks with the expanded scheduler trace. The shell/check-in ladder returned, the Home key was ACKed, and SpringBoard returned a non-null Home entity about 340 seconds later. HMP remained uniformly black at 1179×2556 (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). Backboardd repeatedly entered `render_for_time` and passed its sampled full-path predicate with a non-null temporary list. At the active display, the sampled update had flags `0x80010`, mode byte `+0x2bb=0xff`, state bytes 4/5=`0/1`, and `ignore_update_p` (V170) returned 1. No subsequent branch marker or CA/server/surface callback was captured. This identifies an ignored natural update in the sampled path; it does not establish a DCP/A407 defect.

The gated bottom-center swipe began only after Home entity creation but did not complete. QEMU's input status ended at `guest_state=L` with six dispatched operations, 21 pending wire bytes, and one not-ready/overflow drop; serial recorded an input record unacknowledged after 10 seconds of guest time. There was no guest `DVMI2R L` or `DVM_HID_RECOVER` event, so classify this as host-side ACK-timeout coverage loss rather than a proven HID service termination or gesture result. HMP stayed black, and no Home/unlock success is claimed. The trigger result label was corrected to distinguish an entity-ready but incomplete swipe.

The durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin167-unlock-present-sched1.qcow2` (SHA-256 `aaa6f7e970f031af789b33a93439ffd8d4437f97dab667860010adcd4617c964`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN167_UNLOCK_PRESENT_SCHED1/`. P168 disables the high-frequency scheduler trace during the input window and retries only with the ready helper. No passcode, model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN168 / PLUGIN169: wrong-TC setup miss; corrected-TC P164 child still rejects the bottom swipe

P168 is not a UI result: its launch profile selected `system.tc`, which does not authorize the installed DVMI2 helper. The helper never reached ready state, so no input was sent. P169 corrected this with the merged `system-plus-dev6-input.tc` and a fresh child of P164. The shell/check-in ladder returned, SpringBoard acknowledged Home, and a non-null Home entity was available. The first `(589,2300)→(589,1100)` swipe was above the P141-tested edge. A follow-up exact P141 bottom-center swipe `(589,2540)→(589,500)`, 600 ms/32 steps, sent and ACKed all 38 records, dispatched 33 without failure, and had no coalescing. The Cover Sheet recognizer still entered Failed (`UIGestureRecognizerStateFailed`, value 5); no user-gesture dismissal transition followed. A separately acknowledged Power command did not change the observed pixels.

Unlike P141, P169 recorded three backboardd IOSurface allocation/render/handoff callbacks after the inputs. The reconstructed 1179×2556 surface contains sparse lock-screen chrome (time, charging/status marks, info glyph, home bar) on black; it contains neither a dismissed/Home surface nor wallpaper/poster. The HMP capture remained uniformly black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). This is a rendered-surface observation, not proof about scanout. The final helper status has `contact_sent=true`, `btn_down=true`, and a non-neutral y value after an extra manual attempt made outside the trigger; that attempt is incomplete and excluded from gesture scoring. P169 therefore confirms that the P164-derived lineage did not reproduce P141's accepted gesture; it does not override P141 or prove a display defect. P168's wrong-TC setup miss and P169's corrected-TC gesture result are distinct.

P169 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin169-unlock-present-twoswipe1.qcow2` (SHA-256 `31aee86d2153c82cf9ecffdeaa02c1a4259477c15c8759635deb44260d7ec397b`) passed `qemu-img check -U`; its parent is P164, not P134. The bundle is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN169_UNLOCK_PRESENT_TWOSWIPE1/`. P141 remains the accepted bottom-edge gesture reference on the P134 lineage. The next targeted run uses a fresh P134 child, the merged helper trust cache, P141's accepted geometry, and same-boot gesture plus IOSurface observers. No passcode, model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN170: accepted dismissal transition, but no post-transition display render

P170 ran as a fresh child of the durable P134 gesture-state parent with the merged DVMI2 trust cache. Shell/check-in completed, the Home key was acknowledged, and SpringBoard reported a non-null Home entity. After an earlier swipe and retry, the exact P141 bottom-edge path `(589,2540)→(589,500)` was accepted: both edge recognizers allowed it, Cover Sheet dismissal began/changed/ended, and `LOCKSCREEN_GESTURE_TRANSITION` recorded `forUserGesture=1` at `1791647530.652` (about 545 seconds after shell return). The helper completed with 38/38 ACKs, 33 dispatches, and no dispatch failure; final input status had no active contact or button.

The transition did not produce a visible Home frame. The last `Server::render_update`, display render, and OGL callbacks were at about `1791647526`, before the accepted transition. CA commit entries continued through at least `1791647814`, but no later server render/display/OGL chain or non-null IOSurface swap was captured. HMP stayed uniformly black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`), including before/after an acknowledged Home key after dismissal. The IOSurface sample at `1791647530.188`, just before transition, contains sparse lock chrome on black. P170 confirms accepted input/gesture, not visible unlock. Its scheduler trace was disabled, so the QuartzCore decision that suppresses the next display update remains unidentified.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin170-p134-unlock-present1.qcow2` (SHA-256 `b152eba6a4c5641a8f4d498be99d680828d6a2d9eeedb1de30c5583add9198aa`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN170_P134_UNLOCK_PRESENT1/`. P171 enables the pre-V170 scheduler decision observer from boot and repeats the delayed bottom-edge gesture. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN171: natural display updates are rejected; late swipe delivered after debugger loss

P171 was a fresh child of durable P134 with the merged DVMI2 input trust cache and the unchanged CLASS16/D572/D582/ECHO/A407 hypothesis profile; A408 remained disabled. The shell/check-in ladder returned, and SpringBoard returned a non-null Home entity about 367 seconds later. HMP captures remained uniformly black (1179×2556, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).

Before input, the read-only QuartzCore scheduler trace sampled 128 active-display `render_for_time` full-path decisions. Each sampled update had flags `0x80010`; the active display had mode byte `+0x2bb=0xff`, state bytes 4/5=`0/1`, and zero pending/ignore bytes. `IOMFBDisplay::ignore_update_p` (V170) returned 1 in every sample, so those updates skipped before Server render/surface creation. This matches the predicate inputs established by GATE236; it is not evidence that the accepted P170 transition was processed by this renderer path, because LLDB disconnected before the gesture window.

The first swipe attempt overlapped a QEMU debugger pause. I detached/reconnected only to resume the VM and released the held contact; its gesture result is unscored. A later bottom-edge swipe `(589,2540)→(589,500)` (600 ms, 32 steps) completed with 37/37 records ACKed, 33 HID dispatches, zero dispatch failures, and no coalescing. Its before/after HMP hashes were identical. No P171 gesture-acceptance callback or post-gesture render decision was captured, so this is input-delivery evidence, not an unlock result.

The durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin171-p134-render-decision1.qcow2` (SHA-256 `551751ce085b27d48d185e35179430e51eb42f1f8c0cbaec90954b759ae8a8dd`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN171_P134_RENDER_DECISION1/`. Next trace should minimize breakpoint load and capture the active display's update flags plus the actual prepared-layer contribution on the same `Update*`, while retaining gesture and HMP evidence. Do not force the ignore predicate, change DCP/A407, or infer D572/D582 firmware correctness. No passcode or model/fixture/SpringBoard/CLASS16/A408/scanout change was made.


#### PLUGIN172: accepted lock dismissal still has no visible Home frame

P172 is a fresh child of durable P134 with the merged DVMI2 trust cache and unchanged D572/D582/ECHO/A407 hypotheses; A408 remained disabled. The shell/check-in ladder returned, SpringBoard produced a non-null Home entity, and workspace create/execute requests ran. SpringBoard later recorded `LOCKSCREEN_GESTURE_TRANSITION` with `x3=1` for a user gesture. This confirms an accepted dismissal transition, but not that the lock layer became hidden or that a Home surface was displayed.

Before the transition, backboardd rendered three nonblack 1179x2556 IOSurface buffers with sparse lock-screen chrome on black (largest sample about 10,314 nonblack pixels). The HMP frame stayed uniformly black before and after input (SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). SpringBoard CA commits continued after transition; later framebuffer-swap handoffs carried null IOSurface arguments, and no post-transition Server/OGL render or non-null surface was captured. The bounded window therefore establishes accepted input plus pre-transition lock-surface content, not a visible unlock or Home frame. The Update/V170 trace was not transition-gated in this boot, so its pre-transition samples do not classify the post-dismissal renderer branch.

P173 gates Update/V170 and CA message observations on a marker written only by the accepted `x3=1` transition. P172's durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin172-p134-update-gate-min1.qcow2` (SHA-256 `1caccd925ae5763d846864792047b251869e918408cd71533e40444f0bc8e9f4`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN172_P134_UPDATE_GATE_MIN1/`. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN173: accepted dismissal; sampled post-transition updates pass V170, but no visible frame

P173 was a fresh child of the durable P134 gesture-state lineage with the merged DVMI2 input trust cache. SpringBoard returned a non-null Home entity and logged workspace create/execute activity. `LOCKSCREEN_GESTURE_TRANSITION` recorded an accepted user gesture (`x3=1`, PC `0x22dd39560`, lock manager `0x793dae6300`, velocity about 0.458) at `1791652814.948`. The trigger's automated swipe result exited 4, so the accepted transition is not attributed to that automated result without a matching input record. This is transition acceptance, not visible unlock.

Three marker-gated active-display `render_for_time` decisions arrived about 2,063–2,066 seconds after the transition. Display ID was 1, mode byte `+0x2bb=0xff`, state bytes 4/5 were `0/1`, and each Update had flags `0x01000018` (`flags & 0xa = 8`). V170 returned `w0=0` for all three: the sampled updates passed the ignore gate. P173's loaded probe ended at V170; it did not record the following compare, branch successor, Server render, or surface for those Updates. The expanded read-only successor trace was added afterward for P174 and was not active in P173.

All HMP samples, including the final 1179×2556 frame, remained uniformly black (`nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). Earlier IOSurface captures contained sparse lock chrome, but P173 did not establish a post-transition nonblack surface. Reattaching the present observer reset its Home-return state, so post-reattach silence from that observer is not a negative result. Six early Update-logger `KeyError('time')` rows preceded the runtime correction; the later 18 flag checks and three V170 pairs are valid. No passcode or model/fixture/SpringBoard/CLASS16/SGX/A408/scanout change was made; D572/D582 remain hypotheses.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin173-p134-ca-after-accept1.qcow2` (SHA-256 `86f544c856c9e77665a0541e58603f5bc478969ad3b99ed7893ae5e50fd2aab5`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN173_P134_CA_AFTER_ACCEPT1/`. P174 loads the compare/branch/successor hooks from boot, gated on a new accepted-transition marker, to identify the natural next path.

#### PLUGIN174 — Home input preceded the shell return; later renders are still lock chrome

P174 ran as a fresh child of the durable P134 gesture-state image with the accepted merged DVMI2 trust cache. A chronology error invalidates its initial post-shell input claim: the UI observer became ready at `1791656519.874` and the trigger sent Home at `1791656519.886`, but the actual `SHELL_SERVICE_RETURN` was not until `1791657652.055` (about 1,132 seconds later). The initial accepted user transition at `1791657180.722` therefore occurred before migration check-in and shell return.

SpringBoard later recorded automatic lock transitions (`x3=0`) at `1791657383.273` and `1791657790.814`; the latter followed shell return. The associated workspace callback stack includes `_handleBuddyLaunchFinished` and the lock manager/coordinator lock path. Subsequent manual Home and bottom-edge swipes were ACKed/dispatched but produced no later gesture-dismissal callback. Delivery ACKs do not prove UIKit received or accepted those later gestures.

SpringBoard nevertheless returned non-null Home entities, created/executed workspace transitions, and attached a poster scene. Backboardd later reached OGL render entries at `1791658962.064` and `1791658967.162`. Full-size IOSurface captures at this stage had 9,884, 7,108, and 10,734 nonblack pixels, remaining sparse lock chrome on black; HMP stayed uniformly black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). This proves later render activity and lock-surface content, not a visible Home/unlock frame or scanout.

The trigger now optionally gates Home/swipe input on this boot's `progress.SHELL_SERVICE_RETURN.json`, and the update-gate wrapper enables that gate by default. The SpringBoard lock manager/coordinator entry and dynamic-return observers have also been added for the next boot; they have not yet been runtime-validated. Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin174-p134-postv170-path1.qcow2` (SHA-256 `a66c04311bba63c45670d7129887138d86a4b19a51c2b22f8bab30a67895e6d0`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN174_P134_POSTV170_PATH1/`. A408 stayed off; no passcode or model/fixture/SpringBoard/CLASS16/scanout change was made. D572/D582 remain hypotheses.

#### PLUGIN176: lock chrome reaches IOSurface, but HMP stays black

P176 was a fresh P134 child with the merged DVMI2 trust cache and an explicit shell-service-return gate. SpringBoard returned a non-null Home entity, and the Home key plus two exact bottom-edge swipes `(589,2540)→(589,500)` were ACKed and dispatched: 37/37 and 38/38 records, with no coalescing or dispatch failures. HMP before/after input remained uniformly black at 1179×2556 (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The observer ended on its planned post-input completion marker, not a guest crash.

Backboardd produced 1179×2556 BGRA surfaces and non-null framebuffer-swap handoffs. The third raw surface (`events/first-surface/iosurface-007-0x7594d5d9e0-hit3.raw`) contains 10,228 nonblack pixels of 3,013,524 (9,514 above RGB 32; peak RGB 255). It visibly contains only status time/indicators, swipe-up prompt, info glyph, and home indicator against black. It has no wallpaper, large lock clock, or Home icons. This is compositor-surface evidence, not scanout evidence; adjacent HMP captures were entirely black.

The loaded readiness record identifies `home-render`, scene-pipeline, Setup-scene, and first-surface hooks, but no lock-gesture callback was loaded and no `LOCKSCREEN_GESTURE_TRANSITION` was observed. Therefore the acknowledged swipes have no gesture-acceptance verdict. Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin176-p134-shellgate-scenesurface1.qcow2` (SHA-256 `6c223fc94bdcc20e1be1448854a299b2ad3a550f032b69f4ac59c683cd2b75fe`) passed `qemu-img check -U`; records/raw captures are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN176_P134_SHELLGATE_SCENESURFACE1/`. The next cut loads the corrected recognizer/present composition and gates update sampling on its accepted-transition marker. A408 stayed off; D572/D582 remain hypotheses.


#### PLUGIN178: Home entity and rendered lock frame, but swipe rejected; touch-service probe missed this boot

P178 was a fresh P134 child with the merged DVMI2 trust cache, shell-return gate, and one ordinary bottom-center swipe `(589,2540)→(589,500)` (600 ms, 32 steps). Shell/check-in returned, SpringBoard produced a non-null Home entity, and the helper ACKed/dispatched the Home key and swipe without errors or coalescing. The swipe reached SpringBoard's `_UISystemGestureWindow` at `(294.5,1270)` and the same-timeline `SBCoverSheetScreenEdgePanGestureRecognizer` and `SBFluidSwitcherScreenEdgePanGestureRecognizer` states became Failed (5). The observed `LOCKSCREEN_GESTURE_TRANSITION` samples had `x3=0`; no accepted `x3=1` transition was recorded. The helper ended with `contact_sent=false` and `btn_down=false`.

The compositor did run late in the 1,200-second post-input interval: six `Server::render_update`/display/OGL cycles were observed. A captured 1179×2556 IOSurface has 10,540 nonblack pixels (9,637 RGB>32), visibly the lock status/time, charging icon, Chinese swipe prompt, info glyph, and home indicator on black; it has no wallpaper, large lock clock, or Home icons. HMP and trigger before/after frames remained uniformly black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). This distinguishes a rendered lock surface from the HMP scanout and is not a visible Home/unlock result.

P178 cannot answer which BackBoard touch service handled the gesture: its runtime logged `UNLOCK_INPUT_READY sites=9`, while the three direct-touch service hooks were added after that LLDB module had loaded. Do not score their silence. P179 re-arms those hooks from boot and requires the 12-site marker before interpreting service selection. Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin178-p134-dispatch-route1.qcow2` (SHA-256 `939220717e4e45df9f15bed5aeaa89e5e68723163ae1c788a99d82c117b71b25`) passed `qemu-img check -U`; records and the rendered surface view are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN178_P134_DISPATCH_ROUTE1/`. The source image remained unmodified; A408 stayed off and D572/D582 remain hypotheses.

#### PLUGIN179: accepted dismissal still yields a black HMP frame; scheduler trace was off

P179 was a fresh child of durable P134 with the merged native-input trust cache. The shell/check-in ladder returned, SpringBoard returned a non-null Home entity, and the shell-gated Home key was ACKed. The ordinary bottom-edge swipe `(589,2540)→(589,500)` (600 ms, 32 samples) was ACKed/dispatched without errors or coalescing; SpringBoard recorded `LOCKSCREEN_GESTURE_TRANSITION` with `x3=1` at `1791673177.065`. This proves an accepted user dismissal transition, not a visible Home surface.

Every HMP sample through the 2,142-second post-shell trigger bound was uniformly black at 1179×2556 (`nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). A later Home key, center tap, and ordinary swipe were also ACKed, with before/after hashes unchanged; those manual inputs occurred after LLDB coverage was detached and have no SpringBoard gesture verdict. The live trace recorded 31 SpringBoard CA commits, last at `1791672997.273` before the accepted transition, and nine marker-gated Update flag checks through `1791673645.550` (+468.5 s). At that last sample `prepared_bit22=1`, `mask_has_28000=false`, `update_flags_140=0`, and `update_state_5da=0x43`. The full `render_for_time` scheduler observer was explicitly disabled in P179, and its post-transition Server/OGL result is therefore unresolved; LLDB coverage ended before the manual retries. P180 enables that scheduler trace and keeps the post-transition gate.

The frozen child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin179-p134-touch-service1.qcow2` (SHA-256 `78c93f124b56db296d492be3d0ca9d180daa22d8b285e6578d37d745df65ac20`) passed `qemu-img check -U`; evidence is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN179_P134_TOUCH_SERVICE1/`. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.


### PLUGIN180 — scheduler-heavy capture lost LLDB before a scored dismissal

P180 used a fresh child of durable P134 with the merged native-input trust cache and shell-service-return gate. The post-shell Home entity was available, and the gesture/render/update observers reported ready. The full render-scheduler trace was enabled; it produced 128 pre-V170 samples plus related per-update rows, then LLDB lost its QEMU connection at `1791675237.298`, before an accepted `LOCKSCREEN_GESTURE_TRANSITION` (`x3=1`) was recorded. The only gesture-recognizer state event before disconnect was `_UIRelationshipGestureRecognizer` entering state 5. Those pre-transition scheduler samples had active-display flags `0x80010`, mode byte `+0x2bb=0xff`, and state bytes `+4/+5=0/1`; they do not describe a post-dismissal update.

After the debugger disconnected, the guest continued. A later ordinary Home/swipe/power/tap sequence was delivered with no held contact and no dispatch failures, but it occurred outside debugger coverage. It is not a SpringBoard gesture verdict. HMP remained uniformly black at 1179×2556 (`nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`) throughout the 3,900-second sampler window, and no A407 IDs were captured. This is a bounded coverage result; it does not classify the compositor after dismissal or establish a scanout/firmware defect.

The durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin180-p134-render-decision2.qcow2` (SHA-256 `96e457ea99be76c656f5d54ce65618628171e9f03361534233bdc7d8a7a2228e`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN180_P134_RENDER_DECISION2/`. The runner was closed after the frame-sampler bound; the trigger exit 143 reflects that deliberate cleanup, not an input result. P181 disables the high-volume scheduler and BackBoard input traces while retaining the gesture transition, minimum post-transition Update gate, compositor/present hooks, and HMP sampling. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN181 — accepted dismissal, but natural updates still skip before rendering

P181 used a fresh P134 child, the merged DVMI2 input trust cache, and a shell-service-return input gate. The Home key was ACKed; SpringBoard subsequently returned a non-null Home entity. A normal bottom-edge swipe was fully delivered, and SpringBoard recorded an accepted user-gesture lock dismissal (`LOCKSCREEN_GESTURE_TRANSITION`, `x3=1`) at `1791679625.405`. A later ordinary follow-up swipe also completed (33 dispatched records, zero dispatch failures or coalescing) but did not change the frame hash. These are accepted/delivered input results, not visible Home.

The HMP sampler completed its 3,900-second post-shell window. Every 1179×2556 sample remained uniformly black (`nonblack_gt8=0`; SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). After the accepted transition, 65 paired `RFT_AFTER_V170` / skip-target rows showed `ignore_update_p=1`, Update flags `0x80010`, and `flags & 0xa = 0`; no downstream render/OGL/surface event followed in the captured rows. The call-site `RFT_BEFORE_V170` observer emitted no samples, so P181 does not contain the active display/state inputs needed to explain the return. LLDB stopped at `RFT_V170_BIT0_SKIP_TARGET` (breakpoint 68.1, runtime `0x190315c48`); the command file then quit, and the runner could not re-freeze the kept guest. The input trigger and frame sampler nevertheless completed; debugger silence after that stop is not evidence.

The durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin181-p134-update-gate-min2.qcow2` (SHA-256 `376588885715dedae8706d75c0ada7864bfd6c493a0f7037c017289ab3130905`) passed `qemu-img check -U`; evidence is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN181_P134_UPDATE_GATE_MIN2/`. For the next child the unnecessary V170 skip-target breakpoint has been removed, a direct `ignore_update_p` implementation-entry sample added (static `0x1844561a0`), and the transition-time sampler initialized correctly so its first post-sample does not fire just because the wall clock is nonzero. This is a probe-only change. No passcode, guest/model/fixture/SpringBoard/CLASS16/A408/scanout modification was made; D572/D582 remain hypotheses.

#### PLUGIN184: accepted user dismissal, but scanout remains black

P184 was a fresh child of durable P182 with the merged DVMI2 input trust cache. The shell/check-in ladder returned, SpringBoard returned a non-null Home entity, and the manual Home key and bottom-edge upward swipe were acknowledged and dispatched without failure or coalescing. BackBoard input routing reached SpringBoard; the Cover Sheet home pan's `shouldBegin` returned true, the delegate accepted the path, and the gesture completed began/changed/ended. `LOCKSCREEN_GESTURE_TRANSITION` recorded `x3=1` (`for_user_gesture=1`) at `1791686696.572`. This closes input and gesture acceptance for this boot.

HMP captures before and after the accepted transition remained uniformly black at 1179×2556 (`nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). P184's readiness record explicitly says `update_gate_min_trace=false`, so the run did not sample the V170 decision after the accepted gesture. It does not show whether the natural post-transition update was ignored, rendered without a visible surface, or lost later in the display path. A later automatic `x3=0` lock-manager transition does not negate the earlier accepted user transition.

P184 was deliberately stopped after over 280 seconds following the accepted transition. The durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin184-p182-unlock-diag1.qcow2` (SHA-256 `7342232e1e82be70fb0f6719255eb5ee73986bf9686fe40053d37b085c4f8b9`) passed `qemu-img check -U`; records and frames are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN184_P182_UNLOCK_DIAG1/`. The trigger was updated to accept P184's custom readiness marker, and the composite callback can install the minimal Update/V170 observer after the accepted-transition marker; those changes require fresh-boot validation. No passcode or guest/model/fixture/SpringBoard/CLASS16/A408/scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN185: pre-transition V170 breakpoint ended LLDB before Home input

P185 used a fresh P182 child, and the readiness record confirms that the custom gesture/input observer and the 23-site minimum Update observer installed. However, the Update observer was still armed at shell return in this boot. LLDB stopped on its first `RFT_V170_IMPL_ENTRY` hit (`0x19755a1a0`, breakpoint 78.1) and exited before a SpringBoard Home-entity return or accepted gesture was recorded. The transition-gated Update output contains only its ready row; no V170 result, post-V170 branch, or renderer conclusion was captured. The trigger had sent Home but never passed its Home-entity gate. After LLDB detached, one manual `(589,2540)→(589,500)` swipe was fully ACKed/dispatched (37/37 records, 33 HID dispatches, zero dispatch failures/coalescing); the before/after HMP frames stayed black. With no gesture callback active, that swipe is unscored.

The run stopped by probe stop-file and durably copied child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin185-p182-posttransition-v1701.qcow2` (SHA-256 `07ff9ae0fcbb975f2b4981cb42c0980065a6f228f9f7959abf7aef134f5e9fa1`), which passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN185_P182_POSTTRANSITION_V1701/`. The next probe change defers Update breakpoint installation until the gesture observer records `x3=1`, and verifies each installed Update callback command before continuing. P185 is debugger coverage loss, not a V170 or unlock result. No passcode or model/fixture/SpringBoard/CLASS16/A408/scanout change was made; D572/D582 remain hypotheses.


#### PLUGIN186: accepted dismissal and three OGL calls; surface/swap path not yet observed

P186 was a fresh child of durable P182 using the merged DVMI2 input trust cache. Shell/check-in returned, SpringBoard initialized a non-null Home entity, and a bottom-edge gesture was accepted as a lock-dismiss transition (`for_user_gesture=1`, PC `0x225361560`, lock manager `496799143616`, velocity 0). This proves an accepted transition and a Home entity, not a visible Home or completed unlock.

The post-transition Update observer sampled three updates with flags `0x18`; V170 returned `w0=0` for each, and all three reached OGL on the same `Update*` before returning. Those events establish renderer entry, but P186 did not have the downstream Server/surface/swap observer armed. HMP stayed uniformly black through the completed 2,645-second frame window (`nonblack_fraction=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`); the guest remained responsive and the input helper reported no present. Thus P186 does not tell whether OGL produced a surface, whether a surface reached framebuffer swap, or where it was lost.

The durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin186-p182-posttransition-v1702.qcow2` (SHA-256 `f60c7ac261a7e09d77015ccb22023ed8a0eb641e9292f5b55934a23fbaa99982`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN186_P182_POSTTRANSITION_V1702/`. P187 adds a read-only post-transition Server/display/surface/swap observer, installed only after the accepted gesture. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN188: accepted dismissal, natural updates remain rejected, swap handoff has no surface

P188 was a fresh child of durable P182 with the merged DVMI2 input trust cache. Shell/check-in returned; the first Home key was acknowledged and the SpringBoard Home entity became non-null after the initial gate. A fully ACKed and dispatched bottom-edge swipe then reached SpringBoard's accepted dismissal path: `SHOULD_BEGIN_RETURN_VALUE` was true, the Cover Sheet pan began and ended, and `LOCKSCREEN_GESTURE_TRANSITION` recorded `x3=1` / `for_user_gesture=1` at `1791694519.803`. A subsequent ordinary Home-key command was also acknowledged, but its before/after HMP captures were identical.

The HMP frame stayed uniformly black at 1179×2556 throughout the 3,000-second post-input sample (`nonblack_gt8=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). On the accepted transition's later BackBoard swap activity, three `FB_SWAP_HANDOFF` calls at `1791696097.402`, `.416`, and `.430` had null surface arguments (`x2=0`, `x3=0`). The minimum Update observer continued to see `ignore_update_p=1`, Update flags `0x80010`, and `update_mask_0a=0` through at least 2,694 seconds after dismissal. Thus gesture acceptance is proven, but no visible Home frame or non-null post-dismissal surface reached the observed swap calls. This is not a passcode/unlock result and does not establish why mode/update eligibility remains in that state.

The child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin188-p182-swipe-sequence-present1.qcow2` (SHA-256 `6b476a3c0f618a86b5275e437af3a089f925ba29419f5a79c46c7d72ea80cce2`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN188_P182_SWIPE_SEQUENCE_PRESENT1/`. P188 did not arm the new display-mode source trace or post-accepted-transition mutator-flags trace. P189 arms those read-only observers on a fresh P182 child to identify where mode `-1` and the natural update rejection originate. No passcode or guest/model/fixture/SpringBoard/CLASS16/A408/scanout change was made; D572/D582 remain hypotheses.

#### PLUGIN189: post-shell mode trace import exposed a probe setup error

P189 was a fresh child of P182 and reached `DM_PERFORM_RETURN`, BKS check-in return, and `SHELL_SERVICE_RETURN`. During `dm_gate_light_callbacks.install`, Python raised `UnboundLocalError` at the mode-trace `sys.modules` lookup because a nested `import sys` in the optional native-input bootstrap branch made `sys` local to the entire function. As a result the gate-light ready event was never written and the deferred SpringBoard observer reported `missing-gate-light-slide`; no Home entity or accepted gesture was captured, and no display-mode/mutator verdict is scored. The input helper had no failures, but the trigger was terminated after the setup failure rather than run as an unlock attempt.

The fix removes the redundant nested import and uses the module-level `sys`. `py_compile` and `git diff --check` passed. P189's durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin189-p182-mode-source-home1.qcow2` (SHA-256 `42196c24a4d38f818e8d4dc825961c7e3974b4ddd1ea3a9fd278442dbb22a546`) passed `qemu-img check -U`; records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN189_P182_MODE_SOURCE_HOME1/`. This was probe-only; no guest, model, fixture, SpringBoard, CLASS16, A408, or scanout change was made.


#### PLUGIN190 — primary display mode enumeration fails; Home input was interrupted

P190 was a fresh P182 child with the merged native-input trust cache. The migration/check-in/shell-service ladder returned and SpringBoard reached `SB_ADFL_ENTRY`, but no non-null Home entity or workspace transition was captured. The read-only mode trace shows display object ID 1's mode-array query returning `0xe00002bc`; its fallback and stored mode were `-1`, and its power-mode lookup also returned `-1`. Other enumerated display objects accepted mode 0 and returned success. This is a concrete primary-display mode-source error and a plausible contributor to black output, but P190 had no accepted gesture or post-transition render, so it is not yet a causal diagnosis.

The trigger was manually terminated before its 900-second Home-entity timeout. Two host-driven swipes then encountered a non-running VM and are unscored; a follow-up mouse-up released the partial contact, and the helper ended with `contact_sent=false`, `btn_down=false`, `queue_len=0`, `inflight=0`, and `wire_pending=0`. The runner stopped through its normal stop-file path. P191 is configured to let the trigger send one fallback swipe after its timeout and retain continuous observer coverage.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin190-p182-mode-source-home1.qcow2` (SHA-256 `2a609d6e5084e3fdc2d174a91c1afc4296b48b79211011992c4268c21dfa85ab`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN190_P182_MODE_SOURCE_HOME1/`. No passcode or model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.


#### PLUGIN191 — no cache-backed EL0 sample before the resolver deadline

P191 was a fresh P182 child using the same merged DVMI2 trust cache, but `resolve_live_dsc.py` returned `no unique cache-backed EL0 PC before deadline` with `attempts=[]`. LLDB therefore never attached, the Home/SpringBoard observers did not arm, and the trigger sent no Home or swipe input. The serial log ended at the first `dyld[1] check_np()` line before launchd's later cache-map and boot records; this is a setup/boot-coverage miss, not a SpringBoard result. The original 120-second resolver window is the only confirmed limit; the reason no qualifying PC appeared is unresolved.

The child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin191-p182-mode-source-timeout-swipe1.qcow2` (SHA-256 `6794cb35cc9fc46b0c23693df94b8d4d83046a9b5d31e1c1ca0a60fbdbe0076b`) passed `qemu-img check -U`; resolver, serial, stderr, and input-status artifacts are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN191_P182_MODE_SOURCE_TIMEOUT_SWIPE1/`. P192 increases only the slide-resolution deadline before arming the same UI observer; it does not change the guest image, model, fixture, or display behavior.

#### P192 — accepted lock dismissal and Home entity, but V170 continues to ignore the active display

P192 used a fresh child of durable P182 with the merged native-input trust cache. The shell/check-in ladder returned; the Home key and two bottom-edge swipes were fully acknowledged and dispatched without errors. SpringBoard produced a non-null Home entity. The follow-up swipe produced `HOME_GESTURE_PAN_SHOULD_BEGIN_RETURN` accepted=true, Cover Sheet dismissal began, and `LOCKSCREEN_GESTURE_TRANSITION` returned `x3=1` (`for_user_gesture=1`) at `1791700297.323`. The gesture observer's stricter sequence-confirmation field was false because it did not find the expected recent pan record; the direct accepted transition remains positive evidence. An automatic Home key after the accepted transition was also acknowledged. These inputs did not change the captured frame.

The HMP screenshots before/after Home and both swipes, plus the final frame, all had the same uniform-black SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`; the frame sampler had stopped earlier after an HMP timeout, so only the successful trigger captures are counted here. No post-transition IOSurface event or DVMI2 present was recorded. On the accepted-transition gate, 94 sampled active-display V170 calls through +1,625 seconds had display ID 1 `+0x2b8=-1` (`+0x2bb=0xff`), state bytes 4/5=`0/1`, `Update+0x140=0x80010`, and mask `0xa` absent; each V170 result was `ignore=1`. The armed `OGL_RENDER_ENTRY` and `CALL_OUTER_A` sites had zero hits. Thus SpringBoard's Home entity and lock-transition acceptance are established, but there was no visible Home frame; the active update still exits at the predicate before OGL on this boot. This strengthens the mode/update-gate correlation, without proving the underlying DCP mode answer is wrong or changing D572/D582 from hypotheses.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin192-p182-mode-source-timeout-swipe1.qcow2` (SHA-256 `7f59752e37e33af8035c5e0e80de41b20b0cbee367c29d22dc17b6b254b1b54a`) passed `qemu-img check -U`. The complete run is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN192_P182_MODE_SOURCE_TIMEOUT_SWIPE1/`. No passcode, model, fixture, SpringBoard, CLASS16, A408, or scanout change was made. Next, identify the natural producer of the missing `Update` eligibility bits / valid display mode and trace it through the first downstream render boundary; do not poke the fields or synthesize a frame.

#### PLUGIN193 — accepted lock dismissal and Home entity, still black

P193 was a fresh child of durable P182. The DM-perform, BKS migration/check-in, and shell-service returns were observed; shell-service returned `x0=0x1fcf49c28` at `1791703886.291`. SpringBoard then initialized a non-null Home entity. The Home key and bottom-edge swipe were acknowledged and dispatched. SpringBoard accepted the Cover Sheet pan (`shouldBegin=true`), and the gesture sequence completed; `LOCKSCREEN_GESTURE_TRANSITION` recorded `x3=1` / `for_user_gesture=1` at `1791704315.112653`. A Home key after the accepted transition was also acknowledged. No passcode was entered.

Despite the accepted lock dismissal and initialized Home entity, all captured 1179×2556 frames remained uniformly black (`nonblack_fraction=0`; SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). SpringBoard recorded 209 CA commits after the transition. P193 did not arm CA handoff/message or render-scheduler observers, so the client-to-server CA path and render decision remain unclassified.

The read-only mode-source trace recorded primary display ID 1's mode-array query returning `0xe00002bc` at `1791702933.990745`, selecting fallback mode `-1` and leaving current/pending mode at `-1`; the power-mode lookup also returned `-1`. Other display objects accepted mode 0. Later framebuffer layer/handoff observations had null surface arguments; later update activity mostly targeted display ID 2. This associates the primary-mode discrepancy with black output but does not prove causation or establish that a DCP answer is wrong. D572/D582 remain hypotheses. No guest, model, fixture, SpringBoard, CLASS16, A408, or scanout change was made.

The durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin193-p182-accepted-present-mutator1.qcow2` (SHA-256 `6756b122659fac3992c86c67cc7b2c0b1e78c187552c7ee5473004dfea0e331b`) passed `qemu-img check -U`; records and frames are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN193_P182_ACCEPTED_PRESENT_MUTATOR1/`. P194 adds read-only CA handoff/message, display-mode provider, and post-transition scheduler sampling on a fresh P182 child to join the accepted transition to the active Update and primary-display mode source.

#### PLUGIN194 — provider observer setup error, no UI result

P194 was a fresh P182 child, but the optional mode-provider installer raised `NameError: name 'SITES' is not defined` before `ready.gate-light.json` was written. The module's static breakpoint table is named `USER`; the eight requested provider sites are present there. The runner stopped on an explicit setup-failure marker and froze the guest before shell return or user input. This is probe setup failure only; it does not change P193's accepted-dismissal evidence or add a rendering result.

The partial child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin194-p182-ca-mode-scheduler1.qcow2` (SHA-256 `6a088d8163e4462ab52e7a288a85c5997e239a66558da8f9795b06be376c596a`) passed `qemu-img check -U`. Its partial records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN194_P182_CA_MODE_SCHEDULER1/`. The installer now filters the eight requested labels from `USER`; static validation finds all eight and Python compilation passes. P195 revalidates the corrected observer on a fresh P182 child.

#### PLUGIN195 — shell and Home entity reached; primary display still has no mode

P195 used a fresh child of P182 with the shell-service-return gate, native-input
observer, and read-only display-mode/provider trace. The shell ladder returned
and SpringBoard created a non-null Home entity after the Home key. The captured
lock transition had `x3=0`; no accepted user-dismissal transition (`x3=1`) was
observed. A complete bottom-edge swipe was acknowledged 37/37 with 33 HID
dispatches and no coalescing or dispatch failures, but the HMP frame remained
uniformly black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).
Later attempts overlapped debugger/runner stops and ended with a held contact;
they are incomplete and unscored.

The read-only provider trace strengthens the mode-source finding. BackBoard's
primary display ID 1 queried service `0x13303` through connection `0x11803`;
the `ColorElements` property read was null and the mode API returned
`0xe00002bc` with both output pointers null. The display selected and persisted
mode `-1`; its power-mode lookup also returned `-1`. Other display objects
accepted mode 0. Active-display updates still had mode byte `0xff`, state
bytes 4/5=`0/1`, and flags `0x80010`; V170 rejected the sampled updates before
the render path. No IOSurface/present event or visible Home frame resulted.
This is a stronger same-boot correlation between the missing primary mode and
the pre-render skip, but it still does not establish that a particular DCP
reply is wrong or justify fabricating mode data.

The child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin195-p182-ca-mode-scheduler1.qcow2`
(SHA-256 `1c2cb90d647e6cd1618327782cc83e0e5ffa160ba0a6e0cd1c52c789971b8136`)
passed `qemu-img check -U`; the complete recovery bundle is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN195_P182_CA_MODE_SCHEDULER1/`.
P195 stopped on the planned observer condition, not a guest crash. D572/D582
remain hypotheses; no model, fixture, SpringBoard, CLASS16, A408, or scanout
change was made. Next work should trace the genuine producer of the selected
service's mode property, not repeat input-only runs.

#### P196 — null property confirmed; static DCP publication gate narrowed

P196 was a fresh P182 child with the corrected BackBoard mode-provider observer and seven slim MIPI AP kernel probes. The first primary-display `ColorElements` query returned null for that boot's live BackBoard service and connection; `_kern_GetSupportedDigitalOutModes` then returned `0xe00002bc` with null mode/timing outputs. LLDB stopped at that planned property-result marker. The kernel breakpoint list shows the MIPI probes were installed at the boot slide, but no `MIPI_AP_*` event preceded the stop. This is a no-hit in the covered interval, not proof the MIPI driver never ran or that the static PCs are valid.

The same boot's DCP model log shows the scripted endpoint-0x37 D120 callback completed with four zero bytes and no D126/D127/D128 property callback. Static firmware disassembly narrows the real producer's prerequisites: in `UnifiedPipeline::start_ios_gated()` (`0x429b718`), the code loads global `AppleDCPDisplayService` (`0x49850a8`), reads its provider at `service+0x58`, and calls that provider's vtable `+0x130` at `0x429c118`. A zero return skips `constructColorElements` (`0x429c134`, implementation `0x4288a60`). The constructor assembles per-display property objects; the subsequent publication checks each object separately and passes each non-null one to the transfer helper at `0x4289694`. Its ordered keys are `ColorElements`, `TimingElements`, `PreferredTimingElements`, `DisplayAttributes`, and `Transport` (call block `0x42893ac` onward). P196 did not execute the DCP task or prove this method's runtime result or the presence/absence of those objects inside firmware.

The P196 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin196-p182-mipi-provider1.qcow2` (SHA-256 `99c3a0a038c43ee2a352272b404ccd75d490c2a876ad6dc2b1cc0451d1684d59`) passed `qemu-img check -U`. The durable record at `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN196_P182_MIPI_PROVIDER1/` includes the launch profile and corrected hashes for the child, parent, bootkc, device tree, and trust cache. No mode, DCP response, property payload, model, fixture, SpringBoard, CLASS16, A408, or scanout value was changed. D572/D582 remain hypotheses. The next evidence-led cut is the provider's build-matched class and `+0x130` implementation, or matching-hardware property callback data; keep synthetic D120/A475 results separate and do not invent a firmware property payload.

#### P197 — static link from the gated call to AppleDCPDisplayService's provider

The start-style body at `0x431e550` saves `x1` (provider) at `this+0x58` (`0x431e634`) and stores `this` into global `0x49850a8` (`0x431e658`). `start_ios_gated()` retrieves that service global and provider field before the `+0x130` call. Thus the receiver is the matched provider, not a separately identified global controller. Its dynamic class, slot implementation, and return value remain unresolved; the DCP task did not execute in P196's QEMU path, so there is no live value to infer. The static listing and source hash are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN197_DCP_PROVIDER_GATE_STATIC/static-chain.txt`. No guest or DCP model behavior changed.

#### P198 — MIPI timing selector and AP match target

The selector used by DCP `AppleDCPSynopsysMIPIDSIController::handleStart(AFKService *)` now has a narrower static origin. The method calls its own vtable `+0x3e8` at `0x4132dfc`; the returned value is checked against `-1`, and the non-default path combines it with a provider-returned base at vtable `+0x48` before formatting `%s-%u`. The result selects `display-timing-N` / `mipi-config-N`; the zero/default path uses the unsuffixed keys (`0x41335c8`, `0x413371c`). A separate `mipi-swmpr` property query occurs afterward (`0x4132f68` onward), so it is **not** the selector. The selector method's semantic name/input and the source of the provider base remain unresolved. The matching AP `AppleSynopsysMIPIDSIController` kext also contains `mipi-swmpr` and reads `display-timing`, `display-timing-lpm`, and `mipi-config-lpm` from its IOService provider. Shared key names are a lead, not proof that the AP provider and DCP AFK property object are the same source.

The exact AP kext personality from the P182 bootkc matches `IOClass=AppleSynopsysMIPIDSIController`, `IOProviderClass=AppleARMIODevice`, and `IONameMatch=mipi-dsim-1,synopsys`. In the saved `system.dtree` (SHA-256 `172805b97889b6b4259f4cd44ea39c5bba591a6299e15cf633502391a2fe4430`), no node name or `compatible` value contains `mipi-dsim-1`; `/arm-io/disp0` instead matches `disp0,t8140` and has scalar timing values including `frame-duration-nclks`, `frame-duration-lpm-nclks`, and `mipi-support-ioav=1`. It has no `mipi-swmpr`, `display-timing`, `display-timing-lpm`, or `mipi-config` key. The sole tree `display-timing*` property is `dispext0/display-timing-info`, which belongs to the external-display path and is not interchangeable with internal-panel timing. P196's seven MIPI AP kernel probes were installed at the recorded slide but produced no hit before the planned null-`ColorElements` stop. These facts make AP MIPI matching/property population a concrete next check; they do not prove the kext failed to start, explain the missing DCP `ColorElements`, or justify copying values across paths.

The next useful capture is an early, read-only IORegistry/driver-match observation for `mipi-dsim-1,synopsys`, followed by the actual provider object and its `mipi-swmpr`/timing properties if the kext starts. On a positive match, join those values to the DCP-side AFK property lookup or its transfer path before attributing the null `ColorElements` result. No QEMU response, device-tree property, model, fixture, SpringBoard, or scanout value changed for P198; D572/D582 remain hypotheses.

### P199 — the CLCD2 mode-table probe was the wrong path for the primary property

P199 ran for its 900-second bound on a child of the durable P182
`plugin182-p134-v170-entry-home1` parent. The natural BackBoard mode query
reached `IOMFB_MODE_COLOR_PROPERTY_RESULT` and got `value=None` from service
`0x11f03` / connection `0x13603`; the mode call returned after the null
ColorElements read. The four CLCD2 sites (`0x918d3f8`, `0x918d540`,
`0x918d5e8`, `0x918d61c`, each with runtime slide `0x20000000`) were proven
installed but had no dynamic hits. Their disassembly shows a primary/attached
object mode-table routine with an optional object at `+0x61b8`; it is a
dual-pipe/tiled-mode candidate, not the ordinary primary service property
writer. This is a probe-coverage miss for that helper, not a negative result
about primary mode publication. The boot had 193,985 serial lines, zero XNU
panics, and was frozen at the 900-second bound.

Durable child:
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin199-p182-clcd2-mode-properties1.qcow2`
(SHA-256
`56eff03479e7a71367cd28ef5b785dfe5b700d12fc9afc8e61d5533ae7eae8ec`),
validated with `qemu-img check -U`. Logs and launch artifacts are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN199_P182_CLCD2_MODE_PROPERTIES1/`.
No QEMU answer, device-tree property, DCP response, model, fixture,
SpringBoard, CLASS16, A408, or scanout value changed. D572/D582 remain
hypotheses. The next probe should resolve the primary property's actual
writer/transfer path; do not rerun the dual-pipe helper as a proxy.

#### P200–P201 — observer repair and primary mode query

P200 is setup-only: its wrapper imported `render_gate_callbacks.py` as an
ordinary Python module, so LLDB did not register the callback named by the
breakpoints. The run was stopped before a mode conclusion. P201 fixed this by
using `command script import` in `tools/re/iomfb_mode_property_callbacks.py`;
the ready marker confirmed all eight mode-provider breakpoints were installed.

P201 used a fresh P182 child with accepted `system.tc` and `system.dtree`,
CLASS16/D572/D582, and `io=0x1f`. At the live primary-display query,
BackBoard service `0x13f03` / connection `0x13e03` returned null for
`ColorElements`; the mode call returned `0xe00002bc` with null mode and timing
outputs. Runtime shared-cache slide: `0x15258000`. The boot reached Early Boot
with 1,060 serial lines and zero XNU panics, then stopped at the planned query
condition. This confirms the missing-property observation, not its cause.

Before that query, serial included `AppleDCPLinkService (pipe:0)`,
`AP DRIVER START!`, and `AppleCLCD2` registration, but no textual
`AppleSynopsysMIPIDSI` or `mipi-dsim-1` line. That bounded absence does not
prove the AP MIPI personality never loaded or that it supplies the DCP AFK
property. P200's callback-registration miss and P201's valid null-property
sample must remain separate. Neither run provides a home-screen, unlock, or
terminal migration result.

P201's durable child is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin201-p182-iokit-match2.qcow2`
(SHA-256
`f5fdcd731520848b157dd4d5cdba8d9bc3f51457c847a61a03b19aae6fc23e70`),
validated with `qemu-img check -U`. Its record is in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN201_P182_IOKIT_MATCH2/`.
No tree, DCP response, model, fixture, SpringBoard, CLASS16, A408, or scanout
value changed. D572/D582 remain hypotheses. Next, statically test whether
the AP MIPI controller's matched-provider dataflow reaches the AFK
`ColorElements` builder; matching property names alone are not a join.

#### PLUGIN202 — Home entity returned; gesture was withheld on helper setup mismatch

P202 completed the DM/BKS/shell ladder and returned two non-null SpringBoard
Home entities. It did not reach a Home or swipe verdict: optional
`INPUT_BOOTSTRAP=1` spawned a second native-input helper while the ordinary
launchd job also ran. The second process met the singleton lock; the first
remained initializing (`DVM_HID_INIT begin` only, no `DVMI2R R`). The trigger
waited for readiness and sent no input. This is a setup mismatch, not a
failed gesture. P188 is the valid no-extra-bootstrap control: its helper
reached `R`, Home/swipe were accepted, and SpringBoard recorded a user-driven
dismissal, but its HMP frame was still uniformly black.

P202 independently repeated the black primary-display result: the live
`ColorElements` read was null, `_kern_GetSupportedDigitalOutModes` returned
`0xe00002bc`, and shell/Home-return/CA-commit captures had zero nonblack
pixels. Seven early MIPI AP probes were installed at the resolved runtime
slide and had no hits before the bounded stop; that is limited coverage, not
proof the driver never starts or a DCP firmware verdict. The Home entity and
CA commit establish UI activity, not visible Home pixels.

P202 used the durable P182 parent and accepted native-input trust cache. The
child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin202-mipi-home1.qcow2`
(SHA-256
`7cab8e00e5266739115529faf911a59386f068a3b7d1f24a77b46a5ce0e9176b`) passed
`qemu-img check -U` before and after durable copy; its backing chain was
verified. Records are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN202_MIPI_HOME1/`.
The runner's self-copy of its trigger log was removed after it aborted before
post-run archival. P203 uses the P188 no-bootstrap configuration and retains
post-accepted-transition render sampling. No model, fixture, SpringBoard,
CLASS16, A408, or scanout changes were made; D572/D582 remain hypotheses.

#### PLUGIN206 — accepted lock dismissal and Home input, but no display render

P206 used a fresh child of durable P182 and the accepted V12 native-input
trust cache. The shell-service return and non-null SpringBoard Home entity
were observed. The Home key and 32-step bottom-up swipe were ACKed and
dispatched without helper drops; SpringBoard then recorded an accepted
user-gesture lock dismissal (`for_user_gesture=1`, `x3=1`) at
`1791720977.019`. This is a recognized dismissal transition, not evidence of
visible Home pixels or an unlocked framebuffer.

During the 1,800-second post-input window SpringBoard emitted 321
post-transition CA commit entries. The transition-gated BackBoard observer
recorded six framebuffer swap set-layer/handoff pairs, all with a null
IOSurface argument (`x3=0`). It recorded no post-transition Server
`render_update`, `Display::render_display`, OGL render, finish-update, or
IOSurface allocation event. The pre/post input and final HMP frames were
byte-identical uniform black (1179×2556; SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The
update-gate sample had 14 flag checks, but P206 did not arm the primary mode
provider or render-scheduler/CA-message handoff observers. It therefore
localizes the visible gap only to the path before a non-null swap surface; it
cannot distinguish mode eligibility, CA transport, or scheduler selection.

The durable child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin206-p182-v12-home-transition1.qcow2`
(SHA-256
`b13a171fdec6dfe7f47916f289d09fb4476157fc4cbea8aa862f40f8dc27f840`)
passed `qemu-img check -U`; its backing chain resolves to durable P182. The
record is under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN206_P182_V12_HOME_TRANSITION1/`.
P207 attempted boot-time mode/provider tracing, but it is a coverage miss for
the post-input question. The primary display query repeated the measured
failure: service `0x13903` / connection `0x13803` returned null
`ColorElements`; `_kern_GetSupportedDigitalOutModes` returned `0xe00002bc`
with null outputs, and display ID 1 stored mode `-1` while IDs 2/4/6 stored
mode 0. `render_gate_callbacks.on_break` then raised `KeyError: 49` at the
optional `IOMFB_MODE_VIRT_RETURN` breakpoint and LLDB exited. The selected
`dm_post_shell_home_render_callbacks` module also did not install the
gesture-transition observer, so helper ACKs and a black screendump do not
establish whether dismissal was accepted or whether downstream tracing ran.
The guest was stopped and archived as a coverage failure. An orphan-BP guard
now makes missing callback metadata log-and-continue; the next run must use
the gesture-aware transition module and check its ready marker. P207's child
SHA-256 is `bc3866f277e93193447f1081161ffc8f8f22226dc4549b2f5ec5a457be19d19c`;
it passed `qemu-img check -U` and was copied to
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin207-p182-v12-home-display-join1.qcow2`.
No model, fixture, SpringBoard, CLASS16, A408, or scanout change was made;
D572/D582 remain hypotheses.

#### P288 static — DCPDisplayInterface has a one-entry no-link fallback

In the pinned Type-4 T8140 DCP image, the helper at `0x41df3a4` is in
`DCPDisplayInterface.cpp` (the call-site log arguments identify source line
`0x51a`). It obtains an interface object through the input object's vtable
`+0x198`, then asks that object's `+0x78` and `+0x80` methods for two link
mode lists. If either list is null, the branch logs
`WARNING: no color or timing modes from the link. Faking, since we have
non-null VI.` and takes a fallback path. That path writes count `1` to both
caller-provided count slots and allocates one 24-byte record for each of the
two output lists. One record is initialized with four 32-bit ones, a word
`0x20` at `+0x10`, and `0x003c0000` at `+0x0c`; the other gets word `1` at
`+0`, and return values from interface vtable `+0x138` and `+0x130` at
`+0x4` and `+0x8`. The helper returns zero on this path. The value
`0x003c0000` is numerically 60 in 16.16 fixed-point, but this disassembly
does not identify the record schema or prove that interpretation.

This is a second, lower-level candidate-list fallback, not proof that the
`0x42a796c` `UnifiedPipeline` pair selector succeeds and not proof that these
records become the primary display's published `ColorElements` or
`TimingElements`. No direct call/data-reference join between this helper and
the builder was recovered in this pass. One structural link did emerge:
`0x41df3a4` and `0x42a796c` query vtable `+0x78` and `+0x80` with the same
PAC-diversifier immediates (`0x8c10` and `0x37f7`, respectively). This
supports a shared virtual-slot convention for the two link-list queries, but
does not prove that their receiver pointers or returned list objects are the
same at runtime. The practical real-device capture
can now include the input object's `+0x198` result, the `+0x78`/`+0x80` list
results, interface `+0x130`/`+0x138` returns, both output counts and 24-byte
records, and the helper status; those values must still be joined to the
later builder/publication path before assigning them to a display property.
The disassembly slice, strings, and hashes are preserved in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN288_DCP_INTERFACE_MODE_FALLBACK_STATIC/`.
The DCP QEMU model does not execute this firmware helper. No firmware,
property, mode, model, device-tree, or pixel response changed; D572/D582
remain hypotheses.

#### P289 — no-second-touch control captures a Lock Screen IOSurface, while HMP remains black

P289 used a fresh durable child of the P276 recap-diagnostic checkpoint and
completed the measured migration/check-in/shell path. `DM_PERFORM_RETURN`
reported `x0=1`; BKS migration/check-in returned `x0=0`, followed by
`SHELL_SERVICE_RETURN` on the check-in thread. SpringBoard created Home
entities and entered ADFL. The ordinary CoverSheet pan was accepted as a
user-gesture transition. The legacy input helper then sent its scheduled Home
key and received an ACK; the experiment disabled only the second touch swipe,
so this is not a no-input control.

Without that second swipe, BackBoard's accepted-transition capture recorded a
1179×2556, 4864-byte-row IOSurface with a black background, status bar,
charging battery, home indicator, and localized “Swipe up to open” prompt.
The decoded surface contains 10,430 nonblack pixels out of 3,013,524, and was
captured 312.29 seconds after the accepted gesture and 138.33 seconds after
shell-service return. This is direct Lock Screen content at the sampled
BackBoard IOSurface boundary, not evidence of Home icons or authenticated
unlock. HMP captures through the 420-second bound remain uniformly black.

The post-shell trace also has repeated Home-entity/ADFL, workspace, LockUI,
and poster-host activity. P289 therefore advances the measured path beyond
“no lock UI content was generated”: a sparse Lock Screen surface exists, but
the HMP/scanout output remains black. P284's follow-up-swipe run instead
captured a temporary white/status-bar surface during that swipe. Since the
timing and input sequence differ (including P289's post-transition Home key),
the comparison does not establish that the second swipe caused P284's white
surface. Next capture should place an ordinary swipe after the lock prompt is
present and keep BackBoard IOSurface and HMP sampling alive across it. No
passcode was entered; no model, firmware, device-tree, or response changed.
D572/D582 remain hypotheses. Durable child, raw/decoded surfaces, timeline,
logs, and integrity record are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN289_NO_FOLLOWUP_CONTROL1/`.

#### PLUGIN208 — post-shell hooks ready; V12 input ramdisk mismatch stopped the gesture attempt

P208 used the durable P182 parent, V12 TC, and the corrected gesture-aware
transition module. It reached `SHELL_SERVICE_RETURN`; the gesture observer
and transition-gated scheduler/CA installers printed ready markers. The
boot-time primary query again found null `ColorElements` on service
`0x11903` / connection `0x11a03`, with `_kern_GetSupportedDigitalOutModes`
returning `0xe00002bc`; mode ID 1 remained `-1` while secondary IDs accepted
mode 0. A `LOCKSCREEN_GESTURE_TRANSITION` with `x3=0` was recorded before any
input and is not an accepted dismissal. The input helper remained `L` with
no PID, ACK, contact, or dispatched event because this run accidentally used
the repository default ramdisk rather than the V12 native-input ramdisk.
The trigger sent no input; P208 is setup-only for gesture/render conclusions.

The callback-ID guard remained active and LLDB stayed attached through shell
return. P208's checked child SHA-256 is
`9c8453c236285ea8398a8f9a05caaec6a62dea479b5d2f612f65edbf9a940ad4`, stored
at `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin208-p182-v12-home-display-join2.qcow2`;
records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN208_P182_V12_HOME_DISPLAY_JOIN2/`.
The next run uses the V12 `ramdisk-input.dmg` explicitly. No model, fixture,
SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain
hypotheses.


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

#### PLUGIN213 — acknowledged Home swipes reach SpringBoard but do not begin dismissal

P213 was a clean child of durable P210 with the V12 native-input path and the
gesture, mode, and framebuffer observers armed before use. Shell/check-in and
the SpringBoard Home-entity path completed; `SB_HOME_ENTITY_READY` carried a
non-null entity. Two new 33-event bottom-up swipes were acknowledged and
dispatched without input-helper errors. BackBoard's direct-touch path resolved
the first contact to `(589,2540)` and SpringBoard's system-gesture window
hit-test returned `UISystemGestureView`. Thus the contact reached the
system-gesture hit target, but this is not itself an accepted unlock.

On both attempts `SBCoverSheetScreenEdgePanGestureRecognizer` and
`SBFluidSwitcherScreenEdgePanGestureRecognizer` received touch-began; the
Cover Sheet recognizer received movement, then UIKit reported recognizer state
5 (Failed). There was no `HOME_GESTURE_PAN_SHOULD_BEGIN`, no Cover Sheet
dismissal begin/change, and no accepted `LOCKSCREEN_GESTURE_TRANSITION`
(`x3=1`). Before/after HMP frames remained uniformly black with SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`.
The earlier successful P141 gesture used a higher first attempt followed by
this bottom-edge profile; P213 did not reproduce that first-high-then-low
sequence. This comparison gives a precise next input control but does not
explain the recognizer failure.

P213 also repeated the primary-display mode-source result: display ID 1 had
mode/target/pending `-1` (`+0x2b8`, `+0x2c0`, `+0x6b00`) and state bytes
`[0,1]`; the ColorElements query remained null and no usable primary mode was
returned. Other display objects accepted mode 0. This is a parallel display
blocker, not a demonstrated cause of the failed gesture. P213 was stopped
deliberately after the bounded input/frame capture. Its durable child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin213-p210-accepted-update1.qcow2`
(SHA-256
`1fb6d4b85ceb57a7a93c1cd3959c8568839b87289b32542e51dd4a8a6af1792d`)
passed `qemu-img check -U`; records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN213_P210_ACCEPTED_UPDATE1/`.
No passcode, device-tree/property, DCP response, model, fixture, SpringBoard,
CLASS16, A408, or scanout behavior was changed; D572/D582 remain hypotheses.

#### PLUGIN215 — Cover Sheet dismissal succeeds, but no post-transition display update follows

P215 was a fresh V12-input child of durable P210. The post-shell migration
ladder and Home-entity callbacks remained active. A 15-second bottom-up swipe
was delivered through 33/33 input dispatches; `HOME_GESTURE_PAN_SHOULD_BEGIN`
returned true, the Cover Sheet dismissal recognizer began and ended, and
`LOCKSCREEN_GESTURE_TRANSITION` recorded `for_user_gesture=1` at
1791734240.853 with `gesture_sequence_confirmed=true`. SpringBoard then
returned success from workspace transition calls and the lock manager's
`lockUI` call returned `x0=0`. A subsequent Home-key event was acknowledged,
but before/after HMP captures remained uniformly black (SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). This
closes the earlier “swipe may not have been accepted” ambiguity for P215;
it does not establish visible Home or unlock success.

The render evidence places the next gap after SpringBoard activity. CA commits
continued after the accepted transition, but the last recorded
`Server::render_update`, `Display::render_display`, OGL, surface-create, and
IOSurface-allocation entries were about 220 seconds before it. Afterward,
`fb_swap_set_layer`/handoff rows had a null IOSurface (`x3=0`), including the
last row at +452 seconds; the observed A408 build remained only an entry hit.
No post-transition visible surface or HMP frame appeared. The earlier sparse
lock-chrome IOSurface samples predate the accepted gesture and do not classify
the post-transition frame.

P215's CA-message trace was loaded by the general post-shell gesture-present
module and reached its 64-send cap before the accepted swipe. The launch
profile therefore does not prove post-transition CA send/receive coverage.
The dedicated deferred observer `dm_post_shell_home_transition_present_callbacks`
was not selected in this boot. P215 was stopped deliberately after +452 seconds
of post-transition evidence. Durable child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin215-p210-ca-handoff1.qcow2`
(SHA-256
`f00b8edffb9b6460b6b8bbd41407d8e1207a4839ecc45b9a195e7dda20d0c2ae`)
passed `qemu-img check -U`; records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN215_P210_CA_HANDOFF1/`.
Next, a fresh P210 child should use the deferred observer so CA message and
render hooks arm only after this same confirmed transition, with the accepted
gesture retained as the prerequisite and success measured by a nonblack frame.
No passcode, SpringBoard/CLASS16, DCP response, model, fixture, A408, or
scanout changes were made; D572/D582 remain hypotheses.

#### PLUGIN218 — v6 input helper mismatch; legacy swipe was not an accepted pan

P218 was a fresh child of durable P210 with the V12 input ramdisk and TC
requested, but the guest actually started `/usr/local/libexec/dvm-input`
version 6. It emitted `DVM_INPUT_READY protocol=1`; the QEMU DVMI2 bridge then
sent records the v6 helper rejected as `malformed-or-old`, so the v2 input
status remained not-ready. A separate legacy `DVMINPUT1` relay successfully
ACKed a Home event and a 15-second touch sequence. Home was sent before the
swipe, however, and the swipe's `HOME_GESTURE_PAN_SHOULD_BEGIN` returned zero;
the later lock-manager transition was explicitly not corroborated by a
Cover Sheet pan sequence. It is not evidence of an accepted unlock gesture.
HMP captures through +481 seconds remained uniformly black
(`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The
durable child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin218-p210-true-transition-ca3.qcow2`
(SHA-256
`b971c40bedd1b9410866fb802652bd081aa866582083b747b5f6e9bde8483b7a`)
passed `qemu-img check -U`; records are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN218_P210_TRUE_TRANSITION_CA3/`.
The next run uses only the legacy helper protocol and sends a long bottom-up
swipe before any Home key. No passcode or UI/model/firmware/scanout change.

#### PLUGIN219 — accepted Cover Sheet dismissal and Home entity; V178 still exits before render

P219 was a fresh child of durable replay2b/D5825-class P210 with the stock
`system.tc`, legacy DVMINPUT1 input, and deferred post-transition observers.
After correcting two invalid relay attempts (coalesced/unacknowledged gesture
records and then a release), one upward bottom-edge swipe was serialized by
ACK and completed 34/34 records. SpringBoard confirmed the Cover Sheet pan,
ended it, and returned an accepted user-gesture lock transition
(`for_user_gesture=1`, `gesture_sequence_confirmed=true`) at
`1791738306.205`. Workspace calls returned success and the Home entity was
non-null. A Home key after the accepted transition was also ACKed; no
passcode was entered. These are valid lock-dismissal/Home-state markers, not
proof of a visible Home frame.

All 1179×2556 HMP captures, including after the accepted swipe and Home key,
were uniformly black (SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The
deferred BackBoard scheduler observer recorded thousands of natural
`render_for_time` iterations and its full-branch marker, but the sampled
`+0x178` return was decimal 250 (`0xfa`), nonzero; later V248/V218/VF8/V170
and outer-render markers did not appear. Three nearby framebuffer set-layer /
handoff pairs had null IOSurface arguments. The single display-power sample
was display ID 2 and did not match an identified render receiver, so it is not
assigned as the cause. P219 did not sample the `+0x178` receiver's own
`+0x330` state bytes; old post-call x19-derived fields are stack locals and
are not valid display-state evidence.

The durable child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin219-p210-legacy-swipe1.qcow2`
(SHA-256
`e3a3a48796d6578185d3ed6deb57f917974273bc8e15ad333413f4d3d19cd6e3`)
passed `qemu-img check -U`; complete records are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN219_P210_LEGACY_SWIPE1/`.
The next read-only child logs the actual `+0x178` receiver, state bytes,
mode, and skip landing, and arms the existing hotplug-state writer watch only
after an accepted user gesture. No guest, model, fixture, SpringBoard,
CLASS16, A408, or scanout changes were made; D572/D582 remain hypotheses.

#### PLUGIN220 — accepted dismissal retains Mode(-1) and natural updates are ignored

P220 was a fresh child of durable P210 and reached the shell-service return,
created a non-null SpringBoard Home entity, and accepted a Cover Sheet
lock-dismiss gesture (`x3=1`, `for_user_gesture=1`,
`gesture_sequence_confirmed=true`) at 1791739372.679. This confirms the Home
state transition, not an unlocked screen or visible Home frame. Every recorded
1179x2556 HMP frame from the shell-return interval through +602 seconds was
uniform black (`nonblack_gt8=0`, SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).

In the debugger-covered post-gesture interval, 65 paired active-display
`render_for_time`/V170 samples retained display ID 1 mode `-1` (`+0x2bb=0xff`),
state bytes `01 01 00 00 00 01 00 00`, and natural Update flags `0x80010`
(`flags & 0xa == 0`). Every `IOMFBDisplay::ignore_update_p` returned 1. This
is consistent with the GATE236 predicate path and places those sampled updates
before OGL. It does not prove that a fabricated mode or an isolated
`ColorElements` property would fix Home rendering.

A read-only writer watch for the active display mode/state was installed
immediately after the accepted transition; it observed no writes before LLDB
detached a few seconds later. It did not cover the later frame-sampler window,
so it is not a 602-second no-writer result. Later uniform-black HMP captures
also lack render-hook coverage. The saved device-tree files from all six
`bp-patched-dev1..6` bundles are byte-identical (SHA-256
`172805b97889b6b4259f4cd44ea39c5bba591a6299e15cf633502391a2fe4430`); there
is no existing alternate MIPI tree to compare.

P220's durable child is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin220-p210-v178-state1.qcow2`
(SHA-256
`2639a27286b967f12c2a4741d21bf6e8bcbea500d166244ad8bc1e3294a018c8`),
with records under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN220_P210_V178_STATE1/`.
`qemu-img check -U` reported no errors. The recovered DCP image is a saved
input artifact, but this QEMU build substitutes its RTKit/AFK model and does
not execute that firmware; it cannot supply a live firmware return or justify
an invented panel property. No passcode, device tree, DCP response, model,
fixture, SpringBoard, CLASS16, A408, or scanout behavior was changed;
D572/D582 remain hypotheses.

#### P221 — AP MIPI start property reads tied to exact provider keys

The P182 bootkc confirms that `AppleSynopsysMIPIDSIController::start(IOService *)`
is the override at its vtable slot `+0x2b0` (`0xfffffff008155ef8` points to
`0xfffffff0096f0e68`; the method-signature string is at
`0xfffffff00776899d`). Its `x1` provider is retained as `x21`. In the method,
the controller invokes that provider's IORegistryEntry `+0x118` property
lookup for `display-timing` at `0xfffffff0096f1be8`,
`display-timing-lpm` at `0xfffffff0096f1c44`, and `mipi-config-lpm` at
`0xfffffff0096f1ca0`; the corresponding first-instruction-after-call samples
are `0xfffffff0096f1bec`, `0xfffffff0096f1c48`, and
`0xfffffff0096f1ca4`. The same start routine reads `#lanes`, `mipi-version`,
`phy-test-num`, and `phy-test` from that provider earlier. This statically
confirms the exact AP-side provider queries; it does not establish that this
controller starts, that the provider is `/arm-io/disp0`, or that these AP
properties feed the separate DCP AFK provider.

The MIPI entry/property probes were installed at the recorded slide in P196,
P211, and P212, but produced no `MIPI_AP_*` event in their captured windows.
That is bounded coverage rather than proof that the driver never starts.
Together with the saved-tree absence of the personality's
`mipi-dsim-1,synopsys` match node, the next discriminator remains an early
driver-match/start observation that records the matched registry provider.
Only a positive match should trigger provider-property capture and an
explicit join to the DCP AFK lookup. No property was copied into `disp0`, and
no DCP answer or display behavior was changed.

#### PLUGIN223/224 — accepted SpringBoard workspace transition; active display still refuses updates

P223 was a coverage miss: the helper sent pixel coordinates to a 0–32767 normalized input protocol, and the transition-gated renderer observer also rejected an accepted transition when its auxiliary gesture-sequence reconstruction was incomplete. Neither the black dump nor the quiet post-transition renderer hooks in P223 classifies the display path.

P224 used the normalized center-bottom-to-upper-middle swipe and fixed transition gate. On the durable replay2b parent, the migration/check-in/shell ladder returned, SpringBoard created a non-null Home entity, and the matched workspace request/execute path returned success. The subsequent Cover Sheet dismissal was accepted (`LOCKSCREEN_GESTURE_TRANSITION` `x3=1`, `for_user_gesture=1`) at `1791744911.763`; a CA commit followed at `1791744913.712`. The HMP captures at that commit and about 95 seconds after the accepted transition remained uniformly black (`nonblack_fraction=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). This establishes SpringBoard/Home transition progress, not a visible or unlocked screen.

The read-only scheduler trace after acceptance repeatedly entered the full `render_for_time` path on the active display (ID 1). The sampled active mode was `-1` (`+0x2bb=0xff`), state bytes were `01 01 00 00 00 01 00 00`, and Updates had flags `0x80010` with `flags & 0xa == 0`; V170 `ignore_update_p` returned 1. Thus the observed updates were rejected before normal Server render/surface presentation, consistent with P192/P220. The Server/display/swap hooks were armed, but LLDB stopped at breakpoint 169 roughly 19 seconds after acceptance; therefore their silence is only a short-window coverage limit. No passcode, model, fixture, SpringBoard, CLASS16, A408, or scanout change was made; D572/D582 remain hypotheses.

P224's durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin224-posthome-present2.qcow2` (SHA-256 `c9db7ea681d515feab6e62686dd5f7214e6e654429f4832d3af79b29a54fba24`) passed `qemu-img check -U`. Records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN224_POSTHOME_PRESENT2/`. Next, follow the natural primary-display mode-selection and provider path on a fresh replay2b child; do not force mode or alter a DCP answer.

#### P225 — accepted lock dismissal and Home state still produce a black frame

P225 was a fresh child of the durable replay2b parent with its existing trust
cache and display hypotheses. The corrected legacy-input sequence sent Home,
waited for the resulting Home entity, then submitted a normalized Recap swipe.
The matched Cover Sheet pan began and ended, and SpringBoard accepted the
user-gesture lock transition (`for_user_gesture=1`,
`gesture_sequence_confirmed=true`, `dismissal_gesture.confirmed=true`) at
guest time `1791746486.296`. This confirms lock dismissal state, not a visible
Home frame or a passcode unlock.

The matched transition was followed by 64 post-home presentation events,
including CoreAnimation commits, BackBoard `render_update`/display work,
surface allocation, and framebuffer swap callbacks. All 19 captured
1179×2556 frames from shell return through post-shell +722 seconds, including
after the accepted dismissal, were uniformly black with SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. The
natural BackBoard mode-provider observer returned null `ColorElements`
(`value_isa=None`) at three queries. The first query (service `0x13803`,
connection `0x13703`) was followed by `_kern_GetSupportedDigitalOutModes`
returning `0xe00002bc` with zero mode and timing outputs; later queries used
services `0x18203`/connection `0x18303` and `0x1f003`/connection `0x1ef03`.
This identifies the missing property at its consumer, not why it is absent
or whether that alone explains every black pixel.

P225 exposed an input-helper ordering bug: the helper waited for a Home entity
before sending Home, while this boot created the entity only after a Home
key. The helper now sends Home first, waits for the entity, clears a stale
accepted-transition marker, then swipes; if a Recap swipe is rejected, it
sends a Recap lift. `tools/input/relay.py` now emits protocol kind `R` for
Recap swipe and release packets instead of touch kind `T`. The corrected
sequence was verified in P225's manual relay log. The modified helpers pass
Python compilation and `git diff --check`.

The durable child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin225-home-modetrace1.qcow2`
(SHA-256
`e8e2c2dbe09f7b2897bb80714b82dd4067886a344af5a82c78a22ebef368b8c9`)
passed `qemu-img check -U`; records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN225_HOME_MODETRACE1/`.
The next read-only boot traces AP MIPI start, DCP MIPI proxy, AFK reports,
IOAV video-property publication, and the internal-display consumer from reset.
It will not synthesize `ColorElements` or alter the device tree, model,
fixture, SpringBoard, mode, or scanout.

#### P226 — interface-16 PUBLISH seen; accepted Home gesture, black frame

P226 used a fresh child of the durable replay2b parent. The migration,
check-in, and shell-service ladder returned successfully. SpringBoard created
its Home entity and accepted the normalized Recap lock-dismissal gesture at
guest time `1791748176.662` (`for_user_gesture=1`, pan sequence confirmed).
This is a state-transition result, not proof of a visible Home/unlock frame.
All six HMP captures through post-shell +361 seconds were uniformly black
(nonblack fraction 0; SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The
periodic sampler stopped before the trigger's +240-second post-gesture wait
completed, so P226 has no exact final post-gesture frame.

The active primary display (ID 1) again took `DISPLAY_MODE_FALLBACK_MINUS_ONE`;
its natural supported-mode query returned `0xe00002bc` with empty mode/timing
outputs. Unlike P225, the mode-provider trace was disabled, so this run adds
no new `ColorElements` sample. The early AFK observer did record a 300-byte
interface-16 `dcpmipi-controller-epic` PUBLISH. It later captured interface-13
DisplayPort parsing, but no interface-16 parse/decode/handoff before the
early session ended at slide resolution. `IOMFB_AP_START` returned `x0=1` and
the EDT-read hook did not hit in that bounded interval. This corrects the
earlier interpretation that interface 16 was absent; the report was observed,
while its downstream parse path remains unresolved.

The legacy-input helper now captures an HMP frame after its post-transition
delay and before requesting runner shutdown. The render runner can select
that validated helper directly and records the accepted-transition probe
flag. P226 did not enable post-transition present/scheduler hooks. The next
child enables those hooks plus the mode-provider observer to pair the exact
accepted gesture with the display path. No mode, property, DCP response,
device tree, model, fixture, SpringBoard, or scanout value was changed;
D572/D582 remain hypotheses.

P226's durable child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin226-ioav-early-trace1.qcow2`
(SHA-256
`584a0649c2c15ac612711762688dbc21eb742febc0e796a9f9d74fbedfda15bf`)
passed `qemu-img check -U`; records are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN226_IOAV_EARLY_TRACE1/`.

#### P227 — accepted lock dismissal and Home state; primary display remains mode-less

P227 is a fresh child of the durable replay2b parent. DataMigrator,
SystemAppMigrator, `DM_PERFORM`, BKS migration/check-in, and shell-service
returns completed. SpringBoard initialized a non-null Home entity; the
workspace execute transition returned success. The normalized Recap gesture
then produced a confirmed Cover Sheet pan and
`ACCEPTED_LOCKSCREEN_GESTURE_TRANSITION` (`for_user_gesture=1`, confirmed
gesture sequence) at guest time `1791749422.812`. This proves the lock
dismissal state transition, not a visible Home frame or passcode unlock.

The trigger remained attached for 240 seconds after that accepted transition
and saved its final HMP frame. Every saved frame, including
`post-transition-0240s.png`, was uniformly black (`nonblack_fraction=0`,
SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).
The natural primary-display (ID 1) `ColorElements` read returned null on
BackBoard service `0x13603` / connection `0x11c03`; the subsequent supported
mode call returned `0xe00002bc` with null mode/timing outputs, and the
display selected mode `-1` (`+0x2bb=0xff`). This query occurred before the
user gesture. After the gesture, 64 sampled active-display V170
`ignore_update_p` calls returned 1 with mode still `-1`, state bytes
`01 01 00 00 00 01 00 00`, and update flags `0x80010`. The scheduler's
65 time decisions had two list shapes (34 five-entry/bit17-clear and 31
single-entry/bit17-set). A hotplug-state watch was armed at the accepted
transition; no state-write event was recorded.

Post-transition presentation probes armed successfully. One SpringBoard
`CA_COMMIT_ENTRY` was recorded, with no Server `render_update`, display
render, surface handoff, or swap callback in the captured interval. The
final black frame does not identify the missing property publisher, but the
combined mode and scheduler trace localizes the immediate display boundary:
the primary display has no usable mode and its sampled updates are ignored
before the normal presentation chain. P226's interface-16 MIPI PUBLISH
remains unjoined to a parsed report or property publication; P227 did not
alter that evidence. No property, DCP answer, mode, model, fixture, device
tree, SpringBoard, CLASS16, A408, or scanout value was changed; D572/D582
remain hypotheses.

P227's durable child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin227-posthome-modetrace1.qcow2`
(SHA-256
`070f71ab0f33ad2a8c74ad3cfe450ee2b9ba536e3ae8badb6fc066304972a888`)
passed `qemu-img check -U`. Complete records, including the final screendump,
accepted-gesture marker, mode/provider trace, scheduler trace, hotplug watch,
and input relay log, are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN227_POSTHOME_MODETRACE1/`.

#### PLUGIN230 — interface-16 report repeats; no parse/handoff before mode query

PLUGIN230 used a fresh child of the durable replay2b parent. The reset-time
AFK trace observed the 300-byte interface-16 `dcpmipi-controller-epic`
PUBLISH. After live slide resolution, LLDB confirmed that the AFK
parse/decode/nub/handoff/helper probes, DCP-MIPI proxy lifecycle probes, and
the selected IOAV/internal-display consumers were re-armed. Through
BackBoard's natural ID-1 mode query, none recorded an interface-16
parse/decode/handoff, DCP-MIPI proxy start, or IOAV video/internal-display
consumer hit. `ColorElements` was null and the mode query returned
`0xe00002bc`. The run stopped at that query (coverage cut), not at a 900-second
no-event bound; it does not prove that a later parser/handoff could never run.

This full external-endpoint announce profile is consistent with the earlier
GATE221/224 queue-order result: omitting external DPTX/HDCP announcements lets
the queued MIPI PUBLISH decode and start on interface 12. That controlled
announce-list change did not create `ColorElements`, so the remaining internal
video-interface/property-publication path is still open. No answer, property,
mode, device tree, model, fixture, SpringBoard, or scanout value was changed;
D572/D582 remain hypotheses. The durable child
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin230-afk16-handoff1.qcow2`
(SHA-256
`f392d07801acf906f7b43811be76a80b080989194e726e89e0e9a51fbd6fccc8`)
passed `qemu-img check -U`. The reset/post-slide logs, launch profile,
backing chain, and result are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN230_AFK16_HANDOFF1/`.


#### P231 static — provider display-state gate precedes `ColorElements`

Static cross-use analysis of the durable T8140 DCP image
(`t8140dcp.macho`, SHA-256
`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`)
narrows the firmware-side gate. The provider retained at
`AppleDCPDisplayService+0x58` is queried through vtable `+0x130` in
`UnifiedPipeline::start_ios_gated` (`0x429c118`); zero skips
`constructColorElements` (`0x4288a60`). The same slot is used at `0x428626c`,
where `(result & ~1) == 2` selects a branch that passes the literal `external`
to a method at `this+0x180`, and at `0x4290cd0`, where zero triggers a
`+0x1d8` query used to log `IOMFB: System Headless: %d`. This supports treating
`+0x130` as a shared display/output-state gate, not as a proven method name or
known runtime value.

The finding explains exactly how a zero provider result would suppress
firmware construction of `ColorElements`, which is consistent with—but does
not prove the cause of—the natural BackBoard null property and mode `-1`
observed in P225–P230. The provider's dynamic class/implementation and its
value remain unknown; the QEMU DCP RTKit model does not execute the recovered
firmware path. No mode, property, DCP response, device tree, model, fixture,
SpringBoard, or scanout value was changed. D572/D582 remain hypotheses. A
build-matched provider implementation or real callback/value capture is the
next useful discriminator; the attached image was not used.

#### P232 static — trace the internal `VideoInterface::Plug` producer

Static disassembly of the same saved T8140 DCP image adds a second
`constructColorElements` path beyond P231's `start_ios_gated` provider gate.
The `VideoInterfaceIOAV.cpp` event dispatcher at `0x42879dc` handles
`w1 == 0` in the branch labelled `VideoInterface::Plug`; with non-null `x2`,
it records plug state and submits callback target `0x428b584` through the
interface service channel (`0x4287bf8`–`0x4287c28`). That callback calls the
builder at `0x428b590`/`0x428865c`. The separate
`VideoInterface::Plug Sync callback` at `0x42884c8` checks provider slots
`+0x850` and `+0x1f0` and then calls the same builder at `0x4288550`.

This gives a specific internal-display producer to investigate: the type-0
plug event and its callback. It does not identify the event's upstream
sender, prove the event reaches the firmware on this boot, or establish that
this is the only missing property publisher. The DCP headless-message path
(`0x431f700` → provider `+0x130` and, on a separate branch, `0x429f790`)
has no proven static connection to this plug dispatcher. Keep it separate
from the plug hypothesis. QEMU does not execute the DCP RTKit task, so a
repeat guest boot cannot score these firmware PCs; the next useful evidence
is a build-matched provider implementation or a runtime that executes the
firmware and exposes the type-0 event, callback, and resulting property.
P230's null primary `ColorElements` / mode `-1` and P227's black post-dismissal
frame remain the guest-side boundary. No property, mode, DCP response,
device tree, model, SpringBoard, or scanout value was changed; D572/D582
remain hypotheses. The attached image was not used.

#### P233 static — bootkc slot cross-check does not identify the DCP provider

`ipsw kernel cpp --methods --entry com.apple.driver.AppleMobileDispH17P-DCP
firmware/bootkc` reports `AppleCLCD2` inheriting `UnifiedPipeline2`, with
vtable `0xfffffff00808c778` slot `+0x130` resolving to
`UnifiedPipeline2::fn_0x130` at `0xfffffff00b1ecdec`. P231's call at DCP
firmware `0x429c118` uses slot `+0x130` on the provider saved by
`AppleDCPDisplayService::start`; the AP-kernel address is not the DCP target.
Matching offsets do not join the objects or prove that the methods or results
are equivalent. The dynamic DCP provider and its state remain unidentified.

The DCP Mach-O and command/result note are preserved in the durable P233
recovery record under `~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/`
(`t8140dcp.macho` SHA-256
`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`). This
cross-check does not advance the UI: P230 still records null primary
`ColorElements` / mode `-1`, and P227's post-dismissal frame remains black.
The next meaningful evidence is still the type-0 Plug producer/consumer on a
runtime that executes the DCP firmware, or a build-matched provider
implementation. No guest, property, model, or pixel data changed; D572/D582
remain hypotheses.


#### P234 static — the Plug handler gates on a startup-set readiness byte

The durable T8140 DCP image (`t8140dcp.macho`, SHA-256
`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`)
connects the internal Plug callback to one concrete startup flag. At
`UnifiedPipeline::start_ios_gated` (`0x429b718`), the function stores byte `1`
to DCP global `0x4984ec4` at `0x429b750`. In the type-0 `VideoInterface::Plug` dispatcher, callback `0x428b584`
(which calls the builder, per P232) is submitted at `0x4287c18`–`0x4287c28`.
The dispatcher then conditionally submits a separate readiness callback,
`0x428b568`, at `0x4287c54`–`0x4287c64`; that callback computes
`[x1] = 1 & ~byte[0x4984ec4]` and returns zero. A nonzero result reaches the
“AP not ready yet, mark hot-plug pending” log path at `0x4287c7c`–`0x4287c9c`.
This establishes the flag's role in the readiness test, while the surrounding
branch/order does not by itself prove that the builder is conditional on that
test. A targeted scan of the
full disassembly found one direct store to this byte, at `0x429b750`; it did
not resolve indirect aliases or establish the byte's formal source name.

This gives the smallest firmware-side discriminator: whether
`start_ios_gated` sets the byte before a type-0 Plug event, what value the
readiness callback returns, and whether the submitted builder callback runs.
It supports interpreting the byte as an AP-readiness gate from the neighboring
log string and control flow, but the semantic name and runtime value are not
independently proven. The current QEMU DCP model does not execute this RTKit firmware path,
so these PCs cannot be scored by another ordinary guest boot. No model,
property, firmware response, device-tree value, SpringBoard behavior, or pixel
data was changed; D572/D582 remain hypotheses. The disassembly and exact
search result are preserved in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN234_AP_READY_FLAG_STATIC/`.

#### P235 source cross-check — no observed firmware-originated Plug payload

The local QEMU source confirms that `dcp_afk_started()` calls
`dcp_announce()` to construct and enqueue the EPIC PUBLISH for
`dcpmipi-controller-epic`; P230's interface-16 PUBLISH is therefore a
model-generated announce, not a runtime observation of the DCP firmware.
`dcp_afk_recv()` consumes AP-originated EPIC messages and only emits a
synthetic response when the explicit probe switch is enabled. The D-series
IOMFB callback script is a separate opt-in path in `darwin_iomfb.c`; no
firmware execution links that script to the type-0 Plug dispatcher or to
`ColorElements` construction.

This corrects the evidence boundary without changing the display diagnosis:
P230 still shows no observed AP parse/handoff before its stop, GATE221/224
show only that interface-12 MIPI proxy startup/open succeeds when earlier
announces are omitted, and P232–P234 identify firmware-side candidate paths
but no real event body or provider result. A guessed callback would be
synthetic pixel-path input, so no model, response, property, mode, device-tree,
SpringBoard, or scanout value was changed. Next evidence must come from a
captured real D-series callback/payload or a DCP runtime that executes this
firmware; another ordinary QEMU boot cannot observe these firmware PCs.
D572/D582 remain hypotheses.

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

#### P237 static — narrow the firmware-side ColorElements producer inputs

The saved T8140 DCP image (`t8140dcp.macho`, SHA-256
`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`) gives
one more exact cut inside `constructColorElements` (`0x4288a60`). It loads the
provider from `this+0x5cd0`, calls provider vtable `+0x70` at `0x4288aa0`, and
returns before building properties when bit 0 is clear (`0x4288aac` →
`0x4288c70`). This is a second, builder-local gate in addition to P231's
`start_ios_gated` provider `+0x130` check; neither slot's dynamic
implementation/value is known.

On the passing path, provider slots `+0x80`, `+0x88`, and `+0x78` supply the
arrays later stored at `this+0x5c58`, `+0x5c70`, and `+0x5c78` after the
`0x41be504` array materializer (`0x4288ad4`–`0x4288bb8`). The publication
sites identify them as `TimingElements`, `PreferredTimingElements`, and
`ColorElements` (`0x42893d8`, `0x42893f0`, `0x42893c0`). Provider slots
`+0x140` and `+0x150` feed the separate valueset conversion `0x41bfb60` and
fields `+0x5c80` / `+0x5c88`, published as `DisplayAttributes` / `Transport`.
The type-0 Plug callback reaches the builder through `0x428865c`:
`0x428b590` calls the wrapper, which calls `0x4288a60` at `0x428894c`.

This makes the next useful on-device/firmware-runtime observation specific:
on one builder invocation, capture provider identity, the `+0x70` result,
and the `+0x78` returned array before tracing its property transfer. It does
not supply a guessed property body. Since QEMU does not execute this DCP
firmware path, no normal guest run can score these instructions. No QEMU
response, property, mode, device-tree value, SpringBoard behavior, or pixels
changed; D572/D582 remain hypotheses. Details and boundaries are in
`docs/re/dcp-firmware-services.md` P237.

#### P238 — shell return and Home entity recovered; user-gesture dismissal not reached

P238 is a fresh durable child of the replay2b D5825 parent, using the accepted
`system-plus-dev6-input.tc`. The launchd `xpcproxy com.apple.datamigrator`
spawn returned zero; DataMigrator and SystemAppMigrator ran; the matched
SystemApp sync returned; `DM_PERFORM_RETURN`, BKS migration/check-in returns,
and `SHELL_SERVICE_RETURN` were recorded on this boot. SpringBoard then entered
the ADFL path. Its return breakpoint was armed and returned the same non-null
object at guest time `1791765084.476`, about 187 seconds after entry.
`SB_WORKSPACE_CREATE_REQUEST` and `SB_HOME_ENTITY_INIT_ENTRY` also returned
non-null objects, and `SB_HOME_ENTITY_READY` was emitted. This is slow ADFL
completion, not a permanent ADFL stall.

The legacy DVMINPUT1 relay (the input protocol actually served by this disk)
received successful ACKs for the Home-button pair and every normalized Recap
swipe step. SpringBoard input/gesture callbacks observed the Cover Sheet pan:
the Cover Sheet recognizer's `shouldBegin` returned true, dismissal began and
changed, and the same pan recognizer ended. A lock-manager transition callback
was recorded at `1791765071.570551` with `x3=0` (`for_user_gesture=0`) before
the pan began. The workspace execute transition returned `x0=1` both before
and after the swipe. No later `LOCKSCREEN_GESTURE_TRANSITION` with `x3=1`
appeared, so the trigger withheld its follow-up Home event. A successful
workspace transition alone does not establish an accepted lock dismissal or
visible Home.

The read-only render observer logged three SpringBoard CA commit entries and
two returns but no Server render-update, display-render, OGL, IOSurface, or
swap event. The HMP captures at shell return, after the first Home ACK, at
Home-entity return, at a SpringBoard-attributed CA commit, and later in the
post-shell interval were all 1179x2556 uniformly black with
`nonblack_fraction=0` and SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. The
SpringBoard/Home lifecycle evidence therefore advances past shell-service
return, while the user-gesture transition and visible UI remain unproven. The
earlier P227 run on this lineage did accept the same normalized Recap gesture;
P238 does not supersede that result. P238's durable child has SHA-256
`f1bc8291841c4950e04d23ffdfd8db7f9a1d70797869123c5418af2b761dd8da`, passed
`qemu-img check -U`, and its logs, event bundle, launch profile, and frames are
under `~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/`
`UI_CLASS16_REFRESH60_DM_PLUGIN238_POST_ACCEPTED_HOME1/`. No firmware,
device-tree, model, lock state, or pixel data was changed; D572/D582 remain
hypotheses.

The next replay2b UI capture uses one evidence-based host-side ordering change:
the legacy trigger waits for the matching `SB_ADFL_ENTRY_RETURN` as well as
`SB_HOME_ENTITY_READY` before sending the bottom-edge swipe. P238 measured that
ADFL return about 187 seconds after entry. The change will be scored on its
next boot; it does not modify guest state.

#### P239 — accepted lock dismissal reaches render/surface/swap sites; HMP remains black

P239 used a fresh child of the durable replay2b parent and the legacy
DVMINPUT1 relay. The full migration ladder returned successfully in this same
boot: `DM_PERFORM_RETURN` (`x0=0`), BKS migration/check-in returns, and
`SHELL_SERVICE_RETURN`. SpringBoard emitted `SB_HOME_ENTITY_READY`; its matching
ADFL call returned the same non-null object about 227 seconds after entry.
The trigger waited for both markers before swiping. Every Recap swipe point
and release was ACKed. The accepted pan recognizer returned `shouldBegin=1`,
then the Cover Sheet dismissal began, changed, and ended on that same
recognizer. At `LOCKSCREEN_GESTURE_TRANSITION` (`0x233a59560`,
`1791766376.981`) the lock manager recorded `x3=1`,
`for_user_gesture=1`, with the matching pan sequence. Only after this marker
did the host send the second ordinary Home event; that Home pair was ACKed.
This closes the P238 transition-coverage gap. It establishes an accepted
lock-dismissal transition plus a Home request; it does not by itself prove
the Home UI became visible.

The accepted transition opened the deferred render observers. In the ensuing
300-second observation window SpringBoard produced 158 CA-commit entries
(three observed returns); BackBoard entered one server render update, one
display render, and one OGL render. BackBoard also returned success-shaped
values from IOSurface allocation and surface creation (`x2=1` at both
recorded return sites), and the framebuffer swap set-layer/handoff sites ran
15 times. However, all 15 `FB_SWAP_SET_LAYER_ENTRY` calls had `x3=0`, and all
15 following `FB_SWAP_HANDOFF` calls had `x2=0`. The first qword read from
set-layer `x1` was a `0x7502...` pointer value, numerically different from the
allocation candidate (`0x756eabab80`); its type and relationship to the
handoff object were not established. The candidate surface was not connected
to the swap argument in this trace. The V170 return samples reported `x0=1`. These
hits show that the
post-transition path reaches substantially deeper into the compositor and
surface/swap code than P238, but do not show a non-null IOSurface at the swap
boundary or prove physical panel scanout/non-black content. The sampled
display mode field at `+0x2b8` remained the
all-ones `-1` sentinel, and the pending field at `+0x7558` was zero. Every HMP
capture, including `post-transition-0300s.png`, remained uniformly black at
1179x2556 (`nonblack_fraction=0`, hash
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).
Thus the logical lock-dismissal/Home request is now demonstrated, while
visible Home pixels remain unresolved; do not classify this as blank poster
content or as proof of a DCP firmware defect.

The post-input host trigger stopped normally after the requested observation
interval (`runner_rc=0`, `trigger_rc=0`). The durable child is
`~/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin239-adfl-home1.qcow2`,
SHA-256 `4d5e3c7f7f7c2a82957cf581bcf19f0e728541cc819e8ec8b0d1c4eddd49457d`;
`qemu-img check -U` reported no errors. Its logs, full event stream, and
frames are under `~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/`
`UI_CLASS16_REFRESH60_DM_PLUGIN239_ADFL_HOME1/`. P239 also observed a later
`DM_PERFORM_RETURN` with `x0=1` after the gesture; this is recorded separately
from the successful pre-shell return (`x0=0`) and is not used to infer a UI
failure. No firmware, model, device-tree, display mode, or pixel data was
changed. D572/D582 remain hypotheses.

Next, decode the captured framebuffer swap layer/object fields and connect
them to the IOSurface/render result and display-mode state on this same
accepted-transition path. Keep the measured shell/Home/gesture chain as the
setup waypoint; do not repeat migration or input discovery unless that chain
regresses.

#### P240 instrumentation — accepted-transition allocation and swap-object join (boot-validated by P240C)

The next read-only probe revision defers IOSurface metadata/pixel capture until
the accepted `for_user_gesture=1` marker, rather than opening that capture
window at the earlier Home/input marker. It samples up to two
`ALLOCATE_IOSURFACE_RETURN` candidates and records metadata plus a bounded raw
capture, then compares their pointer values with `fb_swap_set_layer` `x3`.
For each observed swap handoff it also reads the raw qword at `x0+0xb38`,
records `x2`, and compares handoff `x0` numerically with the latest same-thread
set-layer `x1` first qword. These are pointer-value comparisons only; they do
not assign object types or ownership. The probe remains observational and
does not force A408, write guest state, or synthesize pixels. P240C validated
the gate and callbacks. It captured one allocated-surface candidate, but its
decoder returned invalid geometry and the swap comparisons remained
pointer-only. See P242 for the bounded live result.

#### P241 static — `constructColorElements` caches the timing/color pair

Further disassembly of the saved, hash-identified T8140 firmware narrows P237's
builder trace. After provider vtable `+0x70` returns with bit 0 set, the builder
checks byte `this+0x5ca6` (`0x4288ab4`–`0x4288abc`) and returns without calling
the provider array getters when its bit 0 is already set. On the build path,
provider `+0x80` and `+0x78` results are materialized into builder fields
`+0x5c58` and `+0x5c78`. Near the return (`0x428952c`–`0x4289540`), firmware
sets `this+0x5ca6` bit 0 only if both stored pointers are non-null. In this
control flow, that byte is a cached-pair guard for the TimingElements and
ColorElements references; it does not reveal their array contents or explain
why a real provider might return null.

The discriminating hardware observation should therefore include the
pre-call `this+0x5ca6` byte, provider identity and `+0x70` result, getter returns
from `+0x80`/`+0x78`, the resulting `+0x5c58`/`+0x5c78` fields, and the
subsequent transfer. The static analysis is based on the same T8140 DCP image
used for P237 (`8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`);
the stripped image supplies no provider implementation or runtime value, and
the QEMU DCP model does not execute this firmware code. No guest boot, model,
property, mode, device-tree, or pixel data changed.

#### P242 — accepted lock dismissal reaches render/swap code, but no surface is handed to the sampled layer

P240C continued the durable replay2b shell path through SystemApp `CLIENT_SYNC_RETURN`, `DM_PERFORM_RETURN`, BKS migration/check-in returns, and SpringBoard shell-service return. The same boot created a non-null Home entity, returned from the ADFL call, accepted the Cover Sheet pan (`for_user_gesture=1`, matched `shouldBegin`/began/ended callbacks), then accepted the follow-up Home event. This proves the logical lock-dismissal and Home transition on this boot; it does not prove visible Home pixels.

The accepted transition gated a 300-second render observation. HMP frames from shell return through the final post-transition sample remained uniformly black at 1179x2556 (`nonblack_fraction=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). One transition-associated Update passed the sampled V170 ignore gate (`return_w0=0`, flags `0x400080018`) and reached Server `render_update`, `render_display`, surface creation/allocation, and OGL render. The surface and creation calls returned pointers/success-shaped values, but the current IOSurface metadata decoder read impossible geometry and no pixel conclusion is drawn from it. The display mode field at `display+0x2b8` still read `-1` in the scheduler sample.

During the same window, the framebuffer set-layer/handoff callbacks ran 15 times. Every set-layer call had `x3=0`; every handoff had `x2=0`. In all 15 same-thread pairs, handoff `x0` equaled the first qword read from the immediately preceding set-layer `x1` descriptor; the sampled dispatch slot at object `+0xb38` was consistently `0x246d3eeec`. This proves that the handoff receives that descriptor's referenced object, but does not identify its dynamic type or the slot's behavior. The referenced object differed from the allocated-surface candidate, and no non-null IOSurface was established at the layer boundary. Thus the accepted gesture is followed by real compositor and swap-site activity, but the allocated candidate is not joined to those swap arguments. This localizes the next measured gap after the transition/update gate and before a proven pixel-bearing handoff; it does not assign cause to the `-1` mode sentinel, DCP firmware, A407, or scanout.

This is a real null-argument result at the observed API boundary, not merely the failed metadata decoder: P103 previously captured a non-null IOSurface in `x3` at the same `fb_swap_set_layer` entry and decoded sparse lock-screen chrome from it. P240C's 15 post-dismissal calls instead submitted no surface in `x3`; that contrast still does not identify why the later Home-layer state had no surface.

The child is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-plugin240c-surface-join1.qcow2` (SHA-256 `17c2c652b22391ca2b5786226be6ad2ea14c4169f4bceb3c23ce41fe4d448a1b`); `qemu-img check -U` reported no errors. Full logs, events, frames, and launch profile are in `~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN240C_SURFACE_JOIN1/`. The post-gesture trigger and runner both exited successfully. No DCP/model/device-tree/mode/pixel changes were made. D572/D582 remain hypotheses.

#### P243 — shell/Home revalidated; accepted dismissal missed by the host timeout

P243 used a fresh child of the durable replay2b parent and reached
`DM_PERFORM_RETURN`, BKS migration/check-in return, and
`SHELL_SERVICE_RETURN` on the same SpringBoard thread. The Home key was
acknowledged, SpringBoard created a non-null Home entity, and the measured
ADFL call returned with a non-null result. The subsequent Recap pan input was
acknowledged. SpringBoard then emitted an accepted Cover Sheet dismissal
(`for_user_gesture=1`, `shouldBegin` accepted, matching pan began/ended), but
the host trigger's fixed 60-second marker wait expired about five seconds
before that marker was written. The runner therefore stopped before the
accepted-transition render window; P243 contains no post-dismissal pixel or
OGL result and is a trigger-timing miss, not a dismissal failure.

The trigger now accepts a configurable `--accepted-transition-timeout`
(default 180 seconds); the runner passes and records the corresponding
`LEGACY_ACCEPTED_TRANSITION_TIMEOUT`. A fresh P244 sibling is testing this
host-side coverage correction with the same measured path and a 300-second
post-transition observation. P243's durable child is
`ui-class16-refresh60-dm-plugin243-ogl-layer-join1.qcow2`, SHA-256
`e196eae65e54fcb27a6879f6ae0df3245120320d94cbb5dfe782627b128fb5cf`; its
recovery bundle is `recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN243_OGL_LAYER_JOIN1/`
and `qemu-img check -U` passed. No guest, DCP, model, fixture, device-tree,
mode, or pixel values were changed. D572/D582 remain hypotheses.

#### P244 — accepted transition outlasted the gesture correlator's 15-second window

P244 reused the replay2b parent with the longer host marker wait. The
migration/check-in/shell ladder cleared; SpringBoard produced the Home entity
and a non-null ADFL return; Home input was acknowledged; and the swipe was
acknowledged. The lock-manager transition recorded `for_user_gesture=1`.
Its event history contains a `shouldBegin` acceptance, dismiss-began, and
pan-ended for the same recognizer, with the pan-end 17.04 seconds before the
transition. The existing correlator allows only 15 seconds from pan-end, so
it marked `gesture_sequence_confirmed=false`; the trigger stopped before the
follow-up Home event or post-transition observation. This is a temporal
correlator coverage miss, not a pixel result. The correlation allowance is
now 30 seconds while preserving PID/TPIDR, accepted-shouldBegin, ordering,
and same-recognizer checks; P245 will validate that narrow change.

P244 child SHA-256 is recorded in its durable recovery bundle at
`recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN244_OGL_LAYER_JOIN2/`; the
child passed `qemu-img check -U`. No guest/DCP/model/device-tree/mode/pixel
changes were made. D572/D582 remain hypotheses.

#### P245 — accepted lock dismissal and Home path reproduced; post-transition framebuffer remains black

P245 used a fresh child of the durable replay2b parent and kept the accepted trust cache, migration gate, and measured SpringBoard observers armed. The same boot recorded `DM_PERFORM_RETURN`, BKS migration/check-in returns, and `SHELL_SERVICE_RETURN`; SpringBoard then returned a non-null ADFL result and created non-null Home entities. The Cover Sheet gesture was positively correlated: `shouldBegin` returned accepted, began and ended used the same recognizer and SpringBoard TPIDR, and the lock manager emitted `for_user_gesture=1`. Thus the shell-to-lock-dismissal/Home control path is active on this child; it does not establish visible Home pixels.

The HMP framebuffer was uniformly black at shell return, Home creation, CoreAnimation commit, render/surface samples, swap samples, and the final 300-second post-transition capture. Every frame was 1179x2556 with `nonblack_fraction=0` and SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. One transition-window Update passed the sampled V170 gate (`return_w0=0`, flags `0x18`) and reached BackBoard `render_update` and OGL entry; surface allocation/creation returned pointer-shaped values, but the allocated candidate was not joined to a pixel-bearing swap input. The correlated display-mode read was still `-1`; this is not a demonstrated cause.

The sampled swap callbacks produced 15 same-thread set-layer/handoff pairs. In every pair, set-layer `x3` and handoff `x2` were null; handoff `x0` equaled the first qword of the preceding set-layer `x1` descriptor in all 15. This confirms descriptor-to-handoff object continuity while leaving the object's dynamic type and its relation to the created surface unresolved. The measured gap remains between compositor/update activity and a proven pixel-bearing Home-layer handoff, not at SpringBoard gesture dispatch. P245 does not justify changing DCP firmware responses, mode data, the device tree, or pixels.

The child was preserved at `~/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-plugin245-ogl-layer-join3.qcow2` (SHA-256 `c03be03711819b6167dc8cf4dcf9db207ee3c7fb965931be521fae479ffae638`). Its recovery bundle, including logs and all frames, is `~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN245_OGL_LAYER_JOIN3/`; `qemu-img check -U` reported no errors. The run validates the 180-second accepted-transition wait and 30-second gesture correlation. No guest/DCP/model/device-tree/mode/pixel values were changed; D572/D582 remain hypotheses.

#### P246 — provider factory narrowed; swap handoff gains a load-address annotation

Static disassembly of the saved T8140 DCP firmware narrows the provider
selection feeding P237's `constructColorElements`: the `UnifiedPipeline.cpp`
path at `0x42861f8` derives a boolean from its 32-bit `this+0x6730` state and
passes it to factory `0x42a4890`, storing selector-0's result at `this+0x5cd0`
(`0x4286f80`–`0x4286f88`). The factory also has an `iomfb_vistub`-controlled
`0x58`-byte object branch and selector-specific `0x98`/`0x1e8` allocations.
The stripped image omits usable vtable contents and no runtime factory input
was captured, so this identifies an observation recipe rather than the real
panel's provider class or `ColorElements` source. Exact branch details are in
`docs/re/dcp-firmware-services.md` P246.

For the measured UI path, `dm_posthome_present_callbacks.py` now asks LLDB to
resolve the captured handoff object's `+0xb38` pointer against the current
process's loaded modules and emits module/section/function/symbol when
available, retaining the raw address when not. This adds no guest reads beyond
the already sampled pointer and does not dereference the candidate as code.
The annotation is implemented but not yet boot-validated; P245 remains the
last runtime result: accepted gesture, Update/OGL activity, null swap surface
arguments in 15 samples, and a black HMP capture. Do not infer that the
`-1` mode sentinel or a particular firmware provider caused the missing
pixels. No model, device tree, mode, or pixel values changed.

#### P249 static — `+0x6730` has nested readers and a guarded initializer

P249 added a cross-reference pass over the recovered T8140 DCP text. A direct
getter at `0x428f190` returns the 32-bit word at `this+0x6730`; separate
accessors at `0x4263cb4` and `0x42669f0` read `*(this+0x80)+0x6730` and
`*(this+0x90)+0x6730`. The `+0x90`-nested form is compared to `2` at several
call sites, including `0x4277378`, `0x42e4ad4`, `0x42e5d00`, `0x42e964c`, and
`0x4300338`. At `0x4277378` the result selects a branch involving the
`bics_mode` property and a later store to `x19+0x1d68`. This is evidence of
multiple consumers, not proof that these nested bases alias P246's factory
receiver.

P249 then found a guarded writer in the constructor beginning at `0x429085c`;
`0x4290a00` is an interior call site, not a function entry. After an
object check at `0x4218468`, the constructor calls `0x41e2618` and enters the
writer path only when bit 0 of that result is set (`0x4290a00`–`0x4290a08`);
the other edge branches to `0x4290bcc`. On its normal path, `0x41e2618` is
not a display-data-availability gate: it dispatches the new helper object's
vtable slot `+0x20`, stores that virtual result at helper offset `+0xc`, and
then returns constant `1` (`0x41e26b0`–`0x41e26c0`). The constructor's bit
test is therefore satisfied whenever the helper returns normally; it does
not test the virtual result or the `iomfb_system_type` property. On the
proceeding path the constructor forms
`x24 = this+0x6730` at `0x4290a10`–`0x4290a18`, zeroes the 32-bit word at
`0x4290b6c`, and reads the four-byte `iomfb_system_type` property through
`0x42abf88` at `0x4290b40`–`0x4290b70`. A nonzero helper return copies the out
word into `*x24` at `0x4290b9c`. A zero return does not settle the field: the
code enters a fallback that, depending on a global flag, performs another
four-byte query or calls a virtual method. Either path can write `2` to
`+0x6730` when its result is `1`; otherwise the initialized zero remains. The
common member offset links this initializer to P246's factory input only as a
static same-layout inference; live identity/value capture is still needed.
Nearby logging prints `IOMFB: SystemTypeBootArg - Detecting system type %d`.
This supports the field's system-type role but does not identify its enum
values. The property helper at `0x42abf88` has its own prerequisite: it calls
`0x40363a0`, then `0x4022ac8` to obtain a state word; if bit 0 is clear it
returns zero without calling keyed helper `0x4017ff0`, and if set it calls
`0x4017ff0(key, out, size)`. That callee parses a global argument-text buffer
before matching the key. This is consistent with the `SystemTypeBootArg` log,
but the state word's meaning and the real-device argument value remain
unknown; it does not show that setting an argument would create
`ColorElements`. P246's separate
`0x42861f8` path writes byte `this+0x674a` and zeroes `+0x6734..+0x6747`, not
the leading word. The next device-side cut is to join the initializer object,
property result, factory boolean/selector, and returned provider. No
`bics_mode`/`iomfb_vistub`-based fabrication or QEMU/model change follows from
these static results; D572/D582 remain hypotheses.

A focused decode makes the fallback's inputs observable. If the byte flag at
`0x4984ec4` is set, the four-byte query at `0x4290c28`–`0x4290c40` receives
selector `0x44313233`, null/zero secondary arguments, an output word at
`sp+0xc`, and length `4`; the output byte is compared with `1` at
`0x4290c44`–`0x4290c4c`. If the flag is clear, the code follows the object at
`0x49850a8` through `+0xa8` and `+0x58`, then invokes vtable slot `+0x128`
(`0x4290c54`–`0x4290c7c`) and compares its return with `1` at
`0x4290c80`–`0x4290c84`. A successful result in either branch stores `2` to
`+0x6730` (`0x4290c88`–`0x4290c8c`). The selector's semantics and virtual
slot implementation remain unknown. A device capture should therefore
record the flag and query selector/output, or the virtual receiver/return,
beside the field and the later factory input; this does not identify a value
to fabricate in QEMU.

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
that configuration cannot silently suppress the follow-up on a future run.

#### P251 static — the `+0x6730` fallback query is D123

The P249 initializer's `0x44313233` selector is the existing D123 FourCC,
not an unidentified private selector. In the constructor at `0x429085c`,
when global byte `0x4984ec4` is set, `0x4290c28`–`0x4290c40` call the shared
request helper `0x41d1a2c` with selector D123, null input/zero input length,
an output pointer at `sp+0xc`, and output length four. The caller tests the
first output byte for one and, only on that result, stores `2` into
`this+0x6730` (`0x4290c44`–`0x4290c8c`). This ties the fallback to the D123
request family already described in the DCP callback notes; it does not
identify the semantic name of the returned byte or prove that D123 supplies
the primary display's `ColorElements`.

A second call site in the routine beginning `0x42861f8` is gated by the same
global byte and calls `0x41d1a2c` with D123 but null input and output pointers
and zero lengths (`0x4287044`–`0x428706c`). It is a distinct no-payload call;
do not merge it with the constructor's four-byte-output query. The shared
helper's arguments are copied into a request descriptor and passed through
`0x41cec3c` to an indirect service method, but the stripped firmware does not
reveal the provider's dynamic class or the runtime response. This yields a
concrete device-capture target: record both D123 call sites, the constructor
output byte, `+0x6730`, and the subsequent factory selector/object on one
identified receiver. No response, mode, `ColorElements`, firmware, device
tree, or QEMU value was changed; D572/D582 remain hypotheses.

The existing endpoint sweep records D123 as a four-byte-output callback whose
QEMU-side/harness result was status zero. That is a useful comparison point,
not a hardware-correctness claim: if this constructor takes the global-flag
branch and receives a zero first byte, its static code leaves `+0x6730` at
zero; the device may return a different value. A real iPhone capture should
therefore include the D123 request/reply bytes and the same receiver's flag,
post-query `+0x6730`, factory boolean/selector, and returned provider object.


#### P250 — accepted lock dismissal reaches Home control state, but pixels remain black

P250 used a fresh durable replay2b child, `ui-class16-refresh60-dm-plugin250-replay2b-legacy-gesture1.qcow2` (SHA-256 `2ce4eec056c9aaa778056effa930c35bce762ed6da7f18c0d4468ada18eb6fba`), with `qemu-img check -U` passing. The first shell-service return at guest time `1791776517.197` armed the post-shell observer. SpringBoard later returned a non-null ADFL at `1791776658.0806`, created Home entities, and returned success from the workspace transition. The lock dismissal was accepted: `shouldBegin=true`, the same SpringBoard TPIDR and recognizer persisted across began/ended, and the transition recorded `for_user_gesture=1` with `gesture_sequence_confirmed=true` at `1791776819.5708`. Home-button down/up were ACKed. This establishes the SpringBoard control path; it does not establish a visible Home image.

Input delivery needs the corrected accounting: the initial recap-swipe relay timed out on its final touch-up; a later same-coordinate fallback release was ACKed. Do not use the older `LEGACY_INPUT_RESULT` field `swipe_acked=true` as proof that the original swipe completed. The positive gesture classification comes from SpringBoard's recognizer/transition evidence.

The 600-second post-transition frame sample contained 18 identical 1179x2556 black frames (`nonblack_fraction=0`, SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The mode observation was `-1`, but that is correlation only, not a demonstrated cause. BackBoard/render activity continued, IOSurface create/allocation calls returned, and 15 set-layer/handoff pairs preserved a descriptor relationship: each handoff's `x0` matched the first qword at the preceding set-layer descriptor (`x1`), while set-layer `x3` and handoff `x2` were zero. This does not prove the descriptor contains visible pixels or that it identifies the separately allocated IOSurface. The candidate dispatch pointer at `+0xb38` did not resolve to a loaded user module.

A `DM_PERFORM_RETURN` with `x0=1` occurred at `1791777036.814989`; a later BKS migration/check-in return with `x0=0` occurred at `1791777350.532`, followed milliseconds later by shell-service return. These events are not asserted to be one causal ladder because their timing/thread evidence does not establish that relation. P250 changed no firmware, model, device tree, mode, or pixel data; D572/D582 remain hypotheses. Full records are in `~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN250_REPLAY2B_LEGACY_GESTURE1/`.


#### P252 static — D123's AP handler is a four-byte boolean getter

A build-matched cross-check between the DCP's D123 request and the AP bootkc callback table refines P251. The AP endpoint map lists D123 at `0xfffffff00919b4f4` with a four-byte output. In that handler, `x4` is saved as the output pointer and `w5` is checked against four; values below four return `0xe00002c2` without writing the output. With sufficient output capacity, the handler reads an index from its callback context at `x0+0x38`, resolves a 48-byte entry in the IOMobileFramebuffer manager table rooted at `0xfffffff00808fe48`, and passes the selected entry minus `0x90` to `0xfffffff009186e50`. That helper replaces the incoming `x0` before preserving or reading it, so the table entry does not supply the returned value. P253 recovers the helper's test and corrects the earlier `0xfffffff00918b2a8` address attribution.

The DCP fallback call at `0x4290c28` supplies null input/zero input length but a non-null output pointer and output capacity four (`x3=sp+0xc`, `w4=4`); helper `0x41d1a2c` preserves those output arguments in the request descriptor. Thus the static call satisfies the AP handler's four-byte **output** requirement. This is not an input-length failure. P253 shows that the AP result is derived from the root DeviceTree `target-type` suffix, not the callback's selected manager entry: the helper reads `target-type` and returns true only for suffix `sim` or `imb`. The saved tree says `D47`, so QEMU's status-zero/output-zero result is consistent with this code. The constructor's conditional `+0x6730=2` path is not taken for that property value. Nothing links this predicate to primary `ColorElements`; no response or model value was changed.

P252 also extends the post-home probe for the next measured run. With `DVM_POSTHOME_SURFACE_LINEAGE_TRACE=1`, it records runtime class-pointer candidates for the allocation result and its internal object, then compares those identities against the `fb_swap_set_layer` descriptor and up to eight immediate pointer children (32 descriptor hits maximum). It keeps the existing non-null `x3` IOSurface test as the actual swap-boundary criterion. A graph match is only pointer identity; no match does not prove absence through deeper objects, and neither result is pixel or scanout proof. P250 remains the current measured run: its allocation candidate had invalid metadata under the current offsets and its sampled swap calls passed `x3=0`.

#### P253 static — D123 is a target-type check, not a display payload

The AP helper actually called by D123 is `0xfffffff009186e50`. It resolves
`IODeviceTree:/`, requests the root `target-type` property and checks its
last three bytes against `sim` and `imb`. Although the D123 handler first
checks the indexed slot in the IOMobileFramebuffer manager table, that slot
is not used to compute the helper's value: the helper replaces its incoming
`x0` immediately. The saved root property is `D47`, which makes the predicate
false; the recorded QEMU/harness output byte zero is consistent with the
checked-in tree and AP implementation.

The DCP initializer at `0x4290c28` stores `2` in `this+0x6730` only when that
one-byte result is `1`. For `D47`, retaining zero is consistent with the
static code. This closes the D123 contract question as a target-type policy
query; it does not explain the null primary `ColorElements` property, mode
`-1`, or black HMP frame. No display data, response, firmware, or model value
was modified. The command transcript, source hashes, symbol lookups, DCP call
site, and decoded root properties are preserved in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN253_D123_TARGETTYPE_STATIC/`.

#### P254 static — the D123-derived word is a factory hint with a `vistub` override

In the DCP caller at `0x4286f70`–`0x4286f88`, the 32-bit `this+0x6730`
value is reduced to a boolean and passed as `w0` to `0x42a4890`; `w1` is
selector zero. If the constructor takes the D123 branch, P253's saved D47
tree makes that branch's output false; the alternate virtual fallback can
also write `2` to `+0x6730`, and its runtime result is unknown. Thus the
factory call's live boolean is not known from the DeviceTree alone. Inside
the factory, an internal state bit decides whether to read `iomfb_vistub`.
On a successful property read, the property value replaces the caller
boolean; if the read fails, the caller boolean is used. A selected true value
takes a `0x58`-byte allocation path and sets byte `+0x50`; otherwise selector
zero uses a `0x1e8`-byte allocation. Nonzero selectors use a separate
`0x98`-byte allocation. This is a control-flow and allocation-shape result,
not a recovered dynamic class or panel provider.

The durable disassembly and source hashes are in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN254_DCP_FACTORY_BRANCH_STATIC/`.
The result does not show that changing D123 or `iomfb_vistub` supplies
`ColorElements`, changes the mode, or fixes pixels; no values were changed.

#### P253 — shell return reaches Home control state; unlock input was incomplete

The durable P253 replay2b child recorded `BKS_MIGRATION_CHECKIN_RETURN` with
`x0=0` at `1791779620.854`, `BKS_CHECKIN_RETURN` with `x0=0` at
`1791779620.864`, and `SHELL_SERVICE_RETURN` with `x0=0` at
`1791779620.867`. SpringBoard later produced a non-null Home entity at
`1791779857.602`; ADFL returned non-null at `1791779897.421`; and the
workspace transition returned success (`x0=1`) at `1791780125.472`. These
markers establish the shell-to-Home control path, not a visible Home surface.

The lockscreen transition observed before the swipe had `x3=0`; the legacy
recap swipe then ran, but its relay did not complete its final touch-up. The
fallback release also timed out, so there is no accepted dismissal marker and
no unlock verdict for P253. HMP was uniformly black. Do not infer that
SpringBoard rejected a fully delivered gesture or that the screen was visibly
unlocked. The evidence bundle is
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN253_REPLAY2B_SURFACE_LINEAGE1/`.

#### P255 static — DCP provider array helpers copy ranges and adjust references

Build-matched disassembly of `constructColorElements` (`0x4288a60`) and its
helpers shows that each non-null provider result assigned to builder fields
`+0x5c58`, `+0x5c70`, and `+0x5c78` passes through `0x41be504`; null results
skip the copy call. That helper copies pointer entries from the input range into a container; it does not
decode or synthesize timing/color entries in the observed path. Provider
results used for fields `+0x5c80` and `+0x5c88` pass through `0x41bfb60`,
which copies 16-byte pointer pairs. Both helpers call `0x41c4888` for copied
members. That routine dispatches the object's vtable slot `+0x40` with a
`+1` reference adjustment; adjacent `0x41c48c4` dispatches through the same
slot with `-1`. The dynamic method and class are unnamed, so this note does
not assign a specific retain/release API name.

This narrows the static boundary: these paths materialize provider-returned
containers; they do not prove that a live provider returned entries, that any
range was nonempty, or what was serialized over the DCP channel. Disassembly
and hashes are preserved in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN255_DCP_PROVIDER_ARRAY_MATERIALIZATION_STATIC/`.
No live provider, mode, property payload, firmware, model, device-tree, or
pixel value was changed. D572/D582 remain hypotheses.

#### P256 — Home entity is present; swipe/input coverage ended at a debug stop

P256 used a fresh P134-parent child with the accepted native-input trust cache.
The migration/check-in path reached `SHELL_SERVICE_RETURN` at guest time
`1791780582.605323`; SpringBoard later returned a non-null Home entity at
`1791781106.35857`. This confirms Home control state, not a visible Home
surface. The HMP samples, including the Home-entity frame, were uniformly
black (`nonblack_fraction=0`, SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The
sampled display-mode field was `-1`; this remains correlation, not cause.

The trigger began an upward swipe and the helper reported 30 dispatched
events, 203 acknowledged records, zero dispatch failures/coalescing/not-ready
drops, but `btn_down=true`, one in-flight event, and 14 wire-pending records
at the debug stop. The saved lock-gesture log has no accepted `shouldBegin`,
Cover Sheet began/ended sequence, or `LOCKSCREEN_GESTURE_TRANSITION` event.
LLDB then lost its connection while QEMU was debug-paused; the runner was
stopped deliberately because the virtual contact could not be released while
the VM was paused. Therefore P256 has neither a completed swipe nor an
accepted dismissal, and it is not an unlock rejection or pixel-path verdict.
The durable child, copied logs, frames, and lineage are recorded in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN256_P134_DVMI2_UNLOCK_SURFACE1/`;
the paused child copy is retained under `~/dvm-artifacts/bp-patched-dev6/boot1/`.

#### P257 static — the DCP property object is serialized before callback chunking

P257 links the P255 provider-container boundary to the D126-D128 transfer
helper. For each property, `0x4289694` calls the object's vtable `+0x30` with
a separate `0x1000`-capacity output object, reads serialized length from that
output object's `+0x48`, and obtains chunk bytes from `+0x50`. It sends D126
with length plus one, D127 with up to `0x1000` bytes plus offset/length, and
D128 with the property key. For the P255 `ColorElements` AFKArray, P236 maps
the serializer slot to `0x41c8ed4`, whose children are AFKObject IDs rather
than embedded ColorElement dictionaries. This identifies the static
serializer-to-transfer boundary; there is still no live provider, serialized
payload, or proof that the firmware producer executes in this QEMU run. The
disassemblies and source hash are in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN257_DCP_PROPERTY_SERIALIZE_TRANSFER_STATIC/`.

#### P259 static — factory `+0x130` slot interpretation remains unresolved

**Correction:** P260 below supersedes this entry's BUND-to-Mach-O offset
mapping and candidate-byte interpretation. The raw runtime slots remain
unresolved after the corrected reconstruction.

Restoring the DCP bundle's larger-task `__DATA_CONST` makes the selector-1
factory address inspectable, but does not establish a callable at
`0x4a296b8+0x130`. The factory stores a signed pointer to `0x4a296b8`; the
raw bundle has a zero first qword there. At `0x4a297e8`, the raw words have
numeric-looking float interpretations (700, 920, 1130, 1415, 1730, 2175),
not a verified code target. The candidate is retained as unresolved raw data,
not named a provider method or mode table.

The `+0x130` provider call in `UnifiedPipeline` uses the provider obtained
from `AppleDCPDisplayService`; static evidence does not equate it with the
factory-created selector-1 object. Runtime loader transformations also mean
the raw zero vptr is not a runtime null result. The restored image,
reproduction script, hash, and corrected slot report are in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN259_DCP_PROVIDER_VTABLE_STATIC/`.
No property, firmware response, mode, model, or pixel value was changed;
D572/D582 remain hypotheses.

#### P260 static — corrected DCP Type-4 runtime-data mapping

The earlier P259 copy used the wrong BUND `rdat` offset. P260 reconstructed
the extracted P233 Mach-O from the Type-4 BUND ranges (`rtxt` at
`0x32000..0x5b8000`, `rdat` at `0x5c4000..0xf94000`) and reproduced its exact
SHA-256, `8175dc0ed5ae80ffb016d9b8b9a87ed024883ff8670b281b54dca8a3445a08d8`.
The layout agrees with the pinned
[ipsw runtime assembler](https://github.com/blacktop/ipsw/blob/37e6badcbba4b82c4b66cd0376bd23f88839ad08/pkg/bundle/runtime.go).

With that mapping, file offset `0x952000` maps to BUND `0xa71000`; the
selector-1 vptr at file `0x98a6b8` maps to `0xaa96b8`, and file
`0x98a7e8` (`vptr+0x130`) maps to `0xaa97e8`. Both raw slots are zero, as is
the raw `__mod_init_func` slot at file `0x9a6350` / BUND `0xac5350`. P259's
nonzero float-like words came from an unrelated late BUND range and must not
be interpreted as a provider method or timing table.

The entire corrected `__DATA_CONST` range (`0x55000` bytes) is zero
(SHA-256 `179a0bea61861bbf7b5c52d8d60d662991c65c3d424aa03564c43f568ac06b58`);
`ipsw macho info --fixups` reports no fixups, and the `0x90`-byte
`__mod_init_func` section is zero. This describes the extracted file only;
it does not rule out runtime initialization.

This is a static mapping correction, not evidence that the runtime pointers
are null: loading/initialization may transform the bytes. No live provider,
`ColorElements` payload, mode, or pixels were identified or changed. The
validator and hash-matched reconstruction are preserved in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN260_DCP_RDATA_MAPPING_CORRECTION_STATIC/`;
the source tool is `tools/re/dcp_validate_runtime_data_mapping.py`.

#### P258 — Home entity arrives late; later dispatched swipe still fails before dismissal

P258 reached `SHELL_SERVICE_RETURN` at `1791782142.888`; the Home key was
acknowledged. The trigger waited only 10 seconds for the Home entity and then
sent a swipe by explicit option. The non-null Home entity was recorded later,
at `1791782375.359`, so that first swipe was early. A second QMP swipe came
after the Home-entity marker and was fully dispatched (33 operations, no
coalescing or dispatch errors). SpringBoard saw the Cover Sheet edge-pan
touches, then set the recognizer to Failed (`x2=5`); no `shouldBegin`, grabber
zone, dismissal begin/end, or accepted lock-transition event followed.

Both HMP captures remained uniformly black
(`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The
later swipe is transport-complete but not a successful dismissal. The P134
parent did not expose the legacy DVMINPUT1 ready marker; its failed manual
legacy relay is excluded as a protocol miss. P250 remains the positive
Recap-protocol control on replay2b. The next attempt should preserve that
replay2b lineage and its Home/ADFL gating, with the staged CA/present observer
armed after the accepted transition. Durable evidence is in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN258_P134_STAGED_GESTURE_PRESENT1/`;
the child passed `qemu-img check -U` and has SHA-256
`773bdfe94ef3e43645b96ee3d86dfe274d96120b9e8ae40adebe62587510ca30`.

#### P261 — check-in clears, Home entity appears, but Recap touch is not ACKed

On a replay2b child, `DM_PERFORM_RETURN` at `0x265c8444c` and BKS migration
check-in return at `0x1914db6a4` both carried `x0=0`; BKS check-in returned at
`0x1914db774`, followed by shell-service return at `0x1cbdd005c`. The shell
ladder completed at guest time `1791785700.043`. SpringBoard later created a
non-null Home entity at `1791785804.295`; the ADFL entry at `0x22b864ba4`
returned at `1791785822.660`. This proves the lifecycle path to a Home entity,
not a visible Home screen: the shell-return HMP capture was all black, SHA
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`.

The staged trigger initially waited for legacy ready names and missed its own
`POST_SHELL_HOME_GESTURE_STAGED_READY`; the host-side matcher now includes that
label. With the gate corrected, Home down/up were ACKed. The subsequent Recap
touch-down (`DVMINPUT1 R`, sequence `1791785855747`) and its safety release
(`1791785915857`) both timed out at the input relay. A lock-manager callback
with `for_user_gesture=1` was not an accepted pan transition:
`gesture_sequence_confirmed=false`, no recent Cover Sheet pan, and no
dismissal sequence. The staged display observers consequently provide no
valid unlock or post-dismissal render result. P262 switches only to direct
touch after the same Home and ADFL gates.

The archived child passed `qemu-img check -U`; SHA-256 is
`5b20966d25626588c6bd4ca8313f50c0fc99e3a1cbbb723c0d67096aceb2f293`. Its
logs and frame records are in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN261_REPLAY2B_LEGACY_STAGED_PRESENT1/`.

#### P262 — direct touch reaches the dismissal recognizer; P263 must capture after transition

P262 used a fresh replay2b child and retained the measured shell/check-in,
Home-entity, and ADFL gates. The Home key was ACKed; SpringBoard created its
Home entity and ADFL returned. Shell-return, Home-entity-return, and 120-second
post-shell HMP captures remained uniformly black (`d312521c…42b9`). The 11
direct-touch swipe points and final lift were ACKed. SpringBoard recorded a
same-recognizer sequence: `HOME_GESTURE_PAN_SHOULD_BEGIN_RETURN x0=1`, Cover
Sheet dismissal `Began`, pan `TouchesEnded`, then a lock-manager transition
with `for_user_gesture=1`.

The runtime marker's first pass said the sequence was unconfirmed because the
correlator allowed only 60 seconds from should-begin/began to transition. The
same PID, TPIDR, recognizer, ordering, accepted should-begin, and pan end
re-evaluated offline with a 180-second event-age bound match; pan end is 1.61
seconds before transition. This corrects callback correlation only. P262
stopped before the trigger's post-transition Home key and render observation,
so no visible unlock or post-transition pixels are claimed. The durable child
passed `qemu-img check -U`, SHA-256
`760b066cb23d72a2f73354ac36622c851232f4508e1332c29c043e8286250939`; its
records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN262_REPLAY2B_LEGACY_DIRECT_STAGED_PRESENT1/`.
P263 will retain the corrected 180-second correlation and continue through the
post-transition capture. No passcode or text was sent.

#### P263 — shell and Home lifecycle reproduced; direct swipe fails before shouldBegin

P263 repeated the replay2b gate on a fresh child. `DM_PERFORM_RETURN`, BKS
migration/check-in return, and shell-service return completed successfully.
SpringBoard created its Home entity, returned from ADFL, and requested lock-UI
presentation. The HMP samples at shell return and Home-entity return remained
uniformly black (`d312521c…42b9`).

The corrected gesture correlator was present, and all 11 direct-touch points
plus lift were ACKed. Unlike P262, both the Cover Sheet screen-edge pan and
FluidSwitcher screen-edge pan entered Failed (`x2=5`) immediately after touch
begin; there was no accepted should-begin, Cover Sheet dismissal begin, or
`LOCKSCREEN_GESTURE_TRANSITION x3=1`. After the 180-second marker bound, the
trigger stopped without sending the post-dismissal Home event. Therefore the
display remains visually unclassified after dismissal, while the input path
is now distinguished from the recognizer's failure. This P263 miss does not
change P262's same-lineage, identity-matched accepted-pan evidence.

The durable child passed `qemu-img check -U`, SHA-256
`2f03ae484763aa00111a11832ae2521701242bbe0af9ac27f427f86310fd17bd`; logs and
frames are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN263_REPLAY2B_LEGACY_DIRECT_STAGED_PRESENT2/`.
The next controlled input comparison should try the known-positive Recap
protocol with an extended ACK timeout, retaining the same Home/ADFL gating and
the corrected 180-second correlation. No passcode or text was sent.

#### P265 — static Plug Sync callback narrows the firmware-side capture

Offline disassembly of the saved T8140 DCP Mach-O adds a conditional cut ahead
of the known builder path. In `VideoInterface::Plug Sync callback`
(`0x42884c8`), provider slot `+0x850` is called on the object at `this+0x5ee0`.
Only when that return is nonzero does the callback inspect `this+0x42bc`; a
zero field value then logs “System is powering down, dropping this hotplug”
and returns `0x80000007`. A zero slot return skips that check and continues. The continuing path calls provider slot `+0x1f0`
on `this+0x5cd0` with the saved second callback argument, then enters the
`ColorElements` wrapper at `0x428865c` and builder at `0x4288a60`. The slot
semantics and runtime values remain unknown, so this is a static discriminator,
not evidence that the callback or builder ran on the emulated boot.

This refines the next real-hardware capture: record the `+0x850` return and
`this+0x42bc` together, then the `+0x1f0` receiver/argument, followed by the
builder's already identified `+0x70` result and `+0x78` ColorElements result.
The disassembly, hashes, and full output are preserved in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN265_DCP_PLUG_SYNC_CALLBACK_STATIC/`.
No firmware, device-tree, QEMU/model response, property, mode, or pixel data
was changed. D572/D582 remain hypotheses.

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

#### P267 static DCP note — callback receiver versus property provider

The offline T8140 DCP follow-up distinguishes the Plug Sync callback's
`this+0x5ee0` `+0x850` receiver from the `this+0x5cd0` receiver used for the
callback's `+0x1f0` operation and the builder's property getters. The member
fields have separate roles in static data flow, but their runtime pointer
values may still alias. This keeps future real-device probes from treating
the first callback receiver as a proven ColorElements source. Evidence and
disassembly are preserved in the P267 recovery record; no DCP response or
display value was synthesized. D572/D582 remain hypotheses.

#### P268 static DCP note — helper's secondary pointer view

Offline analysis found a second initialized pointer word at helper `+0x8`
and a registry-table entry that points to `helper+8`; the known Plug Sync
callback path uses the primary word at `+0x0`. This supplies a concrete
additional pointer to capture on real hardware, without assigning a class
or method to it. P268 evidence is in the durable DCP recovery record; no
firmware response or display data was synthesized.

#### P264 — shell-to-Home lifecycle advances, but captured output stays black

On replay2b child `ui-class16-refresh60-dm-plugin264-replay2b-recap-ack180-present-lineage1.qcow2`, the same boot reached `SHELL_SERVICE_RETURN` after BKS check-in. SpringBoard entered and returned from ADFL with a nonnull result. It returned nonnull workspace-create and Home-entity objects, and `SB_WORKSPACE_EXECUTE_TRANSITION` returned `x0=1`. The lock coordinator's `lockAllUI` and lock manager's `lockUI` callbacks also returned nonnull values. Repeated later workspace/Home-entity callbacks and CoreAnimation commit callbacks show continued userspace activity, not a home-screen pixel result.

The HMP captures at shell return, Home-entity return, CA commit, and 1203 seconds after shell return were all `1179x2556`, had zero pixels above the `>8` threshold, and shared SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. Thus the measured lifecycle now reaches SpringBoard Home/workspace and lock UI code while the QEMU framebuffer remains black. This does not identify whether the missing image comes from display mode, layer preparation, render, or scanout.

The attempted Recap gesture is unscored: P264's automated trigger rejected its 180-second ACK timeout before it sent the planned sequence, and a separate manual relay wrote Recap traffic during the same boot. No same-gesture accepted dismissal marker was captured. No passcode was submitted and no display firmware response or mode was changed. The child is preserved at `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-plugin264-replay2b-recap-ack180-present-lineage1.qcow2` (SHA-256 `72b1ee71c57ee8095b35a6d679dc359a972f0457a2886bf5ecd94ce655428ae7`); its recovery record is `recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN264_REPLAY2B_RECAP_ACK180_PRESENT_LINEAGE1/`. D572/D582 remain hypotheses.

#### P272 — offline secondary-image candidate remains separate from display proof

The new `nold` inventory in `docs/re/dcp-firmware-services.md` preserves a conditional second-image reconstruction only. Its candidate zero `__DATA_CONST` does not prove runtime vtable state, DCP callback behavior, mode publication, or scanout. The verified main-image reconstruction is unchanged. No display response or pixels were inferred from the candidate; D572/D582 remain hypotheses.

P272 follow-up found that the segment-only stitch omits the second image's
`LC_SYMTAB` and string-table ranges (ending at file offset `0x660b30`), so it
is not a complete Mach-O. Appending those tables after the stitched segments
also exceeds the remaining `rdat` capacity. This narrows the static lead to
locating separate linkedit bytes or the actual secondary layout; it adds no
runtime or pixel claim.

The candidate's header-only `LC_UUID` (`62F319FD-46B6-3219-B2EF-F8BADDFA72E0`)
differs from the verified main-image UUID (`7A8B83FB-1EC8-3F13-A7C4-D9CD5BC71718`). This gives a stable identifier for a future runtime capture, while leaving the candidate mapping unvalidated.

P275 repeated the Type-4 reconstruction check against the pinned 24A5430a
bundle and P233 reference image. The main Mach-O hash remains an exact match;
the two Plug helper address points are still file-backed zero samples, and
the global table address is still zero-fill rather than file-backed data. This
confirms the offline mapping only and does not resolve runtime vtables or
callback execution. The report and reproduction inputs are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/P275_DCP_STATIC_VERIFY/`.

#### P273 — accepted lock dismissal, but the compositor still supplies lock chrome

P273 is a fresh child of the durable replay2b D5825-class parent, with the
accepted `system.tc`, legacy-UART-only input, and the established D572/D582
hypothesis profile unchanged. The same boot returned from `DM_PERFORM`, BKS
migration/check-in, and shell service; SpringBoard returned a nonnull Home
entity and `SB_WORKSPACE_EXECUTE_TRANSITION` returned `x0=1`. The helper's
Home key and all 34 Recap swipe records were ACKed. The Cover Sheet pan's
`shouldBegin` returned true, the same recognizer began and ended the gesture,
and `LOCKSCREEN_GESTURE_TRANSITION` recorded `x3=1` / `forUserGesture=1` at
`1791792548.994735`, correlated to that input. This is an accepted dismissal
transition, not proof the lock layer was removed.

Three captured full-size IOSurfaces after input still show sparse lock-screen
chrome on black. Sample 2 is a valid 1179×2556 BGRA surface with 6,774
nonblack pixels (6,561 above RGB 32): status time/indicators, info glyph, and
home indicator, with no wallpaper or Home icons. Its view is preserved at
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN273_REPLAY2B_LEGACY_UART_ONLY_ACK120_HOME_UNLOCK1/frames/iosurface-posthome-02.png`.
The HMP frames—including the final capture after the 600-second post-transition
observation—remained uniformly black at 1179×2556 (`nonblack_gt8=0`, SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The
transition therefore advances the measured input and compositor path, but
does not produce visible Home or unlock output in this run. A later
`LOCKSCREEN_GESTURE_TRANSITION x3=0` came from another SpringBoard TPIDR; it is
not scored as a relock or as negating the earlier accepted transition. The
`ANY_LOCK_SCREEN_VISIBLE` and `COVERSHEET_DISMISS_GESTURE_ACTIVE` function
breakpoints were armed but produced no callback rows, so lock-state visibility
remains an instrumentation gap.

P273's historical `IOMFB_A408_BUILD` breakpoint at runtime
`0xfffffff02a0c4664` hit four times. Its saved, hash-identified bootkc
(`da1e254a…`) disassembles the address as `braa x16,x17`, after loading a
vtable word into `x8` at `+0x4634`, copying it to `x16`, and forming the
modifier in `x17`. P273 logged `x8=0xfffffff02a0c8ff4`, which is an address
candidate; the breakpoint alone did not prove the indirect branch completed
or identify an operation.

P274's live row at the same runtime PC has `x8=0xfffffff02a0c8ff4`,
`x16=0xbe54fff02808d110`, and `x17=0x998`. Those registers do not match the
P273 saved instruction sequence at the breakpoint. P274's launch profile
names the durable bootkc path, and a later hash check shows that file is also
`da1e254a…`; the separate repository `firmware/bootkc` is `dc0f5b6a…` and
was not the path in P274's profile. A different host bootkc file is therefore
not the leading explanation, but no P274 guest-memory instruction dump has
reconciled the register difference. The older P274 probe
canonicalized `x16` and armed `0xfffffff02808d110`; because the code bytes for
that exact run are not established, that arm is not a scored branch-target
test. The revised probe preserves raw `x8/x16/x17/x2` and arms only the
canonical `x8` address as a candidate; this remains unvalidated in a matching
fresh boot. A candidate hit would still establish address execution only,
not method semantics or an A408 operation. No firmware or display behavior
changed.

The recovered P273 context at its `x8` candidate shows a separate indirect
call site (`ldr x8,[x16,#0xb0]!; blraa x8,x16`) after loading a receiver
candidate from `[x19+0x90]`. This adjacent static control/data flow does not
name a class, method, or relationship to A408. P273's accepted dismissal and
sparse IOSurface remain valid; HMP remained black. D572/D582 remain
hypotheses.

The child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-plugin273-replay2b-legacy-uart-only-ack120-home-unlock1.qcow2`
(SHA-256 `82a1c8a843e29da13dcef9d34448db3e1ba49903d2a8395cc624e397fd99c69d`)
passed `qemu-img check -U`; records are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN273_REPLAY2B_LEGACY_UART_ONLY_ACK120_HOME_UNLOCK1/`.
No passcode, display-mode/property payload, DCP response, model, fixture,
SpringBoard, CLASS16, A408, or scanout behavior was changed. D572/D582 remain
hypotheses.

#### P277 — Lock Screen IOSurface reaches `fb_swap_set_layer`; HMP remains black

P277 is a fresh child of the P276 Recap-helper checkpoint, with the accepted
trust cache and the existing CLASS16/D572/D582 hypothesis profile. The same
boot returned from `DM_PERFORM`, BKS migration/check-in, and shell service;
SpringBoard then returned a nonnull ADFL result and created its Home entity.
The Home key was acknowledged. A same-recognizer, same-TPIDR Cover Sheet pan
passed `shouldBegin`, began, ended, and produced
`ACCEPTED_LOCKSCREEN_GESTURE_TRANSITION` with `for_user_gesture=1` at
`1791798125.993292`. At that instant the sampled lock-manager state was
`_isUILocked=0`, `_uiUnlocking=1`, `_handlingUnlockRequest=1`, with no
passcode-entry transient overlay. That is transition state, not a completed
unlock.

The strongest new evidence is a readable 1179×2556 BGRA IOSurface whose
pointer is present as `x3` at `FB_SWAP_SET_LAYER_ENTRY` in
`fb_swap_set_layer`. At set-layer count 23, `x3` is `0x7c70d9c4c0`. The
historically named paired `FB_SWAP_HANDOFF` breakpoint at `0x184472488` also
logged that pointer in `x2`, but later static disassembly corrected the
interpretation of that PC: it follows loading an Objective-C receiver from
`[x24+0x18]` and branches to `objc_msgSend$sender`. The selector is `sender`
(no colon), so the matching `x2` value is only a register observation, not an
established formal IOSurface argument or proof of a consumer. The two
preceding nonnull surfaces likewise match by pointer at the setter and this
sender-call boundary. Across 23 observed pairs, only the final three setter
calls had nonnull `x3`. The final pixels show Lock Screen chrome (time/status, the Chinese
“Swipe up to open” prompt, information glyph, and Home indicator) on black;
there are no Home icons or wallpaper. The final IOSurface contained 9,886
nonblack pixels, including 9,006 above RGB 32. Its preserved view is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN277_COMMIT_PAIR_HOME1/frames/iosurface-posthome-03.png`.

HMP frames stayed uniformly black through the final 420-second
post-transition capture (`nonblack_gt8=0`, SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). P277
therefore establishes that BackBoard produced Lock Screen pixels and passed
the surface as `x3` to `fb_swap_set_layer`, while HMP remained black. It does
not show what the subsequent `sender` call does with the surface, prove virtual
scanout consumed it, or assign the gap to DCP firmware, mode `-1`, or a QEMU
response. The allocation-return metadata decoder still reports invalid
geometry for its wrapper candidate; it was not used to identify the actual
pixel-bearing surface. The CA Mach-send observer reached its pre-gesture cap,
so post-gesture CA message traffic is unmeasured; the probe was corrected for
the next run to gate its count on the accepted-transition marker. No
passcode, DCP response, display mode, device-tree property, model, fixture,
SpringBoard, CLASS16, A408, or scanout behavior was changed.

The durable child is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin277-commit-pair-home1.qcow2`
(SHA-256
`ccde5785072f3875d7ef8a7c90678aff5e23f4c9b3874b0f2c6cc315a5628fd0`). Its
recovery record is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN277_COMMIT_PAIR_HOME1/`;
`qemu-img check -U` passed. D572/D582 remain hypotheses.

P277 offline correction: the preceding P277 paragraphs' “paired handoff” wording
overstates the evidence and is superseded here. The saved QuartzCore image
(`EADDE613-ADB9-3558-B557-51ED142E9834`) disassembles
`CA::WindowServer::IOMFBDisplay::fb_swap_set_layer` with the `__IOSurface *`
arriving in `x3` and saved in `x20`. After the layer-ID cache/dirty-byte
updates, it loads `*(x24+0x18)` into `x0`, sets `x1=x22`, `x2=x20`, `x3=x19`,
restores its frame, and branches at `0x184472488` to the shared
`objc_msgSend$sender` stub. The selector index resolves this selector to
`sender` (no colon). The static result establishes the selector-call boundary
and register state, but not the receiver class, implementation, or any use of
the extra register values by that implementation. Thus P277's runtime
`x2 == x3-at-set-layer` match is only a register-value correlation, not proof
that the surface was handed to a consumer or reached scanout. The sound result
is that BackBoard supplied a readable Lock Screen IOSurface to
`fb_swap_set_layer`, the separate `sender` call boundary ran, and HMP stayed
black. Future probe rows use `FB_SWAP_SENDER_CALL`; P277's saved rows retain
their historical `FB_SWAP_HANDOFF` label. No display model or guest behavior
was changed.

#### P279 static — IOSurface accessor offsets match the pixel probe

The saved iOS 27 beta 8 dyld cache extracted IOSurface image UUID
`6838EB44-218E-33CC-BF2D-9F0A323C34F1` (arm64e). Its `_OBJC_IVAR_$_IOSurface._impl`
global is at `0x1e5c16b44`; the public width, height, allocation-size,
bytes-per-row, pixel-format, base-address, and plane-count accessors load
that signed ivar offset, dereference the `_impl` pointer, and read the same
fields used by `tools/re/first_surface_callbacks.py`: `+0x98`, `+0xa0`,
`+0x90`, `+0xa8`, `+0xb8`, `+0x70/+0xb0`, and `+0xc8`, respectively. For
zero-plane surfaces, `IOSurfaceGetBaseAddress` returns `_impl+0x70` plus
`_impl+0xb0`; the accessor has a separate compressed/plane path that can
return null. The disassembly and input hashes are in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN279_IOSURFACE_ACCESSORS_STATIC/`.

This confirms the decoder's static field layout. P178's plausible geometry
followed by a read failure at byte zero is therefore a backing-memory
readability/mapping issue, not evidence that those accessor offsets are
wrong. P278 later passed non-null `x3` surfaces to `fb_swap_set_layer` after
the accepted lock transition, but its already-loaded sampler did not
recognize the marker after the follow-up helper moved it to `.first.json`;
P278 has no pixel sample and is not a surface-content classification. The
capture gate now accepts either the active marker or its retained `.first.json`
copy, with four unit tests for the move/retention behavior. No IOSurface bytes
were fabricated, and no guest, DCP, QEMU model, display mode, or scanout
behavior was changed; D572/D582 remain hypotheses.

#### P278 — measured SpringBoard path, accepted dismissal, sampler miss

P278 reached shell-service return, SpringBoard's Home entity and ADFL return,
workspace transition, Home key, and a pan-confirmed Recap dismissal.
BackBoard recorded `ACCEPTED_LOCKSCREEN_GESTURE_TRANSITION` with
`for_user_gesture=1` and `gesture_sequence_confirmed=true`; the ordinary Home
event after it was ACKed. One later ordinary follow-up swipe was also ACKed,
but no second accepted-transition marker appeared in its bounded window.
This establishes the SpringBoard lifecycle and an accepted lock-dismissal
transition, not completed unlock or visible Home. No passcode was entered.

After the first accepted transition, the log contains 32
`FB_SWAP_SET_LAYER_ENTRY` rows; five had non-null `x3` pointers. No
`POSTHOME_IOSURFACE_PIXELS` row exists: the helper moved the acceptance marker
after the sampler had loaded the old marker-only gate, so pixel capture did
not run. The corrected gate checks both active and retained `.first.json`
markers and has four unit tests, but requires a fresh runtime validation.
HMP frames stayed 1179x2556 and uniformly black through the 900-second
post-gesture window (zero pixels above threshold; SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). Thus
P278 establishes compositor/swap activity with black HMP output, while
IOSurface contents remain unclassified. It does not identify a DCP, firmware,
display-mode, or QEMU cause.

The durable child is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin278-two-swipe-surface1.qcow2`
(SHA-256
`e6bc857412039eb5279d4db7339ba3f64e08b2e092310aa9091cd215f2941890`).
`qemu-img check -U` passed; records are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN278_TWO_SWIPE_SURFACE1/`.
The next run should load the corrected sampler before its marker is emitted
and leave that accepted marker in place.

#### P280 — accepted lock dismissal, Home lifecycle, no post-transition surface

P280 is a fresh child of the P276 Recap-diagnostic checkpoint using its
installed input helper and the established D572/D582 hypothesis profile. The
same boot completed the migration/check-in/shell-service ladder. SpringBoard
returned from ADFL, created non-null Home entities, and recorded successful
workspace-transition returns. The legacy Recap swipe was ACKed, the same
SpringBoard recognizer accepted/began/ended the Cover Sheet pan, and the lock
manager emitted a gesture-confirmed `ACCEPTED_LOCKSCREEN_GESTURE_TRANSITION`
at `1791801699.1061099`. The post-transition Home event was ACKed. This is a
measured dismissal/Home lifecycle path, not proof of visible Home or a fully
completed unlock; no passcode was entered.

P280 validates that the accepted-transition sampler gate was open: the first
confirmed event is retained at
`recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN280_ACCEPTED_MARKER_SURFACE_SAMPLE1/accepted-lockscreen-gesture-transition.first.json`.
The active marker was later overwritten by a second, unconfirmed lock-manager
event; the trigger's own result preserves the earlier confirmed record. The
sampler then observed 12 post-acceptance `FB_SWAP_SET_LAYER_ENTRY` calls, all
with `x3=0`. The earlier non-null `x3` surfaces and all four
`ALLOCATE_IOSURFACE_RETURN` events preceded the accepted transition. There
were no post-acceptance IOSurface pixel samples because no non-null swap
surface was supplied. All six `SERVER_RENDER_UPDATE_ENTRY`, display-render,
and OGL render events also preceded the accepted marker; none recurred during
the 900-second post-transition observation.

SpringBoard nevertheless continued to complete CA commit calls and reached
5,113 AP-side `CA_RENDER_MACH_MSG_SEND_CALL` observations after the accepted
marker. These are pre-syscall client boundaries with task-local Mach names;
they do not prove message delivery. HMP captured 1179×2556 uniformly black
frames from shell return through the 900-second post-transition final sample,
all with SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. One
post-marker `IOMFB_A408_BUILD` row is only the already-documented unresolved
dispatch boundary, not proof of an A408 builder result. Thus P280 narrows the
measured gap to post-dismissal CA client traffic without a server render
update or non-null swap surface in this window; it does not assign a cause to
DCP firmware, mode, scanout, or QEMU.

The durable child is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin280-accepted-marker-surface-sample1.qcow2`
(SHA-256
`44c53f82dbc58f84790ab51c228f2f54ca54b1923bfe7e980b4a43acf8f699e3`). Its
backing chain begins at the P276 diagnostic child and continues through the
replay2b parent; `qemu-img check -U` found no errors. Full logs, input ACKs,
events, frames, launch profile, and the retained accepted marker are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN280_ACCEPTED_MARKER_SURFACE_SAMPLE1/`.
The next targeted run should capture full `0x9c42` send bodies and render-
server receives with the accepted-marker gate enabled, then compare body
identity before investigating later display stages. D572/D582 remain
hypotheses.


### P282 — accepted Lock Screen surface content versus black HMP

P282 follows the P276 checkpoint, with the existing input helper and CLASS16/D572/D582 hypothesis profile. After shell-service return, SpringBoard recorded a gesture-confirmed Lock Screen dismissal at `1791803519.1124082`; a second swipe was accepted at `1791804162.5634959`, and the Home-key relay was ACKed. No passcode was entered, so this is not an authentication or completed-unlock result.

At BackBoard `FB_SWAP_SET_LAYER_ENTRY`, two accepted-transition samples had non-null `x3` IOSurface pointers (`0x7687190520`, `0x7687191300`). Both decoded as 1179×2556, stride 4864, pixel format `0x42475241`, with 6,879 nonblack pixels, 6,634 pixels above RGB 32, and peak RGB 255. Captured pixels visibly contain Lock Screen time/status/charging battery, information control, and home indicator. Sample times were +256.398s and +261.923s after the accepted gesture. These are AP/BackBoard surface bytes at the userspace swap boundary; they do not prove kernel submission, DCP acceptance, scanout, or visible output.

The separate HMP capture remained uniformly black through the 900-second post-transition endpoint (1179×2556; zero nonblack pixels; SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The post-transition ordinary swipe and Home-key ACK did not change it. This revises the earlier P280 implication that there was no Lock Screen surface after acceptance: P282 demonstrates populated Lock Screen pixels in a surface, while Home-screen content and final scanout remain open.

P282 additionally observed 19 SpringBoard pre-syscall `0x9c42` messages and 8 BackBoard receives (message size 608). Each receive's first 232 body bytes had a unique earlier sender-prefix/time candidate, with 99.498–139.833ms deltas. The sender's remaining body was not captured, and task-local port names were not compared across tasks. This is a bounded payload-prefix correlation, not a transaction or IPC-right join. Raw events, decoded surface samples, frame hashes, and counts are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN282_CA_9C42_POST_ACCEPT_RECEIVE1/`.

The next accepted-marker-gated capture arms H17P `swap_submit`, generic mapping, and A407/A408 dispatch sites after this measured non-null surface. P282 supplies no DCP Plug/`ColorElements` payload and does not validate the D572/D582 hypothesis stack; it moves the unresolved boundary past “no Lock Screen pixels were produced” to kernel submission/mapping and the relationship to the HMP output.

### P283 — full-body CA receive correlation; HMP still black

P283 repeated the P276 checkpoint path and recorded an accepted Cover Sheet
dismissal at `1791805251.8935392` with matching gesture-recognizer
should-begin/began/ended evidence. No passcode was entered. BackBoard
captured full-size 1179×2556 zero-plane BGRA surfaces `0x75a8de1530` and
`0x75a8de17d0` at `FB_SWAP_SET_LAYER_ENTRY` with non-null `x3`. The latter
decodes to the Lock Screen time, charging indicator, Chinese swipe-up prompt,
information control, and home indicator. The surface content is therefore
measured at the BackBoard swap API boundary; P283 does not prove successful
kernel submission or scanout. Its HMP screendumps remained uniformly black.

The A408 site emitted unresolved indirect-dispatch boundary records; these
are not builder results. There was no interpreted A407 mapping result or
H17P `swap_submit` event establishing scanout. Full-body `0x9c42` comparison
found 19 exact prior-send/BackBoard-receive pairs by message size, body, and
timestamp (6.705–89.574 ms), while 13 receives had no captured full-body
sender match. This establishes payload identity and ordering only, not a
transaction ID or cross-task IPC-right identity. The second-swipe helper
started after QEMU had stopped, returned `uart-not-present`, and sent no
input; it provides no second-gesture verdict. Raw captures, decoded images,
full-body report, and HMP frames are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN283_CA_9C42_FULLBODY_SWAP_SUBMIT2/`.

P283 corrects the immediate next-step priority: arrange the optional second
ordinary swipe sidecar before guest startup, then distinguish a fresh
gesture-confirmed Home transition from continued lock UI. Keep passcode
entry manual and only if the owner supplies it. D572/D582 remain hypotheses;
no firmware, model, device-tree, or callback response was changed.

#### P285 static — builder gate's exact diagnostic

The pinned Type-4 T8140 DCP image was disassembled again at
`constructColorElements` (`0x4288a60`). Its first property-provider call is
the `this+0x5cd0` vtable `+0x70` slot at `0x4288aa8`; `tbz w0,#0` at
`0x4288aac` skips the later getter calls when bit 0 is clear. That path goes
to `0x4288c70`, prepares `UnifiedPipeline.cpp:0x2779` and the exact string
`HPD not present, skipping mode export`, then tail-branches at `0x4288cb0`
to the common log helper `0x40311bc`. The register setup `x0=0x40000001`
is a logging argument in this path, not a status value demonstrated to be
returned from the builder. The branch exits before provider slots `+0x80`,
`+0x88`, `+0x78`, `+0x140`, and `+0x150` are read.

This is a static refinement of P231/P237/P281: P231's upstream display-state
gate may prevent entry to the builder; P285 names the separate in-builder
gate if the builder is reached. Neither condition has a runtime value in this
QEMU DCP model. No DCP response, `ColorElements`, timing value, mode, device
tree, firmware, or pixel data was changed. Disassembly, string addresses, and
hashes are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN285_BUILDER_GATE_LOG_STATIC/`.
D572/D582 remain hypotheses.

#### P286 static — capture the selected provider pointer, not only `+0x5cd0`

The same pinned DCP image contains a selector at `0x428fd5c` that stores an
active pointer in `this+0x5cd0`: a zero `w1` selects `this+0x5ce0`; nonzero
selects `this+0x5cd8`. On the nonzero path, byte `this+0x5641 == 1` is cleared.
If bit 0 of `this+0x674a` is also clear, the code logs
`found stale fTimingsUpdatePowerState, pipe may not power on properly. Cleaning up`
at `UnifiedPipeline.cpp:0x3bb8`, sets that bit, then selects `+0x5cd8`.
This narrows the real-device trace: save the selector input, both candidate
pointers, the selected `+0x5cd0`, and those two bytes before evaluating the
P285 `+0x70` gate. The log string is not proof of a physical hot-plug event,
and the selector's API meaning and runtime values remain unknown. No raw-image
pointer reference joins these candidates to P259's factory object. The QEMU
DCP model does not execute this firmware code; no property or mode was
changed. Disassembly and hashes are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN286_PROVIDER_SELECTION_STATIC/`.
D572/D582 remain hypotheses.


### P284 — accepted lock dismissal, white BackBoard surface, black HMP

P284 used a durable replay2b child after the P276 checkpoint. In the same
boot, `DM_PERFORM_RETURN` returned `x0=0`, followed by BKS migration/check-in
returns and `SHELL_SERVICE_RETURN`; SpringBoard later published a non-null
Home entity and entered ADFL. These are separate milestones: shell-service
return alone is not Home UI evidence.

The Home-key relay was acknowledged. A subsequent ordinary swipe passed the
gesture-recognizer `shouldBegin`, began and ended a CoverSheet gesture, and
produced `ACCEPTED_LOCKSCREEN_GESTURE_TRANSITION` with `x3=1`. The first
BackBoard IOSurface sample was nearly black. The second, captured 3.43 seconds
later while the follow-up swipe was in progress, was 1179×2556 with stride
4864 and ARGB metadata. It contains a white
canvas, black Dynamic Island, time `5:06`, small status indicators, and a
charging battery, but no recognizable Lock Screen content or app-icon layout.
This proves populated BackBoard surface bytes after the accepted transition,
not that Home appeared, the Lock Screen was visibly dismissed, or the surface
was submitted and scanned out. The final HMP screendump at +420 seconds was
uniformly black (SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The follow-up swipe began about 30.3 seconds after the accepted transition
and was ACKed. While it was in progress, the second sampled IOSurface changed
from near-black to white. About 86 seconds after the first accepted marker,
SpringBoard entered `SB_LOCK_COORDINATOR_LOCK_ALL_UI` and
`SB_LOCK_MANAGER_LOCK_UI` (`x2=1016`), then `SB_WORKSPACE_EXECUTE_TRANSITION`
returned `x0=1`; the gesture observer did not report a second accepted user
transition. Timing does not establish that the swipe caused these calls or
what the transition represented. No passcode was entered, and no authenticated
unlock is claimed.

The child is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin284-two-swipe-present2.qcow2`
(SHA-256
`9f90ddf5b24f8c07e9e01efcb6c6f1672fafdd69a2c7fed6d12b99d9aabbe`). The
backing chain and `qemu-img check -U` result are preserved with the logs,
events, relay records, and frames in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN284_TWO_SWIPE_PRESENT1/`.
The decoded surface is `frames/iosurface-002.png` (SHA-256
`c795b490aa609f396ef5cf2ccfc0244a5a59937025b94ff94c63c16429c802b0`). No
display model, firmware, device-tree, or fixture response was changed.
D572/D582 remain hypotheses.

#### P287 static — pair-selection helper has an explicit no-match return

Offline disassembly of the pinned Type-4 T8140 DCP image identifies a
selection helper at `0x42a796c`. It first tests bit 0 of byte `[x0+0x1b9]`.
The clear-bit path logs the diagnostic at `UnifiedPipeline.cpp:0x5f2` and
returns `0x80000104`. When set, the helper obtains candidate lists from
`[x1+0x18]` and `[x1+0x20]`, with null-list fallback calls through the `x0`
provider's `+0x78` and `+0x80` slots. It walks and compares candidate
records. The no-match branch logs a diagnostic at line `0x628`, then
`WARNING: no color or timing modes from the link` at line `0x5fb`, and
returns `0x80000001`. The helper compares the first two 32-bit words at
`x1+0` and `x1+4` against values returned by vtable `+0x48` on the object at
each candidate's `+0x18`, traversing first-list then second-list entries. On a
match, the second-list candidate receives two `+0xd0` calls with selector
arguments 0 and 1; their 32-bit results are stored through the caller's
pointers at `x1+8` and `x1+0x10`. The matched path retains both candidate pointers, populates downstream
fields, and tail-branches to `0x42a5184` with `x0` set to the owning object,
`x1=0`, and `x2=1`. The final return status after that branch is not
established by this slice. The key/value semantics of these fields and vtable
slots remain unknown; this is control/data-flow only.

This gives a real-device trace a concrete readiness byte, list pointers,
candidate-comparison inputs, and return status to capture before following
the selected candidate into later provider/output calls. It is a separate
static stage from P285's builder `+0x70` gate and P286's provider selector.
The QEMU DCP model does not execute this firmware path, so the branch and
return values have not been observed at runtime. This does not explain the
white P284 surface or black HMP output, and no mode or payload is inferred.
The adjacent static helper `0x42a7c88` selects `Chromaticity` and
`DefaultWhitePoint` keys when its third argument is zero, or
`DolbyVisionChromaticity` and `DolbyVisionWhitePoint` when nonzero. It then
reads Red/Green/Blue child X/Y properties into six float slots of the caller's
x2 buffer and selected white-point X/Y into x1 offsets 0 and 4. The conversion
uses an integer getter value with a 16-bit fractional shift before narrowing
to float. Literal strings support the colorimetry interpretation, but units,
ranges, runtime values, and API semantics remain unknown. The DCP input hash,
disassembly/string hashes, and buffer map are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN287_LINK_MODE_FALLBACK_STATIC/`.
D572/D582 remain hypotheses.

#### P290 — confirmed lock-dismissal input, but the post-transition swap receives no surface

P290 is a fresh P276 child with the same accepted `system.tc` and CLASS16 /
D572 / D582 hypothesis profile. The same boot passed DM completion, BKS
migration/check-in, and shell-service return; SpringBoard then created Home
entities and entered ADFL. An ordinary Recap swipe passed the matched
CoverSheet recognizer's accepted `shouldBegin`, began and ended on the same
recognizer, and produced a confirmed `ACCEPTED_LOCKSCREEN_GESTURE_TRANSITION`
at guest time `1791809738.4652102`. A later one-shot upward swipe, sent after
the configured delay, was ACKed by the input relay. SpringBoard emitted
gesture-recognizer activity for it, but no fresh confirmed dismissal
transition was recorded in the 120-second marker window. No passcode or text
was entered; the result is not an authenticated unlock or visible Home.

The post-transition boundary is clearer than the HMP image alone. The three
`SERVER_RENDER_UPDATE_ENTRY`, surface-creation, allocation, and OGL-render
sequences all occurred before the accepted gesture. After it, BackBoard
recorded six `FB_SWAP_SET_LAYER_ENTRY` calls, each with `x3=0`, in two groups:
three at about +76 seconds and three at about +663 seconds. There was no
post-transition server-render update or OGL render in the captured events.
SpringBoard continued to produce CA-commit entries and returns. HMP
screendumps through at least 722 seconds after shell return, including the
follow-up swipe's before/after captures, remained uniformly black (1179×2556,
SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The
measured gap on this boot is between continuing SpringBoard CA commits and a
post-dismissal render/surface being supplied at the observed swap boundary;
this does not identify the DCP/model cause. At about +663.31 seconds the same
BackBoard thread also hit the armed `IOMFB_GENERIC_MAP` entry; about 8.6 ms
later it reached the `IOMFB_A408_BUILD` dispatch boundary, and an address
candidate taken from `x8` was hit on the same TPIDR about 4 ms after that. The
run did not record the generic-map return, descriptor fields, A407 entry, or
H17P swap-submit. These are late control-flow observations, not proof that a
map succeeded, an A408 operation completed, or a surface reached scanout.

The follow-up helper also exposed a host-side marker bug: it copied the
current active transition over `.first.json` after its delay, even when that
active value was a later unconfirmed event, then removed the active marker.
The original confirmed JSON had already been preserved separately, so the
capture gate was restored from that copy during P290. The helper is now
changed to leave both marker files untouched and wait for a fresh confirmed
record in the append-only gesture log; nine unit tests pass, but the revised
helper has not yet been boot-validated. The guest is no longer live; its
durable child is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin290-delayed-prompt-swipe1.qcow2`
(SHA-256 `62e7151b79cb2ba828790c05cfa23f1cad57d9e960d47d2e5f114d6a66e350c3`;
`qemu-img check -U` passed). P290 logs and frames are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN290_DELAYED_PROMPT_SWIPE1/`.
No passcode, DCP response, model, device-tree, fixture, or scanout behavior
was changed; D572/D582 remain hypotheses.

#### P290 static follow-up — the pair selector's zero-input modeset depends on prior state

Offline disassembly resolves part of P287's previously open tail branch. At
`0x42a7c84`, a matched candidate pair tail-branches to `0x42a5184` with
`x1=0` and `x2=1`. The `x1==0` branch logs `plug gated: modeset received.`
(`UnifiedPipeline.cpp:0x38f`) and jumps to `0x42a5264`; it does not execute
the nearby store to `[self+0x188]`. The function loads the preexisting
`[self+0x188]`, initializes its status to `0x8000000a`, and returns that
status if the pointer is null. If the prior pointer is non-null, it calls
`0x42a6634`, which checks cached byte `[self+0x1b8]`; if clear, it calls the
prior object's vtable `+0xf8`, stores the returned bit, and returns it. A
false result logs `IOAVVideoInterface open failed` at
`UnifiedPipeline.cpp:0x127`; the caller cleans up and still returns
`0x8000000a`. A true result continues into the existing modeset/state path,
whose final status and visible display result are not established by this
slice. P287's selected pair is not passed in `x1`; the runtime value at
`[self+0x188]` and this open gate's result/cached state are the missing
discriminators. A separate caller at `0x42a9d18` passes `x8` in `x1` after
its own gates and does not resolve the P287 path. This remains static-only;
no runtime value, property payload, or scanout result is inferred. Relevant
disassembly and source hashes are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN290_MODESET_NULL_INPUT_STATIC/`.
A second excerpt shows the constructor path at `0x42a6d68` initializing
`[self+0x188]` to null and `[self+0x1b8]` to zero after its input/helper gates;
the cleanup at `0x42a4ca8` releases a non-null `[self+0x188]` and clears it.
This supports treating `+0x188` as a managed pointer field and `+0x1b8` as a
cached state byte, but does not identify the pointed-to class or the vtable
method's semantics. The `0x4985090` value loaded into `x1` at the `+0xf8`
callsite lies in `__DATA,__bss`; its on-disk zero-fill does not reveal its
runtime value. The added lifecycle excerpt is
`modeset-object-lifecycle-and-global.txt` in the same recovery directory
(SHA-256 `b6e8801e0513736db67e3e30a74448f386245dff8bbe24407bab7a1afdf4d690`).
For a matching-device capture, sample `x0`/`[x0+0x188]` and
`[x0+0x1b8]` at `0x42a6634`, then at the `0x42a668c` indirect call record the
receiver, vtable `+0xf8`, runtime `x1` (loaded from `0x4985090`), `x2`/`x3`,
and `w0` at `0x42a6690` and the return. Also capture the caller's branch at
`0x42a527c` and cleanup/final status. Preserve the raw values and runtime
type evidence; do not substitute a guessed property or interface. D572/D582
remain hypotheses. For the next QEMU present-path capture, the generic-map
return is paired to its own entry TPIDR and restored SP, with descriptor mask
and surface slots retained as raw fields. The A408-named site is kept as a
dispatch boundary; its source-site LR is not used as a return breakpoint. A
return candidate is instead armed from the same-TPIDR dynamic-target hit's
LR/SP, still without assigning that target an operation name.

The same BSS address is also loaded at a distinct close-labeled callsite:
caller `0x42a62a0` tests `[self+0x1bd]`, reaches interface vtable `+0x100` at
`0x42a65e8`, then logs `ioav i/f is now closed!` and clears cached state.
This differs from open-labeled helper `0x42a6634` / vtable `+0xf8` at
`0x42a668c`; a matching-device sample must record both callsite and state
context. Shared `x1` source address does not reveal the BSS value's type,
payload, or display effect.
The static cross-reference also finds a writer at `0x431e590` in an
AppleDCPDisplayService.cpp routine beginning `0x431e550`: it stores entry
`x0` to `0x4985090` near the `DCPI2C, Publish` log and a subsequent
`0x4280950` call. That supports (but does not runtime-verify) a
display-service-object pointer flow into both later `x1` arguments;
registration semantics, persistence, and virtual-call meaning remain open.

P290 dual-global static follow-up: the `0x431e550` AppleDCPDisplayService
startup routine stores its entry `x0` at `0x4985090` (`0x431e590`) and later
stores the same saved `x19` at `0x49850a8` (`0x431e658`). Prior analysis
identifies `0x49850a8` as the AppleDCPDisplayService global. This gives a
specific runtime equality check for the open-labeled vtable `+0xf8` call and
the close-labeled `+0x100` call, whose `x1` comes from `0x4985090`. Static
initialization is not proof of runtime equality or persistence; compare both
globals and both call arguments on matching hardware. The updated lifecycle
excerpt SHA-256 is
`b6e8801e0513736db67e3e30a74448f386245dff8bbe24407bab7a1afdf4d690`.

P290 offline DCP follow-up — the `DCPDisplayInterface` helper at `0x41df3a4`
has a specifically logged no-link-modes fallback. It calls the receiver's
`+0x198` method, then the returned object's `+0x78`/`+0x80` list methods; if
either list is null, it logs “no color or timing modes from the link. Faking,
since we have non-null VI,” and creates two singleton arrays of 24-byte
records. The caller output struct's pointer/count slots and the record bytes
are captured in durable static note
`fallback-helper-output-shape.txt`. The `0x003c0000` word is only recorded as
a constant; its units and schema are unknown. This is not a runtime
`ColorElements`/`TimingElements` sample and is not yet linked to the
`UnifiedPipeline` builder or display publication. The note SHA-256 is
`29376240f4693620b5cfccbf65f9e2613201eb2109482b2860b504c43136a858`.

P291 final measured shell-to-lock result: after `DM_PERFORM_RETURN`, BKS
check-in, shell-service return, ADFL return, and a ready Home entity,
SpringBoard accepted two separate cover-sheet pan sequences. Three readable
`FB_SWAP_SET_LAYER_ENTRY` x3 captures at 1179x2556/stride 4864/BGRA contain
Lock Screen clock/status; one also shows the swipe-up instruction. All three
readable IOSurface samples precede the second accepted pan. About 169 seconds
after that pan, backboardd emitted three more `FB_SWAP_SET_LAYER_ENTRY` rows
(counts 30–32): the same numeric x1 layer pointer, x2=0/1/2, and x3=0 each
time. Their adjacent sender calls also had x3=0. This is a layer-update
callback burst with no surface pointer at the sampled argument, not a black
pixel sample. Over the remaining ~548 seconds to the 900-second observer
bound, there were no further swap, OGL/server/display-render, or IOSurface
callbacks, while 150 CA commits, 151 commit returns, and 1,777 CA Render Mach
sends were logged. HMP captures remained black. This does not prove Home UI,
authenticated unlock, or physical scanout; no passcode was entered. Final
chronology, raw hashes, event associations, and surface manifest are in
`recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN291_RETURN_DESCRIPTOR1/iosurface-capture-final.txt`
and `iosurface-lockscreen/manifest.json`.

P291 source trace: HMP `screendump` serializes the selected QEMU console's
`DisplaySurface` (`qemu-sptm/ui/ui-qmp-cmds.c`); console 0 defaults to the
`darwin-fb` device surface. `darwin-fb` initially wraps the guest boot-
framebuffer RAM from `boot_args.Video`. It switches to copied IOMFB pixels
only when `darwin_fb_present_bgra()` replaces the console surface. The only
caller found is `iomfb_scanout()` after descriptor parsing and `dart-disp0`
DVA translation (`qemu-sptm/hw/arm/darwin_iomfb.c`). Thus black P291 HMP
dumps and nonblack IOSurface bytes are compatible: HMP measures the QEMU
console surface, while the IOSurface probe measures a BackBoard surface at
`FB_SWAP_SET_LAYER_ENTRY`. This identifies distinct capture sources; it does
not establish physical scanout. Live process inspection showed P291 had
`DARWIN_DCP_IOMFB_A408_SCANOUT=0` and `DARWIN_DCP_IOMFB_A407_SCANOUT=1`, but
the model's A408 copy gate reads `DARWIN_DCP_IOMFB_SCANOUT` (absent in that
process). P291 stderr contained A408 RPCs but no scanout/present/failure
record; the A407 scanout option was enabled but no A407 candidate appeared
through guest time `1791813213.887`. This explains the black HMP console for
this capture: no copied DCP image replaced the boot-framebuffer surface. The
runner now explicitly sets `DARWIN_DCP_IOMFB_SCANOUT=0`; the old A408 spelling
is not consumed by QEMU. This is a QEMU presentation-path result, not a
physical-panel or firmware verdict. Keep these stages separate.

P291 final artifact integrity: `iosurface-capture-final.txt` has SHA-256
`04b07056f6e8675515b066d7e5e11f17c1f7908ca00b48b5494465e62c58db13`; the
HMP/IOSurface source trace has SHA-256
`04da7891ca12db5ef1a19f414e29db9981fa89dbe28c6228f62be94a72c15a68`. The
trace records the three post-gesture callbacks with x3=0 and the remaining
548-second quiet swap/render window. Those are no-surface/no-sample results,
not a pixel verdict.

P292 offline DCP reference audit: the complete saved disassembly has no direct
same-image branch to `0x41df3a4`; the Mach-O also has no exact raw pointer or
chained-fixup target for it. The nearby `constructColorElements` and
`constructTimingElements` strings at `0x413fe38` / `0x413fe90` are diagnostic
labels passed to the logger on allocation-failure paths, not calls to named
builders. The separate `0x4288a60` virtual-interface path has not been joined
to either array from the helper. Their schema and route to publication remain
unknown. See
`recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN292_FALLBACK_BUILDER_XREF_STATIC/fallback-to-elements-reference-audit.txt`
(SHA-256
`93fe8bc9b9284f765754a47c26ef79e869ea13d4dd4673313ab2137f199c8bde`) for
the search scope and limitations. The helper and builder do call provider
slots `+0x78/+0x80` with the same PAC diversity values (`0x8c10/0x37f7`),
which makes a shared interface plausible. Receiver and returned-range
identity remain unproven; the next device sample should compare helper input
`+0x198`, builder provider `this+0x5cd0`, both slot-return ranges, and the
helper's output-array addresses on the same Plug event. The durable P292
record now includes `helper-fallback.dis`, `log-label-callsite.dis`,
`unified-provider-slots.dis`, `fixup-chain-details.txt`, and
`reference-search.txt` beside the analysis note.

P293 post-home probe hardening: `dm_posthome_present_callbacks.py` now emits
the known `FB_SWAP_SET_LAYER_ENTRY` x3 status in the same JSON row, before
printing it, and in a separate cumulative status event. It continues to
classify null/invalid/pointer values after the 16-image read cap, distinguishing
“no pointer” from “valid pointer not sampled because capped.” Four pure tests,
`py_compile`, `git diff --check`, and runner `bash -n` pass. This change has not
yet been boot-validated and adds no P291 runtime evidence. Durable note:
`recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN293_POSTHOME_SURFACE_STATUS_PROBE/probe-hardening.txt`
(SHA-256
`422c4e5454d04ea3d7bef37ac2ef9fc7e1bda38c9d4d578ab26660ebc3498d0e`).

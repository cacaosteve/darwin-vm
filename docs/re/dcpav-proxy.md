# DCPAVRemoteSACControllerProxy: why "failed to start", and what the proxies want

Scope: the `DCPAVFamilyProxy` sub-drivers that bind to our EPIC announces
(`dcpav-*-epic`), read out of `firmware/bootkc` (iOS 27, iPhone17,3). The
trigger was `DCPAVRemoteSACControllerProxy failed to start` appearing as soon
as `dcpav-sac-epic` is announced.

**Status correction (2026-09-26):** the missing-`EPICLocation` diagnosis in
section 2 was a real historical QEMU bug, fixed by QEMU commit `f90e74c`. The
current QEMU source and binary include `EPICLocation="Embedded"`. GATE198
observed a non-null `EPICLocation` lookup and a successful `DCPAVProxy::start`
return on all 13 sampled calls. GATE199/201 then observed the seven-handler
`DCPAVPowerControllerProxy` path, not the video-interface proxy. GATE203
confirmed that the actual five-handler video proxy starts and registers, but
no common message send or dispatch on that object occurs before BackBoard's
mode query. That historical fix is not the current explanation for the
missing `ColorElements` property.
Section 5 below records the runtime confirmation; the remainder of this note
preserves the original static explanation of the old startup failure.

Address convention: every address below is an **unslid** kernelcache VA.
`file_offset = VA - 0xfffffff007004000` (verified: the DCPAVFamilyProxy
`__TEXT_EXEC` load command says `off=0x02aef4f0 addr=0xfffffff009af34f0`, and
the same delta holds for `__TEXT`, `__DATA_CONST` and the kernel's segments).
Runtime = unslid + `0x20000000`, so the `start()` under discussion runs at
`0xfffffff029af88e4`.

Two tooling facts that everything else depends on:

- **Vtable entries are chained fixups**, not raw pointers. In the file an
  auth entry is `bit63=1, bit62=0, target = low 32 bits + 0xfffffff007004000`;
  a plain rebase entry is `bits 42:0 + base`. Decoding them this way is what
  makes every "slot N -> function" claim below checkable.
- **iOS kernelcaches drop the `OSMetaClassDeclareReservedUnused` padding
  slots.** `IOService`'s vtable is 0x880 bytes in this Mac's own
  `/System/Library/Kernels/kernel.release.t8140` but 0x540 here (the
  `DCPAVProxy` subclass vtable is 0x590, `AFKEndpointInterface` 0x590,
  plain IOService-derived AFK classes exactly 0x540). Slot names in this
  document were taken from the symbolized macOS t8140 kernel (same XNU
  generation) with every `_RESERVED*` entry removed, then cross-checked by
  disassembling the iOS function in that slot. E.g. macOS `__ZTV9IOService`
  minus padding puts `getProvider` at 0x370 and `getWorkLoop` at 0x378; the
  iOS function in the DCPAV vtable's slot 0x378 (`0xfffffff00b1f49a0`) is
  `x = this->vfunc[0x370](); return x ? x->vfunc[0x378]() : NULL`, which is
  `IOService::getWorkLoop()` verbatim, and the function right after it reads
  `__providerGeneration`/`__provider` (`[x0,#0x38]`, `[x0,#0x30]`), i.e.
  `getProvider`.

## 1. The virtual at 0xfffffff009af896c is `IOWorkLoop::addEventSource`

`DCPAVRemoteSACControllerProxy::start` (vtable `0xfffffff0081ce7e0`, slot
0x2b0 -> `0xfffffff009af88e4`, class from its `getMetaClass` at slot 0x38 ->
`gMetaClass 0xfffffff00b869270`, name string `0xfffffff0078a3485`):

```
9af8908  bl   0xfffffff009b03e40          ; DCPAVProxy::start(provider, table=0xfffffff0081cee40, 1)
9af890c  cbz  w0 -> fail
9af8910  strb #1, [this,#0xd0]
9af891c  bl   stub 0xfffffff009b0969c      ; __auth_got 0xfffffff0081d5270 -> 0xfffffff00b240658
9af8920  str  x0, [this,#0x610]           ;   = IOCommandGate::commandGate(this, NULL)
9af8924  cbz  x0 -> fail
9af8928  this->vfunc[0x378]()             ; IOService::getWorkLoop()  (kernel 0xfffffff00b1f49a0)
9af8950  x1 = [this,#0x610]
9af8954  x0->vfunc[0xa0](x0, gate)        ; <-- the call in question
9af8970  cbnz w0 -> fail
9af897c  gate->vfunc[0xe8](gate, 0xfffffff009af8e4c, 0,0,0,0)   ; IOCommandGate::runAction(bootCompleteGated)
9af89e8  this->vfunc[0x2a0](this, 0)      ; IOService::registerService(0)
9af8a14  fail: log 0xfffffff0078a359f "DCPAVRemoteSACControllerProxy failed to start"; return false
```

- `0xfffffff00b240658` is `IOCommandGate::commandGate`: it calls
  `gMetaClass->vfunc[0x68]` (alloc) on the metaclass at
  `0xfffffff00b6bec60`, which the kernel's own `__mod_init_func` constructs
  with the name string `"IOCommandGate"`, then `obj->vfunc[0xd8](owner,
  action)` (= `IOCommandGate::init`, slot 0xd8 in the iOS IOCommandGate
  vtable `0xfffffff007e1d130`) and releases on failure -- instruction for
  instruction the macOS `__ZN13IOCommandGate11commandGateEP8OSObjectPFiS1_PvS2_S2_S2_E`.
- The receiver of the `+0xa0` call is therefore whatever `getWorkLoop()`
  returned: an **`IOWorkLoop`**, not an `AFKEndpointInterface`. In the iOS
  kernel's `IOWorkLoop` vtable (`0xfffffff007e1ce40`, 35 slots) slot 0xa0 is
  `0xfffffff00b23b984`, whose body matches macOS
  `IOWorkLoop::addEventSource` exactly (`ldr x8,[x0,#0x30]` workThread name
  check, then `controlG->vfunc[0xe0](NULL, newEvent, 0, 0)` =
  `IOCommandGate::runCommand(mAddEvent, newEvent)`).
- Every `IOWorkLoop` subclass in the whole kernelcache was found by scanning
  for vtables that carry `_maintRequest` (`0xfffffff00b23bd50`) at slot
  0x78: six of them (AppleH16ANEInterface, AppleH16CameraInterface,
  AppleMultiFunctionManager, AppleT8140CLPC, IONVMeFamily,
  IOThunderboltFamily), all inheriting `addEventSource`, none in the
  DCP/AFK/RTBuddy chain. AppleFirmwareKit's `AFKWorkloop`
  (`gMetaClass 0xfffffff00b7ce0a0`, super = `OSObject`, size 0x28, 15-slot
  vtable `0xfffffff007fb0348`) is an OSObject wrapper, not a workloop.
- Which workloop: the nub is an `AFKEPInterfaceKextV2` (allocated with
  `operator new(kalloc_type 0xfffffff007fb2468, 0x1e0)` at
  `0xfffffff008b7bd5c` in the PUBLISH handler; 0x1e0 is that class's size).
  Neither it nor `AFKEPInterfaceServiceKextV2` overrides `getWorkLoop`
  (slot 0x378 in both is the kernel function); `AFKEPKextV2` (the
  `AFKDCPEndpoint1` driver) does, and returns `[this+0x1a8]`
  (`0xfffffff008b832f8`). A plain kernel `IOWorkLoop`.

### It cannot return non-zero here

`IOWorkLoop::addEventSource` returns `controlG->runCommand()`, which is
`IOCommandGate::runAction` (`0xfffffff00b23f6b8`). That function's only
error exits are `kIOReturnBadArgument` when the action is NULL
(`cbz x1 -> w19 = 0xe00002eb - 0x29`), `kIOReturnNotReady` when the gate has
no workloop (`cbz x20 -> - 0x13`), and `kIOReturnAborted` if a disabled gate's
sleep is interrupted. `controlG`'s action is `_maintRequest` and its workloop
is set in `IOWorkLoop::init`, so none apply. `_maintRequest` for `mAddEvent`
returns 0 on both paths (macOS `0xfffffe0007bd2af0` and `0xfffffe0007bd2b24`
are `mov w0,#0`; `kIOReturnBadArgument` is only for an unknown command).

So the `cbnz w0` at `0xfffffff009af8970` is not what fails unless a live
register dump says otherwise. The same log line is printed for all three
`fail` edges, and the first one is the interesting one.

## 2. What actually fails: `DCPAVProxy::start` at 0xfffffff009b03e40

This is the "super-start". It is `DCPAVProxy::start(IOService *provider,
const HandlerTable *table, unsigned count)`; the SAC proxy passes its
one-entry table `0xfffffff0081cee40` = `{ handler 0xfffffff009af8a28
(handleRegisterSACAggressor), commandID 0x30, "RegisterSACAggressor"
0xfffffff0078a358a, ... }`. Every checked exit, in order:

| addr | check | fails when |
|---|---|---|
| `9b03e44` | `table && count` | never (constants) |
| `9b03f74` | `this+0xb8 = OSMetaClassBase::safeMetaCast(provider, *0xfffffff007fa7ee8)` (= `AFKEndpointInterface::metaClass`, deref -> `gMetaClass 0xfffffff00b7cdc58`) | provider is not an AFKEndpointInterface -- ours is (`AFKEPInterfaceKextV2` derives from it) |
| `9b03fa8` | `getWorkLoop() != NULL` | no workloop in the provider chain |
| `9b03fb8` | `this+0xf0 = IOLockAlloc()` (`0xfffffff00b1e16d0`) | OOM |
| `9b03fe8` | `this+0xc0 = IOCommandGate::commandGate(this, NULL)` | OOM |
| `9b0400c` | `workloop->addEventSource(gate)` | **return value ignored** |
| `9b04038` | `this+0xf8 = thread_call_allocate_with_options(0xfffffff009b03b2c, this, 1, 1)` (`0xfffffff00ab50168`) | OOM |
| `9b04060` | `IOService::start(provider)` (via `__ZTV9IOService`+0x10+0x2b0, got `0xfffffff0081d5558`) | never in practice |
| `9b040ac` | `OSDynamicCast(OSString, provider->getProperty("EPICLocation"))` (slot 0x118 = `IORegistryEntry::getProperty(const char*)`, string `0xfffffff0078a29a5`, metaclass ptr `*0xfffffff007e59340` = `OSString::gMetaClass 0xfffffff00b6bdcc0`) | historically failed when the nub lacked this property; fixed in current QEMU as described above |
| `9b04158` | `OSDynamicCast(OSNumber, provider->getProperty("EPICUnit"))` (`0xfffffff0078a29c4`, `OSNumber::gMetaClass 0xfffffff00b6bdba8`) | we already send `EPICUnit` as a 64-bit number, passes |
| `9b041dc` | `this->vfunc[0x550](this, provider)` | never: `DCPAVProxy` slot 0x550 = `0xfffffff009af557c` = `mov w0,#1; ret` |
| `9b04214` | `afkIface->vfunc[0x570](this, blkA, blkB, blkC)` = `AFKEndpointInterface::open(client, ...)` `0xfffffff008b6fb40` | see section 3 |

Failure path `0xfffffff009b04230`: `this->vfunc[0x2b8](provider)`
(`IOService::stop`) then `return false`, which is what the SAC `start`
sees at `0xfffffff009af890c`.

What is done with the two properties (so the values can be chosen sensibly):

- `EPICLocation`: `str->isEqualTo("External")` (OSString slot 0xb8,
  string `0xfffffff0078a29b2`), inverted, stored at `this+0xe8`; then
  IOAVFamily `0xfffffff009db7868` maps `0 -> "External"`, `1 -> "Embedded"`
  (strings `0xfffffff007903804`, `0xfffffff00790380d`) and the result is
  `setProperty("Location", <that string>)` (slot 0xd0). The firmware's own
  string table has `External`/`Embedded` adjacent at `dcpfw+0x3f65d5/+0x3f65de`
  and `EPICName/EPICUnit/EPICLocation/EPICProviderClass` adjacent at
  `dcpfw+0x3f6ef6..+0x3f6f15`, so these are the two values it emits. For the
  built-in panel's services use **`"Embedded"`**; anything that is an
  OSString passes the check.
- `EPICUnit`: `unsigned32BitValue()` -> `this+0xec`,
  `setProperty("Unit", value, 32)`.

## 3. What comes after the property fix

`AFKEndpointInterface::open` (slot 0x570, `0xfffffff008b6fb40`) is:
`if (isOpen(NULL)) return false;` (slot 0x2d0) then `this+0xa8 = 1`, build a
client descriptor from the three blocks (`0xfffffff008ba0cfc`), and
`IOService::open(this, client, 0, descriptor)`. That lands in
`AFKEPInterfaceKextV2::handleOpen` (slot 0x2d8, `0xfffffff008b789bc`): cast
the descriptor, append it to the `OSArray` at `+0xa0`
(`[x22,#0xa0]->vfunc[0xe8]`), clear `+0x1b8`, os_log, `return true`
(`0xfffffff008b78af0`). **No wire traffic, no waiting.** So an `OPEN`
(`0x12`) report from the firmware is not a precondition for `start()`.

Once `start()` returns true the SAC proxy immediately runs
`bootCompleteGated` (`0xfffffff009af8e4c`, name string `0xfffffff0078a3639`)
under its command gate. It builds a 64-byte header on the stack:
`+4 = 0x00000007` (command), `+8 = 0x00000010` (data_len) from the constant
at `0xfffffff0078a2198`, `+0xc = 0x69706378` (the `epic_service_call`
magic), and calls `DCPAVProxy::sendMessage` (`0xfffffff009af37e0`). That
runs a block through `IOCommandGate::runActionBlock`
(`0xfffffff00b240704`). The common send routine branches on bit 1 of the
message flags: when set, its action calls `AFKEndpointInterface` slot `+0x558`
(`sendCommand`); when clear, it enters helper `0xfffffff009b03928`, calls
endpoint slot `+0x548`, and that dispatches to `AFKEPInterfaceKextV2` slot
`+0x578`. The AP then waits for the response with a timeout (the
`"AFKEPInterfaceKextV2::%s: command response timeout (%u ms) on %s,
commandID:%u, EP:%s"` string at `0xfffffff00740aa5f` is reached from
`0xfffffff008b77bd0`). `bootCompleteGated` reads back `msg+0x40` and the
send result, logs on error via `stringFromReturn` (slot 0x470), and start
proceeds to `registerService(0)` either way.

So the phases are, in order: (1) property -- cheap, blocks everything;
then (3) a real command (`0xc0`, group 0 / command 7 / 16-byte payload) that
wants a response, which is where `docs/re/afk-epic-references.md` section 2.3
takes over. Whether the AP also emits an `OPEN` report before that command
is not settled here: the report sender is `0xfffffff008b8fdd4` (its tail at
`0xfffffff008b8fe4c` sends type `0x12`), reached only through slot 0x20 of a
non-OSObject interface vtable, and the `_openSent` bookkeeping lives in
`0xfffffff008b79114` (both `"!this->_published && !this->_openSent"` and
`"this->_openSent"` assert thunks, `0xfffffff008ba37a4/0xfffffff008ba37d0`,
belong to it). Reading that one function decides it; it does not affect
whether `start()` succeeds.

## 4. The other proxies

Every `DCPAV*Proxy` (`DCPAVControllerProxy`, `DCPAVDeviceProxy`,
`DCPAVServiceProxy`, `DCPAVVideoInterfaceProxy`, `DCPAVCECInterfaceProxy`,
`DCPAVAudioInterfaceProxy`, `DCPAVPowerControllerProxy`) has
`super = DCPAVProxy::gMetaClass 0xfffffff00b8691a8` and goes through the
same `DCPAVProxy::start`, so they all need `EPICLocation` too. The
`dcpdp-*`/`dcpdptx-*`/`dcpmipi-*` classes live in other kexts and were not
checked.

## 5. Runtime confirmation and current boundary

GATE198 broke at the shared `DCPAVProxy::start` path and observed 13
`EPICLocation` lookup results, all non-null, followed by 13 returns of `w0=1`.
These are observed calls, not necessarily 13 unique endpoint objects. GATE199
and GATE201 followed the seven-handler `DCPAVPowerControllerProxy` path at
`0xfffffff009b091c0` (its first table entry is `SetRemoteDevicePower`). It
called `registerService`, and static disassembly shows the method returns its
saved successful base-start result. The breakpoint at `0xfffffff009b09218` is
before `mov x0,x20`; there, `x0` is only the raw value left by the void
`registerService` call, while `x20` holds the base-start result. Do not
interpret that `x0` as an IOReturn. The exact subclass return site is
`0xfffffff009b0921c`. Its distinct five-handler table at
`0xfffffff0081d20e8` has `ForwardMessage`, `Terminate`,
`FrameReceiveCallback`, `HPDCallback`, and `EDIDCallback`.

GATE202 reached the video-proxy path but is invalid because its callback raised
`UnboundLocalError` while formatting the base-start return. GATE203 fixed the
callback and observed the runtime video vtable `0xfff0081d1990`, a five-handler
base-start call returning `1`, the `registerService` call, and the subclass
start return `1`. Hooks on common `DCPAVProxy::sendMessage` and
`DCPAVProxy::handleCommand`, filtered to this vtable, did not fire before
BackBoard's query. This confirms successful service startup but leaves the
video-command trigger unresolved.

Despite successful proxy startup, BackBoard still finds no
`ColorElements` property on its selected `disp0` service (`0xe00002bc`). The
armed IOAV `withProperties`, parser, and setter hooks did not fire in GATE199,
and the QEMU log showed the AP open interface 4 but no AP command type `0xc0`
on that interface before the mode query. Those runs did not trace the actual
video proxy. GATE203 then confirmed that the five-handler video proxy starts
and registers, but its common send/dispatch hooks stay quiet through the
query. GATE204–209 broadened tracing to every DCPAV proxy and found successful
`0xc0` sends on interfaces 1 (controller), 8 (SAC), and 10 (DP device). All
sampled requests take the bit-1-clear path described above. Corrected GATE207
payload reads showed zero-filled request payloads, and GATE209 found zero
response bytes before parsing. Those replies are QEMU-generated probes, not
real firmware semantics, so they do not explain the missing internal-panel
`ColorElements` property. Interface 10 is a DCPDP-family DisplayPort path and
should not be conflated with the internal panel. The remaining boundary is
the IOAV property-publication owner and the real response schema for the AV
controller/SAC exchanges.

GATE221 and GATE224 disabled the external DPTX/HDCP announcements so the
internal `dcpmipi-controller-epic` publish could complete. The AP decoded and
matched it, `DCPMIPIControllerProxy::start` and `registerService` returned
success, and QEMU observed `REPORT OPEN` on interface 12. Thus the earlier
interface-16 no-open result was queue starvation, not a missing MIPI proxy.

GATE223 corrected the common-message filter with
`RFT_TRACE_DCPAV_ALL_MESSAGES=1`. It recorded the three known sends on the
controller, SAC, and external DP-device proxies, with no common
`handleCommand` and no send or dispatch on the five-handler video proxy. The
same run showed no IOAV video child setup, property parser, or setter. GATE224
added the IOAV interface-tracker entry, ID-compare, and conditional child-call
sites; none ran before BackBoard's natural type-1 query.

GATE225 separately confirmed AppleCLCD2 `start` on `disp0` returns success
with its provider equal to its registry object, but the traced start path does
not request framebuffer width or height. BackBoard still reads null
`ColorElements` from that service and receives `0xe00002bc`. The immediate
runtime boundary is now the registry publisher/property path that should
populate `ColorElements`; the AV controller/SAC response schema remains a
separate firmware question. See the current probe map in
`docs/re/quartzcore-render-bounds-followup.md`.

GATE226 saw no AFK report on interface 12 after the AP opened the MIPI proxy;
only the initial PUBLISH arrived. GATE227 found that
`IOAVHandlerInternalDisplay::plugEvent_async_gated` and its color/timing
selection branches also do not run before BackBoard's query. The gap is before
that handler consumes a video interface. The current QEMU EPIC model has no
spontaneous MIPI report producer, so firmware-originated interface-12 traffic
is still a candidate missing input; the AP's expected report schema is
unknown. GATE228 found no base `IOAVHandler::IOAVEvent` dispatch; GATE229 also
found no internal-display handler allocation, base construction, setup, or
event registration. That rules out a cast/rejection at the handler and moves
the immediate boundary to earlier interface creation. GATE230 resolved the
five video-class allocator targets but did not arm them until after the early
boot phase. GATE231 armed those target addresses from reset and observed no
allocator or `IOAVInterface::start` hit before BackBoard's query. This narrows
the observed gap to the request/matching event that should instantiate those
classes; the next step is to identify that path from the kernelcache's
personalities and provider flow. Do not fabricate the report or
`ColorElements` payload.

### Hot-plug string scope correction (2026-10-11)

The H17P DCP firmware string `HotPlugDetectChangeOccurred` belongs to the
DCPAV DisplayPort remote-port event path. Its caller validates the event's
sender against the stored remote `_port` object and then changes DP link
state. It is not a generic internal-display/MIPI notification and should not
be used to manufacture the missing internal-panel `IOAVVideoInterface` or
`ColorElements`. This is a static classification of the recovered firmware;
QEMU does not execute that DCP task.

P226 adds a runtime qualification: the reset-time AFK observer saw a 300-byte
`dcpmipi-controller-epic` PUBLISH on interface 16. The corresponding
interface-16 parse/decode/handoff was not captured before the early trace
session ended; interface-13 DisplayPort parsing is a distinct path. The
current boundary is therefore downstream of the observed MIPI PUBLISH, not
absence of that report. No `ColorElements` value or mode was injected. See
P226 in `docs/re/iomfb-dseries.md` for the Home/mode observations and artifact
paths.

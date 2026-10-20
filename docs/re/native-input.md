# Native input development (24A5430a)

## Current transport: DVMI2 over the console UART (2026-09-05)

The relay/Recap bridge is no longer the normal input path. The replacement
has three parts, all in the repo:

| Part | Where | Role |
|---|---|---|
| `darwin-input` device state | `qemu-sptm/hw/arm/darwin_input.c`, `include/xnu/darwin_input.h`, hooked from `darwin_fb.c` | bounded queue, framing, UART backpressure, ACK/readiness parsing, counters, migration state, status file |
| UART TX observer | `qemu-sptm/hw/char/exynos4210_uart.c` (`exynos4210_uart_set_tx_observer`) | lets QEMU read the helper's replies out of the console stream; the FIFO full/empty fix is untouched |
| `dvm-hid` guest helper | `tools/input/dvm_hid.c` (installed as `/usr/local/libexec/dvm-input`, same cached launchd job) | IOKit-only virtual HID service; reads records on the console thread independent of HID initialisation |

Enable with `DARWIN_INPUT_UART=1` (and `DARWIN_INPUT_STATUS=<path>` for the
JSON status file; `tools/warm_boot_probe.py` sets it per run). The legacy
capture `DARWIN_TOUCH_EVENTS` plus `tools/input/relay.py` and helper v6
remain the diagnostic control on the old checkpoints; do not run both
against one guest, they share the console.

### Why not a QEMU device consumed by a kernel touch driver

Evidence from `/tmp/dvm/RTC_SOURCE1/DISPLAY_SMP6.rtc-ns.bootkc` (prelink
info parsed with plistlib) and the warm DeviceTree
`/tmp/dvm/SETUP_ACTIVATION1/dt_warm_development_hactivation.bin`:

- The real touch controller is `/arm-io/dockchannel-mtp/mtp-transport/multi-touch`
  (`mtp-transport` has `role = MTP`). The kernel chain is `AppleDockChannel`
  (`IONameMatch dockchannel,t8002`) -> RTBuddy over DockChannel ->
  `AppleHIDTransportDeviceMailbox` (`IONameMatch MTPEndpoint1` on
  `RTBuddyEndpointService`, `AppleHIDTransportMailbox` 1000.41) ->
  `AppleMultitouchHIDService` (`DeviceUsagePairs` 0x0D/0x04, `Manufacturer`
  Apple, `parser-type 1`, `AppleMultitouchDriver` 1000.44). The kernelcache
  strings show that parser speaking the proprietary Z2/HBPP multitouch
  protocol (`AppleHIDTransportProtocolZ2.cpp`, `AppleHIDTransportBootloaderHBPP.cpp`,
  `getMultitouchReport`, `Family ID`). Modelling it means emulating the MTP
  coprocessor firmware's RTKit endpoints and the Z2 report/feature protocol
  with no public reference; that is not invented here.
- `IOHIDEventDriver` (2.0.0) on this kernelcache has no generic digitizer or
  pointing personality: only keyboard (0x01/0x06,0x07), buttons, sensor, the
  FIFO-transport "Peppy Touch Display" consumer-control entry and BLE. A
  generic USB/virtio HID touchscreen would bind to nothing, and there are no
  virtio kexts at all. `IOHIDResource`/`IOHIDUserDevice` exist (entitlement
  string `com.apple.developer.hid.virtual.device` is in the kernelcache), but
  a user device is only useful if a HID event driver matches its descriptor;
  for touch that is again `AppleMultitouchHIDService` (or its `MTUserDevice`
  personality, usage page 0xFF60 usage 7) with the same proprietary parser.
- USB is present (`AppleUSBXHCIARM`, `IOUSBHostHIDDevice`) but the XHCI on
  `/arm-io/usb-drd` needs the ATC PHY/`dart-usb` path, and iOS would still
  have no digitizer event driver for a USB touchscreen.
- A second Samsung UART would bind natively (`AppleSamsungSerial` matches
  `uart-1,samsung`, `IOSerialBSDClient` publishes `/dev/cu.*`), but the
  firmware DeviceTree only has `uart0`, `darwin.c` deliberately leaves the
  console UART interrupt unconnected because `exynos4210_uart` cannot model
  Apple's `UINTP` semantics, and `AppleSamsungSerial` is interrupt driven.
  The console UART is polled by XNU and already proven, so it stays.

The one native consumer that works without display services is backboardd's
HID event system through `IOHIDVirtualServiceClientCreateWithCallbacks`
(IOKit `0x18f00e8f8`) and `IOHIDVirtualServiceClientDispatchEvent`
(`0x18f00afa0`), the exact calls HID.framework's `HIDVirtualEventService`
makes (`-[HIDVirtualEventService activate]` `0x2678a8ee4`,
`dispatchEvent:` `0x2678a71d0`, open source
`IOHIDFamily/HID/HIDVirtualEventService.m`) and that Recap wrapped for the
old helper. The callbacks struct is
`IOHIDVirtualServiceClientCallbacksV2 {version=2, notify, setProperty,
copyProperty, copyEvent, setOutputEvent, copyMatchingEvent}`; the thunks at
`0x2678a747c`/`0x2678a6138`/`0x2678a7410` confirm `target` is argument 0 and
the key/type/value follow `context` and the service client. The service
properties mirror Recap's touchscreen sender: `displayUUID` `"<main>"`
(`BKSDisplayUUIDMainKey` -> BackBoardServices cstring `0x18a1813ad`),
`Built-In` false (`kCFBooleanFalse` `0x1e8ee0108` at `0x29b2082b4`),
`Authenticated` true, `Transport` `"Recap"` (defaults block `0x29b1ffc30`).

### Wire protocol

Host -> guest, one ASCII line per record injected into the console RX FIFO:

```
DVMI2 <epoch> <seq> <K> <a> <b> <c> <host_ms>
```

`K` is `D` down, `M` move, `U` up, `C` cancel, `P` ping, `B` button
(`a` = consumer usage, `b` = down), `W` wheel (`a,b` = pointer, `c` = signed
notches). `a,b` are QEMU's normalised 0..32767 coordinates for `D/M/U/W`;
`c` is 0 for every other kind. Guest -> host:

```
DVMI2A <epoch> <seq> <code> <state> <guest_us>   code S submitted, F dispatch failed,
                                                 N not ready, Q accepted without HID, E rejected
DVMI2R <state> <pid> <epoch>                     state I initialising, R ready, L lost
```

Policies (all in `darwin_input.c`, host-tested by
`tools/tests/test_native_input.py`):

- Queue of 64 records; motion behind unsent motion is coalesced, button
  edges never are. Overflow of edges opens a new epoch and cancels instead of
  replaying a late gesture. At most four records are unacknowledged.
- Records are only queued while the helper has announced `I` or `R`; before
  that they are dropped and counted, never buffered in the console tty.
- An unacknowledged record after 10 s of **guest** time marks the guest
  `L` and is never resent. Virtual time is used so a paused guest cannot
  time out.
- Device reset, migration restore, a helper restart (new pid announcing
  `I`) and overflow all bump the epoch; the first record of a new epoch makes
  the helper release any contact it holds. The helper also releases on
  console EOF.
- Coordinates: QEMU 0..32767 -> helper `x/32767.0` -> digitizer event
  fraction -> backboardd scales to the 1179x2556 display; UIKit points are
  half that (verified earlier in `single-touch.md`). Pixel (x, y) is
  `round(x*32767/1179), round(y*32767/2556)`; `native_input.py --px`.
- Host buttons: the left button is the finger; the **right button is Home**
  (consumer 0x0c/0x40, both edges) and so are F5 (Home) and F6 (Power).
  **Mouse wheel**: QEMU counts notches for 40 ms (max 8) and sends one `W`
  record at the pointer; the guest performs a drag of 5 % of the display
  height per notch (capped at 40 %), eight 12 ms steps, then a 60 ms hold
  before the release so the pan velocity is zero and UIKit scrolls exactly
  that distance without a fling. Wheel up moves the finger down (content
  scrolls up), which is AppKit's normalised delta sign in `ui/cocoa.m`, so it
  is independent of the "natural scrolling" setting. A wheel while the left
  button or a contact is down is ignored, and the helper ignores a `W`
  while it holds a real drag.
- Migration: epoch, sequence, readiness and counters travel in the
  `darwin-fb/input` subsection; queued, in-flight and wire bytes deliberately
  do not, `post_load` opens a new epoch. Old checkpoints without the
  subsection load unchanged (`NATIVE_HID_COMPAT1`: `POWERD_GUARD_BATTERY1`
  restored on the new binary at the exact PC, epoch 3 after restore).

### Results

All runs below are independent disk boots of the `CLOCK_SOFTWARE_PATCH1`
lineage with the DVMI2 helper installed (`tools/re/install_staged_helpers.py`,
children `NATIVE_HID_INSTALL2..5`), no LLDB, no restored RAM, on a host that
was also running five other 12 GiB TCG guests (load average 7-9 on 18 cores).
Pinned binaries: QEMU `/tmp/dvm/native-input-qemu6/qemu-system-aarch64`
(SHA-256 `9f0be86f...`; qemu4/5 are earlier builds of the same series;
`native-input-qemu7` adds only the delivery-field range guard and is the
build matching the committed source), helper v10
`/tmp/dvm/native-hid-v10/dvm-input`, fresh-boot manifests
`/tmp/dvm/native-hid-v10/warm-manifest.json` (qemu6) and
`warm-manifest-qemu7.json`.

| Run | Helper | Early boot | Helper `R` | First presentation | Notes |
|---|---|---|---|---|---|
| `NATIVE_HID_FRESH1` | v7 | 10.1 s | 31.0 s | none before quit | notify-type bug marked the service lost; quit |
| `NATIVE_HID_FRESH2` | v8 | 10.0 s | 44.3 s | +136 s after resume | first helper instance died, launchd restart detected, re-epoched |
| `NATIVE_HID_FRESH3` | v9 | 10.7 s | 30.2 s | +172 s after resume | pid 87 died after `R`; pid 204 init took 207 s under load |
| `NATIVE_HID_FRESH4` | v10 | 12.0 s | 42.4 s | ~190 s guest time | same pid all run; ping RTT mean 107 ms |

Stage timings (QEMU status file, `ui/input.jsonl` under each run):

- Transport: ping RTT (inject -> helper ACK line back) mean 1.3 s / max 10 s
  on v9 while SpringBoard was starting, 107 ms mean / 5.5 s max on v10 with
  `setpriority(-20)` and user-interactive QoS (`NATIVE_HID_FRESH4`). Ten
  pings on v9 exceeded the 10 s guest-time bound and were counted as
  timeouts, never resent.
- Guest handling: reader `handle()` 4-30 us; HID dispatch
  (`IOHIDVirtualServiceClientDispatchEvent`) 2-60 ms when the guest is idle,
  1.4-7.1 s when SpringBoard is busy (v8/v9). The 7.1 s sample was a Home
  release; the following release-to-frame time was 16 ms.
- Input-to-presented-frame (host send of the release -> first `iomfb:
  presented`): 14-135 ms across 12 gestures, i.e. the DCP path is not the
  latency.

Visible results, all through QMP `input-send-event` (the Cocoa window path):

- Fresh boot 3 and 4: F5 Home from the lock screen reached the icon grid
  (`NATIVE_HID_FRESH4/ui/02-after-home.png`). A tap on the Settings icon at
  pixel (138,256) started the Preferences launch each time (launch surface
  in `03-settings-after.png`, `07-seq-20.png`), and the guest was back on the
  icon grid 45 s later on every fresh-boot attempt (four attempts across the
  two boots). No Preferences corpse appears in `NATIVE_HID_FRESH4/corpses.txt`
  (only the InputUI/PosterBoard/AccessibilityUIServer `0x8badf00d` launch
  watchdog kills and a chronod SIGABRT). This is an application/launch
  failure under load, reported separately from input.
- Snapshot restore: `checkpoints/NATIVE_HID_HOME1` was captured from fresh
  boot 3 at Home (4.31 GB, 16.6 s). `NATIVE_HID_RESTORE1` restored it at the
  exact PC in 1.31 s; QEMU opened epoch 4 and the helper acknowledged the
  cancel. A native tap then launched and rendered Settings
  (`NATIVE_HID_RESTORE1/ui/01-settings-after.png`), a native swipe scrolled
  the list (`02-scroll-after.png`, 11 records, 10 dispatched, max 29 ms),
  dispatch 9.8 ms per touch. Two later Home presses on that guest were
  dispatched (`S`) with no visible effect and a third returned `F` from the
  button service; the touch service kept working. Cause not established.
- No stuck contact was observed: every gesture ended with `contact_sent`
  false and the helper's `held` cleared; the freeze experiments released
  through explicit `U` records after `cont`.

Two stalls, measured separately with frozen RAM (`warm_boot_postmortem.py`,
slides `0x1d28c000` / `0x544000` from `mach_msg2_trap`):

1. Delivery: `NATIVE_HID_FRESH2` frozen 0.73 s after injecting a `D` found
   the helper still in `fgets` -> `read` on the console; the record had not
   been delivered. The kernel console input path is polled and starved
   under load.
2. Dispatch: `NATIVE_HID_FRESH3` frozen 1.46 s into a "dispatch" found the
   helper's worker inside `malloc` under `IOHIDEventCreateDigitizerEvent`
   (`stacks-symbolicated.txt`), while backboardd's main thread was in
   `CA::Context::invalidate` from animation callbacks and another thread was
   encoding a `BKSHIDEventDeliveryChain` for the previous touch. The stall
   is CPU starvation of the helper, not a backboardd wait; v10's priority
   change is the response and cut ping RTT tenfold.

Right-click Home and mouse-wheel scrolling (helper v11, QEMU `native-input-qemu8`):

- `NATIVE_HID_FRESH5`: a right-button press/release from the lock screen
  reached the icon grid (`ui/02-after-rightclick.png`, button dispatch 9 ms /
  51 ms). Two wheel-up notches on the icon grid produced one `W` record and a
  ten-event drag (1.7 s of dispatch under load) with no visible Spotlight
  pull; the icon grid needs a longer pull than 10 % of the screen.
- `NATIVE_HID_RESTORE2` (checkpoint `NATIVE_HID_HOME2` of fresh boot 5): a
  native tap launched and rendered Settings, then wheel notches -3, +1, -1,
  -2 each scrolled the list by the dragged distance and stopped without
  flinging (`ui/02-wheel-down3-after.png`, `03-wheel-*-after.png`;
  dispatch 198-247 ms per ten-event gesture, contact released each time).
- On that restored guest, about a minute after restore the right-click Home
  was dispatched (`S`) with no effect and every later dispatch, touch
  included, returned `F` (`serial.log` `DVMI2D 4 266..274 F`) while the UI
  kept presenting. The same sequence happened on `NATIVE_HID_RESTORE1`.
  Cause: `NATIVE_HID_RESTORE2/process-stacks.json` (frozen RAM after the
  failures) lists backboardd PID 74 (stale) and PID 410 (live) and
  SpringBoard PID 35 (stale) and PID 429 (live): both UI processes were
  replaced after the restore, the helper's virtual services died with the
  old backboardd, and the helper received no notification for a dead
  server. This also explains why Settings survives its launch only on
  restored guests: they run a fresh SpringBoard. Why the restore triggers
  the restart (wall-clock jump from the RTC leaf is the obvious suspect) is
  not established. Helper v12 re-creates both virtual services after two
  consecutive dispatch failures or a reset notification and announces
  `I` -> `R`; see the v12 entry below for whether that recovers.

Known limitations:

- The first helper instance died in fresh boots 2 and 3 (no corpse in the
  RAM dumps; not a `DVM_HID_EOF`), and launchd's 10 s throttle plus a busy
  backboardd made the replacement's initialisation take up to 207 s. v10 ran
  a single instance; one run is not proof.
- Button-service dispatch failed (`F`) after the restore experiment above.
- Settings does not survive its launch on these fresh boots; it does on the
  restored guest. Repeated Settings launches/navigation therefore pass only
  on `NATIVE_HID_RESTORE1`.
- The `delivery_ms` field is unusable: the guest wall clock is not aligned
  with the host (`1788459...` values); QEMU ignores it. RTT is the transport
  latency figure.
- Cocoa clicks were not exercised by hand; QMP `input-send-event` feeds the
  same absolute handler.

Reproduce:

```sh
python3 tools/warm_boot_probe.py /tmp/dvm/native-hid-v10/warm-manifest.json \
  --tag NATIVE_HID_NEXT --seconds 600 --stop-on 'DVMI2R R' --keep-paused
python3 tools/hmp.py /tmp/dvm/NATIVE_HID_NEXT/monitor.sock cont
python3 tools/input/native_input.py --run /tmp/dvm/NATIVE_HID_NEXT wait-ready
python3 tools/input/native_input.py --run /tmp/dvm/NATIVE_HID_NEXT --frames /tmp/dvm/NATIVE_HID_NEXT/home home
python3 tools/input/native_input.py --run /tmp/dvm/NATIVE_HID_NEXT --px --frames /tmp/dvm/NATIVE_HID_NEXT/tap tap 138 256
python3 tools/input/native_input.py --run /tmp/dvm/NATIVE_HID_NEXT --px --hold-ms 250 swipe 589 2100 589 900
```

For a restore, symlink the restore's `.restore.qmp.sock`, `.restore.sock`
and `DARWIN_INPUT_STATUS` file as `qmp.sock`, `monitor.sock`,
`input-status.json` in a run directory (see `NATIVE_HID_RESTORE1`).


## Historical helper v4-v6 notes

Latest disk-boot result: helper v5 opens its own console input, and the
development launchd cache now includes its service. `WARM_INPUT_AUTO2`
automatically registered HID and acknowledged 10/10 pings without a debugger
endpoint. The screen remained black. See
[warm-boot-stability.md](warm-boot-stability.md) for the preserved candidate,
runtime evidence, and remaining startup work. The v4 result below describes
the earlier rendered-RAM control.

Status (2026-09-05, NATIVE_HOME_UART_R2): native helper v4 is installed in the
migrated six-core Home image. Fixed UART full/empty ambiguity; 50 unpaced pings
all ACKed (median 17.89 ms, maximum 127.90 ms). A native tap opened Home Search,
and native Home down/up returned to the icon grid. No per-touch LLDB callback
was installed. This proves visible response, not uniformly fast application
launches or completed verification of the user's Cocoa clicks.

The user authorized a native iOS helper for this development image. Display
continues to use the existing snapshot and allocation/watchdog accommodations;
removing those display callbacks is deferred.

## Home input transport fix

`/tmp/dvm/NATIVE_HOME_R1.wire.jsonl` records a bounded read-only breakpoint
immediately after helper v4's `fgets`, executable `0x10000424c` plus slide
`0x22ec000`, reading the line at SP+0xa4. Sent:
`DVMINPUT1 1788615036986 S 0 0 0\n`; received: `DVMINP6 S 0 0 0\n`.
Exactly 16 bytes vanished. This occurred with a single registered helper,
so competing console readers do not explain this later failure.

`qemu-sptm/hw/char/exynos4210_uart.c` advertised all 16 free bytes, then
`fifo_store()` wrapped `sp` onto `rp`; `fifo_elements_number()` interpreted
equal cursors as empty. The fix retains a full bit, clears it on retrieval and
reset, and serializes it in an optional `exynos4210.uart.fifo/full` subsection.
The old v1 cursor/data layout is unchanged and old checkpoints default to
full=false. Full stores cannot overwrite unread bytes. A regression compiles
the actual device FIFO routines and verifies capacity, wraparound, exact byte
ordering and backpressure across sizes 1..256, including the 16-byte console.

`relay.py` now relies on UART backpressure: no default per-byte sleeps, padding
after records, or periodic blank-line nudges. `--ping --ping-count 50` exercises
the transport without HID. An ACK timeout still stops rather than resubmitting
an ambiguous touch. One newline at connection startup terminates a prior
writer's incomplete record.

Runtime evidence (all under `/tmp/dvm`):

- `checkpoints/NATIVE_HOME_UART_BEFORE1/manifest.json` captured the Home grid,
  registered v4 helper PID 211 and no active injected call/breakpoints.
- `checkpoints/NATIVE_HOME_UART_BEFORE1/restores/NATIVE_HOME_UART_R2/restore-report.json`:
  new binary loaded the old snapshot at exact PC `0xfffffff02aa654c8` in 1.419 s.
- `NATIVE_HOME_UART_R2.ping50.jsonl`: all 50 successful, unpaced, no retries.
- `NATIVE_HOME_UART_R2.input.jsonl`: Search tap at `(16300,28200)` submitted
  down/up in 2.41/5.43 ms; subsequent Settings-coordinate tap was sent while
  Search was opening, so it is **not** evidence of launching Settings.
- `NATIVE_HOME_UART_R2.before.png`, `settings.png` (actually Search),
  `home-later.png`: icon grid -> Search -> icon grid. Home packets in
  `NATIVE_HOME_UART_R2.home.jsonl` ACKed in 10.27/18.62 ms.
- Helper nice was restored from diagnostic -20 to 0 using native
  `setpriority`/`getpriority` at `0x237cd46e8`/`0x237ce3a20` plus shared-cache
  slide `0x6fa0000`; both returned zero. No scheduler-memory edits.
- `NATIVE_UART_V5_BASELINE.verdict.log`: `tools/probe.sh --secs 45`, default
  restore ramdisk, reached shell **yes**, XNU panics **0**.

Installed helper files are in `native-home-v4/` (checksum 3524758031, 53744
bytes); its build/install path did not repeat the expensive data-attribution
migration. The QEMU app copy with the UART fix is
`native-input-uart-v5/Darwin VM Touch Test.app`. Keep helper and QEMU version
labels distinct. The historical sections below describe earlier experiments.

At handoff, NATIVE_HOME_UART_R2 remains visible and running; its live relay
uses `NATIVE_HOME_UART_R2.input2.jsonl`. The preceding input log contains 18/18
successful native submissions (median 8.60 ms, maximum 37.43 ms), including a
12-state drag. These ACKs alone do not prove that the drag changed pages.
The next Settings tap, after restoring normal helper priority, had both ACKs
(15.44/35.53 ms), but no completed Settings launch was established. User
confirmation of Cocoa left-clicks is pending. No computer-use APIs were used.

To resume the saved Home image on another unique tag, use the BEFORE1 manifest
above with the v5 QEMU app executable, `--observe-seconds 0 --leave-paused`,
`--display cocoa,zoom-to-fit=on,show-cursor=on`, and that tag's
`DARWIN_TOUCH_EVENTS` path. Attach LLDB to its chosen GDB port and install
`home_display_callbacks.install(lldb.debugger, 0x6fa0000)` before continuing.
Run the relay against the new tag's events/UART paths. The checkpoint already
contains the helper; do not spawn a second copy. This saved snapshot predates
the priority reset and therefore retains nice -20, unlike the live R2 guest.

## Why move touch out of LLDB

The prototype in `tools/re/touch_bridge.py` records the completed gesture and
constructs a native Recap stream one function call at a time. Each debugger
stop pauses every virtual CPU. A shared CF observer also stops unrelated
processes. QEMU `accel/tcg/cpu-exec.c:check_for_breakpoints_slow` forces
single-instruction translation on a breakpoint's page, so callback hit counts
alone understate the cost of leaving such a breakpoint installed.

Read-only QMP samples (`tools/re/runtime_perf_sample.py`):

| Run/sample | Running fraction | Presentations / second |
|---|---:|---:|
| SETUP_TOUCH_R23.perf-broad.json | 78.2% | 0 |
| SETUP_TOUCH_R23.perf-narrow.json | 95.3% | 0 |
| DISPLAY_PERF_R24.no-touch.json | 99.0% | 14.33 |
| DISPLAY_PERF_R24.timer.json | 63.7% | 0 |

All JSON artifacts are under `/tmp/dvm`. These are different guest intervals,
not matched animation benchmarks. Presentations can contain identical pixels.
R23's narrow selector had no hits, so its measurement did not prove working
input. R24 proved that selector can fire and execute idle resets, but it still
causes frequent global stops. No end-to-end latency improvement is established.

R23's saved SpringBoard main stack was inside `CA::Render::Message::send_message`
at static `0x184404fb0` (the call uses MACH_SEND_MSG without a timeout).
R24 later had an ordinary main-run-loop receive wait. A single blocked stack
does not establish a persistent display-server deadlock.

## Proposed native path

QEMU's existing absolute-mouse capture file -> `tools/input/relay.py` -> the
existing UART -> `dvm-input` -> a persistent native Recap virtual HID service.
The relay preserves button edges, coalesces pending motion, and measures ACK
latency. The installed v1 helper ACKs event allocation plus a void native call;
this can falsely report success. Updated source checks HID service registration
and returns the underlying dispatch BOOL. Even that means submission, not that
an application processed the event. No Apple touch-controller MMIO is invented.

The helper uses runtime symbols and Objective-C selectors, not fixed addresses.
24A5430a native evidence:

- `+[RCPEventSenderProperties touchScreenDigitizerSenderForDisplayUUID:]`,
  `0x29b208234`, provides the same explicit main-display association used by
  the successful LLDB prototype.
- `-[RCPEventDeliveryServicePool deliveryServiceForSenderProperties:]`,
  `0x29b1fc1e8`, obtains and starts the service.
- `-[RCPVirtualHIDService postHIDEvent:]`, `0x29b1fff38`, dispatches through
  HIDVirtualEventService. The helper retains this service between packets.
- Device IOKit exports digitizer/finger constructors at `0x18f009520` and
  `0x18f0094ec`. Home uses native consumer-page usage `0x0c/0x40`, also
  observed in Recap `_createIOSButtonHIDEventWithButtonType:down:`.

The constructor declarations and display-integrated digitizer shape were also
cross-checked against the upstream
[vphoned HID implementation](https://github.com/Lakr233/vphone-cli/blob/main/scripts/vphoned/vphoned_hid.m).
Our path uses Recap's registered virtual service instead of copying its fixed
sender ID. Coordinate mapping and down/move/up delivery still need runtime proof.

## Build, install, and bootstrap evidence

`bash tools/input/build.sh` builds the arm64/iOS executable and adds its exact
CDHash to a derived trust cache. It does not replace the existing system cache.
`python3 tools/input/test_input.py` compiles a host validator that never loads
HID frameworks or submits host events, and checks framing/coalescing.

`prepare_ramdisk.sh` attaches only a new copy of the small restore ramdisk,
through `safe_attach.sh`. `install_in_guest.sh` installs into a writable child
of the migrated System disk inside iOS. No host attachment of the large iOS
System/Data container is needed. The restore image has no umount executable;
sync and stop the owned staging VM before using that child for normal boot.

NATIVE_INPUT_INSTALL1: installed checksum `3310005994`, size 53184;
`/libexec/dvm-input --validate` printed `DVM_INPUT_ACK 1 1` inside iOS.
Evidence: `/tmp/dvm/native-input/install.console.log` and
`/tmp/dvm/probe/NATIVE_INPUT_INSTALL1.serial.log`.

NATIVE_INPUT_BOOT1 uses `/tmp/dvm/native-input/disk.qcow2`, a child of
DISPLAY_SMP6_QEMU_SCANOUT8's migrated disk. It loads the new trust cache at a
fresh six-CPU boot; resolved shared-cache slide is `0x1bc94000`. Early boot
completed at guest 12.90 seconds. The expensive file-attribution migration was
not repeated. The helper was absent from the live process list, so a one-time
native spawn/bootstrap probe is being used. A RunningBoard-context spawn
returned EPERM; the exact denied operation is not yet established. An earlier
BackBoard-context spawn did not return before that process restarted.

`tools/input/bootstrap_helper.py` stages native mmap, file actions and
posix_spawn at a Mach-message return. It writes argument data only, preserves
raw general/SIMD registers and flags, and disables its breakpoint on completion.
Do not reinitialize it during an active call. If its process dies, establish
that fact before clearing its saved state; never restore another thread's
registers from an abandoned call.

Once actual input works, take a new immutable RAM/device/disk checkpoint, then
compare native ACK and visible UI response with the touch breakpoints disabled.

## Bootstrap connection resolved, UI response still open

NATIVE_INPUT_BOOT2 was a fresh six-core boot of a writable child of the migrated
native-input disk. Early boot completed at guest 9.795558 seconds; the long file
attribution phase did not repeat. Shared-cache slide is `0xa09c000`, kernel slide
`0x20000000`. A direct launchd spawn initially created helper PID 248, but its
`_bootstrap_port` was zero. Recap returned objects with a nil underlying
`HIDVirtualEventService.serviceClient`. IOHID bootstrap lookup returned
`MACH_SEND_INVALID_DEST` (`0x10000003`), explaining the false READY/ACK results.

Do not retry spawning from notifyd. Both tested spawn boundaries killed it;
the exit records establish namespace 25 (SANDBOX), code `0xdd`, flags `0x1042`.
`task_inspect_for_pid` returned -1 and launchd's `task_for_pid` returned 5 for
notifyd, so neither yielded a usable task/bootstrap right. No port-table writes
or copied cross-process port names were used.

The working startup path is implemented in `tools/input/bootstrap_helper.py`:

1. At launchd's verified EL0 `sendto` return, static `0x237cd6214`, call native
   `_kernelrpc_mach_ports_lookup3` (`0x237cfb494`) on its own task.
2. Check the first returned port with `mach_port_type`. In R4 this was `0x1e03`;
   the script discovers it dynamically. Never hardcode that name in a child.
3. Pass that send right to `posix_spawnattr_setspecialport_np` (`0x237cdb870`)
   with `TASK_BOOTSTRAP_PORT=4`, then native `posix_spawn`.
4. Destroy spawn objects, release the cloned rights, unmap argument storage,
   restore raw registers, and disable the breakpoint.

R4 returned success at every native setup stage and spawned PID 1093. Its own
bootstrap name was `0x807`. Its touch service `0x1047be270` owns HID object
`0x7def0641b0`, with non-null service client `0x7deec2d360`. Bounded probes then
observed `HIDVirtualEventService dispatchEvent:` return 1 at static
`0x2678a7220` and the events arriving in BackBoard at `0x22a352848`.

The obsolete helper ignored SIGTERM and competed for console bytes. A physical
read of the new helper's input buffer showed interleaved/missing characters.
After verifying the old process was still PID 248, native SIGKILL stopped it;
complete packets and ACKs resumed. Updated source adds a singleton file lock
and resets inherited SIGTERM/SIGINT dispositions and the thread signal mask.
Those safeguards require installing the new binary; they are not in v1.

With all touch probes disabled, a seven-update swipe completed with 131–186 ms
submission ACKs at 2 ms character pacing. A 12.106-second QMP sample found 112
running observations and zero new presentations. This is evidence of a working
packet/submission path, not responsive UI or an animation FPS measurement.

Native Recap also differs from the initial external reference: its synthetic
stream defaults to transducer type 2 (`0x29b1fcb74..84`), passes options `0x40`
to parent and child constructors (`0x29b213a50`, `0x29b213ca8`), and sets built-in
field 4 (`0x29b213b58`). Updated source follows these fields. A bounded live
constructor-argument probe reached BackBoard's touch/display handling but did
not establish visible response. All argument-changing probes were disabled
before checkpoint capture. Do not silently reinstate them as a runtime fix.

Evidence under `/tmp/dvm`:

- `NATIVE_INPUT_BOOT2/lldb.log` includes both BOOT2 and R4 debugger sessions.
- `NATIVE_INPUT_R4.exit-reasons2.txt` contains the notifyd sandbox exit records.
- `NATIVE_INPUT_R4.helper-root.json` records the registered helper's mapping and
  thread stacks; `NATIVE_INPUT_R4.native-clean.jsonl` contains the full swipe.
- `NATIVE_INPUT_R4.native-clean-perf.json` records the zero-presentation sample.
- `native-input-v3/dvm-input` and `native-input-v3/system.tc` are the rebuilt,
  signed candidate and merged trust cache. **Not installed in the running VM.**

## Resume without repeating the bootstrap probes

`/tmp/dvm/checkpoints/NATIVE_INPUT_REGISTERED1/manifest.json` preserves helper
PID 1093 with its registered HID service and no active injected calls. Capture
took 17.144 seconds including 7.639 seconds of migration. The first restore,
NATIVE_INPUT_R5, took 1.266 seconds and matched PC `0xfffffff02ac63b24` exactly.
After HMP `cont`, native Home packets were ACKed in 160 and 63 ms with LLDB
detached. The visible Cocoa VM remains a six-core development system with the
stale Welcome screen, not an accepted display-liveness checkpoint.

Restore to a unique tag, preserving the immutable disk/backing chain:

```sh
python3 tools/restore_checkpoint.py \
  /tmp/dvm/checkpoints/NATIVE_INPUT_REGISTERED1/manifest.json \
  --tag NATIVE_INPUT_NEXT --gdb-port 1511 \
  --qemu qemu-sptm/build-fast/qemu-system-aarch64 \
  --leave-paused --display cocoa,zoom-to-fit=on \
  --model-env DARWIN_TOUCH_EVENTS=/tmp/dvm/NATIVE_INPUT_NEXT.events.jsonl
python3 tools/hmp.py /tmp/dvm/NATIVE_INPUT_NEXT.restore.sock cont
python3 tools/input/relay.py \
  --events /tmp/dvm/NATIVE_INPUT_NEXT.events.jsonl \
  --uart /tmp/dvm/NATIVE_INPUT_NEXT.restore.uart.sock \
  --log /tmp/dvm/NATIVE_INPUT_NEXT.input.jsonl
```

Do not spawn another helper after this restore. For fresh boots, plist autoload
is still unresolved and `/bin/launchctl` is absent. The one-time launchd native
spawn above is established; fully autonomous cold startup is not.

Checkpoint fixes now follow `-serial chardev:<id>` instead of assuming
`probe_uart`, and restored launches write a recapturable `launch.json`.
Validation: 29 host tool tests, two native-input host tests, Python compilation,
and the signed iOS cross-build pass. The SDK linker still emits the existing
iOS-versus-macOS libSystem stub warning; runtime testing of v3 remains required.


## QEMU window input validation (2026-09-05, in progress)

`TOUCH_NATIVE3` cold-started the migrated native-input-v3 disk with six cores
and the nested D594 completion fix. The installed helper is 53,408 bytes,
`cksum` 3407591787, and launched once from launchd as PID 335.
`serial.log:24447` reports both native services registered. The image needs
that one-time launchd bootstrap; automatic launchd startup is not yet proven.

A Cocoa center click in its 221-by-480-point content area produced normalized
coordinates `(16258,16242)`, close to `(16384,16384)` with window/pixel rounding.
The initial click only captured the mouse and was lost. The Cocoa fix captures
an absolute-input press before forwarding it, and uses NSView's
`convertPoint:fromView:nil` to map window coordinates through the view's guest-
pixel bounds, including resize and fullscreen offsets. Runtime resized-window
and actual UI-action validation remains pending.

The first native swipe timed out because the **entire guest panicked**, not
because an ACK proved touch delivery. `serial.log:24533` names an SKS timeout;
CPU 3 spins at kernel static `0xfffffff00ac6a908`, with the other five CPUs
waiting at `0xfffffff00ac6ff90`. No input arrived in the helper's first fgets
buffer. The rejected endpoint-18 request was recovered read-only via host
LLDB `sep_dma`, and saved as `/tmp/dvm/TOUCH_NATIVE3/rejected-request.bin`,
SHA256 `5ff82948088905d0ae29bd4d2347e257cdcd059eb76f5e4f879990277d26bc81`.
It is the existing 164-byte variant-3 User-volume tagged record, with source
class 2 at +0x68, destination 1 at +0x6c, record length 28 at +0x84, and the
unchanged User tag at +0x90. Native class-transfer ABI evidence remains
`0xfffffff0095730a0..0x957311c`. The parser now accepts this exact class pair,
without relaxing framing/volume checks. The capture replay plus malformed
packet and truncation tests pass (24 SKS cases; 29 host cases). This still
requires an unmodified-model runtime success witness.

The dead TOUCH_NATIVE3 VM was terminated. Its migrated disk is preserved,
with helper v3 installed; it must not be treated as a healthy RAM snapshot.


## Visible host cursor / current resume point (2026-09-05)

The user paused computer-use testing, then requested a visible pointer over
QEMU. No computer-use actions were performed for this change. Cocoa already
supports `show-cursor=on`: `ui/cocoa.m` sets `cursor_hide=0`, so its hide/unhide
methods leave the macOS pointer visible when input is captured. This keeps the
standard contrasting black/white outlined pointer at host speed, independent
of guest frame or touch latency. `tools/input/boot.py` now defaults to
`cocoa,zoom-to-fit=on,show-cursor=on`; the checkpoint restore CLI accepts the
same combination. No QEMU rebuild was needed. All 29 host regressions pass.

`TOUCH_NATIVE4` was saved with no active native call and all guest debugger
breakpoints removed to `/tmp/dvm/checkpoints/TOUCH_NATIVE_CURSOR1/manifest.json`.
The checkpoint contains helper v3, nested display completions, and the
User-class 2-to-1 parser correction; it is an input debugging state, **not**
proof of working unlock or app launch. Its shared-cache slide is `0x17930000`.
`TOUCH_CURSOR_R1` restored the exact PC `0x19b9fb6ec` in 1.431 seconds with
the new display option. Current monitor and QMP are
`/tmp/dvm/TOUCH_CURSOR_R1.restore.sock` and `.restore.qmp.sock`; UART is
`/tmp/dvm/TOUCH_CURSOR_R1.restore.uart.sock`. Native relay reads the inherited
`/tmp/dvm/TOUCH_NATIVE4/events.jsonl` from its end, with outputs under the
checkpoint's `restores/TOUCH_CURSOR_R1/`. Display/UI callbacks remain attached
using the correct slide; no touch-injection callbacks are installed.

Before this cursor change, a real Cocoa swipe reached BackBoard at
`(589,2505)` and `(589,923)`, matching its normalized captured positions.
Native ACKs were 112–226 ms; SpringBoard window hit testing saw the expected
half-size UIKit coordinates. It did **not** visibly unlock. A later Home
packet timed out after reconnecting UART. Native helper startup also took
several minutes: the first helper ultimately registered both services, while
the second exited on the singleton lock. A priority-change diagnostic was
never applied (task lookup failed); its scratch mapping was freed and the
original caller restored. Do not claim the slow-start cause is established.
Further touch reliability/gesture work remains paused at the user's request.


## Native single-packet taps (2026-09-05)

The current autostart and console-reader fix is helper v5;
`WARM_DEV_NATIVE_HOME1` reached the native Home grid without debugger input.
App tapping still needs a separate delivery test. In
`SETTINGS_BITMAP_NATIVE3`, direct down/up records queued 150 ms apart without
waiting for the down ACK produced a Settings long-press menu. Down returned
after 2.501 seconds and up after 3.896 seconds. Sleeping on the helper worker
between the same synchronous `dispatchEvent:` calls cannot enforce tap timing.
The buffered R path (touchDown, stationary moves, liftUp) accepted ten records
but did not launch Settings. Neither result is a reproduced Settings crash.

Helper v6 adds `DVMINPUT1 <sequence> P 0 <x> <y>`. This uses the dedicated
Recap `taps:location:withNumberOfTouches:` method, static `0x29b20041c`,
with tap count 1, screen-space CGPoint, and touch count 1. The argument ABI
is x2=count, d0/d1=point, x3=touches. It then finalizes the processing buffer,
sets events, and calls `+[RCPInlinePlayer playEventStream:options:completion:]`.
The prior working trace is `/tmp/dvm/TOUCH_INPUT_R21.lldb.log:43–55`;
it shows delivery after releasing the stream and options, supporting their
retention by asynchronous playback. P ACK means queued playback, not a
completed UI action. `relay.py --tap` now emits one P packet and requires v6.
`--backend recap --tap` retains the older R diagnostic, while live mouse
records still use direct T events.

Build/staging artifacts: `/tmp/dvm/warm-input-v6-native-tap/`; helper CDHash
`023cff3b44925a3dc16882553f7717f5b3b144b9`.
`INPUT_V6_INSTALL1` installed it in a new child of the native-Home disk;
its restore-guest validator and final `DVM_INPUT_INSTALL_DONE` marker passed.
The input-v6 disk boot and saved-state continuation reached native Home.
`INPUT_V6_TAP1` then used one P packet to show Settings’ launch surface; a
separate `SETTINGS_V6_TRACE1` replay caught the fresh Preferences bitmap trap.
Thus P now supplies a reproducible application-launch trigger; it does not
make Settings stable.

## Buffered lock-screen swipe follow-up (2026-10-09)

P112's native v6 direct `T` gesture was acknowledged and reached BackBoard/SpringBoard but left the sampled screen on lock chrome. It serializes 21 motion samples, so the next lock-screen test uses `relay.py --backend recap --swipe ...` to buffer one ordinary gesture and play it through native Recap with short move durations. The ACK remains a submission marker; inspect a fresh post-gesture surface before deciding whether another action is appropriate. A passcode prompt requires user input and is not automated.

#### PLUGIN119 legacy helper startup bound (2026-10-09)

P119 started the legacy v6 `/usr/local/libexec/dvm-input` through the boot-time launchd bootstrap (PID 20), but it stayed at `DVM_INPUT_INITIALIZING` for a 40-minute run and never emitted `DVM_INPUT_READY` or an error. The host trigger required that readiness marker plus the post-shell observer and a non-null SpringBoard home entity; it sent no input. This is a helper readiness miss, not a touch result. The next attempt uses the sealed DVMI2 v12 helper child and QEMU's `DARWIN_INPUT_UART` status/ACK path; no legacy `DVMINPUT1` relay or helper bootstrap is used in that run.

#### PLUGIN120 trust-cache mismatch and correction (2026-10-09)

P120's v12 helper did not launch because the boot used the dev6 `system.tc` without the helper CDHash. Guest serial evidence shows repeated AMFI rejection of the ad-hoc signed `/usr/local/libexec/dvm-input`; QEMU status remained without a helper PID or `R` ready state, ACK counters stayed zero, and the host trigger sent no packets. This is not a swipe result.

The installed helper at `/Users/steve/dvm-artifacts/bp-patched-dev6/input-v12-native/dvm-input` has CDHash `5135a276753e8b656e05651c91c20f4927a14c1e`, present in its one-entry `helper.tc`. Merging that cache into `/Users/steve/dvm-artifacts/bp-patched-dev6/system.tc` produced `/Users/steve/dvm-artifacts/bp-patched-dev6/input-v12-native/system-plus-dev6-input.tc`, SHA-256 `d60beb27dd6fc3726f205164842de245b9f4f6a2fa5e8bc40a95351a52fb7266` (3,939 entries). The next boot should use this merged cache and must observe DVMI2 state `R` before sending the single guarded upward swipe. A successful ACK still proves submission only; classify the screen from the resulting IOSurface. If a passcode keypad appears, stop and let the user enter it.

#### PLUGIN128: Home and upward swipe delivered through DVMI2 to SpringBoard

P128 used a fresh durable child of P127 and the merged, helper-trusted DVMI2 `system.tc`. The helper reached ready state. After readiness, the host sent one Home key and one ordinary upward swipe. Both commands were acknowledged without dispatch errors. The swipe reached SpringBoard `UI_EVENT` and `_UISystemGestureWindow`; hit-test returned `UISystemGestureView`, followed by `SPRINGBOARD_HID_EVENT`. Home input also preceded additional workspace and non-null home-entity callbacks. This establishes the guest input and UIKit system-gesture path, not that the system recognized an unlock.

The pre/post Home, pre/post swipe, and final HMP frames remained the same uniform black 1179×2556 image (SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The compact P128 observer had no IOSurface sampler, and its status recorded zero DVMI2 presents/input-to-present samples. So P128 has no post-input compositor-pixel verdict. The next controlled run combines the validated SpringBoard input markers with the read-only `fb_swap_set_layer` IOSurface capture, enabled only after the swipe is ACKed. No passcode or text was entered. The durable child and complete evidence bundle are `ui-class16-refresh60-dm-lock-plugin128-min-home-unlock1.qcow2` and `recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN128_MIN_HOME_UNLOCK1/` under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/`; `qemu-img check -U` passed.

#### PLUGIN130 / PLUGIN131: Home-entity gate needed a longer wait

P130 is not an input result: it used the render-only module and input-bootstrap profile by mistake, then was cancelled before sending input. P131 corrected the profile and acknowledged the Home key through the ready DVMI2 helper, with no ACK/dispatch failures. No non-null home entity arrived within the helper's 180-second deadline, so it withheld the swipe. However, the log later recorded `SB_HOME_ENTITY_INIT_ENTRY_RETURN` with non-null `x0` at `1791614769.679`, about 343 seconds after the Home command and before the run's observation ended. The previous helper cached the timeout result and did not re-evaluate it; the swipe stayed unattempted. P131's HMP image remained the uniform-black hash above; no post-input IOSurface was captured because the capture gate opens only for an ACKed gesture. The trigger now supports an explicit lock-screen fallback, but P132 uses the better-evidenced policy: wait up to 600 seconds for the delayed home entity and swipe only after it appears. No passcode or text was sent.

P131's durable child and records are `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin131-home-unlock-render1.qcow2` and `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN131_HOME_UNLOCK_RENDER1/`; SHA-256 `b5ba0911db77dd10a2dd4c1adedc356b7bb82488238ffb1a0611bd1cb7d6ad17`, `qemu-img check -U` clean.

#### PLUGIN132: delayed Home entity and acknowledged swipe

P132 used a fresh durable P128 child, the accepted merged DVMI2 trust cache, `INPUT_BOOTSTRAP=0`, and the combined gated-render/input observer. It ACKed the Home key, then waited about 333 seconds for a non-null SpringBoard home entity before sending one ordinary upward swipe. The swipe was acknowledged and dispatched without errors; SpringBoard recorded `UI_EVENT`, `_UISystemGestureWindow` hit testing returned `UISystemGestureView`, and `SPRINGBOARD_HID_EVENT` ran. This is end-to-end input delivery to SpringBoard's system-gesture view, not proof that SpringBoard accepted or completed unlock.

The pre/post swipe HMP frame hashes were identical and uniformly black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The gated IOSurface sampler wrote no event in the 300-second observation; CA commits occurred, while the armed backboardd render/swap sites did not log hits. P132 therefore leaves lock-gesture recognition and CA-to-display handoff unresolved. No passcode was entered. The durable child is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin132-lockscreen-swipe1.qcow2` (SHA-256 `610d0298e8abd5d3ca6ad9c51fc7ad80e1775df422954a266dcce71d698a037e`), and `qemu-img check -U` passed. Evidence is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN132_LOCKSCREEN_SWIPE1/`.

#### PLUGIN133: native swipe reached SpringBoard pan recognizers

P133 is a fresh child of P132 with the accepted DVMI2 trust cache and `INPUT_BOOTSTRAP=0`. The Home command was ACKed. SpringBoard's non-null home entity returned about 319 seconds after shell-service return; only then did the helper send one upward swipe. The helper reports `home-and-swipe-acknowledged`, `home_entity_after_key=true`, `swipe.ok=true`, no dispatch failures, and unchanged frame hash `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`.

SpringBoard's `UI_EVENT` hit test returned `UISystemGestureView`. Both `SBFluidSwitcherScreenEdgePanGestureRecognizer` and `SBCoverSheetScreenEdgePanGestureRecognizer` received `touchesBegan`; the latter received `touchesMoved`. No recognizer `shouldBegin` or home-gesture action was observed. A lock-screen-manager transition callback ran with `forUserGesture=0` and zero velocity, so it is not evidence that the swipe was accepted. HMP remained all black and no post-home IOSurface/render/swap sample was recorded. No passcode was sent.

P133's durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin133-lock-gesture1.qcow2` (SHA-256 `5415b4d4950aa79c9e7b0febada0f84858f1eb28111b0f77d841dd1e89db1ce9`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN133_LOCK_GESTURE1/`. The follow-up adds read-only UIKit recognizer `setState:`/`_shouldBegin` hooks for the observed classes. No passcode or model/fixture/SpringBoard/A408/scanout change was made; D572/D582 remain hypotheses.

P134's `UIGestureRecognizer setState:` hook recorded three `x2=5` transitions, which the SDK defines as `UIGestureRecognizerStateFailed`: two from `SBCoverSheetScreenEdgePanGestureRecognizer`, one from `SBFluidSwitcherScreenEdgePanGestureRecognizer`. The input helper acknowledged and dispatched the swipe, and SpringBoard home-entity initialization was present. No UIKit `_shouldBegin` or SpringBoard gesture-acceptance/action callback ran. The touch reached SpringBoard, but UIKit failed both observed screen-edge recognizers before they began; the cause is still unknown. The framebuffer remained uniformly black. Next capture adds the Fluid subclass's state-setter caller and the `SBScreenEdgePanGestureRecognizer isLocationWithinGrabberActiveZone` result, with a follow-up swipe starting nearer the physical edge.

P134 child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin134-gesture-state1.qcow2` (SHA-256 `6dabcef60e9762d0385d3e428174972f5a4e1dacdacd2999b337d744ff60e7bd`) passed `qemu-img check -U`; records are in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN134_GESTURE_STATE1/`. No passcode or model/fixture/SpringBoard/CLASS16/A408/scanout change was made; D572/D582 remain hypotheses.


#### PLUGIN141 bottom-edge retry

The initial P141 swipe began about 53 px above the active bottom edge and failed the edge recognizers. A second buffered DVMI2 swipe on the same boot started at `(589,2540)` and moved to `(589,500)` over 600 ms with 32 steps. The cover-sheet recognizer and system-gesture delegate returned true from `shouldBegin`; dismissal began, changed, and ended; the lock-screen transition recorded `forUserGesture=1`. SpringBoard then returned a non-null home entity. Input was delivered and the lock dismissal path ran, but HMP stayed uniformly black and the post-shell observer saw no backboardd render/swap or IOSurface event. This separates gesture acceptance from the still-missing visible-frame path. No passcode was entered.

The durable child and complete evidence bundle are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/` as `ui-class16-refresh60-dm-lock-plugin141-gesture-center600ms1.qcow2` and `recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN141_GESTURE_CENTER600MS1/`. The event summary `events/bottom-center-swipe.json` records the accepted sequence and black-frame hash.


#### PLUGIN142: input delivered; no visible frame or compositor handoff

P142 completed the home key and one ordinary bottom-center swipe after its home-entity gate, then a second pause-aware bottom-center retry. The trigger swipe sent 34 records, received 34 ACKs, and dispatched 32 with no dispatch failures; the manual retry dispatched 31 with no failures. No user-gesture recognizer acceptance was recorded on this boot. The before/after HMP frame hashes remained the uniform-black `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`; the helper recorded zero presents and zero input-to-present samples.

This boot's read-only QuartzCore trace recorded SpringBoard CA sends and backboardd receives for the `0x9c44`/`0x9c45` family, while all post-home renderer/display/swap/IOSurface hooks stayed quiet. The receive sample was bounded at 32 and does not join task-local port names across processes. The durable record is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN142_CA_HANDOFF1/`; the durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin142-ca-handoff1.qcow2` passed `qemu-img check -U`. No passcode was entered. Next is a scheduler and `render_for_time` trace, not another swipe-only run.

#### PLUGIN147: Home entity and two swipes, no visible frame

P147's trusted DVMI2 helper ACKed Home, then SpringBoard returned a non-null home entity. The first upward swipe `(589,2540)→(589,500)` sent 34 records, ACKed all 34, and dispatched 32 with no errors. A second bottom-edge retry dispatched 22 of 23 records without errors. The captured lock-screen transition had `forUserGesture=0`; dismissal was not confirmed. HMP stayed uniformly black before and after the input (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`), and the 900-second post-input window had no IOSurface event or DVMI2 present/input-to-present sample. No passcode was entered. Durable records are `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN147_RENDER_GATE1/`; the child passed `qemu-img check -U`.

#### PLUGIN161: ACKed Home and swipes without Home or gesture evidence

P161 used the merged DVMI2 trust cache and acknowledged Home plus two bottom-edge swipes. The trigger did not see a non-null Home entity within its 180-second gate, so it exercised the lock-screen fallback. The first swipe and follow-up were delivered without helper dispatch errors, but both before/after frame hashes were the same uniform black image (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). The optional render trace caused LLDB to detach before later input, so those later events do not prove UIKit/SpringBoard gesture handling. This is not an unlock result. The observer-light P162 run restores a Home entity, but its selected module omits lock-gesture recognizer hooks; the next run must set `POST_SHELL_UI_MODULE=dm_post_shell_home_unlock_render_callbacks`.

P161's durable child and records are `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin161-unlock-double1.qcow2` and `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN161_UNLOCK_DOUBLE1/`; `qemu-img check -U` passed.


#### PLUGIN167: swipe attempt interrupted by host ACK timeout

P167 ACKed the Home key and eventually observed SpringBoard's non-null Home entity, then began a 600 ms, 32-step bottom-center swipe. During serialized dispatch, the input helper left `R`; final status showed six dispatched operations, 21 pending wire bytes, one not-ready/overflow drop, and `guest_state=L`. The serial log records an input record unacknowledged for 10 guest seconds. There is no `DVMI2R L` or `DVM_HID_RECOVER` line, so the host-side L state is attributable to its ACK timeout; this run is not a SpringBoard gesture result. The frame remained uniform black. The trigger JSON's previous `home-acknowledged-home-entity-not-seen` label was inaccurate because the entity arrived during the 600-second gate; the result classifier now reports `home-entity-ready-swipe-incomplete` for this case. P168 removes the per-frame scheduler observer while preserving the input and gesture/present coverage to avoid repeating this instrumentation-heavy window.

#### PLUGIN168 / PLUGIN169: corrected helper trust cache; P164-lineage bottom swipe rejected

P168 is an input-setup miss: the launcher used `system.tc`, which lacks the installed DVMI2 helper CDHash, so the helper never became ready and the gated trigger sent nothing. P169 used the merged `system-plus-dev6-input.tc` on a fresh P164 child. The helper reached `R`; Home was ACKed and the non-null SpringBoard Home entity was observed. Its first swipe started too high. The exact P141 bottom-center retry `(589,2540)→(589,500)` (600 ms, 32 steps) then sent and ACKed all 38 records, dispatched 33 without errors, and had no coalescing. Despite correct input transport and geometry, the observed Cover Sheet recognizer entered Failed state (5), and no user-gesture dismissal transition followed. Thus this P164-lineage run did not unlock; P141 remains the separate successful-gesture reference on a P134 child.

The trigger completed before an extra manual attempt was made. That manual command did not finish cleanly: the saved final status has `contact_sent=true`, `btn_down=true`, and a non-neutral Y coordinate. Do not interpret it as another complete swipe. The Power command used before the manual attempt was ACKed but did not change the displayed output. P169's child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin169-unlock-present-twoswipe1.qcow2` (SHA-256 `31aee86d2153c82cf9ecffdeaa02c1a4259477c15c8759635deb44260d7ec397b`) and evidence under `recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN169_UNLOCK_PRESENT_TWOSWIPE1/` are durable; `qemu-img check -U` passed. No passcode or text was sent.

#### PLUGIN170: accepted late swipe; framebuffer unchanged

On a fresh P134 child, the Home key was ACKed and a non-null SpringBoard Home entity was observed. The first swipe and early retry did not dismiss the lock screen. The later bottom-edge swipe `(589,2540)→(589,500)` (600 ms, 32 steps) was fully ACKed and dispatched without errors; both edge recognizers allowed it, dismissal began/changed/ended, and the lock transition had `forUserGesture=1`. The helper ended with `contact_sent=false` and `btn_down=false`. An additional Home key was acknowledged after the accepted transition.

The HMP frame was unchanged uniform black before/after the swipe and after the Home key (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). A backboardd IOSurface captured just before transition showed sparse lock chrome on black; no post-transition render/display/OGL chain or non-null surface swap followed. Input and gesture acceptance are proven; a visible Home/unlock is not. No passcode or text was sent.

Durable child `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin170-p134-unlock-present1.qcow2` (SHA-256 `b152eba6a4c5641a8f4d498be99d680828d6a2d9eeedb1de30c5583add9198aa`) passed `qemu-img check -U`; the recovery bundle is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN170_P134_UNLOCK_PRESENT1/`. P171 repeats the late gesture with QuartzCore scheduler tracing enabled.

#### PLUGIN171: released contact and fully delivered bottom-edge retry

P171's first upward swipe overlapped a debugger pause; the input helper/LLDB sequence lost coverage. The guest was resumed and the matching mouse-up was sent at the last absolute coordinates. A later bottom-edge swipe `(589,2540)→(589,500)` completed: all 37 transport records were ACKed, 33 operations reached HID dispatch, and dispatch failures/coalescing were zero. The helper ended ready with no contact/button held. Its before/after HMP frame hashes were identical uniform black. This proves delivery only; the gesture occurred after LLDB disconnect, so there is no SpringBoard gesture or post-gesture render verdict. Evidence: `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN171_P134_RENDER_DECISION1/events/followup-bottom-swipe.json` and `input-status-final.json`.


P172's Home and swipe commands were acknowledged. SpringBoard emitted `LOCKSCREEN_GESTURE_TRANSITION` with `x3=1` for a user gesture, confirming accepted dismissal input. HMP remained uniformly black, so this is not an unlock/Home result. P173 delays its initial swipe until 180 seconds after the Home-entity gate and gates compositor probes on the accepted-transition marker. Durable P172 evidence is in `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN172_P134_UPDATE_GATE_MIN1/`.

#### P188: acknowledged Home key and accepted swipe, unchanged black frame

The native helper was ready and remained responsive. Its shell-gated Home key completed with three records acknowledged and no errors; SpringBoard later exposed a non-null Home entity. The bottom-edge swipe `(589,2540)→(589,500)` sent and acknowledged all 38 records; 33 reached HID dispatch with zero dispatch failure, coalescing, or not-ready drops. SpringBoard's Cover Sheet recognizer/delegate accepted the gesture and completed began/ended, followed by `LOCKSCREEN_GESTURE_TRANSITION` `x3=1` at `1791694519.803`. This is positive input/gesture evidence.

The helper's later ordinary Home-key command also completed (three records acknowledged, no timeout), but the before/after HMP frame hashes were identical uniform black (`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`). Therefore the input path worked, while the visible Home result did not. No passcode was entered. Durable evidence is under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN188_P182_SWIPE_SEQUENCE_PRESENT1/`.


#### P190 — interrupted input was released; no gesture was scored

P190's automated trigger sent Home but was manually stopped before its 900-second Home-entity wait expired. Two separate manual upward swipes encountered `VM not running` and are incomplete. QMP mouse-up cleanup left the input helper neutral: `contact_sent=false`, `btn_down=false`, `queue_len=0`, `inflight=0`, and `wire_pending=0`. No SpringBoard accepted-gesture marker followed. These are cleanup/coverage records only; P191 leaves the trigger and debugger attached and enables its built-in timeout swipe.


#### P191 — no input sent because the guest input helper never became ready

P191's slide resolver failed before LLDB or the post-shell input observer could attach. The input helper remained in state `L`, with no ACKs and no dispatched records; the trigger did not reach readiness and sent neither Home nor swipe. This is input setup coverage only, with no gesture verdict. Its helper-status and boot logs are preserved in the P191 recovery bundle.


#### P192 — Home and swipe transport succeeded; framebuffer did not change

P192 reached the shell-service-return gate and SpringBoard later created a non-null Home entity. The Home-key command after that entity, the initial bottom-edge swipe, a Home key after an accepted transition, and the follow-up bottom-edge swipe all returned `ok=true`; swipe dispatch counts were 33 each, with zero ACK failures, dispatch failures, or coalescing. The follow-up swipe reached the SpringBoard acceptance path (`HOME_GESTURE_PAN_SHOULD_BEGIN_RETURN=true`, Cover Sheet dismissal began, and `LOCKSCREEN_GESTURE_TRANSITION x3=1`, `for_user_gesture=1`). The helper's auxiliary sequence confirmation was false, so that field is not used as the acceptance verdict. The helper ended with no contact/button held and no guest restart. All available before/after and final frame hashes remained the same uniform black `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`; therefore input delivery and gesture acceptance worked, but no visible Home/unlocked screen resulted. No passcode or text was sent. Full trigger evidence is in the P192 recovery bundle.

#### P195 — transport succeeded; user dismissal was not accepted

P195 reached shell-service return and a non-null SpringBoard Home entity. The
Home key and a full bottom-edge swipe were acknowledged; the swipe had 37/37
ACKs and 33 HID dispatches with no dispatch failures or coalescing. The
observed lock transition had `x3=0`, so this run does not prove a user-accepted
dismissal. HMP before and after stayed at the uniform-black frame hash
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. Later
input attempts were interrupted by the runner/debugger and ended with a held
contact; those attempts are incomplete and must not be counted. No passcode or
text was sent. The durable input events and final helper status are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN195_P182_CA_MODE_SCHEDULER1/`.

#### P227 — accepted Recap dismissal with full post-transition capture

P227 used the corrected legacy input sequence: Home was acknowledged, the
Home entity appeared, and a normalized bottom-to-upper Recap swipe was
acknowledged and accepted by SpringBoard as a Cover Sheet dismissal
(`for_user_gesture=1`, confirmed pan sequence). The helper stayed attached
for 240 seconds after that transition and captured the final HMP frame before
stopping the runner. The final frame remained uniformly black; this is a
valid input/state-transition result, not a visible Home or passcode-unlock
result. The accepted-transition JSON, complete relay log, and final frame
are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN227_POSTHOME_MODETRACE1/`.

#### P253 — shell/check-in clears and Home control state appears; legacy unlock input is incomplete

P253 recorded migration check-in, BKS check-in, and shell-service returns with
`x0=0` at `1791779620.854`, `1791779620.864`, and `1791779620.867`. SpringBoard
then returned a non-null Home entity at `1791779857.602`, ADFL returned
non-null at `1791779897.421`, and the workspace transition returned success
(`x0=1`) at `1791780125.472`. This is measured shell-to-Home control state;
it is not a visible Home-frame result.

P253's pre-swipe lock transition had `x3=0`. The subsequent legacy recap swipe
never completed its relay touch-up, and the fallback release timed out. No
accepted dismissal is scored. The HMP frame was uniformly black. This run
therefore leaves unlock input coverage open rather than demonstrating gesture
rejection or visible unlock. The durable record is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN253_REPLAY2B_SURFACE_LINEAGE1/`.

#### P256 — Home exists; debug loss interrupted the swipe before acceptance

P256 reached `SHELL_SERVICE_RETURN`, then SpringBoard produced a non-null Home
entity at guest time `1791781106.35857`. The Home-entity screendump and
periodic HMP samples stayed uniformly black. The trigger began an upward
swipe; the helper status at the debug stop had 30 dispatched records and 203
ACKs with no dispatch failures, but the contact was still down, one record
was in flight, and 14 were wire-pending. The saved SpringBoard gesture log
contains no accepted `shouldBegin`, Cover Sheet began/ended sequence, or
`LOCKSCREEN_GESTURE_TRANSITION`. LLDB lost its guest connection while QEMU was
paused; the run was then stopped to avoid carrying a stuck virtual touch into
another experiment. This is incomplete input coverage, not evidence that
SpringBoard rejected a complete swipe. No passcode or text was sent.

The durable bundle is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN256_P134_DVMI2_UNLOCK_SURFACE1/`;
the paused child copy and its before/after hash are retained under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/`.

#### P258 — shell and Home control state; QMP swipes did not dismiss the lock screen

P258 used a fresh child of the P134 gesture-state parent with the merged input
trust cache. `SHELL_SERVICE_RETURN` was recorded at `1791782142.888`. The Home
key was acknowledged, but the trigger's configured 10-second wait did not yet
see a Home entity; it was explicitly allowed to test a swipe anyway. The
frame sampler later recorded the non-null Home entity at `1791782375.359`.
Thus the first swipe preceded that entity. The later follow-up swipe occurred
after the entity marker.

The first QMP swipe sent/ACKed eight transport records, dispatched six, and
coalesced 27; it is not a full gesture sample. The later bottom-edge QMP swipe
`(589,2540)→(589,500)` used per-step HID dispatch: 37/37 records were
acknowledged, 33 operations dispatched, with zero dispatch failure or
coalescing. SpringBoard saw the `SBCoverSheetScreenEdgePanGestureRecognizer`
touches, but its recognizer entered Failed state (`x2=5`); there was no
`shouldBegin` result, screen-edge-grabber-zone event, Cover Sheet dismissal
begin/end, or accepted user-transition marker. HMP stayed uniformly black
(`d312521c…42b9`). This is a delivered but unaccepted later swipe on this
P134 child; it does not contradict P250's accepted Recap-protocol swipe on a
replay2b child. P258's parent had no legacy `DVM_INPUT_READY protocol=1`, so
the attempted DVMINPUT1 relay was a protocol/setup miss and is not gesture
evidence.

The next controlled attempt should use the replay2b parent and the guarded
legacy trigger, which waits for Home entity and ADFL return before sending the
same Recap swipe; arm the staged present callbacks in that run. The durable
child SHA-256 is
`773bdfe94ef3e43645b96ee3d86dfe274d96120b9e8ae40adebe62587510ca30`; its
`qemu-img check` passed. Logs, input status, trigger result, frames, and
backing-chain record are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN258_P134_STAGED_GESTURE_PRESENT1/`.
No passcode or text was sent.

#### P261 — shell/Home lifecycle works; Recap touch submission timed out

P261 used the same durable replay2b parent as P250, the merged input trust
cache, protocol-1 input service, and staged SpringBoard/UI probes. On this
boot, `DM_PERFORM_RETURN` (`x0=0`) was followed by BKS migration/check-in
returns and `SHELL_SERVICE_RETURN` (`x0=0`) at guest time `1791785700.043`.
The Home key's down and up were both ACKed. SpringBoard created a non-null
`SB_HOME_ENTITY_READY` at `1791785804.295`, and its measured ADFL call returned
at `1791785822.660`. The HMP frame at shell return was uniformly black
(`d312521c…42b9`); this proves lifecycle progress, not visible pixels.

The first Recap swipe packet (DVMINPUT1 kind `R`, down at `(16371,32563)`,
sequence `1791785855747`) did not receive an ACK within 60 seconds. The
matching lift/release (sequence `1791785915857`) also timed out. SpringBoard
later emitted a lock-manager transition with `for_user_gesture=1`, but
`gesture_sequence_confirmed=false`, reason
`no-recent-accepted-cover-sheet-pan-sequence`; there was no accepted dismissal.
This is input submission failure before gesture classification, not evidence
that the lock-screen recognizer rejected a complete swipe. The trigger's
readiness marker match was fixed to include
`POST_SHELL_HOME_GESTURE_STAGED_READY`.

The run was frozen after the bounded release ACK failed. The durable child
passed `qemu-img check -U` and has SHA-256
`5b20966d25626588c6bd4ca8313f50c0fc99e3a1cbbb723c0d67096aceb2f293`; records
are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN261_REPLAY2B_LEGACY_STAGED_PRESENT1/`.
P250 remains the accepted Recap-swipe control. P262 changes only the swipe
backend to direct touch while retaining the replay2b lineage and the same
Home/ADFL gates. No passcode or text was sent.

#### P262 — direct-touch gesture reached SpringBoard; classifier window was too short

P262 is a fresh replay2b child using the same TC, launch arguments, and shell
→ Home/ADFL gates as P261; only the swipe backend changed to direct touch. The
shell/check-in ladder returned successfully, Home down/up were ACKed, and
SpringBoard created its Home entity and returned from ADFL. As in P261, HMP
captures at shell return, Home-entity return, and 120 seconds after shell
return were uniformly black (`d312521c…42b9`).

All 11 direct-touch swipe records, including the lift, were ACKed. SpringBoard
then logged a `SBCoverSheetScreenEdgePanGestureRecognizer` with
`HOME_GESTURE_PAN_SHOULD_BEGIN_RETURN x0=1`, the same recognizer entering
`COVERSHEET_DISMISS_GESTURE_BEGAN`, and `HOME_GESTURE_PAN_TOUCHES_ENDED`. The
lock-manager transition followed with `for_user_gesture=1`. The initial
classifier marked the sequence unconfirmed because its should-begin/began to
transition limit was 60 seconds; on this TCG/LLDB run, the matching events
were 79.97/79.92 seconds before transition, while the matching pan end was
only 1.61 seconds before it. Offline replay with the widened 180-second
same-process/TPIDR and recognizer-identity correlation confirms the event
sequence. This is a probe-window correction based on the captured identities
and ordering, not evidence of rendered pixels.

The trigger had already stopped P262 on the old false-negative marker, before
its post-dismissal Home event and render capture. Therefore P262 establishes
that direct touch reaches an accepted should-begin and Cover Sheet dismissal
gesture path, but it does not establish the resulting Home/lock surface or
visible unlock. The durable child passed `qemu-img check -U`; SHA-256 is
`760b066cb23d72a2f73354ac36622c851232f4508e1332c29c043e8286250939`. Event,
frame, serial, LLDB, runner, and input-relay records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN262_REPLAY2B_LEGACY_DIRECT_STAGED_PRESENT1/`.
P263 repeats this same direct-touch path with the corrected correlation window
and keeps observation running through the post-transition capture. No
passcode or text was sent.

#### P264 transport check — legacy guest helper and DVMI2 host traffic

P264's boot started the guest `dvm-input` v6 service, which advertises
`DVM_INPUT_READY protocol=1` and parses `DVMINPUT1` records. With
`DARWIN_INPUT_UART=1`, the QEMU side instead emitted DVMI2 `P` ping records;
the guest wrote `DVM_INPUT_REJECT malformed-or-old` once for each ping (47
rejects and 47 host pings at the sampled point). The legacy trigger had not
sent a Home or gesture yet, so those rejects are transport-version evidence,
not failed user input. They also explain why QEMU's input-status counters
never reached an acknowledged/ready state in this boot.

The render runner now has an opt-in `LEGACY_UART_ONLY=1` mode, valid only with
`INPUT_TRIGGER_MODE=legacy`. It keeps the serial socket for the direct
`DVMINPUT1` relay but sets `DARWIN_INPUT_UART=0`, preventing the QEMU-side
DVMI2 producer from injecting incompatible traffic into that same guest UART.
This is a measured protocol correction in the launch path, not yet
boot-validated; use it on a fresh child and require actual `DVM_INPUT_ACK`
records plus SpringBoard recognizer evidence before scoring a gesture. It does
not change framebuffer, firmware, DCP responses, mode, or pixels.

P264 continued this check on a fresh replay2b child, but did not validate the
new launch mode or a user gesture. The run used `LEGACY_UART_ONLY=0` and
`DARWIN_INPUT_UART=1`; its automated trigger exited before the first relay
because it passed `--ack-timeout 180` to the then-installed relay, whose
accepted maximum was 120 seconds. The guest helper advertised
`DVM_INPUT_READY protocol=1`, while QEMU's DVMI2 pings were rejected by the
legacy parser. A separate manual Recap relay wrote to the same UART during the
run, so those touch ACKs cannot be attributed to one serialized gesture. No
accepted lock-dismissal marker was produced. P264's input result is therefore
unscored; ACKs alone would only show helper submission.

The runner now preserves artifacts after a nonzero probe exit. Legacy trigger
mode now defaults `LEGACY_UART_ONLY=1`, which disables the DVMI2 producer while
retaining the UART for the legacy relay; an explicit override remains
possible. The trigger also validates the relay's current 300-second ACK limit
before running. The isolated P269 boot is validating this launch path with one
automated input writer and a 120-second per-packet ACK deadline.

#### P263 — direct-touch packets ACK, but SpringBoard fails the Cover Sheet pans

P263 used a fresh child of the same durable replay2b parent, the merged input
trust cache, the same shell/Home/ADFL gates, and direct touch with the updated
gesture correlator. `DM_PERFORM_RETURN`, BKS migration/check-in returns, and
`SHELL_SERVICE_RETURN` all completed with `x0=0` on the recorded success sites.
The Home key was ACKed; SpringBoard created the Home entity and returned from
ADFL. The shell-return and Home-entity HMP frames were uniformly black
(`d312521c…42b9`).

All 11 direct-touch points, including the final lift, were ACKed. In contrast
to P262, SpringBoard moved the `SBCoverSheetScreenEdgePanGestureRecognizer`
and `SBFluidSwitcherScreenEdgePanGestureRecognizer` to Failed (`x2=5`) almost
immediately after touches began. No Cover Sheet/Home `shouldBegin` return,
dismissal `Began`, or user-gesture lock transition followed. The trigger waited
the configured 180-second acceptance window and stopped without issuing the
post-dismissal Home event. This is a fully delivered but rejected recognizer
path; it is not a transport miss. It also does not negate P262's accepted
same-lineage pan, and it provides no post-dismissal pixel result.

The durable replay2b child passed `qemu-img check -U`; SHA-256 is
`2f03ae484763aa00111a11832ae2521701242bbe0af9ac27f427f86310fd17bd`. Its
event, frame, serial, LLDB, runner, and relay records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN263_REPLAY2B_LEGACY_DIRECT_STAGED_PRESENT2/`.
P264 should retain the 180-second same-gesture correlator and compare the
known-positive Recap input protocol with an extended relay ACK window. No
passcode or text was sent.

#### P269 — Home lifecycle is positive; the trigger never reached input

P269 used the durable replay2b parent and legacy UART-only transport. The
same boot completed the DataMigrator/BKS shell ladder, created a nonnull
SpringBoard Home entity, and returned from ADFL. Its post-shell module emitted
`POST_SHELL_HOME_RENDER_READY`, but it did not install the lock-gesture
correlator or emit any of the three readiness markers accepted by
`trigger_home_unlock_legacy.py`. The host trigger log remained empty and no
accepted user-gesture transition marker was created. One SpringBoard UIEvent
and HID callback appears in the log, but it has no relay transaction or
accepted-pan correlation and is not attributed to the trigger.

The shell-to-Home/lock lifecycle evidence and uniformly black sampled frame
remain valid; unlock/input is unscored. The runner now defaults to
`dm_post_shell_home_gesture_present_callbacks`, turns on its BackBoard input
trace, and rejects a legacy-trigger profile whose selected module is not one
of the gesture-ready compositions. The legacy trigger also logs its awaited
observer marker before waiting. P269's child is durably preserved at
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-plugin269-replay2b-legacy-uart-only-ack120-home-unlock1.qcow2`
(SHA-256
`c4d8f95c49631d71f80a5eaf76253e1832986f393faaf0636457b0531e9d833b`); its
complete event/frame and runner records are under
`recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN269_REPLAY2B_LEGACY_UART_ONLY_ACK120_HOME_UNLOCK1/`.
No second gesture was injected into that child. No passcode was sent.


#### P273 — accepted Recap pan does not establish an unlocked screen

P273 used the legacy UART-only helper on a fresh replay2b child. All 34 Recap
swipe records were ACKed; the same Cover Sheet pan recognizer returned
`shouldBegin=true`, began and ended, and the correlated SpringBoard transition
reported `x3=1` / `forUserGesture=1`. This closes the input-acceptance edge for
that gesture. It does not show that the lock layer was removed: the later
readable full-size IOSurface contained only sparse status/lock chrome on black,
and the HMP framebuffer remained black through the 600-second observation.
The separate `x3=0` callback came from another SpringBoard TPIDR and is not
attributed to this gesture. The lock-visibility callbacks had no rows, so
their state remains unobserved. No passcode was entered.

The child, input stream, logs, and captured frame are preserved under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN273_REPLAY2B_LEGACY_UART_ONLY_ACK120_HOME_UNLOCK1/`.

#### P274 — Home is reached; Recap swipe is rejected before its first point

P274 is a fresh replay2b child using the same legacy-UART-only input profile.
The guest helper started at serial line 618 and announced
`DVM_INPUT_READY protocol=1` at line 171574. The Home key's down and up were
both acknowledged; SpringBoard then returned a nonnull Home entity and a
non-null ADFL result. These markers establish the shell-to-Home lifecycle,
not a visible Home frame.

The Recap swipe's first down packet (`R`, sequence `1791794863656`, normalized
point `(16371,32563)`) received guest ACK `0` after about 36.0 seconds. The
separate release packet also received ACK `0` after about 206 ms. The guest
helper's current Recap path can return false before calling `touchDown:` when
`RCPSyntheticEventStream screenSize` is out of range; P274 used the previous
helper without dimension-stage logging, so invalid geometry is a candidate
cause, not a recovered P274 value. No accepted pan/dismissal is attributed to
these packets. This is input-event construction failure, not recognizer
rejection and not an unlock verdict.

The helper now emits `DVM_INPUT_RECAP` stage records for stream setup,
`screenSize`, point construction, processing-buffer lookup, and playback
queueing. Invalid geometry also releases and clears a partial Recap stream so
a later standalone release cannot reuse stale state. This version compiled to
an arm64 iOS helper and merged with the full dev6 input trust cache, but was
not installed or boot-tested in P274. Its first boot should compare the
reported Recap dimensions and buffer/playback stages against a successful
P273-style sequence. The P274 child and logs are preserved in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/P274_LOCK_IVARS_DISPATCH1/`;
the child SHA-256 is recorded in `child-sha256.txt`. The diagnostic helper
build is in `recovery-records/P274_RECAP_DIAGNOSTIC_HELPER_BUILD1/`. No
passcode or text was sent.

#### P276 — measured dismissal/Home path succeeds; HMP stays black

P276 is a fresh replay2b child with accepted `system.tc` and the existing
diagnostic setup. The same run returned from `DM_PERFORM`, BKS migration and
check-in, and the SpringBoard shell service; SpringBoard then returned a
non-null ADFL result, created a non-null Home entity, and returned success from
the workspace transition. The legacy-UART Recap gesture was fully ACKed. Its
`HOME_GESTURE_PAN_SHOULD_BEGIN_RETURN` was accepted, the same recognizer began
and ended, and BackBoard recorded
`ACCEPTED_LOCKSCREEN_GESTURE_TRANSITION` with `for_user_gesture=1` at guest
time `1791796340.687735`. This closes the input/accepted-dismissal control
path; it does not prove visible Home or a completed unlock. No passcode was
entered.

All 16 HMP captures were 1179x2556 and uniformly black, including the sample
420 seconds after the accepted gesture. Every captured frame had
`nonblack_fraction=0` and the same SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`. In the
post-transition observer window, SpringBoard produced 72
`CA_COMMIT_ENTRY` callbacks through `1791796751.388764`; the armed
`SERVER_RENDER_UPDATE_ENTRY`, `DISPLAY_RENDER_DISPLAY_ENTRY`, surface/OGL,
and framebuffer swap callbacks had no hits after the accepted transition.
The CA commit-return observer was not continuous in this run: its return
breakpoint disabled itself after its first hit. Thus the 72 entries do not
establish 72 completed commits or identify a first incomplete commit. The
follow-up probe retains the return observer and pairs returns to entries by
thread, stack pointer, and saved entry LR.
Those same render/surface/swap callback families did hit before the gesture,
so their later silence is a bounded observation of this run, not proof that
all possible render paths are absent. The next useful cutoff is the
SpringBoard CA commit/transaction handoff into the render server, with the
server-render hooks retained after an accepted dismissal. Do not send another
gesture or a passcode to explain the black frame.

The durable child is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin276-recap-diagnostic-home1.qcow2`
(SHA-256
`6d82839b8dba9457e23d38d7a1d6503c25e1b408c25a1e4ec5a1dbf4d059586a`). Its
event, frame, LLDB, serial, runner, and input records are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN276_RECAP_DIAGNOSTIC_HOME1/`;
`qemu-img check -U` found no errors. The child remains a diagnostic artifact;
no guest input protocol, DCP response, display-mode value, fixture, model,
SpringBoard, or scanout behavior was changed.

#### P277 — accepted dismissal still renders the Lock Screen

P277 repeated the ordinary Home-key and Recap-swipe sequence on a fresh P276
child. The Home key and swipe were acknowledged; the same pan recognizer
accepted, began, and ended, and the lock manager recorded
`for_user_gesture=1`. At that instant `_isUILocked=0` and `_uiUnlocking=1`,
which establishes an unlock transition in progress, not completion. The
pixel-bearing IOSurface was joined by pointer identity from set-layer `x3` to
handoff `x2`; the saved image still shows Lock Screen status, the Chinese
“Swipe up to open” prompt, and the Home indicator, with no Home icons. HMP
remained uniformly black through 420 seconds after the transition. No
passcode was sent.

P277's child is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/ui-class16-refresh60-dm-lock-plugin277-commit-pair-home1.qcow2`
(SHA-256
`ccde5785072f3875d7ef8a7c90678aff5e23f4c9b3874b0f2c6cc315a5628fd0`). The
record is in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN277_COMMIT_PAIR_HOME1/`;
the preserved frame is `frames/iosurface-posthome-03.png`. The child passed
`qemu-img check -U`. A follow-up should try one further ordinary swipe while
retaining the accepted-transition and surface-to-handoff observers; do not
submit a passcode.

#### P278 — follow-up swipe ACK without a second transition

P278's initial Recap swipe was confirmed by the same recognizer's accepted
`shouldBegin`, began, and ended callbacks; BackBoard recorded a
`for_user_gesture=1` accepted lock-dismissal transition. The Home event after
that transition and one later ordinary follow-up swipe were ACKed. The
follow-up produced no second accepted-transition marker in the bounded
window. This measures input delivery only; it does not establish completed
unlock or visible Home. `passcode_entered=false`; no passcode or text was sent.
Input and frame records are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN278_TWO_SWIPE_SURFACE1/`.

#### P280 — retain the first confirmed dismissal marker

P280 completed the shell/Home setup and received ACKs for the Home key and a
same-recognizer Recap pan. The pan passed `shouldBegin`, began and ended, and
produced a gesture-confirmed `ACCEPTED_LOCKSCREEN_GESTURE_TRANSITION`; the
trigger preserved that accepted JSON in its final result. A later
unconfirmed lock-manager event overwrote the active marker file, so P280 also
preserved the first confirmed event separately. The resulting source fix
writes `.first.json` on the first confirmed transition and makes the CA
send/receive and Update-gate probes accept either a valid active record or a
valid retained first record. It also clears a stale `.first.json` before a
fresh legacy swipe. The new marker helper has unit coverage, but this code was
edited after P280's LLDB session began and is not boot-validated yet. The
P280 gesture result is valid independently because the input trigger itself
read and reported the confirmed marker before later overwrite. Records and
the durable child are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN280_ACCEPTED_MARKER_SURFACE_SAMPLE1/`.

#### P290 — one delayed follow-up swipe is delivered but not accepted as a second dismissal

P290's first Recap swipe was confirmed by the same SpringBoard CoverSheet
recognizer's `shouldBegin`, began, and ended callbacks, followed by a
gesture-confirmed lock-manager transition (`for_user_gesture=1`). The delayed
sidecar waited 330 seconds, captured HMP, and sent exactly one additional
ordinary upward swipe. Every Recap relay event was ACKed. SpringBoard
produced recognizer-state and pan callbacks during that second input, but
the bounded 120-second follow-up window had no new confirmed lock-dismissal
transition. The recorded outcome is
`swipe-acked-no-second-transition-marker-in-bound`, not an authentication or
unlock result; no passcode or text was sent.

This run exposed a helper bug: after its delay, the sidecar copied the latest
active marker over the callback's immutable `.first.json` and removed the
active path. In P290 that latest value was an unconfirmed event, so the
sidecar overwrote the valid first record and closed the surface-observation
gate. The confirmed first marker was already saved separately, and the
overwritten event was preserved as `followup-overwrote-first-marker.json`;
the first gate was restored while the guest remained running. The source fix
now leaves both marker files alone, records an append-only `lock-gesture.jsonl`
cursor before the swipe, and accepts only a fresh confirmed marker appended
after that cursor. Unit tests cover confirmed/unconfirmed markers and partial
JSONL writes. The fix is not yet boot-validated. Artifacts are in
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN290_DELAYED_PROMPT_SWIPE1/`.

#### P403: Recap geometry came from mismatched A453 launch bytes

P403's `dvm-input` Recap getter and `_screenSize` store both measured
66715x2556. Its `A453=9b040100fc090000` launch payload differs from the
accepted P395 run's `9b040000fc090000`; the extra byte produces width
`0x1049b` (66715) instead of `0x049b` (1179). The helper therefore rejected
its Recap swipe before constructing touch events. This supersedes the earlier
unproven theory that Recap added a high word internally. P404 restores P395's
measured-good A453/D586 payload and keeps the same Home-wake, Recap-dismissal,
accepted-transition, then Home sequence. No touch-coordinate fallback or
screen-size override is introduced.

#### P404: corrected Recap geometry and acknowledged post-dismissal Home input

P404 matches the P395 A453/D586 bytes (`9b040000fc090000`), so Recap reports the expected 1179x2556 geometry. Its pan recognizer accepted the dismissal (`1792229283.536`); Home down/up were ACKed at sequences `1792229374102`/`1792229374103`. The SpringBoard Home-HID observer subsequently fired at `1792229477.460`, and a later workspace transition execute returned `x0=1` at `1792229672.596`. The screen remained sparse and lacked recognizable Home icons; input delivery and workspace API success do not identify the first missing content stage. P404's durable child passed `qemu-img check -U`; its event, timeline, screenshot, and launch records are in `recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN404_P276_WORKSPACE_HOME_FROM392_HOME4/`.
#### P405: acknowledged Home input and workspace execution; icon population remains open

On a fresh P392 P276 child with the accepted P395 A453/D586 geometry, the dismissal was accepted and the acknowledged Home event reached SpringBoard (`1792230826.460`). `SBMainWorkspace` creation/execute returned success, and `SBHomeScreenEntity`, root-folder controller/view, and root/dock icon-list views initialized. `SBHIconModel` was initialized with `SBIconModelPropertyListFileStore`; the captured application-data-source argument at this initializer was null. `loadAllIcons` returned. No `setIcons:` event was recorded, but the probe did not verify its LLDB location count, so it cannot establish that the setter was not called.

The HMP capture remained black; P394/P395's sustained natural A408/HMP path remains unchanged. P405's child SHA-256 `9b7b1241e3d8308300a6659f751a28fd012e7ea74615613c71374628a7d6f529` passed `qemu-img check -U`. Records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN405_P276_WORKSPACE_HOME_FROM392_HOME5/`. Next, collect store URL/load results, the model application-data-source getter, root/dock icon counts, and breakpoint location counts, all armed after the accepted dismissal.

#### P405: acknowledged Home input and workspace execution; icon population remains open

On a fresh P392 P276 child with the accepted P395 A453/D586 geometry, the dismissal was accepted and the acknowledged Home event reached SpringBoard (`1792230826.460`). `SBMainWorkspace` creation/execute returned success, and the Home entity and root-folder view were created. SpringBoardHome initialized an icon model backed by `SBIconModelPropertyListFileStore`; the captured application-data-source argument at this initializer was null. `loadAllIcons` returned. Root/dock `SBIconListView`s initialized from list models. No `setIcons:` event was recorded, but the probe did not verify its LLDB location count, so it cannot establish that the setter was not called.

The HMP capture remained black; P394/P395's sustained natural A408/HMP path remains unchanged. P405's durable child SHA-256 `9b7b1241e3d8308300a6659f751a28fd012e7ea74615613c71374628a7d6f529` passed `qemu-img check -U`. Its archive is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN405_P276_WORKSPACE_HOME_FROM392_HOME5/`. Next, collect store URL/load results, the model application-data-source getter, root/dock icon counts, and breakpoint location counts, all armed after the accepted dismissal.

#### P406: post-dismissal Home reaches `dispatchEvent:` and is rejected

P406 used a fresh checked P392 P276 child, the accepted P395 A453/D586 input geometry, and the natural A408 scanout profile (`DARWIN_DCP_IOMFB_SCANOUT=1`). Graphics, CA, OGL, IOSurface, SwapEnd, descriptor, A407, and DCP-completion probes were disabled. The accepted dismissal at guest time `1792232027.252` installed the workspace and icon-state probes; each relevant breakpoint resolved to one location. The Home key down/up before dismissal and all Recap swipe packets were acknowledged. After dismissal, the helper built a non-null keyboard event and called `HIDVirtualEventService dispatchEvent:` twice; both calls returned false (`DVM_INPUT_HOME accepted=0`, sequences `1792232118152` and `1792232120351`). It did not ACK the up edge. No SpringBoard HID receipt followed either rejected attempt, and the accepted-transition-gated workspace/icon probes recorded no calls. Thus this run stops at the virtual-HID dispatch result; it is not evidence of a workspace or icon-store failure.

P405 is the positive control: the same helper code and accepted gesture sequence had post-dismissal `dispatchEvent:` return true for both Home edges, followed by SpringBoard HID receipt and successful workspace execution. The P406 failure therefore occurs after event construction, at the `IOHIDVirtualServiceClientDispatchEvent` boundary inside `HIDVirtualEventService dispatchEvent:`. The Home key event is not proven delivered to BackBoard or SpringBoard in P406. The saved HMP frame remains sparse system chrome (Home indicator and information control) on black; this does not change P394/P395's natural A408 scanout evidence. P406 child SHA-256 `99367959c4393aaa3ad2d27e6238fd2b1c4edb9f88e26c0e2d0e5ff97e440b80` passed `qemu-img check -U`. Archive: `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN406_P276_ICON_STATE_FROM392_HOME6/`.

Next, keep the probe limited to the keyboard service and event: record its runtime class/registry identity and keyboard-event fields before dispatch, then observe the dispatch result and any corresponding BackBoard/SpringBoard receipt. Do not infer a Home/workspace failure from an event rejected before receipt.

#### P407: acknowledged Home dispatch reaches SpringBoard, workspace calls recur, Lock overlay remains

P407 returned to the P392 P276 parent after the P406 dispatch rejection, with P395's exact A453/D586 input bytes and natural `DARWIN_DCP_IOMFB_SCANOUT=1`. The lock-dismissal pan was accepted at guest epoch `1792233170.694`. The post-dismissal Home down/up both received relay ACKs (`1792233261154` and `1792233261155`); the `dvm-input` observer saw the underlying IOConnect call succeed (`x0=0`) and `IOHIDVirtualServiceClientDispatchEvent` return true (`x0=1`) for both edges. A later SpringBoard HID event was observed at `1792233332.215`, after the Home dispatch. The run therefore passes the helper dispatch boundary and has a SpringBoard event after Home; it does not prove that this later event is the exact same event object.

Accepted-transition-gated probes recorded multiple SpringBoard workspace creation and execute-transition calls after Home dispatch. The P407 lifecycle callback accidentally serialized every sampled register under the literal JSON key `x%d`, overwriting prior values; its execute return values are therefore unscored. The HMP capture after the later SpringBoard event still displayed the lock-screen overlay, not Home. No graphics, CA, OGL, IOSurface, SwapEnd, descriptor, A407, or DCP-completion probes were enabled. P407's child SHA-256 is `aadb92280999904d6b48631880563c400080193186da697aecd2db38d40fc9c3`; `qemu-img check -U` passed. The durable record is `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN407_P276_DISPATCH_WORKSPACE_FROM392_HOME7/`.

P408 corrects the register-key serialization and adds only capped icon-model/store/list probes alongside the two workspace boundaries, all installed after the accepted dismissal. It keeps the same acknowledged Home input sequence and natural A408 scanout; the P394/P395 transport result is unchanged.

#### P408: delayed Home dispatch succeeds, but SpringBoard receipt is still missing

P408's accepted dismissal was recorded at guest time `1792234009.425`. The first post-transition Home attempt at `1792234022` was too early and was rejected (`accepted=0`); this is not the scored attempt. The retry at `1792234103` followed the configured 90-second settle interval and succeeded: `HIDVirtualEventService dispatchEvent:` returned true for both Home down/up. The HMP frame captured about 90 seconds later is a 1179×2556 black field with only the Home bar and information glyph (SHA-256 `1af4e756f191c466eb9a9518dc713e24471b43d116ac8b85b2b7f5325a7a9ea3`).

The accepted-transition bootstrap installed the workspace create and execute hooks with one resolved location each; neither callback fired. The icon-model/store/list hooks were also installed with validated locations and recorded no callbacks. The low-rate SpringBoard HID hook had two earlier events (`1792233731.024` and `1792233898.035`), both before the accepted dismissal; it recorded no later receipt after the successful Home dispatch. Thus this is a bounded missing-receipt/workspace observation after an accepted dispatch, not a content-population or scanout failure. The run had a procedural deviation: I manually replayed the swipe and Home retry after stopping the automated trigger before it completed its full 200-second lifecycle grace. P409 repeats the known P407 automated timing and adds only a post-accept, keyboard-processor-filtered BackBoard dispatch observer plus the minimal workspace boundaries. Natural A408 scanout remains enabled and graphics/CA/DCP probes remain disabled.

P408's child SHA-256 is `5551ffb17e0807c5f5c356d90dd13efb7ea7a3e805718ffaa4a98cf3d9fa6df3`; `qemu-img check -U` passed. Durable records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN408_P276_WORKSPACE_ICONS_FROM392_HOME8/`.

#### P409: workspace transition succeeds, Lock Screen remains visible

P409 repeats the P407 P276 input timing on a fresh child of the same checked P392 parent: complete the full 200-second Home-entity/ADFL lifecycle grace, wait for a gesture-confirmed dismissal, then send the delayed Home event. After acceptance it arms only the two workspace lifecycle boundaries, the Home virtual-HID call/return observer, and a narrow BackBoard `BKHIDPrimaryEventProcessor` dispatch hook filtered to `BKHIDKeyboardSignpostEventProcessor`, `BKKeyboardHIDEventProcessor`, and `BKButtonHIDEventProcessor`. That hook records the event wrapper and bounded raw `HIDEvent` bytes. It is intended to distinguish helper construction/dispatch acceptance from BackBoard keyboard processing and SpringBoard HID receipt; it does not add icon-content or graphics probes. Natural A408 scanout stays unchanged.

P409 completed its bounded post-transition window. The dismissal gesture was accepted at `1792235086.800`; delayed Home down/up both received relay ACKs, and `dvm-input`'s virtual-HID dispatch returned true for both. The accepted-transition workspace hooks then recorded three create requests and two execute-transition returns, both with `x0=1`. Thus the workspace operation did not fail at its observed return boundary. The keyboard-processor filter was armed but recorded no matching dispatch; that absence is not promoted to an input failure because SpringBoard did execute workspace transitions. The final 1179×2556 HMP image still showed the Lock Screen time/status, “Swipe up to open,” information control, and Home bar, with no Home icons (SHA-256 `6e8ca162b5234400f0fecaa8c7e51c2734b885ec73c239239dfcf2a97f974feb`). The next discriminator is the accepted-transition-gated icon store/data-source/list state, not display transport. P409's child SHA-256 is `d9202ea822a7bda5d368457180aff45501e39b25ec14438ab90bf8d7e24a6b99`; `qemu-img check -U` passed. Durable archive: `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN409_P276_KEYBOARD_ROUTE_FROM392_HOME9/`.

P395 is already stopped and durably archived; its child SHA-256 is `42e57973ae9c8c6a5f63b03417916ba691f3a23d980cd2896cf6507cc5d72fe9`, and its recorded `qemu-img check -U` reported no errors. No P395 guest remained running when P409 began.

#### P415: acknowledged Home event and successful workspace return, Lock Screen still rendered

P415 used the P392 P276 parent and P395's valid Recap geometry. SpringBoard confirmed the dismissal pan (`1792239337.329`), then acknowledged Home down/up were sent and SpringBoard observed a Home HID event (`1792239538.682`). A later workspace execute-transition returned `x0=1` (`1792239574.670`) and Home-entity initialization returned non-null. This distinguishes P415 from a keyboard dispatch or workspace-return miss. The captured frame nevertheless retained the Lock Screen “Swipe up to open” prompt and had no recognizable Home icons. `isAuthenticated` returned true and `hasPasscodeSet` returned false, so no passcode was required for this observed state.

P415's accepted-transition-gated SpringBoardHome callbacks saw widget-grid and property-list icon-model stores, a returned `loadAllIcons`, and root/dock list-view and root-folder view construction. They did not map list counts/insertions to the root/dock models. P410's earlier four-icon count was on an unjoined model and is excluded as a root-list result. The next run uses only root/dock-associated list count/population and content-visibility/icon-view boundaries, after the accepted gesture; it does not re-enable graphics or display probes. P415 was stopped after the content snapshot; no panic occurred. The durable child SHA-256 is `41eab5f73e4726d411c59a8d37fab7ca74ddc1d1a15aff6b932bbcb0f49ae3e1`; `qemu-img check -U` passed. Records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN415_P276_HOME_SETUP_OFFLINE_CONTINUE_FROM392/`.
#### P419: acknowledged Home reaches SpringBoard; root list contains widgets only

P419 used a fresh P392 P276 child, the P395 A453/D586 geometry and input
sequence, natural A408 scanout enabled, and no CA/OGL/IOSurface/descriptor/
SwapEnd/A407/DCP-completion probes. Workspace and root-content observers were
armed only after the accepted Cover Sheet gesture (`1792244456.794`). The
post-dismissal Home down/up were both acknowledged (sequences
`1792244547361`/`1792244547362`); SpringBoard later recorded the joined HID
receipt at `1792244664.004`. `SB_WORKSPACE_EXECUTE_TRANSITION_RETURN` returned
raw `x0=1` at `1792244467.478`, `1792244607.295`, `1792244705.137`, and
`1792244883.701`; Home-entity initialization returned non-null. No failed
transition return was observed. The Home event therefore reached the existing
workspace/content branch; keyboard construction/dispatch is no longer the
next cut.

The root `SBIconListModel` (`0x750be90d00`) reported eight entries, and every
sampled index 0–7 returned `SBWidgetIcon`; the four root icon views that were
assigned content were assigned widget-icon objects. The dock model
(`0x750be90b00`) reported four entries and its `icons` method returned
`__NSArrayI` objects, but this run did not decode their members. Thus the
evidence says the observed root collection has no `SBApplicationIcon`, while
dock membership remains open. The settled HMP frame still had only status /
charging chrome, the information glyph, and the Home indicator; no recognizable
app grid appeared. A408/HMP was left on the P394/P395-proven natural scanout
profile; this run is not a sink regression.

The child was stopped after the transition and collection boundary rather than
using its idle bound. SHA-256 is
`8c1685664f5aad66e21973518703409958dc51c2434afa1c90191403afd0735c`;
`qemu-img check -U` found no errors. Durable records, screenshots, and the
post-swipe A408/HMP timeline are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN419_P276_ROOT_CONTENT_KEYBOARD_FROM392_HOME19/`.
The next narrow cut is to decode the four dock-array members and the saved
icon-state store/source after the same accepted-transition gate.

#### P420: acknowledged Home reaches UIKit's HID-to-event conversion, but not workspace creation

P420 is a fresh P392 child with P395's exact A453/D586 input geometry and natural `DARWIN_DCP_IOMFB_SCANOUT=1`; all graphics, CA, OGL, IOSurface, descriptor, SwapEnd, A407, and DCP-completion probes were off. Workspace and content observers were installed only after the gesture-correlated accepted dismissal at guest time `1792245660.438`. Home down/up were both relay-ACKed (`1792245684432`/`1792245684433`). BackBoard's keyboard signpost, keyboard, and button processors handled the same HIDEvent object (`0x7663835810`) for each edge. SpringBoard then entered UIKit's `-[UIEventEnvironment UIKitEventForHIDEvent:]` at `0x18493425c` at `1792245687.752` and `1792245691.769`, establishing receipt at the HID-to-UIEvent conversion boundary. The helper source constructs the Home keyboard event with `IOHIDEventCreateKeyboardEvent(page=0x0c, usage=0x40, down/up, options=0x40)` and sets integer field 4 to 1 (`tools/input/dvm_input.c:216-221`).

No accepted-gated `SB_WORKSPACE_CREATE_REQUEST`, `SB_WORKSPACE_EXECUTE_TRANSITION`, root/dock list/view creation, or root-content callback followed. `SBHomeScreenEntity` init returned non-null three times, and `isAuthenticated=1` / `hasPasscodeSet=0`; an `SBIconListModel` count of four happened before the post-dismissal Home key and was not joined to root/dock. The conversion method's return and subsequent UIApplication event delivery were not instrumented, so this is a no-workspace result with the next missing edge narrowed to post-conversion dispatch, not a conversion failure. The sampled post-Home HMP frame is black (SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`); this is a guest UI-state sample and does not change the established P394/P395 scanout path.

P420 recorded no panic. Child SHA-256 `d615da91f2090044e9712bea664d84bced252b1a9a0b0a2c71b838ee2ff8a844` passed `qemu-img check -U`. Its durable records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN420_P276_HOME_INVENTORY_FROM392_HOME20/`. The next run should trace only the accepted Home HIDEvent through UIKit conversion return and `UIApplication handleEvent:withNewEvent:` / `sendEvent:`; keep natural scanout unchanged and do not re-enable graphics probes.

#### P421: Home event executes workspace transitions, but P421 content hooks were invalid

P421 is a fresh child of the checked P392 P276 parent, using the same helper TC,
A453/D586 input geometry, and natural `DARWIN_DCP_IOMFB_SCANOUT=1` profile as
P395. CA/OGL/IOSurface/SwapEnd/descriptor/A407/DCP-completion probes were off.
The accepted Recap dismissal was recorded at `1792246986.127`; the acknowledged
post-dismissal Home event was delivered at `1792247076.706` and its
`HIDVirtualEventService` dispatch returned true. SpringBoard entered UIKit's HID
event conversion, issued six observed workspace create requests, and returned
from three execute-transition calls with `x0=1`. The observed workspace API
path therefore succeeded; that result alone does not establish that the lock
overlay was removed.

P421's 900-second post-Home observation ended with a stable 1179×2556 frame
showing only status/charging chrome, the information button, and the Home
indicator on black (SHA-256
`381a35659a0d1c451f02a5866860fa7ba15aef7fc2e916168dcad6de2e53b35b`). It did
not show recognizable wallpaper or app icons. The root-content probe emitted
only `POSTACCEPT_ROOT_CONTENT_READY`; its class resolver had a misplaced,
unreachable `try` block, so all class-filtered callback silence in P421 is
unscored. I corrected `_class()` to call the existing Objective-C class-name
resolver under its exception guard. P422 will re-run the same accepted-gated
workspace/content path on a fresh child to validate that fix, with graphics
probes still disabled and the complete post-transition sampling window kept.

P421's child SHA-256 is
`08b29dbac243f8f0922d5019069bbef7154aa9f648445aed40241d28fe38551d`;
`qemu-img check -U` found no errors. Its durable records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN421_P276_HOME_EVENT_ROUTE_FROM392_HOME21/`.

#### P424: acknowledged Home reaches SpringBoard; workspace transition succeeds

P424 used a fresh P392 P276 child with P395's exact `A453=9b040000fc090000` input geometry and natural A408 scanout enabled. SpringBoard accepted the Recap dismissal at `1792252639.182535`. The post-dismissal Home key down/up were acknowledged, and a later SpringBoard HID callback ran at `1792252812.015432`. Workspace creation returned objects and `executeTransition:` returned `x0=1` twice. This closes the Home-event and workspace-failure branches for this run; it does not prove the Home grid is visible.

The run intentionally kept only readiness/input, workspace create/transition, natural A408 diagnostics, and HMP sampling. Both post-Home HMP screenshots have SHA-256 `6734179bb6c485ca24207d7add95b30dc979e59c0fbd144b615fef90f8b606a7`. They show sparse system chrome on black, without wallpaper or icons. The timeline records 345 rows, 343 successful HMP samples, 312 source hashes, 70 HMP hashes, and 11 exact source/HMP pixel-hash matches; matching is established for those samples only. The next probe should inspect SpringBoard workspace visibility, icon-store/source state, and Setup transition events, installed only after the accepted dismissal. P424 child SHA-256 `9c829b25ea7804079263b396a557aad1c54ba306facbe0f29324963e82b95bb7` passed `qemu-img check -U`; records are under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN424_P276_MINIMAL_POSTACCEPT_FROM392_HOME23/`.

#### P427: Home is ACKed but no workspace transition follows; next capture the keyboard route

P427 is a fresh child of the P425 P276 lineage, using the accepted `system.tc`,
natural A408 scanout, and no graphics/CA/OGL/IOSurface/descriptor/A407/DCP
probes. The accepted lock dismissal was confirmed at guest time
`1792255317.295358`. SpringBoard emitted one `SB_WORKSPACE_CREATE_REQUEST` at
`1792255322.260976` (`SBMainWorkspace`, `_initialized=1`) in the explicit
`after-accepted-dismissal-before-home-hid` phase, but no
`SB_WORKSPACE_EXECUTE_TRANSITION` callback followed. The post-dismissal Home
key was later ACKed by `dvm-input`; serial records show keyboard event creation
and `dispatchEvent:` returning `accepted=1` for both edges. The sampled HMP
frame still says “Swipe up to open.” Thus helper construction/submission is
proven, while this run did not trace the Home event through BackBoard or
SpringBoard and does not locate the first missing delivery edge.

The generic input observer had exhausted its 64-hit direct-touch budget on the
two Recap swipes, and the Home-specific keyboard/UIApplication route hooks were
not armed. A Setup-tap watcher also matched `SB_ACTIVATE_SETUP` in the READY
record's static `sites` metadata, rather than an actual callback event, and
sent its buffered tap alongside Home traffic. Treat that tap as invalid and
exclude P427 from interpreting the Home route. P428 removes the Setup watcher,
serializes the inputs, and dynamically arms only the Workspace create/execute
sites plus Home helper → BackBoard keyboard → SpringBoard HID/UIKit event-route
observers after the accepted dismissal.

P427 recorded no panic. Its child SHA-256 is
`2d391166dcda7bbe7081e34f9376deda80ef668c9309fa005bfd2a4af2eb5191` and
`qemu-img check -U` reported no errors. The durable bundle, including the
post-Home HMP screenshot, is
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN427_P276_SETUP_CONTINUE_CORRECT_TAP_FROM425/`.
The A408 scanout implementation and its P394/P395 validation remain unchanged.

#### P429: the top-clustered P427 frame is a post-gesture guest transition state

Offline comparison of P427's HMP timeline localizes the layout change to the
accepted CoverSheet dismissal. Before the accepted transition, frame
`a408-0000-02.png` at `1792255289.349` shows the prompt, info control, and Home
indicator at the bottom (bright-pixel row bands 2437–2515). P427 records the
accepted gesture at `1792255317.295`; frame `a408-0000-57.png` at
`1792255322.146` places the same controls at rows 247–334, an upward
translation of about 2,180 pixels. `SB_WORKSPACE_CREATE_REQUEST` follows at
`1792255322.261`, before the Home HID event, and no
`SB_WORKSPACE_EXECUTE_TRANSITION` was recorded. The setup observer emitted
only its READY/site-list row; no Setup activation callback occurred. This is
not evidence of a fixed display-mode error or a proven Setup screen. The
shared displacement after the accepted gesture points to an unfinished guest
CoverSheet/workspace transition or common presentation transform; the exact
layer producer remains unmeasured. P427 HMP RGB hashes for frames 57–75 are
identical (`e530bce7…dbf46e23`) over about ten seconds, so the shifted
composition persists in that sampled interval rather than appearing in one
captured animation frame.

P429 is a fresh, no-input child of the same P425 P276 parent with the same
`system.tc`, 1179×2556 framebuffer mode, A453/D586 profile, and natural A408
scanout. HMP and A408 source frames report 1179×2556 (stride 4864). Before any
Home or Setup input, HMP frame `a408-0000-28.png` at `1792257757.607` shows
the Lock Screen prompt, info control, and Home indicator at their normal
bottom positions; later periodic HMP frames preserve the bottom-positioned
system chrome. This argues against a persistent bad pixel geometry or a host
scanout layout error.

P429's geometry breakpoint addresses resolved, but no `UIScreen`/scene getter
return or view/layer geometry event was recorded. P427's screen-size input
helper values are not UIKit bounds. Therefore 393×852 logical points at 3×
remains an inference from 1179×2556; actual `bounds`, `scale`, `nativeScale`,
orientation, safe-area insets, CA display bounds, and named layer frames are
unscored. P429 never emitted the probe's shell-return gate; its capped view
hooks were armed later on that same no-input guest and produced no samples.
The probe's class resolver and shell-gate process filter were corrected, but
this run does not validate the getter/frame hooks. No values were forced.
P429 recorded zero panic lines, and `qemu-img check -U` found no errors. Its
child SHA-256 is
`bc5ddd9d58b576fa7f435f60c9eb47995ee86b0197621023921512903bc43894`; durable
records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN429_P276_LOCK_GEOMETRY_FROM425/`.

#### P430/P431: accepted dismissal without a workspace request

The initial hardware-button caller attribution is withdrawn: it used the wrong
P415 slide. With each run's saved slide, P409's create-to-execute edge is
`SBWorkspace requestTransitionWithOptions:builder:validator:` at `0x224475e88`.
Creation returns at `0x224475ec8`; the optional builder is called at
`0x224475ee4` with the request in `x1`, then execution is called at
`0x224475ef8` with the same request in `x2`. P415's earlier orientation request
comes from `SBSwitcherController` at `0x22435e9fc` and executes through the
public wrapper `0x224311b80`. Its completion handler is installed before
submission; it does not trigger submission. P427 captured only creation entry
while copying a request (`copyMainWorkspaceTransitionRequest`, return boundary
`0x224384ac8`), with no returned copy. It does not establish a completed
request waiting for an animation callback. P433 archives the exact static
disassembly/selector evidence and adds capped builder call/return boundaries.
P427's create-return matcher additionally required LLDB thread-ID equality,
which can discard a return after vCPU migration; its absent return is not a
proved stall. Current matchers retain guest TPIDR/SP identity without that
brittle requirement.

P430 did not score behavior: the accepted-transition callback failed on a
`None` ivar list and therefore installed the dynamic probes late. The probe
now treats absent ivars as an empty list. P431 validated immediate installation
after a gesture-confirmed dismissal (`1792261048.633`) on a fresh exact P425
P276 child. Its accepted-state ivar values are withdrawn: the metadata reader
rounded 4-byte offset slots down to 8-byte alignment, aliasing neighboring
ivars (including `_windowScene`). The pointer helper is corrected and covered
by a host regression. P431 recorded no
workspace-create or execute event during the 240-second follow-up. Because no
request appeared, P431 does not test whether an existing request advances.

The capped post-accept geometry callbacks saw identity transforms for the
CoverSheet hit-testing, dest-out, and date views; the CoverSheet contents and
fixed footer requested `(0,0,589.5,1278)` frames. Their hierarchy snapshots
were empty, and prompt/root layer geometry was not available, so this run
cannot assign the shifted controls to a root transform or an animation
completion.

The post-dismissal Home relay was ACKed. SpringBoard recorded temporally nearby
`UI_EVENT` at `1792261050.903` and `.932`; the event identity and conversion
return were not decoded. The minimal input observer recorded no
corresponding `SPRINGBOARD_HID_EVENT` and the workspace probe recorded no
workspace request. The next experiment should follow only
the acknowledged Home event through `dvm-input`, BackBoard's keyboard
processor, UIKit conversion/dispatch, and SpringBoard receipt, while retaining
the minimal workspace-create/execute gate. The final HMP frame still showed
the shifted Lock Screen controls (SHA-256
`d1ddd89c29bfe0bf6833c5351bebb0946f40157fec018ed53fe0b81173d4d839`). The
post-accept A408/HMP timeline has 355 rows, 318 distinct source RGB hashes,
and one HMP RGB hash; that is a stable shifted frame, not a transport regression.
No panic was found. P431 child SHA-256 is
`26909cc7e8685cea3a7f9e87524fb16fe1915e899e971495ad6076e061bb8156` and
`qemu-img check -U` passed. Durable records are under
`/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN431_P276_WORKSPACE_HANDOFF_FROM425/`.

P432 was interrupted before instrumentation or input and yields no route or
workspace verdict. Its early serial fragments and checked child are archived.
P433 uses the same exact parent/TC/input profile, with the Home-event route
and the corrected synchronous workspace builder boundaries armed only after
accepted dismissal. Geometry and Setup/content probes are excluded.


#### P433: request copy returns; no retained workspace transaction at freeze

P433 is a fresh exact P425/P276 child with the same helper TC and natural
A408 sink. After a serialized retry, the accepted dismissal is
`1792262467.791527` on guest TPIDR `0xffffffe5cc743060`. Deferred input and
workspace hooks installed immediately. Home down/up were acknowledged;
the helper's two virtual-dispatch returns were true, BackBoard keyboard
processors saw both edges, UIKit classified them as type 3 and returned
`UIPressesEvent` `0x7afbd7cbe0`. That same event reached
`-[SpringBoard sendEvent:]` (IMP `0x224325968`), `UIApplication sendEvent:`
and `handleEvent:withNewEvent:`. Absence of `_handleHIDEvent:` is therefore
not evidence of a delivery failure on this UIKit presses route. The actual
same-build keyboard mapper recognizes consumer page `0xc`, usage `0x40`,
as press type `0x65` (`0x184e32c48..0x184e32c68`); no input values were changed.

`copyMainWorkspaceTransitionRequest` entered at `1792262471.775034`; its
create call returned at `.791099` and the copy returned at `.795387`, both
with `x0=0x7afbf48280`. Return correlation uses guest TPIDR/SP, not LLDB's
vCPU thread ID. The copy caller's LR is `0x232bd9f04`, minus this run's
`0xe9b4000` slide = `0x224225f04`, inside
`-[SBToAppsWorkspaceTransaction initWithTransitionRequest:applicationController:]`.
Its next selector is `ancillaryTransitionRequestsForTransitionRequest:` at
`0x224225f10`; it is not P409's request-builder submission edge. No builder,
public/internal execute, or orientation-enqueue event appeared in the covered
240-second post-accept window. This does not prove an existing transaction
waited for execution: the initializer can be downstream of an earlier execute.

At the held endpoint, the SpringBoard task mapping was validated using both
kernel proc name and userspace `___progname` through that task's explicit
pmap (`root=0x10124240c00`). The retained `SBMainWorkspace` at
`0x7afd8d4380` has both subclass/base `_currentTransaction` slots zero and no
active suspended transitions. Its `FBWorkspaceEventQueue` has
`_processingEvents=0` and `_executingEvent=nil`. The matched main thread's
saved chain returns through `_SBSystemAppMain+0x14a8` (`0x224458c80`) to the
normal event-loop frames; it is no longer inside request creation. The old
request addresses now have other classes and cannot be read as retained
requests.

The retained lock manager `0x7afca0a140` has `_uiUnlocking=0`,
`_handlingUnlockRequest=0`, `_isUILocked=0`, `_isScreenOn=0`. Its retained
presentation manager `0x7afca76800` has `_dismissalCommitted=1` and
`_dismissingCoverSheet=1`; that flag alone is not an outstanding-completion
verdict. The primary sliding controller `0x7afd175500` has current/latest
presentation state 7, no stored completion block/group or active transition
subcompletion/retarget block. The guest's state-name table
`0x268980c38` (`stringForPresentationState:` at `0x224681b48`) names 7
**dismissed**; 3 is translating up and 4 positioned offscreen. Thus a
pending sliding animation is not established by the freeze. No guest
methods were invoked during this read-only retained-object walk.

Static next boundaries are now explicit: the state-7 arm calls the delegate's
cleanup at `0x2243db54c`; delegate IMP `0x224799bf0`; manager
`_cleanupDismissalTransition` `0x2243f0974` calls `_cleanupATransition` at
`0x2243f0a08` and `_notifyDelegateDidDismiss` at `0x2243f0acc`.
`_notifyDelegateDidDismiss` (`0x22479d004`) enqueues block invoke
`0x22479d078`, whose `B+0x20` capture is the manager and whose call at
`0x22479d098` sends `lockScreenViewControllerDidDismiss` to its delegate.
These name the next narrow live discriminator; they do not prove it was
missed on P433.

P433's final HMP is black (PNG SHA-256
`d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`),
and the screen-off state confounds interpreting that endpoint as missing
Home content. This does not reopen the proven host sink. No panic/critical
restart was found. Child SHA-256
`60f9b0be95ab94f16a422a46310dbbdd6652eaff17a703832180daa3b65061f4`;
`qemu-img check -U` passed. All events, explicit-task stacks, sparse physical
pages, retained-object JSON and selector/disassembly evidence are archived in
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN433_P276_HOME_ROUTE_FROM425/`.

P434 keeps the same parent/TC/scanout and installs only capped retained-root
geometry, cleanup/queued-did-dismiss and workspace hooks after acceptance.
It removes the unfulfilled 200-second lifecycle grace before the same natural
Home wake/32-step swipe, because P433's screen was already reported off at
acceptance. This is an explicit input-timing discriminator, not a forced
backlight or display-state change. Results remain pending.


P433's additional offline timing join (`analysis.workspace-entry-relative-accepted.json`)
separates the compared request paths. P409's synchronous builder create/execute
is +130.230/+130.243 seconds after acceptance. P415's copy creation returns at
+11.035 seconds; the later +11.473-second execute is a **different orientation
request**, created from `0x22435eaf4`, not proof that completion of the copied
request triggers execute. P415 also creates a request inside the CoverSheetToApps
initializer (`0x2249d1c14`). P427's sole copy creation is +4.966 seconds; its
missing return remains subject to the superseded thread-ID filter. There is
no demonstrated missing asynchronous copy-to-execute callback in these logs.
The next live cut therefore observes the actual CoverSheet cleanup and queued
DidDismiss boundary rather than forcing or assuming such a callback.

P433's retained `SBCoverSheetPositionView` adds a concrete position measurement:
`_progress=1.0`, `_contentScale=1.0`, `_contentCenter=(294.75,-639.0)` at
`0x7afca77200`, from runtime metadata offsets `+0x1b0/+0x1b8/+0x1d0`.
This is an offscreen content position, consistent with state 7; it is not
proof that the global root/viewport is wrong. P434 snapshots these named
fields at the existing cleanup boundaries without adding a progress-tick probe.


#### P434: normal Lock chrome → presentation-position shift → retained render abort

Fresh exact P425/P276 child; QEMU, parent and helper TC SHA-256 are identical
to P431/P433. Natural A408 scanout remained enabled and unchanged. Initial
input before visual readiness was ACKed and reached BackBoard but produced no
accepted pan; no cleanup verdict is scored from it. The host controller was
stopped while idle. Natural A408/HMP then showed bottom-positioned prompt,
info control and Home indicator (`a408-0000-84.png`, HMP RGB
`ade62aac93a8b55891c1245dc5b43abc265f84c17023c56313553e108a04ac09`).
A second serialized swipe on the **same** child was accepted at
`1792264218.872608`, TPIDR `0xffffffdf425e3830`.

The deferred probe's ready SHA-256 is
`5254fa3c627e8eddd9f849beff1f6a0f5b6ad20ba85cdc4e10a8679b110fc921`.
At acceptance: lock manager `0x798eb1e300` reports screen on, UI unlocked,
UI unlocking; sliding controller `0x798edcc700` is state 3 (translating up),
latest settled state 1 (fully presented), completion group `0x798fe4c820`
(raw value in the event is authoritative), and workspace `0x798f3e4460`
has a current transaction. The position view `0x798ebb7200` reports progress
`0.8043818466`, scale 1, content center `(294.75,-389)`.

The retained root/window bounds stay `(0,0,589.5,1278)`, and observed affine /
layer transforms are identity. The position content view `0x798eb1f480`
returns frame `(0,-1028,589.5,1278)` and later `(0,-1278,589.5,1278)`.
At exact producer `0x224c69034`, `SBCoverSheetPositionView` applies presentation
`transform`; at `0x224c690ac` it applies **position**, with measured stack
points `(294.75,-389)`, `(-410.5 y)`, `(-502 y)`, `(-540.5 y)` and explicit
velocity values. These calls are inside
`positionContentForTouchAtLocation:withVelocity:progress:transformMode:forPresentationValue:`.
The sliding snapshot advances to state 5 (transitioning out); its group and
workspace transaction are still present during these samples. This directly
identifies an upward content translation, not a global display/root scaling
fault. It does not prove the animation finished or that HMP captured every
intermediate presentation value.

No queued DidDismiss/cleanup or new execute was observed before the controller
cut the window at about +31 seconds: post-delay Home down was allocated but
`dispatchEvent:` returned false. The serial line has nonnull service/client/
event (`sequence=1792264249369`). The relay's generic "could not construct"
exception is **not** an allocation failure. The requested 120-second post-Home
window therefore did not run and cannot supply a no-completion verdict.

The guest was retained and a complete, SHA-256-recorded 12 GiB DRAM capture
was made before QEMU quit. The matched thread address now belongs to live
SpringBoard PID 510 in `applicationDidFinishLaunching:` loading SplashBoard
manifests (`_kevent_id` / libdispatch ownership wait / FrontBoard SQLite).
The old lock/workspace pointers are unmapped in that explicit pmap. A stale
SpringBoard PID-390 proc candidate has zero threads and its old root now maps
`watchdogd`; it is excluded. Thus thread-address equality is not sufficient to
extend the old gesture's object identity across this endpoint. No SpringBoard
exit reason was recovered; a specific SpringBoard crash cause is not assigned.

There **is** a same-boot backboardd PID-72 CRASHINFO record:
`ram/10140000000.bin+0x38aa8000`, SIGABRT (namespace 2/code 6), crashed TID
`0x3a4`. The retained PID-72 task mapping validates userspace `backboardd`;
its aborting thread `0xffffffdf423fc180` contains TID `0x3a4` at `+0x4e8`.
A later backboardd PID 483 is resident. The abort stack is
`___pthread_kill` → `abort` → `__assert_rtn` →
`CA::OGL::Context::push_surface` → `AsynchronousNode::retain_surface` → nested
`ImagingNode::render` / `render_layers` / `LayerNode::apply`. Use return PC
**minus four**: LR `0x18445e2d0` is the next function's boundary, not evidence
of `reset_statistics`. The actual fatal call is `0x18445e2cc`, assertion
`found != slist->end ()`, `ogl-context.cpp:1309` (0x51d).

The corpse's saved `__assert_rtn` frame at `0x1040f7420`, and its prologue's
callee-save slots, recover context `0x7496ef8000` and surface `0x74977b5dc0`.
The context's runtime vtable identifies SWContext; slot `+0x308` resolves to
`SWContext::create_surface_from_iosurface` at static `0x1846c253c`.
Surface flags `+0x64=0xf` select context list `+0x60`; that complete list has
one other surface, `0x74977b5ce0`, and not the target. The complete `+0x68`
list likewise lacks the target. The wrapper has dimensions 1179×2556,
stride 4864, CPU base `0x7ee1400000`, IOSurface pointer `0x74971119c0`.
These are **corpse-memory facts**, not a call-time factory-return capture or
a new source-to-HMP identity proof.

Static comparison supplies the next producer discriminator. The SW IOSurface
wrapper (`0x1846c253c..0x1846c25f8`) locks the IOSurface, obtains base/stride/
size, calls `create_surface_from_data`, stores/retains IOSurface at surface
`+0xc0`, and returns without tracking it in a context. In contrast, normal
SW creation calls `Context::track_surface` at `0x1846c2374`, and Metal's
IOSurface wrapper calls it at `0x184568a6c`. `track_surface` inserts into
context `+0x60/+0x68`, following the exact same flag partition as
`push_surface` (`0x18450366c..0x184503688`). Missing registration is therefore
the leading concrete software-path hypothesis; no assertion, pool membership,
completion, pixels, or guest binary was forced. Next: a minimal live join of
SW factory return → wrapper pointer/list state → the matched push/assert,
without the global geometry getters. Probe timing sensitivity remains open.

Other same-boot records include Setup PID 431's 10-second scene-update
watchdog (`+0x2158c000`) and MercuryPosterExtension PID 429 SIGILL
(`+0x1ea88000`). Their exit times are not paired to this gesture and they are
not substituted for the measured backboardd assert.

P434 child SHA-256
`cdb04c6ed609607ed5061bb8ede9abcce321731fb358d03a08db7494a50d270b`;
zero kernel panic/critical-reboot lines; `qemu-img check -U` passed. Durable
records (including complete RAM, input/callback logs, HMP timeline, static
proof and reproducible `ca_surface_tracking.py` output) are under
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN434_P276_CS_COMPLETION_FROM425/`.


### P435: pre-dismissal coverage cut; retry P436 retains the accepted gate

P435 (`UI_CLASS16_REFRESH60_DM_PLUGIN435_P276_SW_TRACKING_FROM425`) used
exactly the P425/P276 parent/TC and unchanged natural A408 host sink. Shell
service and native input initialization returned; one ordinary Home wake
was ACKed (down/up 42.959/7.168 s). No natural A408 frame or accepted
dismissal preceded the diagnostic cut, so the four deferred SW factory/
return/push/assert sites **never armed**. HMP `stop` interrupted the active
LLDB batch during a startup snapshot attempt; LLDB exited and detached
before that first capture. This is coverage loss, not a renderer or
completion negative. The subsequent held-controller capture preserved all
12 GiB under `startup-coverage-loss-snapshot/`.

The frozen roots validate userspace names for backboardd PID 72,
SpringBoard PID 326 and dvm-input PID 85. Examples include backboardd's main
queue in CoreMotion/libdispatch kqueue processing and SpringBoard sampled
in `__xzm_xzone_malloc_freelist_outlined`; the latter's saved FP chain
immediately reaches non-code data, so it cannot name a reliable caller or
permanent allocator stall. No Setup process candidate was found. The
snapshot contains startup watchdog/extension-timeout records, without a
matched P434-style backboardd ownership-abort record. A single snapshot
cannot assign those records as the no-frame cause. Complete evidence is in
`analysis.process-summary.json`, `analysis.selected-thread-symbols.json`
and `oskcdata-full.txt`; task generation/TPIDR reuse remains excluded from
cross-time object joins.

P435 child SHA-256
`d97d91750f2fe3daf919c722228d6a7c0298312045d32ed2cdc4948b6eef3bab`;
`qemu-img check -U` passed, zero kernel panic/critical-reboot serial lines.
The child, logs, source pack, frames and complete RAM are durable under
`~/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN435_P276_SW_TRACKING_FROM425/`.

The host trigger now creates the empty relay event stream on a clean
child. P436 restores the earlier one-shot Home wake after native-input
readiness if no rendered screen exists, then **still** waits for recent
natural lower-screen content before dismissal and post-accept probe arms.
It does not force UI state or call workspace/completion methods. Historical
frame-sampler `home_return_seen` meant **Home-entity constructor return**,
not recognizable Home UI; new rows use `home_entity_return_seen`.
No ownership repair, guest binary, geometry or model change was applied.

### P436: bounded pan-terminal gap; live ownership chain still unscored

`UI_CLASS16_REFRESH60_DM_PLUGIN436_P276_SW_TRACKING2_FROM425` completed its
condition-bounded run and archived the child. The exact P425/P276 parent,
helper TC, model environment and normalized launch argv match P434
(`analysis.launch-parity-P434.json`). Shell returned at `1792267533.624`;
native input became ready by `1792268483.914`. The ordinary Home wake and
all 34 Recap packets were ACKed. The first Recap touch alone took 37.061 s
before ACK (`visual-ready-relay.log`, sequence `1792269420519`); this is a
measured input delay, not proof of a cancellation or a guest timeout.

Natural A408/HMP content appeared before the swipe: frame `a408-0000-02`
has source/HMP RGB SHA-256
`9bb2be25db30a3ab25a1e1e1adce729f09fd93e72e7789017e423a12b43511bc`.
There are 367 timeline rows, 362 captured frames and 5 capture-error rows.
The accepted CoverSheet recognizer `0x791ce27200` on guest TPIDR
`0xffffffe1ef9f5120` reached shouldBegin=1, began, and changed. There was
no `HOME_GESTURE_PAN_TOUCHES_ENDED`. The lock-manager transition at
`1792269474.785` had `for_user_gesture=1`, followed 140 ms later by a
non-user transition. The strict same-pan end/transition join remained
false. A terminal cancellation boundary was not instrumented, so this
cannot distinguish cancellation from an unobserved end callback.

The four ownership sites never armed: **no live factory→return/list→push/
assert verdict**, and no repair was applied. The final HMP again has its
prompt/info/Home indicator near the bottom; this is not proof of completed
dismissal or Home. The first touch delay and missing terminal callback
justify one narrow end/cancel capture, with one bounded settled retry;
they do not justify weakening the completed-dismissal gate. Static
`0x22436f004` is correctly `SBHomeGesturePanGestureRecognizer touchesEnded:`.
Optional new diagnostic boundaries are UIKit pan cancellation
`0x184ecf5cc`, screen-edge touchesEnded `0x184f31234`, CoverSheet gesture-end
`0x22467ca6c`, and its state-getter return comparison `0x22467cb38`.
These observe raw callbacks/state without calling setters or methods.

Child SHA-256
`5b9fc503bed5b2921d719173f570a85ddfa4526fa1653583efc64d2305185db5`;
`qemu-img check -U` passed; zero kernel-panic/critical-reboot serial lines.
Evidence: durable P436 recovery directory, `analysis.coverage-verdict.json`,
`events/lock-gesture.jsonl`, `visual-ready-relay.log`, source pack and HMP
frames. A408 scanout remains unchanged. The ownership repair plan remains
unapplied pending a complete live identity/list join.

The P436/P434 raw lock-manager comparison is retained as
`analysis.lock-transition-vs-P434.json`. At the user-gesture boundary,
P436's `_isUILocked` byte is `01` and d0/d1 are zero; P434's byte is `00`
and d0 is `1`, d1 about `1.00158`. Symbol `0x224c49560` is
`lockScreenViewControllerWillPerformTransitionToFinalValue:forUserGesture:withVelocity:animated:`;
d0 is the **final value**, not an intermediate progress sample. This
difference reinforces the unconfirmed-dismissal classification without
assigning cancellation or security-state causality.

### P437: live IOSurface factory → missing ownership → push assertion closed

`UI_CLASS16_REFRESH60_DM_PLUGIN437_P276_SW_TRACKING3_FROM425` retained the
exact P425/P276 parent, TC and natural A408 profile. The initial Recap touch
timed out at the host's 120 s ACK bound, then the guest logged its stream
creation/touch ACK; it had not finalized/submitted a pan. The bounded
retry completed the retained stream and reached the strict same-recognizer
shouldBegin→began→touchesEnded→transition join at `1792272200.890102`.
The inherited screen-edge and CoverSheet end boundaries ran too; no
cancellation was observed for that completed pan. Initial timeout is not
a cancellation verdict. Ordinary post-dismissal Home down/up were ACKed.

All four SW sites armed after that transition (source SHA-256
`0d13f389d250a2c213892627a5d54e9aa677fb735a9b03c5fb4560b9b178e880`).
On backboardd TPIDR `0xffffffe57651a0c0`, call `0x18455f238`
(`1792272251.835368`) passes Context `0x78acf94000` and IOSurface
`0x78ad161640` to the reviewed SW factory `0x1846c253c` (runtime
`0x19ed1a53c`, exact 192-byte reference hash). Return `0x18455f23c`
produces Surface `0x78acffc8c0`; the same caller SP `0x105cf3f90`, Context,
IOSurface and Surface reach push `0x18455f258` at `…2251.855395`.
The Surface's `+0xc0` points to the original IOSurface, flags remain `0xf`,
and both tracking lists have complete null-terminated walks excluding it
at return and push. The selected list is Context `+0x60` (two other
Surfaces); `+0x68` has one other Surface. Then the same pair reaches
`found != slist->end ()` at `0x18445e2cc` / runtime `0x19eab62cc`
(`…2251.863090`). Dimensions/stride are 1179×2556/4864.

`analyze_sw_surface_tracking.py` validates **one complete live join and
one missing-registration-plus-matched-assert**. This closes the prior
corpse-only ownership gap. The run stopped on that named assertion, with
no repair applied. It does not prove Home success or assign the Surface
nonblack content; the established natural scanout path is unchanged.
Child SHA-256 `214e747826be7e4ff83c244e0d3a83303b906596ed0165e3047fb0b33e62eba2`;
clean `qemu-img check -U`, zero kernel-panic/critical-reboot lines. Durable
records include source pack, events, HMP timeline and
`analysis.live-sw-tracking.json` / `analysis.coverage-verdict.json`.
Next is a disposable compatibility child calling the existing native
`Context::track_surface` on the original nonnull SW factory result; retain
original allocation/IOSurface ownership, assertions, rendering and A408.

P438 completed that child but produced only `SW_SURFACE_TRACKING_READY` after
the accepted-dismissal marker. No watched factory/return/push/assert boundary
was sampled, so it cannot be scored as either a repair pass or failure. The
first unresolved edge is before factory entry; no Context/Surface/list
comparison was reached. The P438 probe silently discarded non-`backboardd`
hits at shared-cache addresses. A narrow follow-up diagnostic now records a
capped first hit for each `(site, PID, process)` before that filter and logs
resolved breakpoint locations; it does not alter the guarded repair or the
five watched addresses.

The same P438 run acknowledged Home input and captured 428 natural A408 rows
over 289.36 seconds after Home, including six exact full-frame source/HMP RGB
matches. The last source and HMP hashes both equal
`88ab7a377c999919581dce92938ef087eca846eaf8fdf3e2dc789e9de2a56ebd`. The
final screen is Setup Assistant's language chooser, not Home. The user-visible
path therefore progressed after dismissal while the SW ownership boundary
remained unobserved. P438 artifacts, child SHA, clean qcow check and empty
panic scan are under the durable `PLUGIN438_P276_SW_REPAIR1_FROM425`
recovery record. The conditional unpatched control remains deferred because
P438 did not pass the ownership boundary.

P515 did not contradict the on-canvas Appearance Continue measurements. The
P515 child was a P514 descendant that presented `BuddyLanguageController` on
the key window after an accepted lock dismissal. Its generic forward-button
scan found zero button actions because the current page is a language table;
no action was dispatched. The first walk also hit a missing `_call` helper in
the view scanner, which is corrected in the current source to use its existing
`arm_call` routine. Neither observation is a Continue failure. The final P515
frame is a separate dark Cover Sheet state, not an Appearance frame.

P516 continues from a fresh child of P514. It loads a dedicated, capped
language-row probe alongside the previously validated native Appearance and
multilingual Continue probes. The English row is selected only if the live
Buddy controller is attached to the key window, its `UITableView` is attached
to that same window and delegated to that controller, and row `(0,0)`'s live
cell is `BuddyLanguageLocaleCell` with decoded text exactly `English`. It
then asks UIKit's table selection path to notify the delegate. There is no
OCR gate, scroll, coordinate tap, controller-state write, or display change.
The probe has passed Python syntax checks and its installer ran on P516, but
the language-row scan itself was not exercised on a live controller.

P516 did not produce a Setup-navigation verdict. Its LLDB log contains the
P515/P501 lifecycle probes and the P516 language-row `READY` marker, but no
P505 `PROBE_ARMED` or `SETUP_RUNLOOP_CONTEXT_SELECTED`: the composed installer
wrapped P505's `_finish_view_scan` and `_after_getter` callbacks without
calling `p505.install`, so no live controller scan could invoke the wrapper.
The Home wake key and all 32 Recap swipe events were acknowledged, but the
only transition record had `gesture_sequence_confirmed=false` and
`dismissal_gesture.confirmed=false`; the strict accepted-dismissal gate never
opened. The child was stopped as a probe-coverage miss, not a Setup failure.
Its clean image check, logs, frame samples and source pack are in the durable
`UI_CLASS16_REFRESH60_DM_PLUGIN516_NATIVE_ACTION_FROM514` record. The combined
installer now arms P505 before P516 wraps its scanner; the next child must
show P505 runloop readiness and a genuinely accepted lock dismissal before
any language-selection result is scored.

P517 validates that corrected composition and reaches the strict accepted
lock-screen dismissal marker. P505's armed runloop scanner resolved
`SBCoverSheetWindow` before the swipe, then observed `SBHomeScreenWindow` with
`SBHomeScreenViewController` at `1792437298.850`, followed by
`SBMainSwitcherWindow` / `SBSwitcherViewController` at `1792437360.471`.
Across the recorded key-window walks, no attached Buddy controller or
Appearance/Language action was found; the separate P516 language selector
therefore did not fire. This is a state/window coverage outcome, not a failed
Continue or English selection. P505 still could not resolve
`UIApplication.windows`, so the walk is limited to the key window and does
not establish that no other app/window exists. One subsequent native Home-key
relay was acknowledged, but no later controller sample was recorded. Natural
A408 timeline output remained empty and sampled HMP frames were uniformly
black in this child; this does not revise the previously proven scanout path.

P517's child `ui-class16-refresh60-dm-plugin517-native-action-from514.qcow2`
is durable under `boot1/`, SHA-256
`177a698c742cb7a7f925930808b84d9fd02b1b1e87421ecfdb39ece8a3840e96`;
`qemu-img check -U` found no errors and the panic scan is empty. Its accepted
transition, relay log, frame samples, HMP images, LLDB/serial logs and source
pack are in `recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN517_NATIVE_ACTION_FROM514`.
The next experiment is based on the durable P486 Appearance checkpoint and
will invoke only the same measured native Buddy action when that live
controller/action is present.

P518 is a valid accepted-dismissal run, not a Continue failure. The matched
Cover Sheet pan ended at guest time `1792438191.762`; the accepted transition
was recorded at `1792438196.404`. The native Continue module was loaded, but
no Appearance target/action result was produced before observation stopped.
The lifecycle rows after acceptance cover only about 84 seconds and show
poster/switcher and widget-renderer controllers; they do not show a live Buddy
Appearance controller. P505's auxiliary inventory resolved only the key
`SBCoverSheetWindow`, and its setup-runloop context changed after the scan
window, so its empty result is not an all-window or all-thread negative.

P486 provides the relevant timing control: its accepted dismissal was at
`1792377277.261`, and the first stable Appearance frame was at
`1792377667.748`, about 390 seconds later. P518 ended more than five minutes
before that comparable Appearance arrival. Its durable child
`ui-class16-refresh60-dm-plugin518-native-continue-from486.qcow2` has SHA-256
`681f10f873c4a05d4cddce4c92b8da1607715ac88613618b1ec49f1d723b5f8f`; the image
check passed and the panic scan is empty. The corrected next cut is a full
post-dismissal window on a fresh P486 child. It keeps the proven native
`BuddyAppearanceController.continueTapped` registration/enabled/transition
guard and invokes only UIKit's registered action when that exact live control
is current. OCR and scrolling do not gate or attempt the Continue action.

#### P519: button hierarchy and blank source pixels are separate evidence

P476/P485's UIKit result does not conflict with the Appearance screenshots:
the enabled, on-canvas `OBBoldTrayButton` and registered
`BuddyAppearanceController.continueTapped` action are present in UIKit, while
the source frame's white tray region lacks the rendered title. OCR and another
scroll therefore cannot decide whether the native action exists; the live
registered action is the right continuation path once that controller is
current.

P519 did not reach that action gate. Its durable child
`ui-class16-refresh60-dm-plugin519-native-continue-from486-fullwindow.qcow2`
has SHA-256
`7f029ae25f91f1a85c260e03e9d661619f8b22438ba49052cfce23abf442f560`, passed
`qemu-img check -U`, and has an empty panic scan. Two 32-step Recap swipes
were host-acknowledged, but there were no guest CoverSheet-dismiss-began,
pan-ended, or accepted-pan events; the transition marker had
`gesture_sequence_confirmed=false`. The final sampled HMP frame was the black
Lock screen with its swipe prompt, so P519 is a pre-Appearance input/coverage
miss, not a Continue or rendering verdict.

P519 did log `BuddyLanguageController` push/view lifecycle events, including
`viewDidAppear`, but its boot-captured
`p516_native_language_row_callbacks.py` SHA-256 was
`e998f6a2d0faf3934636a9a0a1592b81efd900e362699ef1cac554483dda6440` and
predates the direct-controller view-scan fix currently in the workspace
(`eb3b545df91bbb4f39d1face227980350a59af560d7f07480badc3e3945ecbd0`). No
language table scan/English selection, Appearance action gate, or native
Continue result was produced. P520 uses the durable P519 image as its parent
and loads the corrected probe before boot. Runner:
`tools/re/run_plugin520_native_continue_from519_langviewfix.sh`.

#### P520: accepted pan, but the native Continue action was never reached

P520's guest accepted the CoverSheet pan at `1792441283.698`; the recognizer
began and ended the same gesture, and the accepted-transition marker confirmed
the sequence. The durable HMP frame at `1792441805.443` (about 522 seconds
later) still shows the Lock Screen swipe prompt. This is not an Appearance
Continue failure: no `BuddyAppearanceController` action candidate or
`native-continue-result.json` was produced, and no additional input was sent.

P516 observed a `BuddyLanguageController` `viewDidAppear` and armed its direct
`view` getter (`controller=0x78e32f4000`, SP `0x16b2beb50`, TPIDR
`0xffffffedb3480950`, IMP `0x1a07fd938`) at `1792441387.168`. There is no
matching `LANGUAGE_DIRECT_VIEW_SCAN_BEGIN`, English-row selection, or recorded
getter return. The getter was redirected from the lifecycle breakpoint, but
the old return matcher only ran at P505's separate runloop-observer PC. That
left `p505.STATE.stage` occupied and prevented the attached Buddy scan and
forward-action pass from advancing. A later `BuddyLanguageController
viewDidAppear` used another instance (`0x75a72ec000`) at `1792441504.011`; it
does not prove the earlier getter returned or that the visible page changed.

P520 was stopped after this probe gap was established. Its durable child
`ui-class16-refresh60-dm-plugin520-native-continue-from519-langviewfix.qcow2`
passed `qemu-img check -U`, and its panic scan is empty. Natural A408 scanout
stayed enabled and unchanged. P521 handles the redirected getter's return at
the exact saved lifecycle PC/SP, then continues through the measured English
UITableView selection and the registered native Appearance target/action.
P476/P485's on-canvas UIKit action evidence remains the reason not to OCR-gate
or scroll for Continue.

#### P522: P486-child boot did not test Appearance Continue

P522's child of the P486 disk did not expose a live Buddy Appearance page or
the measured `OBBoldTrayButton` target/action to the callback. It therefore
produced no Continue result. This is a coverage/lineage miss, not a failure of
the native action. The P486 frame shows the Appearance preview, accessibility
chip, and selected Default option; P476/P485 independently measured an enabled,
on-canvas Continue control registered to
`BuddyAppearanceController.continueTapped` for `TouchUpInside`. A white or
unreadable title in HMP does not revoke that UIKit registration, so OCR and
scrolling are not prerequisites for advancing.

P523 returns to the known-positive fresh-child route from P483. The existing
driver replays only the prior language/country selection steps. After its
country-to-Appearance gate opens, the P505 scanner matches the live button,
current Appearance controller, registered selector/event, attached window,
and enabled state before invoking UIKit's native
`sendActionsForControlEvents:`. It writes an action-result record from the
same callback return. Generic Buddy forward actions remain disabled until
that exact Appearance action returns; then later enabled native controls may
continue Setup. Continue dispatch is not gated on OCR or scrolling.

Correction after the first P523 bootstrap attempt: LLDB rejected its callback
registration because the wrapper monkey-patched a P505 callback with a Python
function owned by a different module (`could not find callable:
p505_live_native_setup_action.runloop`). The guest was stopped after this
instrumentation error; no native action candidate or Continue result was
scored. P523's child passed `qemu-img check -U` and had no panic record. P524
moves the action-start gate, post-return generic-action policy, and result-file
write into P505 itself, leaving LLDB's callback function in its defining
module. It retries from a fresh P483 child; P523 is not an action verdict.

P524 reached shell return and armed the existing country/input observers, but
P505 then raised `NameError: os is not defined` while reading the gate
environment. The action scanner therefore remained uninstalled; P524 was
stopped immediately and is not an Appearance-action result. P525 includes the
missing `os` import in P505 and repeats the same fresh P483 route. The exact
native action remains gated only by the live controller/registration/window/
enabled checks, not by screenshot text or a scroll.

P525 confirmed the next loader boundary: LLDB still could not resolve
`p505_live_native_setup_action.runloop` because P523's wrapper used ordinary
Python import rather than LLDB's `command script import`. No P505 runloop scan
or Appearance action occurred. P526 explicitly loads P505 through LLDB's
script-module importer before calling `install`, so its callback remains
resolvable in the defining module. P525 is another callback-setup failure,
not a Setup or Continue verdict.

#### P543: Cocoa mouse relay reaches Home; Settings launch remains unproven

P543 is a fresh child of the P540 Home-positive disk, with natural A408 scanout
and the Cocoa display unchanged. The guest's legacy `DVM_INPUT_READY
protocol=1` helper was paired with `tools/input/relay.py --backend recap`,
which relays QEMU absolute-pointer events as native Recap touch records. The
first upward swipe woke the dim Lock Screen (bottom controls appeared); a later
upward mouse gesture changed the HMP frame to the Home grid. This demonstrates
that the Cocoa/QMP mouse event path can reach the guest's touch helper and
produce a visible Lock-to-Home change. No gesture callback was armed, so this
is a visual/input result, not an internal accepted-transition trace.

The Home frame contains visible clock/date, widget text (`No Events Today`),
and Search text. App-name labels, including `Settings`, are absent from the
source frame, although the Settings gear icon is visible. A tap near the gear
and a corrected tap at its measured center (742,1988) framebuffer pixels were
both acknowledged by the guest helper. The corrected tap produced a transient
screen transition, but the settled HMP frame returned to Home; a Settings
screen or app launch was not established. Helper ACKs prove native event
submission only. P543 therefore answers that some text is visible and mouse
gestures can affect the UI, but does not yet prove Settings is launchable by
icon tap. The live Cocoa guest was left at Home with the relay running for
direct interactive follow-up; do not run `qemu-img check` until it is stopped.

The child is `ui-class16-refresh60-dm-plugin543-native-home-cocoa-mouse-recap-from540.qcow2`.
Frames, launch manifest, and captured logs are archived under
`recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN543_NATIVE_HOME_COCOA_MOUSE_RECAP_FROM540/`.

P543 live follow-up correction: a raw HMP PPM screendump was initially
converted directly to PNG and looked diagonally skewed. That appearance is a
known export artifact: this QEMU PPM writer emits 3540 bytes per row for a
1179-pixel RGB image whose packed row is 3537 bytes. The three padding bytes
make a normal P6 reader misalign rows; this is not evidence of a guest layer
transform. A direct HMP PNG capture (`screendump … -f png`) at host time
2026-10-19 21:24:23 PDT is upright and shows the normal Home grid, clock/date,
widget text, and Search, with app-name labels still absent. Its 1179x2556
SHA-256 is `442289ff1c28fb0066241b4ff42baf295f8d39ce77c5136c5b9959b057b59fa5`.
The raw PPM follow-up files are invalid for visual scoring. A later QMP tap
retest was not delivered: the original relay had exited, no guest input ACK
was produced, and a direct release plus helper ping timed out. Therefore that
retest has no UI result; the earlier P543 Settings taps remain helper-ACKed
but lack a SpringBoard/app-launch observer. The guest remains live, so its
image check is deferred until shutdown. Capture metadata is in
`recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN543_NATIVE_HOME_COCOA_MOUSE_RECAP_FROM540/live-followup-corrected.json`.

P543 relay-reconnected tap follow-up supersedes the unscored late-retap note
above. The continuous Recap relay was restarted and QEMU reported its UART
chardev connected. A Settings-center press/release at framebuffer pixel
`(742,1988)` (normalized input `(20622,25500)`) received helper ACKs for both
edges. Direct HMP PNG captures at 3s, 20s, 60s, and 75s after the tap all show
the Home grid; the 3s/20s captures are byte-identical, and the 75s frame is
again the normal complete grid. No Settings page or app-label text appeared.
This closes the input-helper delivery attempt, not UIKit hit-testing or app
launch: no SpringBoard target/action observer was active. The HMP PNG RGB
hashes match the corresponding natural A408 `rgb_sha256` records in
`/tmp/dvm/P543_MOUSE_RECAP1/stderr.log` (lines 252189, 256044, 257571, and
258267), confirming these are the guest scanout pixels. The current frame is
archived as `frames/live-continuation.png` with RGB hash
`b2f0833bc76181c39be9aab5482bd0decf204a1bf70598d1c40acb294413a582` and
matches A408 log line 258267. The guest is still running; image integrity is
deferred until it is shut down. Full frame hashes and the relay ACK rows are in
`settings-tap-reconnected-followup.json` under the same recovery-record folder.

P543 finalization correction: the native guest was subsequently quit cleanly
after the relay follow-up. Its final child check passed; the archived launch,
input, and screenshot records remain valid. No Settings app launch was
observed, so the tap result remains “helper accepted the event, UI action
unproven.” P544 separately verified by read-only guest memory that the
reviewed Settings image-scale replacement and its CodeDirectory hash slot are
already present in the P540 parent. P544's guarded installer made no writes.
The next useful test is a fresh child of P540 with the known Home profile and a
SpringBoard hit-test or app-launch discriminator; do not reuse P543 or stack a
duplicate cache patch.

#### P545: Lock Screen and helper readiness reached; input transport lost

P545 is a fresh P540 child with the same TC, Cocoa profile, and natural A408
scanout. It reached an awake Lock Screen; HMP PNG `frames/lock-ready.png`
(1179x2556, SHA-256
`900a0d0fbda777945676e4655d5e50c4fd42d31ac1ec04bbcee5f1d225aa7bb6`) shows
the clock, bottom controls, and Home indicator. The clock changed in later
HMP captures, so the guest continued presenting Lock Screen frames.

The v7 input helper emitted `DVM_INPUT_HOME_READY` and
`DVM_INPUT_READY protocol=1` at serial lines 93672 and 93684, about 35 minutes
after QEMU launch. Before trying the accepted upward swipe, the already
connected long-lived relay was stopped. This disconnected QEMU's
`input_uart` chardev. Subsequent one-shot swipe and release packets were
logged as sent by the host relay but received no guest ACK; QEMU's `info
chardev` reported `input_uart` disconnected, and the serial log contains no
matching `DVM_INPUT_RECAP` or `DVM_INPUT_ACK`. Those packets therefore have no
guest/UI result and P545 does not test dismissal or Settings launch.

The child was stopped and passed `qemu-img check -U`; no panic marker was
found. Artifacts are under
`recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN545_SETTINGS_TAP_FROM540/`.
For the next child, establish the persistent relay after QEMU creates its UART
socket and keep that same connection open through helper readiness and input.
Do not reconnect or interpret a host-side send log as guest delivery.


#### P546: Lock Screen present; Recap input ACK was delayed, then transport stopped consuming

P546 is a fresh P540 child with the same TC, Cocoa display profile, and natural A408 scanout. It reached `DVM_INPUT_HOME_READY` and `DVM_INPUT_READY protocol=1`; HMP showed the awake Lock Screen, and the clock advanced from 10:54 to 10:56. The first swipe's host relay deadline was too short: its initial Recap down packet was ACKed by the guest only after the relay's 10-second timeout. The remaining swipe motion never reached the guest.

A one-shot release on a new connection was ACKed and completed Recap lift/finalization. A second persistent watcher used a 60-second ACK bound, but after sending the next Recap down it received no ACK; guest serial showed no new input-stage record. A later non-touch protocol ping also timed out. No duplicate gesture was sent. The final HMP PNG remained the Lock Screen; no dismissal or Settings launch is claimed. The child was closed cleanly and passed `qemu-img check -U`; no panic/reboot was observed. Full action/relay/serial/frame records are in `recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN546_SETTINGS_TAP_PERSISTENT_RELAY_FROM540/`.

This narrows the input issue: the guest can receive and submit the first delayed Recap event and a release, but input consumption stopped after reconnect; the exact receiver/transport boundary remains unproven. Next, test one fresh P540 child with the persistent relay connected only after `DVM_INPUT_READY` and a 120-second first-ACK deadline, keeping that connection open throughout. Do not score host-side events without guest ACK and an HMP UI change.

#### P547: delayed relay connection never reached helper readiness

P547 was a fresh P540 child with the same TC, Cocoa display profile, and
natural A408 scanout. Unlike P546, no input relay was connected during boot;
the plan was to attach after `DVM_INPUT_READY` with a 120-second ACK bound.
After about 53 minutes, the helper had emitted `DVM_INPUT_START` and
`DVM_INPUT_INITIALIZING`, then reached `dlopen.begin` for
`Recap.framework` (serial line 704), but had emitted neither the matching
`dlopen.return` nor `DVM_INPUT_READY`/`DVM_INPUT_HOME_READY`. No touch packet
was sent, so P547 has no input result.

An idle relay connected after the first 40-minute observation; QEMU accepted
the UART connection, but readiness remained absent for a further ten minutes
while guest serial/IOMFB power logs continued. HMP frames at 40 minutes and
ten minutes after the late connection are both black and byte-identical
(SHA-256 `d312521cf1e978b08337f0c936bdf71f2d7a61a3259dc5bd07792494c5cf42b9`).
This is a helper-readiness miss under delayed connection, not a touch failure
or an A408 regression. The guest was quit through HMP and the child passed
`qemu-img check -U`. Full records are under
`recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN547_SETTINGS_TAP_READY_RELAY_FROM540/`.

The next discriminator is a fresh P540 child with the persistent relay
connected before guest/helper initialization and a 120-second ACK bound. This
keeps the early-connection condition that reached readiness in P546 while
allowing its delayed first ACK time to complete. Send no duplicate event after
an ambiguous timeout; require guest ACK and visible HMP change to score input.

#### P548: early relay reaches Home; Settings launch remains unresolved

P548 is a fresh P540 child with the same accepted `system.tc`, Cocoa display, and natural A408 scanout. The Recap relay was connected before helper startup with a 120-second ACK bound. `DVM_INPUT_READY protocol=1` arrived after about 1605 seconds; one 32-step upward QMP gesture produced 32 sent/32 ACKed states. HMP showed the recognizable Home grid at 5, 30, and 60 seconds. Clock, widget content, Search, and app icons are visible; app-name labels beneath icons are absent.

A QMP mouse down/up at normalized `(20622,25500)` (about framebuffer pixel `(742,1990)`, the Settings gear center) was ACKed through the older Recap `R` path. Its delayed HMP capture remained Home; the file named `after-settings-tap-5s.png` was actually taken about 95 seconds after the tap because an ACK-count wait hit its 90-second deadline. A separate native one-shot `P` tap at the same point was ACKed in about 405 ms. HMP showed Home at 5 seconds, a mostly white surface with status chrome at 20 seconds, then Home again at 55 seconds. This transient frame does not identify the app or prove a crash; serial/QEMU logs contain no explicit SpringBoard/Settings crash record. Helper ACK proves submission only, not SpringBoard hit-testing or app-launch completion. Natural A408/scanout was unchanged.

P548 was quit through QMP; the child passed `qemu-img check -U` and no panic/reboot was observed. Child SHA-256: `94836cfe196c0db56d753ad7928897be42efa9617dfd7cdfb877e4bf3ad67a5a`. Frames, QMP actions, relay ACKs, serial, QEMU output, and manifest are archived under `/Users/steve/dvm-artifacts/bp-patched-dev6/boot1/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN548_SETTINGS_TAP_EARLY_RELAY120_FROM540/`. Next useful discriminator is a low-rate launch/scene observer on a fresh child, gated on the native Settings tap; another tap-only boot is not justified.

#### P549: native Settings tap opens a stable, readable Settings page

P549 is a fresh child of the durable P548 child, using the same TC, Cocoa
window, and natural A408 scanout profile. Home was visible before the tap. The
host issued 34 QMP pointer states for the upward gesture; the Recap relay
coalesced motion into 14 packets, all 14 ACKed successfully, including the
endpoint and button release. The driver incorrectly waited for 34 relay ACKs
and was stopped after its ACK-count timeout. We did not replay the gesture.
The HMP frame independently confirms Home (`Search` and the app grid), so this
is a valid Home-to-Settings experiment but not a claim that all 34 pointer
states were individually delivered.

A single native `P` tap at normalized `(20622,25500)` was ACKed successfully.
Five seconds later HMP still showed Home; at 20 seconds the Settings page was
fully visible, and it remained visible at 55 seconds. The screenshots are
1179x2556. OCR finds the `Settings` title and rows including Wi-Fi, General,
Accessibility, Appearance, Display & Brightness, and Home Screen & App Library.
This proves that the Settings icon can be tapped and the app's text is
rendered. The earlier absence of app-name labels on the Home grid remains a
separate guest-composition issue. Several Settings rows display blue question
mark glyphs or no leading icon; that is not a scanout failure.

The tap-gated observer recorded launchd spawning
`/Applications/Preferences.app/Preferences` with
`XPC_SERVICE_NAME=UIKitApplication:com.apple.Preferences[...]`; the matching
spawn returned `x0=0` with child PID 353. Preferences then emitted FBS scene
create, workspace-request, client-request, and scene-update callbacks. The
bitmap factory was reached with 28x28 logical dimensions, raw scale 1.0, and
non-null input CGImage pointers; the nil-CGImage trap did not fire. This is
consistent with the stable visible page, but does not identify the
question-mark row glyphs' source assets or prove every row icon loaded.

The driver helper's exact-ACK-count assumption is now corrected: it should
accept a coalesced gesture only after the terminal release is ACKed and all
relay packets have completed. P549's helper exit code 143 records the manual
stop after its bad wait; the gate probe returned 0, the guest exited via the
planned stop marker, the panic scan was empty, and `qemu-img check -U` passed.
Natural A408 scanout was unchanged. The durable child SHA-256 is
`1517fb9612d4078529a757f22d5770819ebea10bb4420a32f575eee761993667`; frames,
input ACKs, event log, LLDB trace, serial, stderr, and checks are under
`recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN549_SETTINGS_APP_TRACE_FROM548/`.

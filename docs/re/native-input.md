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

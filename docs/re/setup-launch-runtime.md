# Setup Assistant launch gate: runtime result (`UI_SETUP_GATE1`)

Run on 2026-09-03 13:06-13:11 with `tools/re/setup_gate_probe.sh UI_SETUP_GATE1`
(persistent-NVMe parent `sks-op09-complete-2.qcow2`, fresh child, `-S`, gdbstub
on 1234, IOMFB level 4 with the D120/D586 callback script).  The raw logs under
`/tmp/dvm` were lost to a host reboot minutes after the run; the values below
were read from `/tmp/dvm/UI_SETUP_GATE1.lldb.log` while it existed and are
recorded here from that reading.  Rerunning the script reproduces them.

## Same-boot slide and callback proof

`tools/re/resolve_live_dsc.py` stopped the guest at runtime PC `0x252353ec4`,
matched its 8 words exactly once in `dyld_shared_cache_arm64e.40` (file offset
`0x78cfec4`, static PC `0x237ccfec4`), giving slide **`0x1a684000`**, runtime
cache base `0x19a684000`, `gva2gpa 0x100005d0000`.  This differs from both
earlier boots (`0x9b98000`, `0x1dcf4000`), as expected.

All 15 breakpoints were installed with `breakpoint command add -F
setup_gate_callbacks.on_break` and each printed a `COMMAND_LIST_PROOF` block
showing the Python callback (the `-o ... -o continue` trap in
`docs/re/lldb-breakpoint-command-trap.md` is avoided).  The callbacks are
bounded multi-hit (16 or 32) and are never disabled on first hit.

The guest reached `Early boot complete` (serial line 641), 0 `panic(cpu`, 0
`Copying`, 1,246 serial lines at freeze; the screendump at freeze was 1179x2556
with a single pixel value (black).

## What the 193 hits prove

| Site (static) | Hits | Value |
|---|---:|---|
| `0x1cac11c18` `_BYSetupAssistantNeedsToRun` entry | 16 (then bounded off) | callers below |
| `0x1cac11c44` non-UI predicate | 16 | `w0 = 0` every time (passes) |
| `0x1cac11ca0` `ForceNoBuddy` | 16 | `w20 = 0` every time (passes) |
| `0x1cac11fc8` after `_MGGetSInt32Answer(DeviceClassNumber)` | 16 | **`w0 = 1`** every time |
| `0x1cac11d20`/`d28` cached `isSupported` byte | 16 | `01` after the first evaluation (`00` before it, once token 0) |
| `0x1cac11d30` after `_LaunchSentinelExists` | 16 | `w0 = 0`: `sem_open("purplebuddy.sentinel", 0)` returned -1, sentinel absent |
| `0x1cac11d44` internal-content check | 16 | `w0 = 0`, so the function returns `w20 = !sentinel = 1` directly |
| `0x1cac11cf0` return | 32 | **`w0 = 1` in all 32** |
| `0x1cac11df4` unsupported-device edge | 0 | never taken |
| `0x1cac23394` `_BYSetupAssistantPrepareLaunchSentinel` | 0 | never called |
| `0x224c4fb78` SpringBoard `_SBWorkspaceActivateApplication` (bricked path) | 0 | never called in 300 s |
| `0x1af732264` MobileGestalt helper | 1 | `w21 = 0x16`; a different caller, not SetupAssistant's query |

So the hypothesis in `docs/re/setup-launch-gate.md` is refuted: **the device-class
gate passes** (`DeviceClassNumber = 1`, in `{1,2,3,4,7}`) and
`BYSetupAssistantNeedsToRun()` answers **YES** to every process that asks.

## Who asked, and who did not

The 16 entry hits came from 8 distinct threads (distinct `tpidr_el1`), 7 of
them main threads (`sp` in `0x16b..0x16e`) each calling twice from two return
addresses whose page offsets are identical across processes
(`...e9d8` then `...eac4`, e.g. `0x1013fe9d8`/`0x1013feac4`,
`0x102a569d8`/`0x102a56ac4`, `0x1045b29d8`/`0x1045b2ac4`, ...), i.e. one
non-cache image mapped at a different slide in each process; the eighth
(`lr 0x103887fa8`, `sp 0x106c86890`) is a different image on a secondary
thread.  All `lr` values were outside the shared cache.  None of the 16 came
from SpringBoard's own call site `0x2246d386c` (`-[SBSetupManager
updateInSetupMode]`, below), but the entry breakpoint was bounded off after 16
hits while the return breakpoint went on to 32, so SpringBoard's evaluation is
**not excluded**, only unobserved.  The next run records `__progname` per hit
(`**(slide + 0x1e6ef1590)`, libsystem_c `___progname_pointer`, read by
`__NSGetProgname` at `0x187f00bb4`) to name the callers.

## SpringBoard's launch logic (static, iOS 27 b8 cache)

`-[SBSetupManager updateInSetupMode]` (`0x2246d37f0`):

```
if ([lockdownManager isInLostMode] && [self hasPasscodeSet]) -> reason 0
else if ([x30 isMultiUserSupported] && [x30 isLoginSession]) -> reason 0
else if (BYSetupAssistantNeedsToRun())            -> reason 1   (0x2246d3868)
else if ([x20 brickedDevice])                       -> reason 2
else if (self->_migrating /*+0x41*/ == 1)          -> reason 3
else                                                -> reason 0
[self _setSetupRequiredReason:reason]               (0x228095e60)
```

Log strings: "inSetupMode = YES because BYSetupAssistantNeedsToRun() returned
YES" at `0x224e1624c`; the selector names come from the `objc_msgSend$` stubs
at `0x2280be090` (`isInLostMode`), `0x228071d80` (`hasPasscodeSet`),
`0x2280be8e0` (`isMultiUserSupported`), `0x2280be670` (`isLoginSession`),
`0x2280a5440` (`brickedDevice`).

`-[SBLockScreenManager _maybeLaunchSetupForcingCheckIfNotBricked:]`
(`0x224c4f7bc`): the block at `0x224c4faf8` that calls
`_SBWorkspaceActivateApplication([setupManager setupApplication])` runs only if
`[[self lockdownManager] brickedDevice]` is true, or if the BOOL argument is
set and `[setupManager updateInSetupMode]` returns nonzero.  Its log string is
"Activating Setup because we think we're bricked." (`0x224e5801a`).  This is
the bricked-device path, not the ordinary first-boot path, so its silence is
expected; the ordinary path goes through `_setSetupRequiredReason:` and was not
yet instrumented.

## Open questions

| Question | Observation that settles it |
|---|---|
| Does SpringBoard evaluate `updateInSetupMode` at all, and with which reason? | hits at `0x2246d37f0` (entry) and `0x2246d395c` (`x2` = reason) in a run where the entry breakpoint is not bounded off before SpringBoard runs |
| After reason 1, which code activates `Setup.app`? | callers of `_setSetupRequiredReason:` consumers; `SBWorkspaceActivateApplication` call sites in SpringBoard other than the bricked block |
| Which 7 processes call the gate twice from the same image? | `__progname` per hit in the next run |

## SpringBoard's ordinary launch path (static, from the full framework dump)

`ipsw dyld disass --image SpringBoard.framework/SpringBoard --quiet` plus
`a2s` on every `bl` to the `objc_msgSend$updateInSetupMode` stub
(`0x2280ea450`) and to `_SBWorkspaceActivateApplication` (`0x22433ff18`):

| Site | Function | Role |
|---|---|---|
| `0x2244d2748` | `-[SpringBoard applicationDidFinishLaunching:]` +7076 (entry `0x2244d0ba4`) | first `updateInSetupMode` |
| `0x2244d8dfc` | `-[SpringBoard _finalizeStartupAfterScenesDidConnect:]` +264 (entry `0x2244d8cf4`) | `updateInSetupMode` once UIKit scenes are connected |
| `0x2244783ac` | `-[SBMainWorkspace _selectTransactionForReturningToTheLockScreenAndForceToBuddy:]` block (`0x22447835c`; method `0x224478298`) | launches Setup if `[setupManager isInSetupMode]` (`0x2244783a0`) and `updateInSetupMode` bit 0, or if the block's `forceToBuddy` byte (+0x28) is 1; otherwise the lock-screen path at `0x224478414` |
| `0x2244783d0` | same block | `[applicationController applicationWithBundleIdentifier:SBBuddyBundleIdentifier]`; nil here means LaunchServices has no `com.apple.purplebuddy` |
| `0x2242e48cc`, `0x2242e4a58`, `0x2242e4a64` | `-[SBToAppsWorkspaceTransaction _didComplete]` (entry `0x2242e47ec`) | `updateInSetupMode`, `setupApplication`, `_SBWorkspaceActivateApplication` |
| `0x2243045ac` | `__SBWorkspaceCanLaunchApplication` +384 (entry `0x22430442c`) | launch gating |
| `0x224312da8` | `-[SBMainWorkspace _validateRequestToOpenApplication:options:origin:error:]` +556 | launch gating |
| `0x224479474` | `-[SBMainWorkspace _handleSetupExited:]` +44 (entry `0x224479448`) | fires only after Setup ran and exited |
| `0x2243bf920` | `-[SBApplicationController setupApplication]` | tail call to `applicationWithBundleIdentifier:` with `SBBuddyBundleIdentifier` (`0x268990448`) |
| `0x2246d3adc` | `-[SBSetupManager _setSetupRequiredReason:]` | stores the reason at +0x38, compares `isInSetupMode` before/after, notifies the delegate (+0x8) and posts a notification; logs "Setup mode state did change - required reason: %@" |

So the first-boot launch is not a direct consequence of
`BYSetupAssistantNeedsToRun`: SpringBoard must (1) reach
`_finalizeStartupAfterScenesDidConnect:`, which needs its UIKit scenes
connected (a FrontBoard/backboardd display dependency), (2) have
`updateInSetupMode` report reason 1, and (3) resolve `com.apple.purplebuddy`
through `SBApplicationController`.  `tools/re/sb_setup_path_callbacks.py`
breaks on every one of those edges with the process name per hit
(`CALLBACKS=sb_setup_path_callbacks tools/re/setup_gate_probe.sh <TAG>`).

### P150 runtime supplement: Setup mode selected; activation still open

On the P150 shell-return boot, SpringBoard's second observed ADFL invocation
called `updateInSetupMode`, received `x0=1`, and passed reason `x2=1` to
`_setSetupRequiredReason:`. This confirms that the runtime selected the
Setup-required branch described above. P150 did not observe a non-null Home
entity, but its setup-activation observers were installed late, so that quiet
window does not show whether an earlier activation attempt occurred. P151
missed the UI test because its DVMI2 bootstrap helper was rejected by AMFI
under the wrong trust cache. P152 uses the merged input trust cache and moves
setup/activation/ADFL-exit observers to the shell-time callback bundle. The
P150/P151 run records and limits are in `docs/re/iomfb-dseries.md` and
`docs/re/dcp-firmware-services.md`.

## Phase 2 (`UI_SB_SETUP_PATH1`, rebuilt parent): SpringBoard never starts its app

Run 13:37-13:43 with `CALLBACKS=sb_setup_path_callbacks`, slide `0x15080000`,
32 breakpoints with proofs, 169 hits, every hit named by `__progname`
(`~/dvm-artifacts/probe-logs/UI_SB_SETUP_PATH1.lldb.log`).  Parent:
`/tmp/dvm/data-seed/rebuild/boot2.qcow2` from
`tools/rootfs/rebuild_persistent_parent.sh` after the host reboot.

| Observation | Value |
|---|---|
| `_BYSetupAssistantNeedsToRun` callers | `bluetoothd` x47, `audioaccessoryd` x5, `coreauthd` x3, `cloudd` x2, `deviceaccessd` x2, `CommCenter` x1; **the phase-1 "7 processes at 7 slides" was `bluetoothd` relaunching** (its `__TEXT` is >4 MB, matching `lr` offsets `+0x3fe9d8`/`+0x3feac4`) |
| SpringBoard | **zero** hits at `applicationDidFinishLaunching:`, `_finalizeStartupAfterScenesDidConnect:`, `updateInSetupMode`, the ForceToBuddy block, `_SBWorkspaceActivateApplication`, `_handleSetupExited:` in 360 s |
| `abort()` | `callservicesd` and `chronod`, both from static `0x1844e4eb0` in QuartzCore `query_displays()` |
| serial | 1,177 lines, 0 `panic(cpu`, 0 critical-process reboots; screendump black |

So Setup's own gate is irrelevant: SpringBoard never gets as far as its app
delegate.  The abort site explains why UIKit clients die and points at the
render server.

### The display query is refused out of process

`query_displays()` (`0x1844e47c8`) loops for 5 s: `CARenderServerGetServerPort`
(`0x1844e4834`), `_CASGetDisplays` (`0x1844e4848`, return code in `w19` at
`0x1844e484c`); `w19 == 0xfb294002` or `-0x134` (`MIG_SERVER_DIED`) sleeps
500 ms and retries; any other nonzero code, or the deadline, reaches
`0x1844e4e54`, logs "CoreAnimation: Unable to query displays from server (%d)"
(`0x184837336`) and calls `abort()` at `0x1844e4eb0`.

Server side, `__XGetDisplays` (`0x18440c4f0`) loads
`CA::Render::Server::_oop_display_observation_disabled` (`0x1e40102a6`) at
`0x18440c560`; if it is 1 and the requester pid (`w21`, request +0x4c) differs
from `getpid()` (`0x18440c56c-0x574`), the reply code is `0xfb294002`
(`0x18440c6d0-0x6dc`).  backboardd creates its window server with exactly that
flag: `backboardd:0x1000258c8-0x100025918` builds
`{kCAWindowServerDisableUpdatesOnMainDisplay: YES,
kCAWindowServerDisableOutOfProcessDisplayObservation: YES}` and calls
`+[CAWindowServer serverWithOptions:]` unconditionally.  The flag is cleared
only by `-[CAWindowServer enableOutOfProcessDisplayObservation]`
(`0x1847574ec`: `stlrb wzr` then `notify_post
"com.apple.CoreAnimation.CAWindowServer.DisplayChanged"`), reachable via
`_CARenderServerEnableOutOfProcessDisplayObservation` (`0x1846d7828`) or the
MIG request `__XEnableOutOfProcessDisplayObservation` (`0x1847da7dc`).
backboardd's own binary contains the selector string, so on real hardware
backboardd re-enables observation itself once its display setup finishes;
here that never happens within 360 s, and every out-of-process CoreAnimation
client that asks for displays aborts 5 s later.  Phase 3
(`tools/re/display_query_callbacks.py`) records who asks, what the server
answers, whether any enable path runs, and backboardd's StartWindowServer
gate outcome.

### backboardd enables observation only on the setup-complete path

`backboardd:0x100025ef0` (`ipsw macho disass` of the extracted binary, text
`0x100001fe0-0x10005aa44`) asks `+[CAWindowServer serverIfRunning]`, logs
"StartWindowServer: enabling out of process display observation"
(`0x100083f6c`), calls `-[CAWindowServer enableOutOfProcessDisplayObservation]`
at `0x100025f48`, and then logs "StartWindowServer: Setup complete"
(`0x100083fab`, `0x100025f74`).  The headless branch selected by the null
tests at `0x1000259fc`/`0x100025a00` (`docs/re/backboardd-start-window-server-gate.md`)
never reaches `0x100025f48`.  Therefore, on this guest, either the null tests
took the headless branch or StartWindowServer never ran; either way
`_oop_display_observation_disabled` stays 1 and every UIKit process aborts in
`query_displays()`.  `tools/re/display_query_callbacks.py` breaks on the
cache-side enable method and, once backboardd's PIE base is resolved from a
live frame, on `B+0x259fc`, `B+0x25a00`, `B+0x25fdc`, `B+0x25ef0`,
`B+0x25f48` and `B+0x25f74`.

## Phase 3 (`UI_DISPLAY_QUERY1`): the render server answers once, then never again

`CALLBACKS=display_query_callbacks`, slide `0xd72c000`, 21 breakpoints with
proofs, log in `~/dvm-artifacts/probe-logs/UI_DISPLAY_QUERY1.lldb.log`.  Times
are seconds after backboardd's first `query_displays` (13:47:34 host).

| t | process | event |
|---:|---|---|
| 0.0 | backboardd | `query_displays` from its own StartWindowServer path; server port `0x7103` found at 1.4 s |
| 8.3 | chronod | `query_displays`, port found |
| 47.7 | callservicesd | `query_displays`, port found |
| 102.2 | backboardd (server thread) | first `__XGetDisplays` ever serviced: own pid `0x47` accepted, six `Server::get_displays` calls; then chronod (`0x72`) and callservicesd (`0x7d`) refused with `0xfb294002` |
| 102.2 | backboardd | its own `_CASGetDisplays` returns 0 with a 543-byte display list |
| 102.3 | chronod, callservicesd | reply arrives long after their 5 s deadline: error edge `0x1844e4e54`, then `abort()` |
| 102.8 | backboardd | `-[CAWindowServer enableOutOfProcessDisplayObservation]` from `backboardd+0x25f4c`, the setup-complete path |
| 103.2 | **SpringBoard** | `query_displays` (via `+[CADisplay mainDisplay]` `0x1843e33b8`), port `0x510b` found |
| 107.2 / 110.9 / 120.5 | chronod, com.apple.datamigrator, callservicesd | relaunched, `query_displays` again, ports found |
| 112.9 | AccessibilityUIServer | `_UIApplicationMain` |
| to 230+ | — | **no `_CASGetDisplays` return and no server-side `__XGetDisplays` for any of them** |

So backboardd does complete StartWindowServer (headless path not taken) and
does enable out-of-process observation, but its render-server thread
serviced the MIG port exactly once, about 100 s after the server was created,
and after that no `GetDisplays` request is received.  SpringBoard's
`_CASGetDisplays` is a reply-wait with no timeout, so SpringBoard sits in
`+[CADisplay mainDisplay]` forever, never reaches `_UIApplicationMain`, and
Setup is never launched.  This is the first-order gate for any UI, ahead of
every SpringBoard/Setup decision above.

Two things still to establish: what the render-server thread blocks on
between and after those bursts (the ~100 s delay and the permanent stall have
the same shape: the thread that owns the MIG port is busy in display
initialisation/rendering, most likely an IOMobileFramebuffer wait on the DCP),
and whether the client refusal window matters once the stall is fixed (the
5 s deadline versus a 100 s server startup would still kill every early
client).

## Phase 4 (`UI_DISPLAY_STALL1`): the server thread is idle in `mach_msg`, and messages arrive in bursts

`CALLBACKS=display_stall_callbacks`, slide `0x1026c000`, 23 breakpoints with
proofs (`~/dvm-artifacts/probe-logs/UI_DISPLAY_STALL1.lldb.log`).  The
render-server thread's `mach_msg` return at `server_thread+0x1a0`
(`0x1843f9804`) was used as a heartbeat; entry/return of every swap and wait
routine was watched by planting a one-shot breakpoint at the caller's `lr`.

| t (host s) | event |
|---:|---|
| 0.0 | backboardd (a dispatch thread, not StartWindowServer) enters `query_displays` |
| 3.6 | `CA::Render::Server::server_thread` starts, locks `_mutex`, broadcasts, enters the blocking `mach_msg` receive on its port (`options 0x0300080e`, no timeout) |
| 9.1, 56.6 | chronod, callservicesd send `_CASGetDisplays` |
| 73.6 | **two** messages received, nothing else |
| 105.1-105.4 | **54 messages received in 300 ms**: 20 `render_for_time` calls (2-5 ms each), then the six `Server::get_displays` and the three queued `GetDisplays` requests |
| 106.1 | backboardd enables out-of-process observation (StartWindowServer completes) |
| 106.7 | SpringBoard sends `_CASGetDisplays` |
| 106-356 | 4 more receives at 106 s, then none; SpringBoard never answered; no swap, vsync, relbuf, or timer breakpoint ever fired |

No `IOMFBDisplay::swap_wait`, `kern_SwapWait`, `wait_for_relbuf_info`,
`vsync_callback`, `add_vsync_source` or `IOMFBServer::timer_callback` hit in
356 s, and the DCP trace shows only 48 AP-to-DCP RPCs (`A030` x14 the most
frequent) and two callbacks, so neither QuartzCore nor the DCP model is busy.
The server thread spends the whole run blocked in a receive while messages
that were sent 60-100 s earlier sit undelivered, then arrive together.  That
is not display behaviour; it is Mach IPC delivery or thread scheduling in the
guest, i.e. timer/scheduler health on the single TCG vCPU.  The launchd
timestamps in the same run show guest time at 194 s when the host had spent
about 340 s, but every debugger stop pauses the virtual clock, so that ratio
is measured separately by `tools/clock_test.sh` on a debugger-free restore
boot (`sleep N` timed from the host, `date +%s` deltas).

### Clock, timers and CPU profile are not the cause

`tools/clock_test.sh`/manual timing on the restore-ramdisk shell (`CLOCKTEST2`,
no debugger): guest `date +%s` advanced 45 s while the host advanced 44.7 s;
`sleep 10` and `sleep 30` returned after 8.4 s and 28.7 s host (the send
latency is inside those numbers).  The guest counter runs at
`clock-frequency = 0x100000` from the device tree (`dt_fixup.py`, `darwin.c:284`),
and QEMU scales it to real time; the 194 s versus 340 s discrepancy seen under
the debugger is the virtual clock pausing at every breakpoint stop.

`tools/re/sampled_boot.sh PCSAMPLE1` (`tools/pc_sampler.py`, 666 samples at
0.4 s over 270 s, no debugger) shows a uniformly busy vCPU: about 65 % of
samples in the kernel and 35 % in shared-cache user code in every 30-s bucket,
none in the idle/WFI path.  The hottest kernel windows are the timebase read
(`com.apple.kernel+0xb4380`, `+0x212d80`, `+0x213080`, the
`cpu_data->base_timebase` / `S3_4_C15_C10_6` retry loop), the context-switch
accounting loop (`+0xaf800`), the EL0 synchronous vector (`+0x400`, syscalls)
and SPTM at `0xfffffff00709a600`/`0xfffffff0070d4e80` (about 7 %).  There is
no single spin loop and no poll storm on the DCP side (48 RPCs in 356 s), so
the render server's 100-s receive blackout is not CPU starvation and not a
timer fault; once woken, the thread drains 54 messages in 300 ms.

## Phase 5 (`UI_DISPLAY_PORT1`): the service port is only received on once

`CALLBACKS=display_port_callbacks`, slide `0xb5ac000`
(`~/dvm-artifacts/probe-logs/UI_DISPLAY_PORT1.lldb.log`).  The heartbeat now
decodes each received message's header at `sp+0x50`; ids are mapped with
`tools/re/carender_mig_ids.json` (the `_CASCARenderServices_subsystem` at
`0x1e905b680` spans ids 40200-40298; `__XGetDisplays` is 40213).

| t | event |
|---:|---|
| 10.4, 12.6 | backboardd `bootstrap_check_in` from IOMobileFramebuffer framework code (`0x22a372014`, `0x22a3720c8`), not CoreAnimation |
| 12.9 | backboardd dispatch thread enters `query_displays` and blocks in `_CASGetDisplays` |
| 15.8 | `CA::Render::Server::server_thread` starts, receives nothing |
| 84.6 | first `CA::Render::Server::server_port()` call, main thread, from `Server::add_callback` ← `Display::end_display_changes` ← `IOMFBDisplay::update_framebuffer_locked` ← `AppleDisplay::AppleDisplay` ← `AppleInternalDisplay::open` block ← `-[CAWindowServer _detectDisplays]` ← `shared_server_init` ← `+[CAWindowServer serverWithOptions:]`; first receives: 24 messages id 40001 on port `0x8b33`, 15 messages id 0 on `0x11413` (internal, not the client subsystem) |
| 115.8-116.1 | ten more `server_port()` calls; `Server::set_display_state(off)` and `IOMFBDisplay::set_power_state_locked(0)` for five display slots, `complete_display_state_transition(1)` for the main one; **the three queued `__XGetDisplays` (backboardd, chronod, callservicesd) plus six `__XGetDisplayInfo`, `__XRegisterClient`, `__XGetNeededAlignment`, `__XGetMaxRenderableIOSurfaceSize` arrive on service port `0x660b`** |
| 116.6 | `enableOutOfProcessDisplayObservation`; one final message (id 40002 on `0x17d03`) |
| 117.0, 121.8, 129.5, 136.2 | SpringBoard, chronod, datamigrator, callservicesd send `_CASGetDisplays`; **none is ever received**; a `render_for_time` returns at 124.5 s |

So the service port becomes receivable exactly once, during the display's
first state transition, and after `enableOutOfProcessDisplayObservation` no
client message reaches the server thread again.  The chain that takes 100 s
is `serverWithOptions:` → `_detectDisplays` → `AppleInternalDisplay::open`
(reaching the `AppleDisplay` constructor at 84.6 s) → the state transitions at
115.8 s.  Phase 6 (`tools/re/display_open_callbacks.py`) times each link of
that chain, counts port-set insert/move/extract for backboardd only, and
stamps the DCP trace and serial log with host time (`tools/re/stamp_growth.sh`).

## Phase 6 (`UI_DISPLAY_OPEN1`): the chain is slow, then the server thread parks in a condition wait

`CALLBACKS=display_open_callbacks`, slide `0x17330000`, times relative to
backboardd's first `bootstrap_check_in`
(`~/dvm-artifacts/probe-logs/UI_DISPLAY_OPEN1.lldb.log`; entry/return watches).

| t | event |
|---:|---|
| 4.2 | `+[CAWindowServer serverWithOptions:]` → `shared_server_init` (4 ms) → server thread starts → `-[CAWindowServer _detectDisplays]` enters |
| 34.9 | `AppleInternalDisplay::open` enters; `AppleDisplay` constructor enters; `IOMobileFramebufferOpen` takes **21 ms** |
| 68.0 | `IOMFBDisplay::update_framebuffer_locked` → `Display::end_display_changes` → first `server_port()` → `mach_port_move_member`; constructor returns after **33.2 s**; `open` returns |
| 99.5 | five more `AppleDisplay` constructions (fast), `initialize_timings` x7, `set_display_state`, power state, `complete_state_transition`; `_detectDisplays` and `serverWithOptions:` return after **95.6 s**; service port `0xa903` moved into port set `0x8313` at 99.8 s and never removed |
| 99.8 | the three queued `_CASGetDisplays` are answered; 50 receives in that second, 4 more at 100 s, none after |
| 100.8 | SpringBoard `query_displays`; never answered |

`IOMobileFramebufferGetMainDisplay` took 22-86 s in every process that called
it (thermalmonitord 30.7 s, MobileGestaltHelper 22.0 s and 32.9 s,
IOMFB_FDR_Loader 86.1 s): it is `mfb_populate_all_display_infos`, a walk of
every `IODeviceTree:/arm-io` child through `IORegistryEntryGetChildIterator`
and `IORegistryEntrySearchCFProperty`, hundreds of MIG calls, on a vCPU that
`tools/pc_sampler.py` showed saturated.  So the three ~30-s spans are the
same walk done three times (before `open`, inside the constructor, and for
the remaining displays), i.e. TCG throughput, not a timeout.

**The blocker is what the server thread does after the burst.**  With the
guest frozen, `tools/re/kthread_peek.py 0xffffffe483d9f060` (the server
thread's `tpidr_el1`) shows its continuation at `+0xe0` is
`com.apple.kec.pthread+0x2e0c` (`0xfffffff0084067ac` unslid), a `psynch`
continuation, not the IPC receive continuation (`com.apple.kernel+0x2e658`,
which codex's snapshot attributed to `mach_msg` waits).  Its wait queue
(`thread+0x108` → `0xffffffe484595b90`) carries user addresses
`0x7cd4d503b0`, `+0x18`, `+0x20`, `+0x27`: a heap `pthread_cond_t`.  The
render-server thread is therefore blocked in `pthread_cond_wait` on a
heap-allocated condition variable, which is why no later client message is
ever received.  Phase 7 catches that wait from user space with a backtrace to
name the condition and what should signal it.

## Phase 7 (`UI_DISPLAY_WAIT2`): the parked wait is a mutex inside `IOMFBServer::set_next_update`

`CALLBACKS=display_wait_callbacks`, slide `0x8d74000`
(`~/dvm-artifacts/probe-logs/UI_DISPLAY_WAIT2.lldb.log`).  The server thread's
`tpidr_el1` (`0xffffffe94f0c0950`) was recorded at `server_thread` entry and
only that thread's `pthread_cond_wait` / `pthread_cond_timedwait` /
`__psynch_mutexwait` entries were reported.  (A first attempt with a
`pthread_mutex_lock` breakpoint stopped the vCPU on every process's lock
call and crawled; that breakpoint was dropped.)

| t | wait | object | backtrace (static) |
|---:|---|---|---|
| 2.8 | `__psynch_mutexwait` | `CA::Render::Server::_mutex` (`0x1e400a8e8`) | the startup handshake in `server_thread+0xa8`; returns |
| 64, 95 | 2 + 45 `mach_msg` receives | | the burst (queued client requests) |
| 95.5 | `__psynch_mutexwait` | heap `0x78eecad8b0` | `IOMFBServer::set_next_update+60` ← `Server::display_changed+124` ← `post_notification` ← `Display::post_display_changed_callback+124` ← `server_thread+0x1060`; returns |
| 96.1 | **`__psynch_mutexwait`** | **heap `0x78eecac3b0`** (`x1 = 0x20000000302`, owned by another thread) | `IOMFBServer::set_next_update+60` ← `Server::context_changed+0x114` ← `post_notification+788` ← `context_changed` ← `CmdStreamMsg::run` (`0x184406288`, `0x184405c60`) ← `0x184594860` ← `server_thread+0xdf4`; **never returns** |
| 96.2 | | | main thread enables out-of-process observation; SpringBoard queries at 96.5 s; no further receives |

So the render-server thread dies while handling the first client command
stream: `Server::context_changed` posts a notification, `IOMFBServer::set_next_update` tries to take the IOMFB server's mutex, and the owner never
releases it.  `pthread_cond_wait` releases its mutex while sleeping, so the
owner is not in a condition wait; it is either computing or, far more
likely on this model, inside an IOMobileFramebuffer kernel call that never
completes (the phase-4 candidates `IOMFBDisplay::swap_wait`,
`IOMobileFramebufferSwapWait`, `wait_for_relbuf_info` were not hit, so the
call is a different selector).  Phase 8 (`tools/re/display_iokit_callbacks.py`)
records every `IOConnectCall*` backboardd makes with its selector and a
return watch; the entry without a return is the missing DCP completion.

## Phase 8 (`UI_DISPLAY_IOKIT1`): the holder is blocked in `IOMobileFramebuffer` selector 79, `GetBlock`

`CALLBACKS=display_iokit_callbacks`, slide `0xd038000`
(`~/dvm-artifacts/probe-logs/UI_DISPLAY_IOKIT1.lldb.log`).  Every
`IOConnectCall*` made by backboardd was recorded with its selector (x1) and a
return watch; `tools/re/iomfb_selectors.json` maps selectors to the
framework's `_kern_*` wrappers (built from `mov w1, #N` before each
`IOConnectCall*` in the IOMobileFramebuffer framework).  The render-server
thread (`0xffffffe9dc72b060`) blocked on `IOMFBServer+0x3b0` at 103.6 s as in
phase 7.  At that moment exactly one backboardd IOKit call had entered
without returning:

| thread | call | selector | entered |
|---|---|---:|---:|
| `...9220c0` | `IOConnectCallMethod` | **79 = `_kern_GetBlock`** | 103.5 s, never returns |

The same thread had just completed selector 25 (`GetDigitalOutState`) and
12 (`DisplayResourcesValidateAllocation`).  Every other call in the run
returned within milliseconds (`OpenByName` 88, `ValidateAllocation` 12 across
seven threads, `AllocationGetV2` 13).  So the display-attach path holds the
IOMFB server mutex while `GetBlock` sleeps in the kernel waiting for data the
DCP model never produces; the render server then deadlocks on that mutex the
first time a client context changes, and every later CoreAnimation client
(SpringBoard included) blocks forever in `_CASGetDisplays`.

The unreturned call's chain (static symbols of the recorded backtrace):
`CFRunLoop block` ← `Server::render_for_time+19312` ←
`IOMFBServer::complete_display_state_transition` (takes `+0x3b0`) ←
`IOMFBDisplay::complete_display_state_transition` ←
`IOMFBDisplay::update_power_state_locked+1088` ←
`IOMFBDisplay::fetch_current_iomfb_mode+208` ←
`CA::IOMobileFramebuffer::get_digital_mode` block (under `BMBlockMonitoring`) ←
`IOMobileFramebufferGetDigitalOutMode+92` ← `IOConnectCallMethod` selector 79.
`GetDigitalOutMode` (`IOMobileFramebuffer:0x22a396868`) requests block kind
**`0x41`** with a `0x2c`-byte result through `_kern_GetBlock`.  The earlier
three `GetBlock` calls from the `AppleDisplay` constructor (kinds `0x4c`,
`0x4a`, `0x79`, see `docs/re/iomfb-a442-payload-consumers.md`) each produced
one `A442` RPC and returned; **the kind-`0x41` call produces no `A442` at
all** (the run's DCP trace has exactly three `A442`s, `#25-#27`), so the
kernel handler sleeps before issuing anything, i.e. on display state that
the DCP is expected to have reported first: the hot-plug / digital-out latch
in `docs/re/iomfb-hotplug-swap-gate.md`.

### The kernel wait: `iomfb_dcp_power_async`, armed by `A500`

The holder's kernel stack (read from its `thread->kernel_stack` at `+0xf0`,
`0xffffffdf5b020000`, with `tools/re/kc_text_map.py`) runs MIG →
`is_io_connect_method` (`kernel+0x1c4df4`) → external-method dispatch
(`+0x82f3a8`, `+0x82f100`) → `IOMobileFramebufferAP::get_block`
(`IOMFB+0x58a0`) → the `A442` wrapper (`IOMFB+0x18b40`) → `IOMFB+0x22af4`,
`+0x23c7c`, `+0x23abc` → `kernel+0x781940` (`bl 0xfffffff00aaf9ca8`, the
`IOLockSleep` primitive) → scheduler.  `IOMFB+0x23c7c` is the return from the
wait loop at `0xfffffff00a0d5524`:

```
IOLockLock([obj+0x78]);
while ([obj+0x30] == NULL) IOLockSleep([obj+0x78], obj+0x30, interruptible=1);
IOLockUnlock([obj+0x78]);
return [obj+0x30] ? 0 : kIOReturnNotReady (0xe00002c0);
```

The object is constructed at `0xfffffff00a0d5354`, which logs the string
`iomfb_dcp_power_async` (`0xfffffff007992f89`), and its `+0x30` is cleared at
teardown (`0xfffffff00a0d55ec`).  So the kind-`0x41` block fetch waits until an
asynchronous DCP power transition has completed.  That transition is what the
AP asked for with `A500`: the wrapper at `0xfffffff00a0d41f0` is
`A500(u8, u8)` (two bytes plus the `0xaaaa` poison, 4 in / 0 out; the
observed input `00 01 aa aa` is state 0 with the async flag set), the last
RPC the AP ever issues, immediately after `Server::set_display_state(off)`.
On hardware the DCP answers the async power request with a callback that
fills `+0x30`; our model completes `A500` with status 0 and nothing else, so
the waiter never wakes, `complete_display_state_transition` keeps
`IOMFBServer+0x3b0`, and the render server deadlocks on it.

### Correction: the wait is a lock, and the lock traces to RTBuddy's TraceKit endpoint

The section above stopped one level short.  Reading the sleeping object live
(`tools/re/kmem.py`, HMP `x/gx` with the vCPU caught at EL1) shows its `+0x30` is
**already filled** with the matched `AppleDCPExpert` (`0xffffffe8f6017000`), so the
match wait passes.  The deepest IOMFB frame, `0xfffffff00a0d5b3c`, is the return from
`IORecursiveLockLock([obj+0x80])` inside `set_device_power(on)` (`0xa0d5b14`), and the
lock's owner field (`IORecursiveLock+0x18`) names another thread.  Walking that
thread's frame chain (`kmem.py fpwalk`) gives the whole story, every frame confirmed
by the words under its saved `x29`:

| frame | where | meaning |
|---|---|---|
| IOMFB work loop | `0xa0eb5e4` ← `0xa0ee6b4` ← `0xa0c6cb4` (`"fPendingPower_on_internal started"`) | the display-off job |
| `0xa0c6fac` → `0xa0d5cfc` → `0xa0d5bd0` | `power_gated` → `set_device_power(on = 0)` | `x20 = 0` in the frame |
| `AppleDCP+0x5ce8` → `+0x734c` → `+0x5628` → `+0x56c0` → `+0x5830` | `setPowerAssertion(0)` → gated action → `_changeDCPPowerStateInternal(0)`, current state 4 | `x19 = 0`, `x20 = 4` |
| `RTBuddy+0x180d8` → `+0x181a0` → `+0x186c8` | `RTBuddy::setPowerState(0)` → gated body → `_setPowerStateGuardEntryGated` | sleeps while `RTBuddy+0x15c` (`_powerStateChangeLocked`) is set |

`_powerStateChangeLocked` is set only by the guard entry (`0xa7bd644`) and cleared only
by `_setPowerStateGuardExitGated` (`0xa7bdab8`), so a transition was still in flight.  A
physical scan of guest RAM for stack slots holding RTBuddy return addresses
(`tools/re/ramscan_frames.py`; kernel stacks are single 16 KiB pages, so no VA
translation is needed) found it: page `0x1001be28000`, **`RTBuddy(DCP)::start` →
`setPowerState(1)`** (`this = 0xffffffe8f601a000`, role `DCP`, ordinal 1), past the
guard, inside `_setPowerStatePerformChangeGated` → client callback → 
`RTBuddyTraceKitEndpoint::_waitForOutstandingRequestsGated` (`0xa7df68c`), sleeping on
the endpoint's outstanding-request counter at `+0xbc`, which read 1.  A second thread
(`tools/re/ramscan_threads.py`, matching `struct thread` by its constant words at
`+0x1a0`/`+0x1d0`/`+0x1d8`) waits on the same object's `+0xac`: the busy-ACK wait of
`_configureGated` (`0xa7e0738`).  `RTBuddy(ANS2)` is parked identically.

So the DCP's **boot-time power-on never finished**: the AP sent TraceKit
`host version 1` and `configure` (type 4, `0x0040000000000000`) on endpoint `0x0a`, and
our ASC model logged both as "no protocol modelled".  Everything after that is
consequence: the display-off `set_device_power(0)` queues behind the guard, holding the
power object's recursive lock; `GetBlock` kind `0x41` (`GetDigitalOutMode`) blocks on
that lock inside `IOConnectCallMethod` selector 79; the render server's
`complete_display_state_transition` never releases `IOMFBServer+0x3b0`;
`+[CADisplay mainDisplay]` in SpringBoard never returns.

The protocol, from `_messageHandler` (`0xa7dec68`, `ubfx x1, x2, #0x34, #4`): type in
bits [55:52]; IOP→AP type 0 completes a flush and decrements the counter, types 1/2 set
`_stateIOP` to 2/3, type 3 sets it to `payload != 0`, type 4 clears it; `_configureGated`
then requires `_stateIOP == 1` (`"_stateIOP == TraceKitState::Configured"`).  The
responder in `darwin_asc.c` (`rtk_handle_tracekit`) answers configure/enable/disable
with `0x0030000000000001` and a flush with type 0.  First boot with it
(`UI_TRACEKIT1`): both DCP and ANS2 immediately followed our configure reply with a
type-2 disable that had never been sent before, i.e. the transition proceeded.

### Runtime lookup-return capture after PLUGIN155

PLUGIN155 observed 24 entries at `-[SBApplicationController setupApplication]` (`0x2243bf920 + slide`) and three `_handleSetupExited:` callbacks, but the existing static `SB_FORCEBUDDY_APP_LOOKUP_RET` breakpoint at `0x2244783d4 + slide` is a different call site's `applicationWithBundleIdentifier:` return. Those static events cannot be paired to the 24 setupApplication invocations or establish that Buddy returned nil.

The read-only activation observer now arms a dynamic breakpoint at each setupApplication entry's saved LR, keyed by PID + TPIDR_EL1 + SP, and records the returned x0 and its first isa word. This follows the documented tail-call to `applicationWithBundleIdentifier:SBBuddyBundleIdentifier`; it is intended to classify each result as null/non-null before assigning meaning to the later setup-exit callback. The dynamic result hook has not yet been boot-validated. The subsequent capture also serializes each DVMI2 swipe point through HID dispatch, because P155 coalesced 11 motion records despite zero dispatch failures. Neither change modifies SpringBoard, guest model, or scanout behavior.

#!/usr/bin/env bash
# Continuous replay2b run: shell ladder → SpringBoard home → CA/backboardd swap.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
TAG="${1:-UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN102_POST_SHELL_RENDER1}"
ART="$HOME/dvm-artifacts/bp-patched-dev6/boot1"
PARENT="${PARENT:-$ART/ui-class16-refresh60-dm-ls-parent-seed-d5825-replay2b.qcow2}"
CHILD="${CHILD:-/tmp/dvm/data-seed/$(printf '%s' "$TAG" | tr 'A-Z_' 'a-z-').qcow2}"
NATIVE_INPUT="${NATIVE_INPUT:-0}"
if [[ -z "${TC:-}" ]]; then
    if [[ "$NATIVE_INPUT" == 1 ]]; then
        TC="$HOME/dvm-artifacts/bp-patched-dev6/input-v12-native/system-plus-dev6-input.tc"
    else
        TC="$HOME/dvm-artifacts/bp-patched-dev6/system.tc"
    fi
else
    TC="$TC"
fi
BOOTKC="${BOOTKC:-$HOME/dvm-artifacts/bp-patched-dev6/bootkc}"
DTREE="${DTREE:-$HOME/dvm-artifacts/bp-patched-dev6/system.dtree}"
RAMDISK="${RAMDISK:-$REPO/firmware/ramdisk.dmg}"
GDB_PORT="${GDB_PORT:-1258}"
SECS="${SECS:-3600}"
FRAME_SECS="${FRAME_SECS:-3300}"
SOCK="/tmp/dvm/$TAG.sock"
REC="$ART/recovery-records/$TAG"
FRAME_LOG="$REC/frame-sampler.log"
INPUT_BOOTSTRAP="${INPUT_BOOTSTRAP:-1}"
INPUT_RUN="/tmp/dvm/$TAG.input"
INPUT_QMP="/tmp/dvm/$TAG.qmp.sock"
INPUT_STATUS="/tmp/dvm/$TAG.input-status.json"
INPUT_RESULT="$REC/native-input-swipe.json"
# The automated unlock trigger waits for a SpringBoard gesture-observer READY
# marker before it sends any input. The render-only composition does not
# install dm_lock_gesture_callbacks, so it cannot satisfy that gate. Use the
# combined Home/gesture/present profile by default; callers can still select a
# narrower profile explicitly when they are not running an input trigger.
POST_SHELL_UI_MODULE="${POST_SHELL_UI_MODULE:-dm_post_shell_home_gesture_present_callbacks}"
POST_SHELL_CALLBACKS="${POST_SHELL_CALLBACKS:-dm_gate_light_callbacks}"
START_INPUT_TRIGGER="${START_INPUT_TRIGGER:-0}"
START_FOLLOWUP_SWIPE="${START_FOLLOWUP_SWIPE:-0}"
INPUT_TRIGGER_MODE="${INPUT_TRIGGER_MODE:-dvmi2}"
INPUT_TRIGGER_TIMEOUT="${INPUT_TRIGGER_TIMEOUT:-7000}"
POST_INPUT_OBSERVE="${POST_INPUT_OBSERVE:-300}"
FRAME_SAMPLER="${FRAME_SAMPLER:-1}"
LEGACY_UART_ONLY="${LEGACY_UART_ONLY:-$([[ "$INPUT_TRIGGER_MODE" == legacy ]] && printf 1 || printf 0)}"
TRIGGER_LOG="$REC/home-trigger.log"
TRIGGER_PID=""
FOLLOWUP_PID=""
FOLLOWUP_LOG="$REC/followup-swipe-runner.log"
FOLLOWUP_SWIPE_DELAY="${FOLLOWUP_SWIPE_DELAY:-30}"
FOLLOWUP_ACCEPTED_TIMEOUT="${FOLLOWUP_ACCEPTED_TIMEOUT:-120}"
FOLLOWUP_ACK_TIMEOUT="${FOLLOWUP_ACK_TIMEOUT:-90}"
FOLLOWUP_RELAY_TIMEOUT="${FOLLOWUP_RELAY_TIMEOUT:-180}"
FOLLOWUP_WAIT_TIMEOUT="${FOLLOWUP_WAIT_TIMEOUT:-$SECS}"
FOLLOWUP_SWIPE_BACKEND="${FOLLOWUP_SWIPE_BACKEND:-recap}"

mkdir -p /tmp/dvm/data-seed /tmp/dvm/probe "$REC"
for file in "$PARENT" "$TC" "$BOOTKC" "$DTREE"; do
    [[ -f "$file" ]] || { echo "missing required input: $file" >&2; exit 1; }
done
[[ ! -e "$CHILD" ]] || { echo "refusing to reuse child: $CHILD" >&2; exit 1; }
if lsof -nP -iTCP:"$GDB_PORT" >/dev/null 2>&1; then
    echo "GDB port $GDB_PORT is already in use" >&2
    exit 1
fi
case "$NATIVE_INPUT" in 0|1) ;; *) echo "NATIVE_INPUT must be 0 or 1" >&2; exit 2 ;; esac
case "$INPUT_BOOTSTRAP" in 0|1) ;; *) echo "INPUT_BOOTSTRAP must be 0 or 1" >&2; exit 2 ;; esac
case "$START_INPUT_TRIGGER" in 0|1) ;; *) echo "START_INPUT_TRIGGER must be 0 or 1" >&2; exit 2 ;; esac
case "$START_FOLLOWUP_SWIPE" in 0|1) ;; *) echo "START_FOLLOWUP_SWIPE must be 0 or 1" >&2; exit 2 ;; esac
case "$INPUT_TRIGGER_MODE" in dvmi2|legacy) ;; *) echo "INPUT_TRIGGER_MODE must be dvmi2 or legacy" >&2; exit 2 ;; esac
case "$FRAME_SAMPLER" in 0|1) ;; *) echo "FRAME_SAMPLER must be 0 or 1" >&2; exit 2 ;; esac
case "$LEGACY_UART_ONLY" in 0|1) ;; *) echo "LEGACY_UART_ONLY must be 0 or 1" >&2; exit 2 ;; esac
if [[ "$LEGACY_UART_ONLY" == 1 && "$INPUT_TRIGGER_MODE" != legacy ]]; then
    echo "LEGACY_UART_ONLY=1 requires INPUT_TRIGGER_MODE=legacy" >&2
    exit 2
fi
if [[ "$START_FOLLOWUP_SWIPE" == 1 &&
      ( "$START_INPUT_TRIGGER" != 1 || "$NATIVE_INPUT" != 1 ||
        "$INPUT_TRIGGER_MODE" != legacy ) ]]; then
    echo "START_FOLLOWUP_SWIPE requires START_INPUT_TRIGGER=1, NATIVE_INPUT=1, and INPUT_TRIGGER_MODE=legacy" >&2
    exit 2
fi
if [[ "$START_INPUT_TRIGGER" == 1 && "$INPUT_TRIGGER_MODE" == legacy ]]; then
    case "$POST_SHELL_UI_MODULE" in
        dm_post_shell_home_gesture_callbacks|\
        dm_post_shell_home_gesture_present_callbacks|\
        dm_post_shell_home_gesture_staged_callbacks|\
        dm_post_shell_home_unlock_diag_callbacks)
            ;;
        *)
            echo "legacy input trigger requires a post-shell module that emits a gesture READY marker: $POST_SHELL_UI_MODULE" >&2
            exit 2
            ;;
    esac
fi

export DVM_POST_SYNC_ARM_UI=1
export DVM_POST_SHELL_UI_MODULE="$POST_SHELL_UI_MODULE"
# The combined profile keeps the BackBoard/UIEvent trace live while the
# gesture correlator validates the same swipe. This is observational only.
export DVM_POSTHOME_BACKBOARD_INPUT_TRACE="${DVM_POSTHOME_BACKBOARD_INPUT_TRACE:-1}"
export DVM_POST_SHELL_HOME_MIN_LOG="/tmp/dvm/$TAG.post-shell-home-min.jsonl"
export DVM_PROBE_SUCCESS_LABELS=""
export DARWIN_DCP_EPIC=all
export DARWIN_DCP_REPLY=1
export DARWIN_DCP_IOMFB=4
export DARWIN_DCP_IOMFB_RPC_TRACE=1
export DARWIN_SMP_PV=1
export DARWIN_DCP_IOMFB_OUT='A401=01,A000=01,A454=01000000,A033=4152474200000000000000000000000000000000000000000000000000000000000000000000000001000000,A453=9b040000fc090000,A412=01000000'
export DARWIN_DCP_IOMFB_CB='D120::4,D586:9b040000fc090000:4'
export DARWIN_DCP_IOMFB_A406_OBJECTS=1
export DARWIN_DCP_IOMFB_A407_COMPLETE=d582
export DARWIN_DCP_IOMFB_A407_SCANOUT=1
export DARWIN_DCP_IOMFB_A407_TRANSFER=1
export DARWIN_DCP_IOMFB_A408_COMPLETE=0
# darwin_iomfb.c gates its A408 surface-copy path on this exact name. The
# older A408_SCANOUT spelling is not read by the model; set the real switch
# explicitly so a caller's exported environment cannot enable A408 scanout.
export DARWIN_DCP_IOMFB_SCANOUT=0
export DARWIN_DCP_IOMFB_D572=1
export DARWIN_DCP_IOMFB_ECHO_MODE=1
if [[ "$NATIVE_INPUT" == 1 ]]; then
    if [[ "$INPUT_BOOTSTRAP" == 1 ]]; then
        export DVM_NATIVE_INPUT_BOOTSTRAP=1
    else
        unset DVM_NATIVE_INPUT_BOOTSTRAP
    fi
    mkdir -p "$INPUT_RUN"
    rm -f "$INPUT_RUN/qmp.sock" "$INPUT_RUN/monitor.sock" \
        "$INPUT_RUN/input-status.json" "$INPUT_QMP" "$INPUT_STATUS"
    ln -s "$INPUT_QMP" "$INPUT_RUN/qmp.sock"
    ln -s "$SOCK" "$INPUT_RUN/monitor.sock"
    ln -s "$INPUT_STATUS" "$INPUT_RUN/input-status.json"
    if [[ "$LEGACY_UART_ONLY" == 1 ]]; then
        # The legacy dvm-input guest helper parses DVMINPUT1. The QEMU-native
        # UART transport sends DVMI2 pings/records, which that helper rejects
        # and can interleave with direct relay packets. Keep the serial socket
        # for relay.py, but leave QEMU's DVMI2 producer/status path disabled.
        export DARWIN_INPUT_UART=0
        unset DARWIN_INPUT_STATUS DVM_PROBE_QMP_SOCKET
    else
        export DARWIN_INPUT_UART=1
        export DARWIN_INPUT_STATUS="$INPUT_STATUS"
        export DVM_PROBE_QMP_SOCKET="$INPUT_QMP"
    fi
else
    unset DVM_NATIVE_INPUT_BOOTSTRAP
fi

SAMPLER_PID=""
if [[ "$FRAME_SAMPLER" == 1 ]]; then
    python3 "$REPO/tools/re/dm_post_shell_frame_sampler.py" "$TAG" \
        --interval 120 --max-secs "$FRAME_SECS" > "$FRAME_LOG" 2>&1 &
    SAMPLER_PID=$!
fi
if [[ "$START_INPUT_TRIGGER" == 1 ]]; then
    [[ "$NATIVE_INPUT" == 1 ]] || { echo "START_INPUT_TRIGGER requires NATIVE_INPUT=1" >&2; exit 2; }
    if [[ "$INPUT_TRIGGER_MODE" == legacy ]]; then
        python3 "$REPO/tools/re/trigger_home_unlock_legacy.py" "$TAG" \
            --timeout "$INPUT_TRIGGER_TIMEOUT" \
            --post-transition-secs "$POST_INPUT_OBSERVE" \
            --ack-timeout "${LEGACY_ACK_TIMEOUT:-60}" \
            --relay-timeout "${LEGACY_RELAY_TIMEOUT:-240}" \
            --swipe-backend "${LEGACY_SWIPE_BACKEND:-recap}" \
            > "$TRIGGER_LOG" 2>&1 &
    else
        read -r -a SWIPE_COORDS_ARGS <<< "${SWIPE_COORDS:-589 2540 589 500}"
        python3 "$REPO/tools/re/trigger_home_then_unlock_dvmi2.py" "$TAG" \
            --timeout "$INPUT_TRIGGER_TIMEOUT" \
            --post-input-observe "$POST_INPUT_OBSERVE" \
            --home-entity-timeout "${HOME_ENTITY_TIMEOUT:-240}" \
            --initial-swipe-delay "${INITIAL_SWIPE_DELAY:-0}" \
            --swipe-coords "${SWIPE_COORDS_ARGS[@]}" \
            --swipe-hold-ms "${SWIPE_HOLD_MS:-600}" \
            --swipe-steps "${SWIPE_STEPS:-32}" \
            --require-shell-service-return --swipe-after-home-timeout \
            --home-after-accepted-transition \
            --swipe-dispatch-each-step > "$TRIGGER_LOG" 2>&1 &
    fi
    TRIGGER_PID=$!
fi
if [[ "$START_FOLLOWUP_SWIPE" == 1 ]]; then
    python3 "$REPO/tools/re/send_one_followup_swipe.py" "$TAG" \
        --delay "$FOLLOWUP_SWIPE_DELAY" \
        --accepted-timeout "$FOLLOWUP_ACCEPTED_TIMEOUT" \
        --ack-timeout "$FOLLOWUP_ACK_TIMEOUT" \
        --relay-timeout "$FOLLOWUP_RELAY_TIMEOUT" \
        --max-wait "$FOLLOWUP_WAIT_TIMEOUT" \
        --backend "$FOLLOWUP_SWIPE_BACKEND" > "$FOLLOWUP_LOG" 2>&1 &
    FOLLOWUP_PID=$!
fi
set +e
CALLBACKS="$POST_SHELL_CALLBACKS" \
SECS="$SECS" STALL_SECS="$SECS" GDB_PORT="$GDB_PORT" \
PARENT="$PARENT" CHILD="$CHILD" TC="$TC" BOOTKC="$BOOTKC" \
DTREE="$DTREE" KEEP_GUEST=1 HOLD_GUEST=0 AUTO_POSTMORTEM=0 \
RAMDISK="$RAMDISK" \
LLDB_DRAIN_SECS=10 \
"$REPO/tools/re/setup_gate_probe.sh" "$TAG" > "$REC/driver.log" 2>&1
SETUP_RC=$?
set -e
if [[ "$SETUP_RC" -ne 0 ]]; then
    echo "setup_gate_probe returned $SETUP_RC; preserving run evidence and child" >&2
    [[ -z "$SAMPLER_PID" ]] || kill "$SAMPLER_PID" 2>/dev/null || true
    [[ -z "$SAMPLER_PID" ]] || wait "$SAMPLER_PID" 2>/dev/null || true
    [[ -z "$TRIGGER_PID" ]] || kill "$TRIGGER_PID" 2>/dev/null || true
    [[ -z "$TRIGGER_PID" ]] || wait "$TRIGGER_PID" 2>/dev/null || true
    [[ -z "$FOLLOWUP_PID" ]] || kill "$FOLLOWUP_PID" 2>/dev/null || true
    [[ -z "$FOLLOWUP_PID" ]] || wait "$FOLLOWUP_PID" 2>/dev/null || true
fi
if [[ -n "$SAMPLER_PID" ]] && kill -0 "$SAMPLER_PID" 2>/dev/null; then
    kill "$SAMPLER_PID" 2>/dev/null || true
fi
[[ -z "$SAMPLER_PID" ]] || wait "$SAMPLER_PID" 2>/dev/null || true
[[ -z "$TRIGGER_PID" ]] || wait "$TRIGGER_PID" 2>/dev/null || true
FOLLOWUP_RC=0
if [[ -n "$FOLLOWUP_PID" ]]; then
    wait "$FOLLOWUP_PID" || FOLLOWUP_RC=$?
fi
python3 "$REPO/tools/hmp.py" "$SOCK" quit >/dev/null 2>&1 || true

cp -p "/tmp/dvm/$TAG.lldb.log" "/tmp/dvm/$TAG.probe.out" \
    "/tmp/dvm/$TAG.slide.json" "/tmp/dvm/$TAG.lldb.cmd" "$REC/"
cp -p "/tmp/dvm/$TAG.early-kernel-lldb.log" \
    "/tmp/dvm/$TAG.early-kernel-lldb.cmd" "$REC/" 2>/dev/null || true
cp -p "/tmp/dvm/$TAG.post-shell-home-min.jsonl" "$REC/" 2>/dev/null || true
# The trigger writes directly to REC; copying it onto itself trips cp -p under
# macOS and `set -e`, aborting before the child is checked and archived.
cp -p "/tmp/dvm/$TAG.watch.log" "$REC/" 2>/dev/null || true
cp -Rp "/tmp/dvm/$TAG.events" "$REC/events"
cp -Rp "/tmp/dvm/$TAG.frames" "$REC/frames"
cp -p "/tmp/dvm/probe/$TAG.serial.log" \
    "/tmp/dvm/probe/$TAG.stderr.log" "$REC/"
if [[ "$NATIVE_INPUT" == 1 ]]; then
    cp -p "$INPUT_STATUS" "$REC/input-status-final.json" 2>/dev/null || true
    cp -p "$INPUT_RESULT" "$REC/" 2>/dev/null || true
fi
cat > "$REC/launch-profile.txt" <<PROFILE
tag=$TAG
parent=$PARENT
child=$CHILD
tc=$TC
bootkc=$BOOTKC
dtree=$DTREE
gdb_port=$GDB_PORT
total_bound_seconds=$SECS
frame_bound_after_shell_seconds=$FRAME_SECS
ramdisk=$RAMDISK
callbacks=$POST_SHELL_CALLBACKS + deferred $POST_SHELL_UI_MODULE
early_kernel_trace=${EARLY_KERNEL_TRACE:-0}
kernel_slim=${RFT_KERNEL_SLIM:-}
kernel_labels=${RFT_KERNEL_LABELS:-}
trace_mipi_ap=${RFT_TRACE_MIPI_AP:-0}
trace_ioav_properties=${RFT_TRACE_IOAV_PROPERTIES:-0}
trace_ioav_allocators=${RFT_TRACE_IOAV_ALLOCATORS:-0}
trace_ioav_controller=${RFT_TRACE_IOAV_CONTROLLER:-0}
trace_internal_display=${RFT_TRACE_INTERNAL_DISPLAY:-0}
trace_ioav_handler=${RFT_TRACE_IOAV_HANDLER:-0}
trace_dcp_mipi_proxy=${RFT_TRACE_DCPMIPI_PROXY:-0}
trace_afk_publish=${RFT_TRACE_AFK_PUBLISH:-0}
render_slim=${RFT_SLIM:-}
input_trigger=$START_INPUT_TRIGGER
input_trigger_mode=$INPUT_TRIGGER_MODE
legacy_uart_only=$LEGACY_UART_ONLY
input_trigger_timeout=$INPUT_TRIGGER_TIMEOUT
post_input_observe=$POST_INPUT_OBSERVE
frame_sampler=$FRAME_SAMPLER
render_scheduler_trace=${DVM_POSTHOME_RENDER_SCHEDULER_TRACE:-0}
present_after_accepted_transition=${DVM_POSTHOME_PRESENT_AFTER_ACCEPTED_TRANSITION:-0}
hotplug_state_watch=${DVM_POSTHOME_HOTPLUG_STATE_WATCH:-1}
ca_handoff_trace=${DVM_POSTHOME_CA_HANDOFF_TRACE:-0}
update_gate_min_trace=${DVM_POSTHOME_UPDATE_GATE_MIN_TRACE:-0}
update_gate_after_lock_transition=${DVM_POSTHOME_UPDATE_GATE_AFTER_LOCK_TRANSITION:-0}
mutator_flags_after_accepted_transition=${DVM_POSTHOME_MUTATOR_FLAGS_AFTER_ACCEPTED_TRANSITION:-0}
display_mode_trace=${DVM_DISPLAY_MODE_TRACE:-0}
display_mode_provider_trace=${DVM_DISPLAY_MODE_PROVIDER_TRACE:-0}
ca_messages_after_lock_transition=${DVM_CA_MSG_AFTER_LOCK_TRANSITION:-0}
home_entity_timeout=${HOME_ENTITY_TIMEOUT:-240}
post_input_observe=${POST_INPUT_OBSERVE:-1800}
initial_swipe_delay=${INITIAL_SWIPE_DELAY:-0}
swipe_coords=${SWIPE_COORDS:-589 2540 589 500}
swipe_hold_ms=${SWIPE_HOLD_MS:-600}
swipe_steps=${SWIPE_STEPS:-32}
swipe_dispatch_each_step=${SWIPE_DISPATCH_EACH_STEP:-1}
swipe_after_home_timeout=${SWIPE_AFTER_HOME_TIMEOUT:-0}
followup_swipe_delay=$FOLLOWUP_SWIPE_DELAY
start_followup_swipe=$START_FOLLOWUP_SWIPE
followup_accepted_timeout=$FOLLOWUP_ACCEPTED_TIMEOUT
followup_ack_timeout=$FOLLOWUP_ACK_TIMEOUT
followup_relay_timeout=$FOLLOWUP_RELAY_TIMEOUT
followup_wait_timeout=$FOLLOWUP_WAIT_TIMEOUT
followup_swipe_backend=$FOLLOWUP_SWIPE_BACKEND
legacy_accepted_transition_timeout=${LEGACY_ACCEPTED_TRANSITION_TIMEOUT:-180}
legacy_ack_timeout=${LEGACY_ACK_TIMEOUT:-60}
legacy_relay_timeout=${LEGACY_RELAY_TIMEOUT:-240}
native_input=$NATIVE_INPUT
input_bootstrap=$INPUT_BOOTSTRAP
qmp_socket=$([[ "$NATIVE_INPUT" == 1 ]] && echo "$INPUT_QMP" || echo disabled)
input_status=$([[ "$NATIVE_INPUT" == 1 ]] && echo "$INPUT_STATUS" || echo disabled)
PROFILE
"$REPO/qemu-sptm/build/qemu-img" check -U "$CHILD" | tee "$REC/qemu-img-check.txt"
shasum -a 256 "$CHILD" | tee "$REC/child-sha256.txt"
CHILD_ART="$ART/$(basename "$CHILD")"
if ! python3 - "$CHILD" "$CHILD_ART" <<'PY'
import os, sys
try:
    same = os.path.samefile(sys.argv[1], sys.argv[2])
except OSError:
    same = False
raise SystemExit(0 if same else 1)
PY
then
    cp -p "$CHILD" "$CHILD_ART"
fi
"$REPO/qemu-sptm/build/qemu-img" info --backing-chain \
    "$CHILD_ART" > "$REC/backing-chain.txt"
echo "POST_SHELL_RENDER_RUN_COMPLETE tag=$TAG setup_rc=$SETUP_RC child=$CHILD_ART"
if [[ -n "$FOLLOWUP_PID" ]]; then
    echo "FOLLOWUP_SWIPE_RUN_COMPLETE tag=$TAG rc=$FOLLOWUP_RC result=$REC/followup-swipe.json"
fi
if [[ "$SETUP_RC" -ne 0 ]]; then
    cat "$REC/driver.log"
    exit "$SETUP_RC"
fi

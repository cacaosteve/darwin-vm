#!/usr/bin/env bash
set -euo pipefail

TAG="${RUN_TAG:-UI_CLASS16_REFRESH60_DM_PLUGIN549_SETTINGS_APP_TRACE_FROM548}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ART="$HOME/dvm-artifacts/bp-patched-dev6/boot1"
REC="$ART/recovery-records/$TAG"
PARENT="$ART/ui-class16-refresh60-dm-plugin548-settings-tap-early-relay120-from540.qcow2"
CHILD_NAME="${CHILD_NAME:-ui-class16-refresh60-dm-plugin549-settings-app-trace-from548.qcow2}"
CHILD="$ART/$CHILD_NAME"
TC="$ART/recovery-records/UI_CLASS16_REFRESH60_DM_PLUGIN480_HOME_READINESS1/build/system.tc"
BOOTKC="$ART/../bootkc"
DTREE="$ART/../system.dtree"
GDB_PORT="${GDB_PORT:-1549}"
BOUND="${BOUND:-3900}"
SOCK="/tmp/dvm/$TAG.sock"
UART="/tmp/dvm/$TAG.uart.sock"
QMP="/tmp/dvm/$TAG.qmp.sock"
EVENT_DIR="/tmp/dvm/$TAG.events"
TOUCH_LOG="$EVENT_DIR/qemu-touch.jsonl"
STOP_FILE="/tmp/dvm/$TAG.stop"
TAP_MARKER="$EVENT_DIR/settings-tap.triggered"
RELAY_PID_FILE="$REC/input/recap-relay.pid"

[[ -f "$PARENT" && -f "$TC" && -f "$BOOTKC" && -f "$DTREE" ]] || {
    echo "missing durable P548 parent or exact P276 boot inputs" >&2; exit 2;
}
[[ ! -e "$CHILD" ]] || { echo "refusing to reuse child: $CHILD" >&2; exit 2; }
if ps -axo pid=,comm=,args= | awk -v me="$$" '
    $1 != me && $2 ~ /qemu-system-aarch64/ && $0 ~ /-M darwin/ { print; found=1 }
    END { exit(found ? 0 : 1) }
'; then
    echo "another Darwin QEMU guest is already running; refusing to start P549" >&2
    exit 2
fi

mkdir -p "$REC/input" "$REC/frames" /tmp/dvm/probe
"$REPO/qemu-sptm/build/qemu-img" info --backing-chain "$PARENT" > "$REC/parent-chain.txt"
"$REPO/qemu-sptm/build/qemu-img" check -U "$PARENT" > "$REC/parent-check.txt"
"$REPO/qemu-sptm/build/qemu-img" create -f qcow2 -F qcow2 -b "$PARENT" "$CHILD" \
    > "$REC/child-create.txt"
"$REPO/qemu-sptm/build/qemu-img" check -U "$CHILD" > "$REC/child-check-preboot.txt"
shasum -a 256 "$CHILD" "$PARENT" "$TC" "$BOOTKC" "$DTREE" \
    "$REPO/qemu-sptm/build/qemu-system-aarch64" > "$REC/input-hashes.txt"
printf '%s\n' "PARENT=$PARENT" "CHILD=$CHILD" "TC=$TC" \
    "DISPLAY=QEMU Cocoa window via qemu_cocoa_display_wrapper.sh" \
    "NATIVE_DCP_PROFILE=P548; natural A408 scanout enabled" \
    "INSTRUMENTATION=settings_app_lifecycle_callbacks; post-tap gated; read-only" \
    "INPUT=one accepted upward swipe, one native P tap at normalized 20622,25500" \
    "BOUND_SECONDS=$BOUND" > "$REC/experiment.txt"

export DVM_PROBE_QMP_SOCKET="$QMP"
export DVM_SETTINGS_TAP_MARKER="$TAP_MARKER"
export DARWIN_TOUCH_EVENTS="$TOUCH_LOG"
export DARWIN_DCP_EPIC=all DARWIN_DCP_REPLY=1 DARWIN_DCP_IOMFB=4
export DARWIN_DCP_IOMFB_RPC_TRACE=0 DARWIN_SMP_PV=1
export DARWIN_DCP_IOMFB_OUT='A401=01,A000=01,A454=01000000,A033=4152474200000000000000000000000000000000000000000000000000000000000000000000000001000000,A453=9b040000fc090000,A412=01000000'
export DARWIN_DCP_IOMFB_CB='D120::4,D586:9b040000fc090000:4'
export DARWIN_DCP_IOMFB_A406_OBJECTS=0 DARWIN_DCP_IOMFB_A407_COMPLETE=
export DARWIN_DCP_IOMFB_A407_SCANOUT=0 DARWIN_DCP_IOMFB_A407_TRANSFER=0
export DARWIN_DCP_IOMFB_A408_COMPLETE=0 DARWIN_DCP_IOMFB_COMPLETE=0
export DARWIN_DCP_IOMFB_D572=0 DARWIN_DCP_IOMFB_ECHO_MODE=0
export DARWIN_DCP_IOMFB_SCANOUT=1 DARWIN_DCP_IOMFB_SCANOUT_DIAGNOSTICS=0
export DARWIN_INPUT_UART=0

PROBE_PID=""
RELAY_PID=""
cleanup() {
    if [[ -n "$RELAY_PID" ]] && kill -0 "$RELAY_PID" 2>/dev/null; then
        kill -TERM "$RELAY_PID" 2>/dev/null || true
    fi
    if [[ -n "$PROBE_PID" ]] && kill -0 "$PROBE_PID" 2>/dev/null; then
        printf '%s\n' 'runner-exit' > "$STOP_FILE"
        wait "$PROBE_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM HUP

CALLBACKS=settings_app_lifecycle_callbacks SECS="$BOUND" STALL_SECS="$BOUND" \
GDB_PORT="$GDB_PORT" PARENT="$PARENT" CHILD="$CHILD" TC="$TC" \
BOOTKC="$BOOTKC" DTREE="$DTREE" RAMDISK="$REPO/firmware/ramdisk.dmg" \
KEEP_GUEST=0 HOLD_GUEST=0 AUTO_POSTMORTEM=off LLDB_DRAIN_SECS=5 \
LLDB_CONTINUE_RETRIES=0 CHECKPOINT_LAUNCH_MANIFEST="$REC/launch.json" \
PROBE_SUCCESS_LABELS='' \
DVM_QEMU_WRAPPER="$REPO/tools/re/qemu_cocoa_display_wrapper.sh" \
"$REPO/tools/re/setup_gate_probe.sh" "$TAG" > "$REC/setup-gate.log" 2>&1 &
PROBE_PID=$!

_wait=0
until [[ -S "$UART" ]]; do
    if ! kill -0 "$PROBE_PID" 2>/dev/null; then
        echo "setup-gate exited before UART socket creation" >&2
        tail -80 "$REC/setup-gate.log" >&2 || true
        exit 1
    fi
    perl -e 'select(undef,undef,undef,0.05)'; _wait=$((_wait+1))
    if (( _wait > 2400 )); then echo "UART socket did not appear" >&2; exit 1; fi
done
mkdir -p "$EVENT_DIR"
touch "$TOUCH_LOG"
python3 "$REPO/tools/input/relay.py" --events "$TOUCH_LOG" --uart "$UART" \
    --log "$REC/input/recap-relay.jsonl" --ack-timeout 120 --backend recap \
    > "$REC/input/recap-relay.stdout.log" 2> "$REC/input/recap-relay.stderr.log" &
RELAY_PID=$!
printf '%s\n' "$RELAY_PID" > "$RELAY_PID_FILE"
sleep 1
if ! kill -0 "$RELAY_PID" 2>/dev/null; then
    echo "persistent Recap relay failed to connect" >&2
    cat "$REC/input/recap-relay.stderr.log" >&2 || true
    exit 1
fi
printf '%s\n' "connected_before_input_ready pid=$RELAY_PID" > "$REC/input/relay-connected.txt"

set +e
python3 "$REPO/tools/re/drive_settings_app_probe.py" "$TAG" \
    --record "$REC" --ready-timeout "$BOUND" --swipe-ack-timeout 600 \
    --relay-pid-file "$RELAY_PID_FILE" --stop-file "$STOP_FILE" \
    --tap-marker "$TAP_MARKER" > "$REC/input/driver.stdout.log" 2>&1
DRIVER_RC=$?
set -e
echo "$DRIVER_RC" > "$REC/input/driver-exit-code.txt"

if [[ -n "$RELAY_PID" ]] && kill -0 "$RELAY_PID" 2>/dev/null; then
    kill -TERM "$RELAY_PID" 2>/dev/null || true
    wait "$RELAY_PID" 2>/dev/null || true
fi
RELAY_PID=""

set +e
wait "$PROBE_PID"
PROBE_RC=$?
set -e
PROBE_PID=""

for suffix in lldb.log probe.out slide.json lldb.cmd watch.log; do
    [[ -f "/tmp/dvm/$TAG.$suffix" ]] && cp -a "/tmp/dvm/$TAG.$suffix" "$REC/"
done
for suffix in serial stderr; do
    [[ -f "/tmp/dvm/probe/$TAG.$suffix.log" ]] && \
        cp -a "/tmp/dvm/probe/$TAG.$suffix.log" "$REC/$suffix.log"
done
[[ -d "$EVENT_DIR" ]] && rsync -a "$EVENT_DIR/" "$REC/events/"
"$REPO/qemu-sptm/build/qemu-img" info --backing-chain "$CHILD" > "$REC/child-chain.txt"
"$REPO/qemu-sptm/build/qemu-img" check -U "$CHILD" > "$REC/child-check.txt"
shasum -a 256 "$CHILD" > "$REC/child-sha256.txt"
grep -En 'panic\(cpu|rebooting due to critical process crashes|EXIT_REASON_' \
    "$REC/serial.log" "$REC/stderr.log" > "$REC/panic-scan.txt" || true
printf 'driver_rc=%s\nprobe_rc=%s\n' "$DRIVER_RC" "$PROBE_RC" > "$REC/result-codes.txt"

# P549's scratch is now fully archived; remove only this run's ephemeral files.
rm -rf -- "$EVENT_DIR" "/tmp/dvm/$TAG.frames"
rm -f -- "/tmp/dvm/$TAG".* "/tmp/dvm/probe/$TAG.serial.log" \
    "/tmp/dvm/probe/$TAG.stderr.log"
trap - EXIT INT TERM HUP
echo "P549 archived at $REC"
echo "driver_rc=$DRIVER_RC probe_rc=$PROBE_RC"
exit "$DRIVER_RC"

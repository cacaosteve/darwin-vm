#!/usr/bin/env bash
# P172: minimal Update-flag/V170 correlation plus the validated Home/swipe flow.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ART="$HOME/dvm-artifacts/bp-patched-dev6/boot1"
TAG="${1:-UI_CLASS16_REFRESH60_DM_LOCK_PLUGIN172_P134_UPDATE_GATE_MIN1}"
REC="$ART/recovery-records/$TAG"
PARENT="${PARENT:-$ART/ui-class16-refresh60-dm-lock-plugin134-gesture-state1.qcow2}"
CHILD="/tmp/dvm/data-seed/$(printf '%s' "$TAG" | tr 'A-Z_' 'a-z-').qcow2"
TC="$HOME/dvm-artifacts/bp-patched-dev6/input-v12-native/system-plus-dev6-input.tc"
mkdir -p "$REC" /tmp/dvm/data-seed /tmp/dvm/probe
[[ ! -e "$CHILD" ]] || { echo "refusing to reuse child: $CHILD" >&2; exit 1; }
for file in "$PARENT" "$TC" "$HOME/dvm-artifacts/bp-patched-dev6/bootkc" \
            "$HOME/dvm-artifacts/bp-patched-dev6/system.dtree"; do
    [[ -f "$file" ]] || { echo "missing required input: $file" >&2; exit 1; }
done

export PARENT CHILD TC
export GDB_PORT="${GDB_PORT:-1272}"
export SECS="${SECS:-4200}"
export FRAME_SECS="${FRAME_SECS:-3900}"
export NATIVE_INPUT=1 INPUT_BOOTSTRAP=0
export POST_SHELL_UI_MODULE="${POST_SHELL_UI_MODULE:-dm_post_shell_home_gesture_present_callbacks}"
export DVM_POSTHOME_RENDER_SCHEDULER_TRACE="${DVM_POSTHOME_RENDER_SCHEDULER_TRACE:-0}"
export DVM_POSTHOME_CA_HANDOFF_TRACE="${DVM_POSTHOME_CA_HANDOFF_TRACE:-0}"
export DVM_POSTHOME_UPDATE_GATE_MIN_TRACE="${DVM_POSTHOME_UPDATE_GATE_MIN_TRACE:-1}"
export DVM_POSTHOME_UPDATE_GATE_AFTER_LOCK_TRANSITION="${DVM_POSTHOME_UPDATE_GATE_AFTER_LOCK_TRANSITION:-0}"
export DVM_CA_MSG_AFTER_LOCK_TRANSITION="${DVM_CA_MSG_AFTER_LOCK_TRANSITION:-0}"
export HOME_ENTITY_TIMEOUT="${HOME_ENTITY_TIMEOUT:-240}"
export POST_INPUT_OBSERVE="${POST_INPUT_OBSERVE:-2500}"
export INITIAL_SWIPE_DELAY="${INITIAL_SWIPE_DELAY:-0}"
export SWIPE_HOLD_MS="${SWIPE_HOLD_MS:-600}"
export SWIPE_STEPS="${SWIPE_STEPS:-32}"
export SWIPE_COORDS="${SWIPE_COORDS:-589 2540 589 500}"
export SWIPE_DISPATCH_EACH_STEP="${SWIPE_DISPATCH_EACH_STEP:-1}"
export SWIPE_DISPATCH_STEP_TIMEOUT="${SWIPE_DISPATCH_STEP_TIMEOUT:-60}"
export FOLLOWUP_SWIPE_DELAY="${FOLLOWUP_SWIPE_DELAY:-700}"
export FOLLOWUP_SWIPE_HOLD_MS="${FOLLOWUP_SWIPE_HOLD_MS:-600}"
export FOLLOWUP_SWIPE_STEPS="${FOLLOWUP_SWIPE_STEPS:-32}"
export HOME_AFTER_ACCEPTED_TRANSITION="${HOME_AFTER_ACCEPTED_TRANSITION:-0}"
export SWIPE_AFTER_HOME_TIMEOUT="${SWIPE_AFTER_HOME_TIMEOUT:-0}"
export DVM_PROBE_SUCCESS_LABELS=""
export REQUIRE_SHELL_RETURN="${REQUIRE_SHELL_RETURN:-1}"
case "$REQUIRE_SHELL_RETURN" in
    0|1) ;;
    *) echo "REQUIRE_SHELL_RETURN must be 0 or 1" >&2; exit 2 ;;
esac
SHELL_RETURN_ARGS=()
if [[ "$REQUIRE_SHELL_RETURN" == 1 ]]; then
    SHELL_RETURN_ARGS+=(--require-shell-service-return)
fi

"$REPO/qemu-sptm/build/qemu-img" info --backing-chain "$PARENT" \
    > "$REC/parent-backing-chain.txt"
"$REPO/qemu-sptm/build/qemu-img" check -U "$PARENT" \
    > "$REC/parent-qemu-img-check.txt"
shasum -a 256 "$PARENT" "$TC" > "$REC/input-sha256.txt"

RUN_LOG="$REC/outer.log"
TRIGGER_LOG="$REC/trigger-stdout.json"
"$REPO/tools/re/run_post_shell_home_render.sh" "$TAG" > "$RUN_LOG" 2>&1 &
RUN_PID=$!
TRIGGER_PID=""
cleanup() {
    rc=$?
    if [[ -n "$TRIGGER_PID" ]] && kill -0 "$TRIGGER_PID" 2>/dev/null; then
        kill "$TRIGGER_PID" 2>/dev/null || true
    fi
    if kill -0 "$RUN_PID" 2>/dev/null; then
        kill -TERM "$RUN_PID" 2>/dev/null || true
    fi
    wait "$TRIGGER_PID" 2>/dev/null || true
    wait "$RUN_PID" 2>/dev/null || true
    exit "$rc"
}
trap cleanup EXIT INT TERM HUP

# The trigger waits for the guest input helper, this boot's callback-ready
# marker, and (by default) the real shell-service return before sending input.
# A caller can select the accepted P141/P170 bottom-edge coordinates.
read -r -a SWIPE_COORDS_ARRAY <<< "$SWIPE_COORDS"
if [[ "${#SWIPE_COORDS_ARRAY[@]}" -ne 4 ]]; then
    echo "SWIPE_COORDS must contain four integers" >&2
    exit 2
fi
SWIPE_DISPATCH_ARGS=()
case "$SWIPE_DISPATCH_EACH_STEP" in
    0) ;;
    1) SWIPE_DISPATCH_ARGS+=(--swipe-dispatch-each-step
                             --swipe-dispatch-step-timeout
                             "$SWIPE_DISPATCH_STEP_TIMEOUT") ;;
    *) echo "SWIPE_DISPATCH_EACH_STEP must be 0 or 1" >&2; exit 2 ;;
esac
SWIPE_AFTER_HOME_TIMEOUT_ARGS=()
case "$SWIPE_AFTER_HOME_TIMEOUT" in
    0) ;;
    1) SWIPE_AFTER_HOME_TIMEOUT_ARGS+=(--swipe-after-home-timeout) ;;
    *) echo "SWIPE_AFTER_HOME_TIMEOUT must be 0 or 1" >&2; exit 2 ;;
esac
HOME_AFTER_ACCEPTED_TRANSITION_ARGS=()
case "$HOME_AFTER_ACCEPTED_TRANSITION" in
    0) ;;
    1) HOME_AFTER_ACCEPTED_TRANSITION_ARGS+=(--home-after-accepted-transition) ;;
    *) echo "HOME_AFTER_ACCEPTED_TRANSITION must be 0 or 1" >&2; exit 2 ;;
esac
python3 "$REPO/tools/re/trigger_home_then_unlock_dvmi2.py" "$TAG" \
    --timeout 3900 --home-entity-timeout "$HOME_ENTITY_TIMEOUT" \
    --post-input-observe "$POST_INPUT_OBSERVE" \
    --initial-swipe-delay "$INITIAL_SWIPE_DELAY" \
    --swipe-coords "${SWIPE_COORDS_ARRAY[@]}" \
    --swipe-hold-ms "$SWIPE_HOLD_MS" --swipe-steps "$SWIPE_STEPS" \
    "${SWIPE_DISPATCH_ARGS[@]}" \
    --followup-swipe-delay "$FOLLOWUP_SWIPE_DELAY" \
    --followup-swipe-hold-ms "$FOLLOWUP_SWIPE_HOLD_MS" \
    --followup-swipe-steps "$FOLLOWUP_SWIPE_STEPS" --retry-observe 300 \
    "${HOME_AFTER_ACCEPTED_TRANSITION_ARGS[@]}" \
    "${SWIPE_AFTER_HOME_TIMEOUT_ARGS[@]}" \
    "${SHELL_RETURN_ARGS[@]}" \
    > "$TRIGGER_LOG" 2> "$REC/trigger-stderr.log" &
TRIGGER_PID=$!

set +e
wait "$RUN_PID"
RUN_RC=$?
TRIGGER_CANCEL_REASON=""
if [[ "$RUN_RC" -ne 0 ]] && [[ -n "$TRIGGER_PID" ]] && \
   kill -0 "$TRIGGER_PID" 2>/dev/null; then
    # The UI trigger cannot produce a scored result after observer setup or
    # the guest runner has failed. End its readiness wait instead of leaving
    # the orchestrator parked until the independent 3900-second timeout.
    TRIGGER_CANCEL_REASON="runner-exited-nonzero-before-trigger-completion"
    kill -TERM "$TRIGGER_PID" 2>/dev/null || true
fi
wait "$TRIGGER_PID"
TRIGGER_RC=$?
set -e
TRIGGER_PID=""
printf 'runner_rc=%s\ntrigger_rc=%s\ntrigger_cancel_reason=%s\n' \
    "$RUN_RC" "$TRIGGER_RC" "$TRIGGER_CANCEL_REASON" \
    > "$REC/orchestrator-status.txt"
[[ -f "$REC/child-sha256.txt" ]] || true
exit "$RUN_RC"

#!/bin/bash
# Build the missing merged iOS System/cryptex image without mounting the full
# System volume on macOS. The cryptex is staged as a second APFS volume; a
# signed helper then overlays it from a disposable restore guest running as
# root. Only the small restore ramdisk and read-only cryptex are host-mounted.
#
# Usage: tools/rootfs/build_merged_system_guest.sh [artifact-root]
#        default: ~/dvm-artifacts
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
ARTIFACT_ROOT=${1:-"$HOME/dvm-artifacts"}
PAYLOAD="$ARTIFACT_ROOT/aea/out"
RAW="$ARTIFACT_ROOT/aea/raw"
BUILD="$ARTIFACT_ROOT/build"
EXPORT="$ARTIFACT_ROOT/extract"
WORK="$ARTIFACT_ROOT/merge-work"
SYSVOL="$PAYLOAD/094-13182-141.dmg"
CRYPTEX="$PAYLOAD/094-13150-145.dmg"
OUT="$BUILD/rootfs_cx.dmg"
MERGED_TC="$ARTIFACT_ROOT/tc/merged_sysvol_cryptex_tc.bin"
RESTORE="$WORK/merge-ramdisk.dmg"
RESTORE_TC="$WORK/merge-ramdisk.tc"
DTREE_IM4P="$REPO/ipsw_db/24A5430a__iPhone17,3/DeviceTree.d47ap.im4p"
DTREE_RAW="$WORK/dtree.raw"
DTREE="$WORK/dt-merge.bin"
QEMU="$REPO/qemu-sptm/build/qemu-system-aarch64"
TAG="CXMERGE_$$"
UART="/tmp/dvm/$TAG.uart.sock"
MONITOR="/tmp/dvm/$TAG.sock"
PROBE_PID=""
ATTACHED=()
MOUNTED=""
STAGE_MOUNT=""

die() { echo "build-merged-system: $*" >&2; exit 1; }

cleanup() {
    if [[ -n "$PROBE_PID" ]] && kill -0 "$PROBE_PID" 2>/dev/null; then
        kill "$PROBE_PID" 2>/dev/null || true
        wait "$PROBE_PID" 2>/dev/null || true
    fi
    if [[ -n "$MOUNTED" ]]; then
        "$REPO/tools/rootfs/safe_attach.sh" detach "$MOUNTED" >/dev/null 2>&1 || true
    fi
    if [[ -n "$STAGE_MOUNT" ]]; then
        "$REPO/tools/rootfs/safe_attach.sh" unmount-device "$STAGE_MOUNT" >/dev/null 2>&1 || true
    fi
    local index
    for ((index=${#ATTACHED[@]}-1; index>=0; index--)); do
        "$REPO/tools/rootfs/safe_attach.sh" detach "${ATTACHED[index]}" >/dev/null 2>&1 || true
    done
}
trap cleanup EXIT
trap 'exit 130' INT TERM

apfs_layout() {
    local physical=${1#/dev/}
    diskutil apfs list -plist | python3 -c '
import plistlib, sys
physical = sys.argv[1]
data = plistlib.loads(sys.stdin.buffer.read())
for container in data.get("Containers", []):
    stores = {item.get("DeviceIdentifier") for item in container.get("PhysicalStores", [])}
    if physical in stores:
        values = [container["ContainerReference"]]
        values.extend(volume["DeviceIdentifier"] for volume in container.get("Volumes", []))
        print(" ".join(values))
        break
else:
    raise SystemExit("no APFS container for " + physical)
' "$physical"
}

attach_nomount() {
    local image=$1; shift
    local device
    device=$("$REPO/tools/rootfs/safe_attach.sh" attach "$image" --nomount "$@") \
        || die "could not attach $image"
    ATTACHED+=("$device")
    ATTACH_RESULT=$device
}

detach_exact() {
    local device=$1
    "$REPO/tools/rootfs/safe_attach.sh" detach "$device" >/dev/null \
        || die "could not detach $device"
    local next=() value
    for value in "${ATTACHED[@]}"; do
        [[ "$value" == "$device" ]] || next+=("$value")
    done
    if (( ${#next[@]} )); then
        ATTACHED=("${next[@]}")
    else
        ATTACHED=()
    fi
}

wait_for_marker() {
    local log=$1 marker=$2 limit=${3:-600} waited=0
    while (( waited < limit )); do
        grep -Fqa -- "$marker" "$log" && return 0
        grep -qa 'panic(cpu' "$log" && die "guest panicked before '$marker'; see $log"
        kill -0 "$PROBE_PID" 2>/dev/null || die "guest stopped before '$marker'; see $log"
        sleep 3
        waited=$((waited + 3))
    done
    die "guest did not report '$marker' within ${limit}s; see $log"
}

send_and_wait() {
    local log=$1 command=$2 marker=$3 limit=${4:-600}
    python3 "$REPO/tools/serial.py" "$UART" send "$command" \
        --log "$WORK/$TAG.console.log" --prompt-timeout 30 --echo-timeout 30 \
        --char-delay 0.02 >/dev/null \
        || die "serial transport rejected command"
    wait_for_marker "$log" "$marker" "$limit"
}

for file in "$SYSVOL" "$CRYPTEX" "$DTREE_IM4P" "$REPO/firmware/ramdisk.dmg" \
            "$REPO/firmware/ramdisk.tc" "$QEMU"; do
    [[ -f "$file" ]] || die "missing $file (run tools/rootfs/fetch_payloads.sh first)"
done
mkdir -p "$BUILD" "$EXPORT/dyld" "$WORK" "$ARTIFACT_ROOT/tc" /tmp/dvm

if [[ -f "$WORK/merge-complete" && -f "$OUT" && -f "$MERGED_TC" \
      && -f "$EXPORT/powerd.original" && -f "$EXPORT/launchd-original.plist" \
      && -f "$EXPORT/dyld/dyld_shared_cache_arm64e.01" \
      && -f "$EXPORT/dyld/dyld_shared_cache_arm64e.13" \
      && -f "$EXPORT/dyld/dyld_shared_cache_arm64e.21" ]]; then
    echo "==> reuse verified completed merged image and profile inputs"
    shasum -a 256 "$OUT" "$MERGED_TC" "$EXPORT/powerd.original" \
        "$EXPORT/launchd-original.plist" "$EXPORT/dyld/"* > "$WORK/outputs.sha256"
    echo "DONE image=$OUT"
    echo "DONE trustcache=$MERGED_TC"
    echo "DONE profile-inputs=$EXPORT"
    exit 0
fi

if [[ -f "$WORK/staging-ready" && -f "$OUT" ]]; then
    echo "==> reuse verified completed cryptex staging copy"
else
    [[ ! -e "$OUT" ]] || die "output already exists without $WORK/staging-ready: $OUT"
    echo "==> clone and grow the System image"
    cp -c "$SYSVOL" "$OUT"
    old_size=$(stat -f%z "$OUT")
    new_size=$((old_size + 12 * 1024 * 1024 * 1024))
    truncate -s "$new_size" "$OUT"

    echo "==> add a temporary cryptex staging volume without host-mounting System"
    attach_nomount "$OUT"
    dst_device=$ATTACH_RESULT
    read -r dst_container dst_system <<<"$(apfs_layout "$dst_device")"
    diskutil apfs resizeContainer "$dst_container" 0 >/dev/null
    diskutil apfs addVolume "$dst_container" APFS CryptexStaging \
        > "$WORK/add-staging-volume.log" 2>&1 || true
    read -r dst_container dst_system dst_staging <<<"$(apfs_layout "$dst_device")"
    [[ -n "${dst_staging:-}" ]] || die "temporary APFS volume was not created"

    # addVolume mounts only the new, empty staging volume. Remount that exact
    # volume through the safety wrapper so fseventsd/Spotlight are suppressed.
    "$REPO/tools/rootfs/safe_attach.sh" unmount-device "/dev/$dst_staging" \
        >/dev/null 2>&1 || true
    STAGE_MOUNT=$("$REPO/tools/rootfs/safe_attach.sh" mount-device "/dev/$dst_staging" --owners off)
    MOUNTED=$("$REPO/tools/rootfs/safe_attach.sh" attach "$CRYPTEX" --readonly --owners on)
    /usr/bin/osascript - "$MOUNTED" "$STAGE_MOUNT" <<'APPLESCRIPT'
on run argv
    set sourcePath to item 1 of argv
    set targetPath to item 2 of argv
    set copyCommand to "/usr/bin/ditto --rsrc --extattr --acl " & quoted form of (sourcePath & "/") & " " & quoted form of (targetPath & "/")
    do shell script copyCommand with administrator privileges
end run
APPLESCRIPT
    sync
    "$REPO/tools/rootfs/safe_attach.sh" detach "$MOUNTED" >/dev/null
    MOUNTED=""
    "$REPO/tools/rootfs/safe_attach.sh" unmount-device "$STAGE_MOUNT" >/dev/null
    STAGE_MOUNT=""
    detach_exact "$dst_device"
    printf 'cryptex-staging-complete\n' > "$WORK/staging-ready"
fi

echo "==> build and trust the guest-side merge helper"
xcrun --sdk macosx clang -target arm64-apple-ios7.0 \
    -isysroot "$(xcrun --sdk macosx --show-sdk-path)" -Os -Wall -Wextra -Werror \
    -Wno-incompatible-sysroot "$REPO/tools/rootfs/cryptex_merge_helper.c" \
    -o "$WORK/dvm-cryptex-merge"
codesign --force --sign - --timestamp=none "$WORK/dvm-cryptex-merge"
codesign -d --verbose=4 "$WORK/dvm-cryptex-merge" 2>&1 \
    | awk -F= '/^CDHash=/{print $2}' > "$WORK/helper.hashes"
[[ -s "$WORK/helper.hashes" ]] || die "could not obtain helper CDHash"
python3 "$REPO/build_tc.py" "$WORK/helper.hashes" "$WORK/helper.tc"
python3 "$REPO/tools/rootfs/merge_tc.py" "$RESTORE_TC" \
    "$REPO/firmware/ramdisk.tc" "$WORK/helper.tc"
cp -c "$REPO/firmware/ramdisk.dmg" "$RESTORE"
MOUNTED=$("$REPO/tools/rootfs/safe_attach.sh" attach "$RESTORE" --owners off)
cp "$WORK/dvm-cryptex-merge" "$MOUNTED/libexec/dvm-cryptex-merge"
chmod 755 "$MOUNTED/libexec/dvm-cryptex-merge"
xattr -c "$MOUNTED/libexec/dvm-cryptex-merge" 2>/dev/null || true
sync
"$REPO/tools/rootfs/safe_attach.sh" detach "$MOUNTED" >/dev/null
MOUNTED=""

if [[ ! -s "$DTREE_RAW" ]]; then
    ipsw img4 im4p extract --output "$DTREE_RAW" "$DTREE_IM4P" >/dev/null
fi
python3 "$REPO/dt_fixup.py" "$DTREE_RAW" "$DTREE" -nvram "$REPO/nvram.bin" \
    -enable ans -enable smc -enable sep -dram 12G >/dev/null

echo "==> merge the cryptex inside the restore guest"
PROBE_OUT="$WORK/probe"
SERIAL="$PROBE_OUT/$TAG.serial.log"
"$REPO/tools/probe.sh" --dtree "$DTREE" --ramdisk "$RESTORE" \
    --tc "$RESTORE_TC" --mem 12G --secs 2400 --tag "$TAG" --out "$PROBE_OUT" \
    --uart-socket "$UART" --bootargs 'rd=md0 serial=3 -v wdt=-1 wlan-olyhal-abort' \
    -- --drive "if=none,id=ans,file=$OUT,format=raw" \
    > "$WORK/$TAG.verdict.log" 2>&1 &
PROBE_PID=$!
wait_for_marker "$SERIAL" "can't access tty" 180
send_and_wait "$SERIAL" \
    'mkdir -p /mnt1 /mnt2; mount_apfs /dev/disk1s1 /mnt1; echo DVM_SYSTEM_MOUNT_RC=$?' \
    'DVM_SYSTEM_MOUNT_RC=0' 120
send_and_wait "$SERIAL" \
    'mount_apfs -o ro /dev/disk1s2 /mnt2; echo DVM_CRYPTEX_MOUNT_RC=$?' \
    'DVM_CRYPTEX_MOUNT_RC=0' 120
send_and_wait "$SERIAL" \
    '/libexec/dvm-cryptex-merge /mnt2 /mnt1; rc=$?; sync; echo DVM_CRYPTEX_HELPER_RC=$rc' \
    'DVM_CRYPTEX_HELPER_RC=0' 1800
grep -Fqa 'DVM_CRYPTEX_MERGE_OK' "$SERIAL" \
    || die "helper returned without its verification witness"

# Small exact preimages needed by bootstrap_profile are copied into the
# disposable staging volume. The full System volume remains unmounted on the
# host, and the staging volume is deleted after those files are recovered.
send_and_wait "$SERIAL" \
    'mount -uw /mnt2; mkdir -p /mnt2/.dvm-exports; cp /mnt1/System/Library/CoreServices/powerd.bundle/powerd /mnt2/.dvm-exports/powerd.original; cp /mnt1/System/Library/xpc/launchd.plist /mnt2/.dvm-exports/launchd-original.plist; sync; test -s /mnt2/.dvm-exports/powerd.original -a -s /mnt2/.dvm-exports/launchd-original.plist; echo DVM_EXPORTS_RC=$?' \
    'DVM_EXPORTS_RC=0' 180

kill "$PROBE_PID" 2>/dev/null || true
wait "$PROBE_PID" 2>/dev/null || true
PROBE_PID=""

echo "==> remove the temporary cryptex volume"
attach_nomount "$OUT"
dst_device=$ATTACH_RESULT
read -r dst_container dst_system dst_staging <<<"$(apfs_layout "$dst_device")"
[[ -n "${dst_staging:-}" ]] || die "temporary cryptex volume disappeared"

echo "==> recover the small original profile inputs from the staging volume"
STAGE_MOUNT=$("$REPO/tools/rootfs/safe_attach.sh" mount-device "/dev/$dst_staging" --owners on)
cp "$STAGE_MOUNT/.dvm-exports/powerd.original" "$EXPORT/powerd.original"
cp "$STAGE_MOUNT/.dvm-exports/launchd-original.plist" "$EXPORT/launchd-original.plist"
"$REPO/tools/rootfs/safe_attach.sh" unmount-device "$STAGE_MOUNT" >/dev/null
STAGE_MOUNT=""
diskutil apfs deleteVolume "/dev/$dst_staging" >/dev/null
detach_exact "$dst_device"

echo "==> merge Apple trust caches"
sys_tc=$(find "$RAW" -name '094-13182-141.dmg.aea.trustcache' -o -name '094-13182-141.dmg.trustcache' | head -1)
cx_tc=$(find "$RAW" -name '094-13150-145.dmg.aea.trustcache' -o -name '094-13150-145.dmg.trustcache' | head -1)
[[ -n "$sys_tc" && -n "$cx_tc" ]] || die "matching trustcache wrappers were not downloaded"
ipsw img4 im4p extract "$sys_tc" -o "$WORK/system.tc" >/dev/null
ipsw img4 im4p extract "$cx_tc" -o "$WORK/cryptex.tc" >/dev/null
python3 "$REPO/tools/rootfs/merge_tc.py" "$MERGED_TC" "$WORK/system.tc" "$WORK/cryptex.tc"

echo "==> export the three original shared-cache files from the read-only cryptex"
MOUNTED=$("$REPO/tools/rootfs/safe_attach.sh" attach "$CRYPTEX" --readonly --owners on)
cache_root="$MOUNTED/System/Library/Caches/com.apple.dyld"
for name in dyld_shared_cache_arm64e.01 dyld_shared_cache_arm64e.13 dyld_shared_cache_arm64e.21; do
    [[ -f "$cache_root/$name" ]] || die "missing cache $name in cryptex"
    cp "$cache_root/$name" "$EXPORT/dyld/$name"
done
"$REPO/tools/rootfs/safe_attach.sh" detach "$MOUNTED" >/dev/null
MOUNTED=""

shasum -a 256 "$OUT" "$MERGED_TC" "$EXPORT/powerd.original" \
    "$EXPORT/launchd-original.plist" "$EXPORT/dyld/"* > "$WORK/outputs.sha256"
printf 'guest-cryptex-merge-complete\n' > "$WORK/merge-complete"
echo "DONE image=$OUT"
echo "DONE trustcache=$MERGED_TC"
echo "DONE profile-inputs=$EXPORT"

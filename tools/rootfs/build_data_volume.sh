#!/bin/bash
# Add an empty APFS Data volume to a copy of the iOS system-volume image.
#
# WHY THIS REPLACES build_dual_volume.sh
#
# build_dual_volume.sh cloned the whole 21 GB system volume, deleted roughly
# half a million files to keep the 697 MB /private/var template, then renamed
# the template to the volume root. The rename was deliberate: we have no sudo,
# and a non-root copy cannot preserve uid 0 / uid 501(mobile) ownership, while
# a rename preserves inodes and therefore ownership for free.
#
# That mass delete kernel-panicked this host three times on 2026-09-02, every
# time with the same signature:
#
#     panic(cpu N caller ...): "Data ObjId overflow" @jobj.c:1152
#     Panicked task ...: pid 109: fseventsd
#
# jobj.c is APFS's object-ID allocator; deleting ~500k files burns object IDs
# and drives apfs_purgatory_cleaner, and fseventsd is logging every one of
# those events onto the same volume. Suppressing Spotlight (.metadata_never_index,
# nobrowse) does NOT help: fseventsd honours neither. The file that stops
# fseventsd is /.fseventsd/no_log at the volume root, and we now write it at
# every mount -- but the real fix is not to generate the churn at all.
#
# So: we do not build the template on the host. We create an EMPTY Data volume
# for the guest-format and guest-mount experiments. Do not mistake that for a
# bootable persistent-var image: on 2026-09-02 both normal and -skip-keybag
# system boots mounted an empty encrypted Data volume and produced zero
# `Copying ` records, so this build has not found a guest seeder yet. Preboot
# and Hardware also need empty slots: the disk fstab resolves *every* remaining
# entry by APFS role before it will mount Data, so a Data-only container leaves
# /private/var read-only.
#
# Host filesystem churn here: one APFS clone (instant, copy-on-write), one
# image resize, three addVolume calls. No file deletions at all.
#
# Usage:
#   tools/rootfs/build_data_volume.sh [out.dmg] [grow_gb]
# Env:
#   SRC   source system-volume image (default ~/dvm-artifacts/build/rootfs.dmg)
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SAFE_ATTACH="$REPO/tools/rootfs/safe_attach.sh"
SRC=${SRC:-$HOME/dvm-artifacts/build/rootfs.dmg}
OUT=${1:-$HOME/dvm-artifacts/build/rootfs_dual.dmg}
GROW_GB=${2:-4}
VOLNAME=${VOLNAME:-Data}

die() { echo "!! $*" >&2; exit 1; }

[ -f "$SRC" ] || die "missing source image: $SRC"

# Refuse to run while the source or target is already attached: concurrent
# read-write attachments of the same image have silently corrupted one before.
for img in "$SRC" "$OUT"; do
    if hdiutil info 2>/dev/null | grep -qF "image-path      : $img"; then
        die "already attached: $img (detach it first)"
    fi
done

OURDEV=""
CONTAINER=""
cleanup() {
    if [ -n "$OURDEV" ]; then
        echo "==> cleanup: detaching $OURDEV"
        "$SAFE_ATTACH" detach "$OURDEV" >/dev/null 2>&1 || true
    fi
}
# Detach our own container on any exit path, including a failure partway
# through. Never a blanket detach: other people's volumes (Xcode simulator
# runtimes among them) are mounted on this machine.
trap cleanup EXIT

echo "==> clone $SRC -> $OUT (APFS copy-on-write, instant)"
rm -f "$OUT"
cp -c "$SRC" "$OUT" || die "clone failed"

cur=$(stat -f%z "$OUT")
new=$(( cur + GROW_GB * 1024 * 1024 * 1024 ))
echo "==> extend the image by ${GROW_GB} GiB ($cur -> $new bytes, sparse)"
# NOT hdiutil resize: that only understands UDIF images, and this file is a
# RAW APFS container that merely has a .dmg name (NXSB magic at offset 0x20,
# no UDIF trailer -- qemu-img rejects it for the same reason). Extending a raw
# image is just making the file longer; dd with count=0 does it sparsely, so
# the 21 GB of real data stays 21 GB on disk.
dd if=/dev/zero of="$OUT" bs=1 count=0 seek="$new" 2>/dev/null \
    || die "could not extend $OUT"

echo "==> attach (no mount: we never need to mount the system volume)"
OURDEV=$("$SAFE_ATTACH" attach "$OUT" --nomount --owners off) \
    || die "could not attach the APFS image"
[ -b "$OURDEV" ] || die "attachment did not return a physical device: $OURDEV"
CONTAINER=$(diskutil apfs list -plist | python3 -c '
import plistlib, sys
physical = sys.argv[1].removeprefix("/dev/")
data = plistlib.loads(sys.stdin.buffer.read())
for container in data.get("Containers", []):
    stores = {item.get("DeviceIdentifier") for item in container.get("PhysicalStores", [])}
    if physical in stores:
        print("/dev/" + container["ContainerReference"])
        break
else:
    raise SystemExit("no APFS container for " + physical)
' "$OURDEV") || die "could not resolve APFS container for $OURDEV"
[ -b "$CONTAINER" ] || die "resolved APFS container is not a device: $CONTAINER"
echo "    image $OURDEV, container $CONTAINER"

echo "==> grow the container into the new space"
diskutil apfs resizeContainer "$CONTAINER" 0 >/dev/null || die "resizeContainer failed"

# APFSX, not APFS: iOS system and data volumes are both CASE-SENSITIVE, and
# diskutil's "APFS" makes a case-insensitive volume. Getting this wrong is the
# kind of thing that surfaces as a mysterious file-not-found deep in a boot.
echo "==> add an empty case-sensitive volume '$VOLNAME' with role D"
diskutil apfs addVolume "$CONTAINER" APFSX "$VOLNAME" -role D -nomount >/dev/null \
    || die "addVolume failed"

# `dt_fixup.py` drops only the three roles macOS cannot create (xART,
# Baseband-Data, Update).  It keeps these two, and DT_get_fstab_entries rejects
# the entire table at the first missing role before mount-phase-2 can mount Data.
for spec in 'Preboot:B' 'Hardware:H'; do
    name=${spec%%:*}
    role=${spec##*:}
    echo "==> add empty case-sensitive volume '$name' with role $role"
    diskutil apfs addVolume "$CONTAINER" APFSX "$name" -role "$role" -nomount >/dev/null \
        || die "addVolume $name failed"
done

echo "==> resulting volume layout"
diskutil apfs list "$CONTAINER" | grep -E "APFS Volume|Role|Name:|Capacity" | sed 's/^/    /'

echo "==> done: $OUT"
echo
echo "Next: format Data in the guest, then boot without -ephemeral-data to test it."
echo "The current guest path mounts an empty Data volume but does not seed it;"
echo "bootstrap_data_volume.sh fails closed unless a future boot proves copies."

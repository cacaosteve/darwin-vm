#!/bin/bash
# Stage the input helper in a new, small restore image. Never mount the system disk.
set -euo pipefail
repo=$(cd "$(dirname "$0")/../.." && pwd)
out=${1:-/tmp/dvm/native-input}
image="$out/ramdisk-input.dmg"
test -f "$out/dvm-input"
test ! -e "$image"
cp -p "$repo/firmware/ramdisk.dmg" "$image"
# This is a disposable helper image copied from the repository, whose files
# are root-owned. Host ownership emulation makes those paths unwritable for
# the normal developer account; guest ownership is supplied by the restore
# environment, so write the staged files with host ownership disabled.
mnt=$("$repo/tools/rootfs/safe_attach.sh" attach "$image" --owners off)
trap '"$repo/tools/rootfs/safe_attach.sh" detach "$mnt"' EXIT
test -d "$mnt/libexec"
cp -X "$out/dvm-input" "$mnt/libexec/dvm-input"
cp -X "$repo/tools/input/com.apple.dvm-input.plist" "$mnt/libexec/dvm-input.plist"
# INSTALLER selects the guest-side script (install_in_guest.sh for the v1
# helper, install_hid_in_guest.sh for the DVMI2 helper); INSTALLER_NAME is
# its /libexec name inside the ramdisk.
cp -X "$repo/tools/input/${INSTALLER:-install_in_guest.sh}" "$mnt/libexec/${INSTALLER_NAME:-dvm-input-install.sh}"
chmod 755 "$mnt/libexec/dvm-input"
cmp "$out/dvm-input" "$mnt/libexec/dvm-input"
sync
"$repo/tools/rootfs/safe_attach.sh" detach "$mnt"
trap - EXIT
python3 "$repo/tools/rootfs/merge_tc.py" "$out/restore.tc" \
    "$repo/firmware/ramdisk.tc" "$out/helper.tc"

#!/bin/bash
# Build a bootable iOS disk with a real, persistent /private/var -- and rebuild
# it from scratch whenever the guest's Data volume gets into a bad state.
#
# This is the "restore" for this project. Everything it produces is derived, so
# losing the outputs costs a rerun rather than the work.
#
# WHY THREE PHASES
#
# /private/var has to be a real APFS volume, not the -ephemeral-data tmpfs that
# every boot re-seeds by copying 41,557 files (~213 s of guest time, roughly 85%
# of boot). Three things have to be true at once, and each defeated a simpler
# approach:
#
#   1. The image needs a Data volume slot. macOS can create that (`diskutil apfs
#      addVolume -role D`), so `build_data_volume.sh` does it -- see its header
#      for why it does NOT try to populate the volume, and how the earlier
#      clone-and-delete approach kernel-panicked this host three times.
#
#   2. The volume must be ENCRYPTED. XNU refuses otherwise:
#        panic: "unencrypted data volume is not allowed" @apfs_vfsops.c:2399
#      and a volume created by macOS `diskutil` is not encrypted in the way iOS
#      requires. Only iOS can make one, using its own keystore -- which is why
#      phase 2 boots the restore ramdisk and runs the guest's own `newfs_apfs`.
#      This works because the SEP `sks` endpoint is implemented; see
#      docs/re/sks-feasibility.md.
#
#   3. The volume must be POPULATED. We cannot do this from the host or from a
#      shell: the /private/var template files carry data-protection classes that
#      an empty keybag cannot unlock, and `cp` panics reading MobileAsset even
#      with --reflink=never. An earlier hypothesis was that iOS's own
#      `mount-phase-2` would seed a real empty Data volume. It does not: the
#      independent normal and -skip-keybag controls both mount Data and emit
#      zero `Copying ` lines (BOOTSTRAP_SEED*.serial.log), and the latter then
#      dies because tzinit cannot find its Data files. The restore-only seeder
#      now supplies those protected files through copy-data, manifest, layout
#      and marker stages. rebuild_persistent_parent.sh orchestrates these and
#      validates two normal boots; its --profile interface additionally selects
#      native or patched runtime configuration (see PROFILES.md).
#
# Usage:
#   tools/rootfs/bootstrap_data_volume.sh [all|image|exclave|format|ramdisk-helper|seed|copy-data|manifest|layout|marker|debug-shell|normal|verify]
#
# Env:
#   SRC   cryptex-merged system volume  (default ~/dvm-artifacts/build/rootfs_cx.dmg)
#   OUT   disk image to build           (default ~/dvm-artifacts/build/rootfs_cx_dual.dmg)
#   OVL   qcow2 overlay the guest writes to
#   WORK  scratch dir for trees, sockets and logs (default /tmp/dvm/bootstrap)
#   RESTORE_RAMDISK_BASE  source ramdisk for ramdisk-helper
#   RESTORE_RAMDISK_OUT   new derived ramdisk for ramdisk-helper
#   RESTORE_RAMDISK       optional derived restore ramdisk containing helper
#   RESTORE_HELPER_SOURCE absolute guest path to that helper
set -uo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)
MODE=${1:-all}
SRC=${SRC:-$HOME/dvm-artifacts/build/rootfs_cx.dmg}
OUT=${OUT:-$HOME/dvm-artifacts/build/rootfs_cx_dual.dmg}
OVL=${OVL:-$HOME/dvm-artifacts/build/rootfs_cx_dual-overlay.qcow2}
TC=${TC:-$HOME/dvm-artifacts/tc/merged_sysvol_cryptex_tc.bin}
WORK=${WORK:-/tmp/dvm/bootstrap}
EXCLAVE=${EXCLAVE:-$HOME/dvm-artifacts/aea/out/094-14052-182.dmg}
QEMU_IMG="${QEMU_IMG:-$REPO/qemu-sptm/build/qemu-img}"
DTREE_RAW="${DTREE_RAW:-/tmp/dvm/dtree_raw}"
FORMAT_TAG="${FORMAT_TAG:-BOOTSTRAP_FMT}"
[[ "$FORMAT_TAG" =~ ^[A-Za-z0-9_-]{1,64}$ ]] || { echo 'invalid FORMAT_TAG' >&2; exit 2; }
BOOTARGS_COMMON='ignition_level=1 launchd_unsecure_cache=1 serial=3 -v wdt=-1 wlan-olyhal-abort'
# Profile builds opt into the native RTC for formatting/seeding as well.
RTC_DT_ARGS=()
case ${NATIVE_RTC:-0} in
    0) ;;
    1) RTC_DT_ARGS=(-enable spmi); export DARWIN_RTC_PV=0 ;;
    *) echo 'NATIVE_RTC must be 0 or 1' >&2; exit 2 ;;
esac
SERIAL_CHAR_DELAY=${SERIAL_CHAR_DELAY:-0.002}
# The seeder is built and trusted only for disposable restore boots.  The
# default transport is the checksummed UART uploader.  A caller may instead
# supply a derived restore ramdisk and guest path; restore_prepare_helper still
# proves its guest-side size and checksum before execution.  This is important
# because the earlier unverified UART path printed success after running no
# helper at all (BOOTSTRAP_FMT attempts 1/2, `echo` received as `cho`).
# Keep the output under WORK so a rerun cannot mistake a binary from another
# checkout for the current source.
SEED_HELPER=${SEED_HELPER:-$WORK/data_seed_helper}
SEED_HELPER_TC=${SEED_HELPER_TC:-$WORK/data_seed_helper.tc}
RESTORE_RAMDISK=${RESTORE_RAMDISK:-}
RESTORE_HELPER_SOURCE=${RESTORE_HELPER_SOURCE:-}
RESTORE_RAMDISK_BASE=${RESTORE_RAMDISK_BASE:-$REPO/firmware/ramdisk.dmg}
RESTORE_RAMDISK_OUT=${RESTORE_RAMDISK_OUT:-$WORK/ramdisk-data-seed.dmg}

die() { echo "!! $*" >&2; exit 1; }
say() { echo "==> $*"; }

build_seed_helper() {
    say "[seed] build/sign helper and a disposable restore trustcache"
    make -C "$REPO/tools/rootfs" data-seed-helper-tc \
        OUT="$SEED_HELPER" HELPER_TC="$SEED_HELPER_TC" REPO="$REPO" \
        || die "could not build the restore-only data seeder"
    [ -s "$SEED_HELPER" ] && [ -s "$SEED_HELPER_TC" ] \
        || die "helper build returned success without binary and trustcache"
}

# ---------------------------------------------------------------- ramdisk helper --
# A paced UART transfer of the 74 KiB helper expands to roughly 1.5 million
# transmitted shell bytes and took about 15 minutes after the leading-byte
# loss workaround.  The helper itself is not secret and the restore ramdisk is
# an unencrypted, disposable 241 MiB APFS image, so a derived copy is the
# narrow host-side write path.  The large System/Data container is never
# attached here.  Every later guest use still requires an independent numeric
# checksum witness; this phase only prepares transport, it does not prove guest
# execution.
phase_ramdisk_helper() {
    local mnt= src_cksum dst_cksum
    build_seed_helper
    [ -f "$RESTORE_RAMDISK_BASE" ] || die "missing restore ramdisk: $RESTORE_RAMDISK_BASE"
    [ ! -e "$RESTORE_RAMDISK_OUT" ] || die "refusing to overwrite derived ramdisk: $RESTORE_RAMDISK_OUT"
    mkdir -p "$(dirname "$RESTORE_RAMDISK_OUT")"
    cp -p "$RESTORE_RAMDISK_BASE" "$RESTORE_RAMDISK_OUT" \
        || die 'could not copy restore ramdisk'
    # The disposable restore ramdisk carries a helper built by this user. Use
    # noowners so its root-owned libexec directory is writable while staging;
    # the later guest checksum is the independent execution-time proof.
    mnt=$("$REPO/tools/rootfs/safe_attach.sh" attach "$RESTORE_RAMDISK_OUT" --owners off) \
        || die 'could not safely attach derived restore ramdisk'
    [[ "$mnt" == /Volumes/* ]] && [ -d "$mnt/libexec" ] || {
        "$REPO/tools/rootfs/safe_attach.sh" detach "$mnt" 2>/dev/null || true
        die "unexpected restore ramdisk mountpoint/layout: $mnt"
    }
    trap '"$REPO/tools/rootfs/safe_attach.sh" detach "$mnt" >/dev/null 2>&1 || true' EXIT INT TERM
    cp -X "$SEED_HELPER" "$mnt/libexec/dvm_data_seed_helper" \
        || die 'could not install helper in derived restore ramdisk'
    chmod 755 "$mnt/libexec/dvm_data_seed_helper" || die 'could not make ramdisk helper executable'
    xattr -c "$mnt/libexec/dvm_data_seed_helper" 2>/dev/null || true
    src_cksum=$(cksum "$SEED_HELPER" | awk '{print $1 " " $2}')
    dst_cksum=$(cksum "$mnt/libexec/dvm_data_seed_helper" | awk '{print $1 " " $2}')
    [ "$src_cksum" = "$dst_cksum" ] || die "host helper checksum mismatch: $src_cksum != $dst_cksum"
    sync
    "$REPO/tools/rootfs/safe_attach.sh" detach "$mnt" || die 'could not detach derived restore ramdisk'
    trap - EXIT INT TERM
    say "[ramdisk-helper] image=$RESTORE_RAMDISK_OUT guest=/libexec/dvm_data_seed_helper cksum=$src_cksum"
}

# Restore-stage transport.  `probe.sh` owns QEMU's lifetime, but its UART log
# is also the only reliable witness: socket reads drain console bytes.  Every
# caller supplies an unambiguous tag so cleanup never reaps another agent's VM.
restore_start() {
    local tag=$1 overlay=$2 stage_tc=$3
    local sock="$WORK/$tag.sock" dt="$WORK/dt_restore.bin"
    local ramdisk_args=()
    python3 "$REPO/dt_fixup.py" "$DTREE_RAW" "$dt" -nvram "$NVRAM" \
        -enable ans -enable smc -enable sep "${RTC_DT_ARGS[@]}" -dram 12G || die "restore dt_fixup failed"
    rm -f "$sock"
    [ -z "$RESTORE_RAMDISK" ] || ramdisk_args=(--ramdisk "$RESTORE_RAMDISK")
    "$REPO/tools/probe.sh" "${ramdisk_args[@]}" --dtree "$dt" --tc "$stage_tc" --mem 12G --secs 2400 \
        --tag "$tag" --uart-socket "$sock" --bootargs "rd=md0 $BOOTARGS_COMMON" \
        -- -drive "if=none,id=ans,file=$overlay,format=qcow2" >/dev/null 2>&1 &
    RESTORE_PID=$!; RESTORE_TAG=$tag; RESTORE_SOCK=$sock
    # Every failing stage exits through die(); keep that from leaking the
    # probe-launched QEMU subshell.  restore_stop disarms this after normal
    # completion, before a later unrelated phase can inherit the tag.
    trap 'restore_stop' EXIT INT TERM
    local log=/tmp/dvm/probe/$tag.serial.log waited=0
    say "[restore] tag=$tag serial=$log socket=$sock"
    while (( waited < 180 )); do
        [ -S "$sock" ] && grep -qa "can't access tty" "$log" 2>/dev/null && return 0
        grep -qa 'panic(cpu' "$log" 2>/dev/null && die "$tag panicked before shell; see $log"
        sleep 3; waited=$((waited+3))
    done
    die "$tag did not reach restore shell; see $log"
}

restore_stop() {
    [ -n "${RESTORE_PID:-}" ] && kill "$RESTORE_PID" 2>/dev/null || true
    [ -n "${RESTORE_TAG:-}" ] && pkill -f "$RESTORE_TAG" 2>/dev/null || true
    RESTORE_PID= RESTORE_TAG= RESTORE_SOCK=
    trap - EXIT INT TERM
}

restore_send() {
    local tag=$1 command=$2 marker=$3
    local clog="$WORK/$tag.console.log" slog=/tmp/dvm/probe/$tag.serial.log waited=0
    python3 "$REPO/tools/serial.py" "$RESTORE_SOCK" send "$command" --secs 180 \
        --log "$clog" --char-delay "$SERIAL_CHAR_DELAY" >/dev/null || die "$tag serial transport rejected command"
    # serial.py verifies the echoed command, but its idle drain is deliberately
    # short.  newfs_apfs and APFS key work can be quiet longer than that; the
    # QEMU logfile is the durable guest->host witness, so poll it rather than
    # treating a quiet socket as command completion.
    while (( waited < ${RESTORE_STAGE_TIMEOUT:-240} )); do
        grep -Fqa -- "$marker" "$slog" && return 0
        grep -qa 'panic(cpu' "$slog" && die "$tag panicked; see first panic(cpu in $slog"
        [ -S "$RESTORE_SOCK" ] || die "$tag UART disappeared before witness '$marker'"
        kill -0 "$RESTORE_PID" 2>/dev/null || die "$tag probe died before witness '$marker'"
        sleep 3; waited=$((waited+3))
    done
    die "$tag missing witness '$marker' after ${RESTORE_STAGE_TIMEOUT:-240}s; see $slog"
}

restore_upload() {
    local tag=$1 remote=$2
    local clog="$WORK/$tag.console.log" slog=/tmp/dvm/probe/$tag.serial.log
    python3 "$REPO/tools/serial.py" "$RESTORE_SOCK" upload "$SEED_HELPER" \
        --remote-path "$remote" --secs 180 --log "$clog" --char-delay "$SERIAL_CHAR_DELAY" >/dev/null \
        || die "$tag helper upload failed"
    grep -qa 'DVM_UPLOAD_FINAL_RC=0' "$slog" || die "$tag upload lacks checksum witness"
}

# Select a helper and prove that the guest sees the exact binary just built.
# A ramdisk-resident helper avoids sending ~1.5 MB of paced Base64 shell input,
# but it is not trusted merely because the host copied it into an image: the
# numeric guest cksum/size witness is independent of that host-side operation.
restore_prepare_helper() {
    local tag=$1 upload_target=$2 expected_cksum expected_bytes command marker
    if [ -z "$RESTORE_HELPER_SOURCE" ]; then
        restore_upload "$tag" "$upload_target"
        RESTORE_HELPER_PATH=$upload_target
        return
    fi
    [ -n "$RESTORE_RAMDISK" ] || die 'RESTORE_HELPER_SOURCE requires RESTORE_RAMDISK'
    [[ "$RESTORE_HELPER_SOURCE" =~ ^/[A-Za-z0-9_./-]+$ ]] \
        && [[ "$RESTORE_HELPER_SOURCE" != *'/../'* ]] \
        || die "unsafe RESTORE_HELPER_SOURCE=$RESTORE_HELPER_SOURCE"
    set -- $(cksum "$SEED_HELPER")
    expected_cksum=$1; expected_bytes=$2
    command="set -- \$(/bin/cksum '$RESTORE_HELPER_SOURCE'); test \"\$1\" = '$expected_cksum' && test \"\$2\" = '$expected_bytes'; rc=\$?; echo DVM_HELPER_SOURCE_RC=\$rc DVM_HELPER_BYTES=\$2"
    marker="DVM_HELPER_SOURCE_RC=0 DVM_HELPER_BYTES=$expected_bytes"
    restore_send "$tag" "$command" "$marker"
    RESTORE_HELPER_PATH=$RESTORE_HELPER_SOURCE
}

seed_child() {
    local parent=$1 child=$2
    [ ! -e "$child" ] || die "refusing to reuse stage overlay $child"
    "$QEMU_IMG" create -f qcow2 -F qcow2 -b "$parent" "$child" >/dev/null \
        || die "could not create child $child"
}

mkdir -p "$WORK"
[ -f "$SRC" ] || die "missing source image: $SRC"
[ -f "$TC" ]  || die "missing trustcache: $TC (boots fail with 'code signature registration for shared cache failed')"
# The canonical tracked NVRAM blob lives at the repository root.  Older
# worktree-only invocations left a private copy under .claude/worktrees, but
# bootstrap deliberately works in the shared checkout too.
NVRAM=${NVRAM:-$REPO/nvram.bin}
if [ ! -f "$NVRAM" ]; then
    NVRAM=$(ls "$REPO"/.claude/worktrees/*/nvram.bin 2>/dev/null | head -1)
fi
[ -f "${NVRAM:-}" ] || die "no nvram.bin found; dt_fixup.py requires -nvram"

# ---------------------------------------------------------------- phase 1 ----
# Add the Data / Preboot / Hardware volume slots. Host-side, no mounting.
phase_image() {
    [ -f "$EXCLAVE" ] || die "missing ExclaveOS payload: $EXCLAVE (run fetch_payloads.sh)"
    say "[image] build $OUT from $SRC"
    SRC="$SRC" "$REPO/tools/rootfs/build_data_volume.sh" "$OUT" 4 \
        || die "build_data_volume.sh failed"
    phase_exclave
    say "[image] fresh overlay $OVL"
    rm -f "$OVL"
    "$QEMU_IMG" create -f qcow2 -F raw -b "$OUT" "$OVL" >/dev/null \
        || die "could not create overlay"
}

phase_exclave() {
    say "[exclave] install matching payload on the offline Preboot volume"
    python3 "$REPO/tools/rootfs/merge_exclave.py" --image "$OUT" \
        --exclave "$EXCLAVE" --report "$WORK/exclave-merge.json" \
        || die "ExclaveOS provisioning failed"
}

# ---------------------------------------------------------------- phase 2 ----
# Let iOS create the encrypted Data volume. Only the guest can do this: the
# encryption keys come from the SEP keystore, so newfs_apfs must run inside.
phase_format() {
    [ -f "$OVL" ] || die "no overlay; run the image phase first"
    local dt="$WORK/dt_ramdisk.bin" sock="$WORK/fmt.sock"
    local slog=/tmp/dvm/probe/$FORMAT_TAG.serial.log
    local clog="$WORK/fmt.console.log"
    say "[format] device tree: ans + smc + sep on the restore ramdisk"
    python3 "$REPO/dt_fixup.py" "$DTREE_RAW" "$dt" -nvram "$NVRAM" \
        -enable ans -enable smc -enable sep "${RTC_DT_ARGS[@]}" -dram 12G \
      || die "dt_fixup failed (need the raw device tree; see CLAUDE.md)"

    rm -f "$sock"
    say "[format] booting the restore ramdisk with a driveable console"
    "$REPO/tools/probe.sh" --dtree "$dt" --mem 12G --secs 240 --tag "$FORMAT_TAG" \
        --uart-socket "$sock" \
        --bootargs "rd=md0 $BOOTARGS_COMMON" \
        -- -drive "if=none,id=ans,file=$OVL,format=qcow2" >/dev/null 2>&1 &
    local probe_pid=$!

    # probe.sh starts QEMU in a subshell, so killing only $probe_pid leaks the
    # guest.  Keep this armed on every error path, including a failed transport
    # handshake, and reap only our unique tag.
    cleanup_format() {
        kill "$probe_pid" 2>/dev/null || true
        pkill -f "unix:/tmp/dvm/$FORMAT_TAG.sock" 2>/dev/null || true
        pkill -f "unix:$sock" 2>/dev/null || true
    }
    trap cleanup_format EXIT INT TERM

    # Wait for the restore ramdisk's shell by watching the ACCUMULATING serial
    # log, not by reading the socket. serial.py *drains* the stream into its own
    # log, so polling it with `read` consumes the prompt and the next poll finds
    # nothing -- the first version of this script searched for evidence it had
    # already eaten, and declared "guest console never appeared" on a boot that
    # had reached the shell with 0 panics. --uart-socket still writes the serial
    # log (the chardev carries logfile=), so the log is the reliable witness.
    local waited=0
    while (( waited < 300 )); do
        perl -e 'sleep 5'; waited=$((waited+5))
        grep -qa "can't access tty" "$slog" 2>/dev/null && break
        grep -qa 'panic(cpu' "$slog" 2>/dev/null && {
            kill $probe_pid 2>/dev/null
            die "guest panicked before the shell; see $slog"
        }
    done
    grep -qa "can't access tty" "$slog" 2>/dev/null || {
        kill $probe_pid 2>/dev/null
        die "no shell after ${waited}s; see $slog"
    }
    say "[format] guest shell up after ${waited}s"

    # `command -v` is the positive control for a negative answer below.  The
    # earlier `cho: not found` came from a mangled echo, not from newfs_apfs, so
    # do not infer the formatter is absent until the guest has echoed and run
    # this checked command.  The probe chardev logfile, not socket output, is
    # the witness: it is QEMU's accumulating guest->host console record.
    rm -f "$clog"
    say "[format] positive control: locate newfs_apfs on the restore ramdisk"
    python3 "$REPO/tools/serial.py" "$sock" send \
        'command -v newfs_apfs; echo NEWFS_AVAILABLE_RC=$?' --secs 30 --log "$clog" \
        | tail -5
    local available=""
    waited=0
    while (( waited < 60 )); do
        available=$(grep -ao 'NEWFS_AVAILABLE_RC=[0-9]\+' "$slog" 2>/dev/null | tail -1 | cut -d= -f2)
        [ -n "$available" ] && break
        perl -e 'sleep 5'; waited=$((waited+5))
    done
    [ "$available" = "0" ] || die "newfs_apfs is unavailable on the restore ramdisk (rc=${available:-none}); see $slog"
    say "[format] positive control passed: restore ramdisk reported newfs_apfs rc=0"

    local before after
    before=$(stat -f%z "$OVL")
    say "[format] newfs_apfs -E on the Data slot (iOS makes the keys, not us)"
    # The host already assigned this slot role D.  Do not repeat `-R D` here:
    # this restore newfs rejects encryption plus that special role with
    # "Can't create an encrypted volume with special role 0x280" (FMT_RC=73),
    # before writing anything.  Formatting the existing Data slot without a
    # role override lets it retain the host-created role while iOS owns keys.
    python3 "$REPO/tools/serial.py" "$sock" send \
        'newfs_apfs -E -W -v Data /dev/disk1s2; echo FMT_RC=$?' --secs 180 --log "$clog" \
        | tail -5

    # VERIFY, do not assert. The first version of this phase printed "done"
    # unconditionally: newfs_apfs never ran, FMT_RC came back as the literal
    # string, the overlay never grew, and the phase still reported success --
    # the exact "a silent no-op looks like success" failure this project keeps
    # being bitten by. Two independent witnesses now have to agree.
    local rc="" waited=0
    while (( waited < 180 )); do
        rc=$(grep -ao 'FMT_RC=[0-9]\+' "$slog" 2>/dev/null | tail -1 | cut -d= -f2)
        [ -n "$rc" ] && break
        perl -e 'sleep 5'; waited=$((waited+5))
    done
    after=$(stat -f%z "$OVL")

    say "[format] newfs_apfs rc=${rc:-<never reported>}  overlay ${before} -> ${after} bytes"
    [ "$rc" = "0" ] || die "newfs_apfs did not report success (rc=${rc:-none}); see $slog"
    (( after > before )) || die "newfs_apfs reported success but wrote nothing to $OVL -- a real format writes a superblock"
    say "[format] verified: the encrypted Data volume is in $OVL"

    # UMLManager requires exactly one APFS role-User volume; mobile_obliterator
    # creates it with this guest-only command before primary-user layout.
    before=$after
    python3 "$REPO/tools/serial.py" "$sock" send \
        '/sbin/newfs_apfs -A -P -v User -R u -D /dev/disk1; echo USER_NEWFS_RC=$?' \
        --secs 180 --log "$clog" | tail -5
    waited=0; rc=""
    while (( waited < 180 )); do
        rc=$(grep -ao 'USER_NEWFS_RC=[0-9]\+' "$slog" 2>/dev/null | tail -1 | cut -d= -f2)
        [ -n "$rc" ] && break; sleep 3; waited=$((waited+3))
    done
    after=$(stat -f%z "$OVL")
    [ "$rc" = "0" ] || die "User role creation failed (rc=${rc:-none}); see $slog"
    (( after > before )) || die "User role creation wrote nothing to $OVL"
    grep -qa 'disk1s5 mount\|volume User\|role.*User' "$slog" 2>/dev/null || true

    cleanup_format
    trap - EXIT INT TERM
}

# ---------------------------------------------------------------- phase 3 ----
# Boot the system volume with NO -ephemeral-data and test whether mount-phase-2
# seeds the empty Data volume. It is the only plausible guest-side actor: it
# runs with the keybag, so any copied protection classes would be correct. Do
# not call a mount success a seed success: the phase requires copy evidence.
phase_seed() {
    [ -f "$OVL" ] || die "no overlay; run the image and format phases first"
    local dt="$WORK/dt_sysvol.bin"
    # The real-volume continuation deliberately runs in independent restore
    # boots: (A) Data+User creates/updates the primary-user manifest; (B)
    # System-at-var+User-at-hardware calls the opaque UML layout selector; and
    # (C) Data+System writes/fsyncs the completion marker.  A restore sandbox
    # denies the production alt_root User target, and /bin/sh has no umount,
    # so combining B with A would be a silent namespace substitution.  Each
    # stage must therefore use a fresh qcow child, a UART upload checksum,
    # its numeric helper RC, and the accumulating probe serial log as
    # witnesses.  Transport is only valid in its upload/execute/cleanup
    # restore boot: encrypted Data inodes can be unwrappable after reboot,
    # and the restore ramdisk's /tmp is read-only.
    # build_seed_helper is intentionally before any guest starts: rebuilding a
    # trustcache or helper while a VM is live would invalidate its evidence.
    build_seed_helper
    say "[seed] device tree: no -ephemeral-data, encrypted Data keybag active"
    python3 "$REPO/dt_fixup.py" "$DTREE_RAW" "$dt" -nvram "$NVRAM" \
        -enable ans -enable smc -enable sep "${RTC_DT_ARGS[@]}" -dram 12G \
        || die "dt_fixup failed"
    say "[seed] booting the system volume off the NVMe disk (testing first-boot seeding)"
    "$REPO/tools/probe.sh" --dtree "$dt" --tc "$TC" --mem 12G --secs 1200 \
        --tag BOOTSTRAP_SEED \
        --bootargs "rootdev=disk1s1 $BOOTARGS_COMMON" \
        -- -drive "if=none,id=ans,file=$OVL,format=qcow2" | tail -8
    # probe.sh intentionally returns after a dead guest so callers can inspect
    # its logs.  Do not let either a dead guest or a Data mount with no copies
    # make an unseeded volume look successful.
    EXPECT_SEED=required phase_verify
}

# ---------------------------------------------------------------- staged seed --
# These stages are intentionally separately invocable.  A successful stage is
# a new child overlay, never a mutation of its parent; callers pass PARENT and
# OUT_OVL explicitly to make reruns and review of each boundary deterministic.
phase_copy_data() {
    local parent=${PARENT:?set PARENT to the guest-formatted/User parent} child=${OUT_OVL:?set OUT_OVL to a new qcow path} tag=${TAG:-BOOTSTRAP_COPY_$$} allowed
    build_seed_helper; seed_child "$parent" "$child"; restore_start "$tag" "$child" "$SEED_HELPER_TC"
    restore_send "$tag" 'mount_apfs /dev/disk1s2 /private/var; mkdir -p /private/var/hardware; mount_apfs /dev/disk1s1 /private/var/hardware; echo DVM_COPY_MOUNTS_RC=$?' 'DVM_COPY_MOUNTS_RC=0'
    restore_prepare_helper "$tag" /private/var/.dvm-data-seed/data_seed_helper
    restore_send "$tag" "chmod 755 '$RESTORE_HELPER_PATH'; '$RESTORE_HELPER_PATH' --copy-data /private/var/hardware/private/var /private/var; hrc=\$?; sync; src=\$?; echo DVM_COPY_SHELL_RC=\$hrc DVM_COPY_SYNC_RC=\$src" 'DVM_COPY_SHELL_RC=0 DVM_COPY_SYNC_RC=0'
    grep -qa 'DVM_SEED_COPY_DATA_RC=0' "/tmp/dvm/probe/$tag.serial.log" || die 'copy helper did not finish'
    grep -qa 'DVM_SEED_TIMEZONE_PRECREATE_RC=0' "/tmp/dvm/probe/$tag.serial.log" || die 'copy helper did not precreate db/timezone/localtime before AKS'
    grep -qa 'DVM_SEED_TIMEZONE_SYMLINK_RC=0' "/tmp/dvm/probe/$tag.serial.log" || die 'copy helper did not preserve db/timezone/localtime symlink'
    allowed=$(grep -ao 'DVM_SEED_ALLOWED_COPY_ERRORS=[0-9]\+' "/tmp/dvm/probe/$tag.serial.log" | tail -1 | cut -d= -f2)
    [ -n "$allowed" ] || die 'copy helper did not report restore-sandbox allowlist count'
    say "[copy] allowed restore-sandbox copy errors=$allowed"
    restore_stop
}

phase_manifest() {
    local parent=${PARENT:?set PARENT} child=${OUT_OVL:?set OUT_OVL} tag=${TAG:-BOOTSTRAP_MANIFEST_$$}
    build_seed_helper; seed_child "$parent" "$child"; restore_start "$tag" "$child" "$SEED_HELPER_TC"
    restore_send "$tag" 'mount_apfs /dev/disk1s2 /private/var; mkdir -p /private/var/hardware; mount_apfs /dev/disk1s5 /private/var/hardware; echo DVM_MANIFEST_MOUNTS_RC=$?' 'DVM_MANIFEST_MOUNTS_RC=0'
    restore_prepare_helper "$tag" /private/var/.dvm-data-seed/data_seed_helper
    restore_send "$tag" "chmod 755 '$RESTORE_HELPER_PATH'; '$RESTORE_HELPER_PATH' --user-manifest /private/var/hardware/private/var /private/var; hrc=\$?; sync; src=\$?; echo DVM_MANIFEST_SHELL_RC=\$hrc DVM_MANIFEST_SYNC_RC=\$src" 'DVM_MANIFEST_SHELL_RC=0 DVM_MANIFEST_SYNC_RC=0'
    grep -qa 'DVM_SEED_USER_MANIFEST_RC=0' "/tmp/dvm/probe/$tag.serial.log" || die 'manifest helper did not finish'
    restore_stop
}

phase_layout() {
    local parent=${PARENT:?set PARENT} child=${OUT_OVL:?set OUT_OVL} tag=${TAG:-BOOTSTRAP_LAYOUT_$$}
    build_seed_helper; seed_child "$parent" "$child"; restore_start "$tag" "$child" "$SEED_HELPER_TC"
    restore_send "$tag" 'mount_apfs /dev/disk1s1 /private/var; mkdir -p /private/var/hardware; mount_apfs /dev/disk1s5 /private/var/hardware; echo DVM_LAYOUT_MOUNTS_RC=$?' 'DVM_LAYOUT_MOUNTS_RC=0'
    restore_prepare_helper "$tag" /private/var/hardware/.dvm-data-seed/data_seed_helper
    restore_send "$tag" "chmod 755 '$RESTORE_HELPER_PATH'; '$RESTORE_HELPER_PATH' --user-layout /private/var/hardware/private/var /private/var; hrc=\$?; sync; src=\$?; echo DVM_LAYOUT_SHELL_RC=\$hrc DVM_LAYOUT_SYNC_RC=\$src" 'DVM_LAYOUT_SHELL_RC=0 DVM_LAYOUT_SYNC_RC=0'
    grep -qa 'DVM_SEED_USER_LAYOUT_RC=0' "/tmp/dvm/probe/$tag.serial.log" || die 'layout helper did not finish'
    restore_stop
}

phase_marker() {
    local parent=${PARENT:?set PARENT} child=${OUT_OVL:?set OUT_OVL} tag=${TAG:-BOOTSTRAP_MARKER_$$}
    build_seed_helper; seed_child "$parent" "$child"; restore_start "$tag" "$child" "$SEED_HELPER_TC"
    restore_send "$tag" 'mount_apfs /dev/disk1s2 /private/var; mkdir -p /private/var/hardware; mount_apfs /dev/disk1s1 /private/var/hardware; echo DVM_MARKER_MOUNTS_RC=$?' 'DVM_MARKER_MOUNTS_RC=0'
    restore_prepare_helper "$tag" /private/var/.dvm-data-seed/data_seed_helper
    restore_send "$tag" "chmod 755 '$RESTORE_HELPER_PATH'; '$RESTORE_HELPER_PATH' --final-marker /private/var/hardware/private/var /private/var; hrc=\$?; sync; src=\$?; test -f /private/var/.dvm-data-seed-complete; mrc=\$?; echo DVM_MARKER_SHELL_RC=\$hrc DVM_MARKER_SYNC_RC=\$src DVM_MARKER_STAT_RC=\$mrc" 'DVM_MARKER_SHELL_RC=0 DVM_MARKER_SYNC_RC=0 DVM_MARKER_STAT_RC=0'
    restore_stop
}

# Install a console shell in a disposable child only.  Normal userspace does
# not expose the restore ramdisk's UART shell, which otherwise makes a simple
# `ps` or launchctl experiment cost a new instrumented boot.  The System
# volume is modified by iOS from a restore boot, never mounted by macOS; the
# parent is immutable and remains the positive-control input.  This shell is a
# diagnostic accelerator, not valid evidence for a normal-boot result.
phase_debug_shell() {
    local parent=${PARENT:?set PARENT to an immutable persistent parent}
    local child=${OUT_OVL:?set OUT_OVL to a new disposable qcow path}
    local tag=${TAG:-DVM_DEBUG_SHELL_INSTALL_$$}
    local plist="$REPO/tools/re/debug-shell.plist"
    local staged=/private/var/hardware/.dvm-data-seed/debug-shell.plist

    [ -f "$plist" ] || die "missing debug shell plist: $plist"
    plutil -lint "$plist" >/dev/null || die "invalid debug shell plist"
    seed_child "$parent" "$child"
    restore_start "$tag" "$child" "$REPO/firmware/ramdisk.tc"
    restore_send "$tag" \
        'mkdir -p /private/var/hardware; mount_apfs /dev/disk1s1 /private/var/hardware; mrc=$?; mkdir -p /private/var/hardware/.dvm-data-seed; echo DVM_DEBUG_MOUNT_RC=$mrc' \
        'DVM_DEBUG_MOUNT_RC=0'
    python3 "$REPO/tools/serial.py" "$RESTORE_SOCK" upload "$plist" \
        --remote-path "$staged" --secs 60 --log "$WORK/$tag.console.log" \
        --char-delay "$SERIAL_CHAR_DELAY" >/dev/null \
        || die "$tag debug-shell plist upload failed"
    restore_send "$tag" \
        "dst=/private/var/hardware/System/Library/LaunchDaemons/com.apple.dvm-debug-shell.plist; mv '$staged' \"\$dst\"; chmod 644 \"\$dst\"; chown 0:0 \"\$dst\"; sync; test -s \"\$dst\"; rc=\$?; echo DVM_DEBUG_SHELL_INSTALL_RC=\$rc" \
        'DVM_DEBUG_SHELL_INSTALL_RC=0'
    restore_stop
    say "[debug-shell] installed in disposable child=$child"
    say "[debug-shell] boot with launchd_unsecure_cache=1; never use this child as final evidence"
}

phase_normal_boot() {
    local parent=${PARENT:?set PARENT to marker child} child=${OUT_OVL:?set OUT_OVL} tag=${TAG:-BOOTSTRAP_NORMAL}
    say "[normal] parent=$parent child=$child tag=$tag"
    seed_child "$parent" "$child"
    python3 "$REPO/dt_fixup.py" "$DTREE_RAW" "$WORK/dt_sysvol.bin" -nvram "$NVRAM" \
        -enable ans -enable smc -enable sep "${RTC_DT_ARGS[@]}" -dram 12G || die "normal boot dt_fixup failed"
    "$REPO/tools/probe.sh" --dtree "$WORK/dt_sysvol.bin" --tc "$TC" --mem 12G \
        --secs "${NORMAL_BOOT_SECS:-600}" \
        --tag "$tag" --bootargs "rootdev=disk1s1 $BOOTARGS_COMMON" \
        -- -drive "if=none,id=ans,file=$child,format=qcow2" | tail -8
    local log=/tmp/dvm/probe/$tag.serial.log copies early panics preboot hardware root data data_crypto user
    copies=$(grep -ac 'Copying ' "$log" 2>/dev/null || true); early=$(grep -ac 'Early boot complete' "$log" 2>/dev/null || true); panics=$(grep -ac 'panic(cpu' "$log" 2>/dev/null || true)
    preboot=$(grep -ac 'mount-complete volume Preboot' "$log" 2>/dev/null || true); hardware=$(grep -ac 'mount-complete volume Hardware' "$log" 2>/dev/null || true)
    root=$(grep -ac 'BSD root: disk1s1' "$log" 2>/dev/null || true)
    data=$(grep -ac '/dev/disk1s2 on /private/var .*protect' "$log" 2>/dev/null || true)
    data_crypto=$(grep -ac 'handle_mount:893: disk1s2 .*encrypted' "$log" 2>/dev/null || true)
    user=$(grep -ac 'disk1s5 mount-complete volume User' "$log" 2>/dev/null || true)
    (( root > 0 )) || die "$tag did not root from disk1s1"
    (( data > 0 && data_crypto > 0 )) || die "$tag did not mount encrypted/protect Data disk1s2 on /private/var"
    (( user > 0 )) || die "$tag did not recognize/mount role-User disk1s5"
    (( copies == 0 )) || die "$tag copied template files; persistent Data was not used"
    (( early > 0 )) || die "$tag did not reach Early boot complete"
    if (( panics > 0 )); then
        grep -m1 -A3 'panic(cpu' "$log" >&2 || true
        die "$tag panicked; see first panic(cpu in $log"
    fi
    (( preboot > 0 )) || die "$tag did not enumerate/mount role-Preboot"
    (( hardware > 0 )) || die "$tag did not enumerate/mount role-Hardware"
    say "[normal] verified child=$child; use PARENT=$child with a distinct OUT_OVL for cold boot #2"
}

# ---------------------------------------------------------------- verify -----
phase_verify() {
    local L=/tmp/dvm/probe/BOOTSTRAP_SEED.serial.log
    [ -f "$L" ] || die "no seed log; run the seed phase first"
    local root mount2 var copies early unencrypted panics expect_seed
    expect_seed=${EXPECT_SEED:-any}
    root=$(grep -ao 'BSD root: [a-z0-9]*' "$L" | head -1 | cut -d' ' -f3)
    mount2=$(grep -ao '(mount-phase-2) <Notice>: [A-Za-z -]*' "$L" | head -1)
    var=$(grep -ao '/dev/disk1s[0-9]* on /private/var[^\"]*' "$L" | head -1)
    copies=$(grep -ac 'Copying ' "$L")
    early=$(grep -ac 'Early boot complete' "$L")
    unencrypted=$(grep -ac 'unencrypted data volume' "$L")
    panics=$(grep -ac 'panic(cpu' "$L")
    say "[verify] what the seed boot achieved"
    printf "    BSD root            : %s\n" "$root"
    printf "    mount-phase-2       : %s\n" "$mount2"
    printf "    Data mounted on var : %s\n" "$var"
    printf "    files seeded        : %s\n" "$copies"
    printf "    Early boot complete : %s\n" "$early"
    printf "    unencrypted panic   : %s\n" "$unencrypted"
    printf "    xnu panics          : %s\n" "$panics"
    echo
    [ "$root" = "disk1s1" ] || die "seed boot did not root from the ANS System volume"
    [[ "$mount2" == *"Doing boot task"* ]] || die "mount-phase-2 did not run"
    [ -n "$var" ] || die "Data volume was not mounted on /private/var"
    case "$expect_seed" in
        required)
            (( copies > 0 )) || die "Data mounted but mount-phase-2 copied no template files; an empty real Data volume is not seeded by this boot path"
            ;;
        none)
            (( copies == 0 )) || die "persistence boot unexpectedly copied template files"
            ;;
        any) ;;
        *) die "invalid EXPECT_SEED=$expect_seed (use required, none, or any)" ;;
    esac
    (( early > 0 )) || die "seed boot did not reach Early boot complete"
    (( unencrypted == 0 )) || die "Data volume was rejected as unencrypted"
    (( panics == 0 )) || die "seed boot panicked; see first panic(cpu in $L"
    echo "    Verified: Data mounted on /private/var, copied its template, and reached early boot."
    echo "    Re-run with EXPECT_SEED=none to require the persistence/no-copy witness."
}

case "$MODE" in
    exclave) phase_exclave ;;
    image)  phase_image ;;
    format) phase_format ;;
    ramdisk-helper) phase_ramdisk_helper ;;
    seed)   phase_seed ;;
    copy-data) phase_copy_data ;;
    manifest) phase_manifest ;;
    layout) phase_layout ;;
    marker) phase_marker ;;
    debug-shell) phase_debug_shell ;;
    normal) phase_normal_boot ;;
    verify) phase_verify ;;
    all)    phase_image; phase_format; phase_seed ;;
    *) die "usage: $0 [all|image|format|ramdisk-helper|seed|copy-data|manifest|layout|marker|debug-shell|normal|verify]" ;;
esac

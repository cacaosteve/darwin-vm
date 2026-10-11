#!/bin/bash
# safe_attach.sh - attach and detach disk images without taking the Mac down.
#
# On 2026-09-02 this project kernel-panicked macOS twice, identically:
#
#   panic(cpu N, caller 0x...): "Data ObjId overflow" @jobj.c:1152
#   Panicked task: pid 109: fseventsd
#
# jobj.c is APFS's object-ID allocator and fseventsd is the filesystem-events
# daemon. The images we work with are APFS containers holding ~500,000-file iOS
# filesystems; mounting them exposes that to the host, where Spotlight indexes
# it and fseventsd tracks every change. Combined with mass create/delete churn
# inside the container, APFS exhausted its object-ID space and panicked.
#
# A filesystem should return an error rather than panic, so this is an APFS bug
# (macOS 27.0 beta, 26A5421a) - but we trigger it, and we can avoid it.
#
# This wrapper enforces the rules we learned the hard way:
#   - always -nobrowse, so Finder and friends leave it alone
#   - drop .metadata_never_index at the volume root, so Spotlight skips it
#   - drop .fseventsd/no_log, so fseventsd stops logging events for it; this
#     is the daemon actually named in every host panic, and it ignores both
#     -nobrowse and .metadata_never_index
#   - refuse a second attachment of an image that is already attached; five
#     concurrent read-write attachments once silently corrupted an image
#   - detach by mount point, never a blanket force-detach: the user has other
#     volumes mounted, including Xcode Simulator runtimes, and we clobbered one
#
# usage:
#   safe_attach.sh attach <image> [--readonly] [--owners on|off] [--nomount]
#   safe_attach.sh detach <mountpoint|device>
#   safe_attach.sh mount-device <device> [--owners on|off]
#   safe_attach.sh unmount-device <mountpoint|device>
#   safe_attach.sh list
set -uo pipefail

die() { echo "safe_attach: $*" >&2; exit 1; }

cmd="${1:-}"; shift 2>/dev/null

case "$cmd" in
attach)
    img="${1:-}"; shift 2>/dev/null
    [ -n "$img" ] || die "usage: safe_attach.sh attach <image> [--readonly] [--owners on|off] [--nomount]"
    [ -e "$img" ] || die "no such image: $img"

    ro=""; owners="on"; volume_name=""; nomount=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --readonly) ro="-readonly"; shift ;;
            --owners)   owners="$2"; shift 2 ;;
            --volume-name) volume_name="$2"; shift 2 ;;
            --nomount) nomount="-nomount"; shift ;;
            *) die "unknown option: $1" ;;
        esac
    done

    # One attachment per image. hdiutil resolves symlinks, so compare real paths.
    real=$(python3 -c 'import os,sys; print(os.path.realpath(sys.argv[1]))' "$img")
    if hdiutil info 2>/dev/null | grep -q "^image-path.*: ${real}$"; then
        die "already attached: $real  (detach it first; concurrent rw attachments corrupt images)"
    fi

    [ -z "$nomount" ] || [ -z "$volume_name" ] \
        || die "--volume-name cannot be combined with --nomount"
    out=$(hdiutil attach -nobrowse -owners "$owners" $ro $nomount "$img" 2>&1) || {
        echo "$out" >&2; die "attach failed"
    }
    if [ -n "$nomount" ]; then
        # No filesystem is exposed to Spotlight/fseventsd in this mode. Return
        # the physical image device so callers can operate on the APFS
        # container without mounting its volumes, and detach that exact image.
        dev=$(printf '%s\n' "$out" | awk '/^\/dev\/disk[0-9]+[[:space:]]/{print $1; exit}')
        [ -n "$dev" ] || { printf '%s\n' "$out"; die "attached but no root device found"; }
        echo "$dev"
        exit 0
    fi
    mounts=$(printf '%s\n' "$out" | grep -oE '/Volumes/.*$' | sed 's/[[:space:]]*$//')
    [ -n "$mounts" ] || { printf '%s\n' "$out"; die "attached but no mount point (nomount image?)"; }
    selected=""; matches=0

    # Keep Spotlight and fseventsd off it. Best effort: a read-only mount cannot
    # take the marker, which is fine because nothing changes on it either.
    while IFS= read -r mnt; do
      last_mount="$mnt"
      if [ -z "$ro" ]; then
        touch "$mnt/.metadata_never_index" 2>/dev/null \
            && echo "safe_attach: Spotlight excluded via .metadata_never_index" >&2
        # .metadata_never_index stops Spotlight (mds) but NOT fseventsd, which
        # honours neither it nor -nobrowse. fseventsd is the task named in all
        # three "Data ObjId overflow" @jobj.c:1152 panics on this host
        # (2026-09-02, pids 109/109/564), so it is the one that actually has to
        # be silenced. /.fseventsd/no_log is the documented way to do it.
        mkdir -p "$mnt/.fseventsd" 2>/dev/null \
            && touch "$mnt/.fseventsd/no_log" 2>/dev/null \
            && echo "safe_attach: fseventsd disabled via .fseventsd/no_log" >&2
      fi
      if [ -z "$volume_name" ]; then
          selected="$mnt"
      else
          actual=$(diskutil info -plist "$mnt" | python3 -c 'import plistlib,sys; print(plistlib.loads(sys.stdin.buffer.read()).get("VolumeName", ""))')
          if [ "$actual" = "$volume_name" ]; then
              selected="$mnt"; matches=$((matches + 1))
          fi
      fi
    done <<< "$mounts"
    if [ -n "$volume_name" ] && [ "$matches" != 1 ]; then
        # This attachment belongs to us; detach its whole image on selection
        # failure, using one of its actual mountpoints.
        hdiutil detach "$last_mount" >&2 || true
        die "expected one volume named $volume_name, found $matches"
    fi

    echo "$selected"
    ;;

detach)
    mnt="${1:-}"
    [ -n "$mnt" ] || die "usage: safe_attach.sh detach <mountpoint|device>"
    hdiutil detach "$mnt" || die "detach failed for $mnt (do NOT force-detach; find what is holding it)"
    ;;

mount-device)
    dev="${1:-}"; shift 2>/dev/null
    [ -b "$dev" ] || die "usage: safe_attach.sh mount-device <device> [--owners on|off]"
    owners="on"
    while [ $# -gt 0 ]; do
        case "$1" in
            --owners) owners="$2"; shift 2 ;;
            *) die "unknown option: $1" ;;
        esac
    done
    case "$owners" in
        on) mount_owners="owners" ;;
        off) mount_owners="noowners" ;;
        *) die "--owners must be on or off" ;;
    esac
    mnt=$(mktemp -d /tmp/dvm-safe-mount.XXXXXX) || die "could not create mountpoint"
    if ! diskutil mount -mountOptions "$mount_owners,nobrowse" -mountPoint "$mnt" "$dev" >/dev/null; then
        rmdir "$mnt" 2>/dev/null || true
        die "could not mount $dev"
    fi
    touch "$mnt/.metadata_never_index" 2>/dev/null \
        && echo "safe_attach: Spotlight excluded via .metadata_never_index" >&2
    mkdir -p "$mnt/.fseventsd" 2>/dev/null \
        && touch "$mnt/.fseventsd/no_log" 2>/dev/null \
        && echo "safe_attach: fseventsd disabled via .fseventsd/no_log" >&2
    echo "$mnt"
    ;;

unmount-device)
    target="${1:-}"
    [ -n "$target" ] || die "usage: safe_attach.sh unmount-device <mountpoint|device>"
    diskutil unmount "$target" >/dev/null || die "could not unmount $target"
    case "$target" in /tmp/dvm-safe-mount.*) rmdir "$target" 2>/dev/null || true ;; esac
    ;;

list)
    hdiutil info 2>/dev/null | grep -E "^image-path|^/dev/disk.*Volumes" | sed 's/^/  /'
    ;;

*)
    sed -n '2,32p' "$0"
    exit 2
    ;;
esac

#!/usr/bin/env python3
"""Build explicit native/patched 24A5430a baselines from a merged base or seeded parent.

Native means no added Apple-userspace patches or runtime helpers, not a stock
physical-iPhone boot. Both profiles retain this project's firmware adaptations,
Data seeder and device models. No profile completes Buddy or imports saved RAM.
See tools/rootfs/PROFILES.md for inputs, boundaries and validation semantics.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import struct
import subprocess
import sys
import uuid

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / 'tools'), str(REPO / 'tools/input'), str(REPO / 'tools/re')]
from checkpoint_common import atomic_json, qcow2_backing_chain, sha256
from prepare_display_policy import PAGE_SIZE, parse_code_directory, OFFSET, EXPECTED, REPLACEMENT
from prepare_guarded_cache_patch import reviewed_edits
from patch_powerd_virtual_battery import SOURCE_SHA256
from smp_pv_patch import SHA256 as KC_SHA256

CACHE_ROOT = '/System/Library/Caches/com.apple.dyld/'
POWERD = '/System/Library/CoreServices/powerd.bundle/powerd'
LAUNCHD = '/System/Library/xpc/launchd.plist'
BOOTARGS = 'ignition_level=1 launchd_unsecure_cache=1 serial=3 -v wdt=-1 wlan-olyhal-abort'
# Measured WARM_CLOCK_SOFTWARE1 display configuration; no GPU node or host plugin.
DISPLAY_ENV = {
    'DARWIN_DCP_EPIC': 'all', 'DARWIN_DCP_REPLY': '1',
    'DARWIN_DCP_IOMFB': '4', 'DARWIN_DCP_IOMFB_COMPLETE': '1',
    'DARWIN_DCP_IOMFB_SCANOUT': '1', 'DARWIN_DCP_IOMFB_RPC_TRACE': '0',
    'DARWIN_DCP_IOMFB_CB': 'D120::4,D586:9b040000fc090000:4',
    'DARWIN_DCP_IOMFB_OUT': 'A401=01,A000=01,A454=01000000,A033=4152474200000000000000000000000000000000000000000000000000000000000000000000000001000000,A453=9b040000fc090000,A412=01000000',
    'DARWIN_PAUTH_CACHE': 'on',
}


def run(*argv, env=None):
    print('+ ' + shlex.join(map(str, argv)), flush=True)
    subprocess.run(list(map(str, argv)), check=True, env=env)


def clean_env():
    # Do not inherit a diagnostic override or a resume stage from another task.
    return {k: v for k, v in os.environ.items()
            if not k.startswith(('DARWIN_', 'DVM_', 'GXFSTAT_'))
            and k not in ('START_AT', 'SEED_ONLY', 'RESTORE_RAMDISK',
                          'RESTORE_RAMDISK_BASE', 'RESTORE_RAMDISK_OUT',
                          'RESTORE_HELPER_SOURCE', 'SEED_HELPER', 'SEED_HELPER_TC',
                          'NO_WATCHDOG', 'STALL_SECS', 'STALL_AFTER_PANIC',
                          'NATIVE_RTC', 'HELPER_SRC')}


def profile_config(profile, development_activation=False):
    if profile not in ('native', 'patched'):
        raise ValueError('unknown profile')
    patched = profile == 'patched'
    env = dict(DISPLAY_ENV, DARWIN_RTC_PV='0')
    if patched:
        env.update(DARWIN_SMP_PV='1')
    return dict(profile=profile, cpus=6 if patched else 1, env=env,
                development_activation=development_activation,
                userspace_patches=['display-allocation', 'settings-scale',
                                   'clock-label-and-non-glass', 'powerd-null-guard'] if patched else [],
                runtime_helpers=['input', 'power-pv-service'] if patched else [],
                kernel_adapters=['smp-pv'] if patched else [],
                clock_source='native-spmi-pmu',
                setup_completion='unchanged', saved_ram=False)


def specs():
    allocation = dict(cache_name='dyld_shared_cache_arm64e.01',
                      purpose='Reviewed software-display allocation policy',
                      edits=[dict(offset=OFFSET, before=EXPECTED.hex(),
                                  after=(EXPECTED[:8] + REPLACEMENT + EXPECTED[12:]).hex())])
    return [allocation] + [json.loads((REPO / 'tools/input' / name).read_text())
                           for name in ('settings_scale_patch_24A5430a.json',
                                        'clock_software_patch_24A5430a.json')]


def cache_payload(cache, spec):
    """Return whole guarded pages plus their signature slots, without editing input."""
    with cache.open('rb') as source:
        header = source.read(4096)
        if len(header) != 4096 or not header.startswith(b'dyld'):
            raise ValueError(f'invalid shared cache: {cache}')
        offset, size = struct.unpack_from('<QQ', header, 0x28)
        if size > 32 * 1024 * 1024 or offset + size > cache.stat().st_size:
            raise ValueError('invalid signature bounds')
        source.seek(offset)
        signature = source.read(size)
        edits = reviewed_edits(spec, source)
        cd, cd_offset, _ = parse_code_directory(signature, 0,
            required_code_end=max(pos + len(before) for pos, before, _ in edits))
        regions = [(0, header, header)]
        for page in sorted({pos // PAGE_SIZE for pos, _, _ in edits}):
            source.seek(page * PAGE_SIZE)
            before = source.read(PAGE_SIZE)
            _, _, slot = parse_code_directory(signature, page)
            old_hash = bytes(cd[slot:slot + 32])
            if len(before) != PAGE_SIZE or hashlib.sha256(before).digest() != old_hash:
                raise ValueError(f'original signed page mismatch: {cache}, page {page}')
            after = bytearray(before)
            for pos, expected, replacement in edits:
                if pos // PAGE_SIZE == page:
                    within = pos % PAGE_SIZE
                    after[within:within + len(expected)] = replacement
            new_hash = hashlib.sha256(after).digest()
            cd[slot:slot + 32] = new_hash
            regions.extend([(page * PAGE_SIZE, before, bytes(after)),
                            (offset + cd_offset + slot, old_hash, new_hash)])
    return regions, hashlib.sha256(cd).hexdigest()[:40]


def original_launchd(path):
    cache = plistlib.loads(path.read_bytes())
    if not isinstance(cache, dict) or not isinstance(cache.get('LaunchDaemons'), dict):
        raise ValueError('unsupported launchd cache')
    if any('dvm-' in str(key) or 'dvm-' in str(value)
           for key, value in cache['LaunchDaemons'].items()):
        raise ValueError('launchd preimage already contains dvm services')
    return cache


def service(name):
    if name == 'input':
        return plistlib.loads((REPO / 'tools/input/com.apple.dvm-input.plist').read_bytes())
    return dict(Label='com.apple.dvm-' + name,
                ProgramArguments=['/usr/local/libexec/dvm-' + name],
                RunAtLoad=True, KeepAlive=True, ThrottleInterval=30,
                UserName='root', ProcessType='Interactive',
                StandardOutputPath='/dev/console', StandardErrorPath='/dev/console')


def installer_script(checks, writes, additions, patched):
    """All preimages and absence checks precede the first System write."""
    lines = ['#!/bin/sh', 'set -eu',
             'mount_apfs ' + ('' if patched else '-o ro ') + '/dev/disk1s1 /mnt1',
             'root=/mnt1',
             'test -s "$root/usr/lib/libobjc-trampolines.dylib"',
             'test -s "$root/usr/lib/libramrod.dylib"',
             'for p in "$root"/usr/local/libexec/dvm-* "$root"/System/Library/LaunchDaemons/com.apple.dvm-*.plist; do',
             '    test ! -e "$p"', 'done']
    for target, payload, offset, length in checks:
        target = shlex.quote('/mnt1' + target)
        read = f'cksum < {target}' if offset is None else f'dd if={target} bs=1 skip={offset} count={length} 2>/dev/null | cksum'
        lines.append(f'test "$({read})" = "$(cksum < /libexec/{payload})"')
    for target, payload, mode in additions:
        lines.append('test ! -e ' + shlex.quote('/mnt1' + target))
    lines.append('echo DVM_PROFILE_PREIMAGES_VERIFIED')
    if patched:
        lines.append('mkdir -p "$root/usr/local/libexec"')
        for target, payload, offset, length in writes:
            target = shlex.quote('/mnt1' + target)
            if offset is None:
                lines += [f'cp /libexec/{payload} {target}', f'chown 0:0 {target}']
                read = f'cksum < {target}'
            else:
                lines.append(f'dd if=/libexec/{payload} of={target} bs=1 seek={offset} count={length} conv=notrunc 2>/dev/null')
                read = f'dd if={target} bs=1 skip={offset} count={length} 2>/dev/null | cksum'
            lines.append(f'test "$({read})" = "$(cksum < /libexec/{payload})"')
        for target, payload, mode in additions:
            target = shlex.quote('/mnt1' + target)
            lines += [f'cp /libexec/{payload} {target}', f'chmod {mode} {target}',
                      f'chown 0:0 {target}',
                      f'test "$(cksum < {target})" = "$(cksum < /libexec/{payload})"']
    # The stock project restore shell has sync but no umount executable.
    # install_staged_helpers quits the owned VM after this durable witness.
    lines += ['sync', 'echo DVM_PROFILE_INSTALLED']
    return '\n'.join(lines) + '\n'


def prepare(a, out, config, env):
    payload = out / 'payload'
    payload.mkdir()
    checks, writes, additions, hashes, records = [], [], [], [], []
    def add(target, before, after=None, offset=None):
        name = f'profile-{len(checks)}'
        (payload / (name + '-before')).write_bytes(before)
        checks.append((target, name + '-before', offset, len(before)))
        if after is not None and before != after:
            (payload / (name + '-after')).write_bytes(after)
            writes.append((target, name + '-after', offset, len(after)))
    for spec in specs():
        cache = a.cache_dir / spec['cache_name']
        regions, cdhash = cache_payload(cache, spec)
        for offset, before, after in regions:
            add(CACHE_ROOT + spec['cache_name'], before,
                after if a.profile == 'patched' else None, offset)
        if a.profile == 'patched':
            hashes.append(cdhash)
        records.append(dict(spec=spec, source_sha256=sha256(cache),
                            applied=a.profile == 'patched', patched_cdhash=cdhash))
    add(POWERD, a.powerd.read_bytes())
    add(LAUNCHD, a.launchd_cache.read_bytes())
    if a.profile == 'patched':
        run('bash', REPO / 'tools/input/build.sh', out / 'input', env=env)
        run('bash', REPO / 'tools/re/build_power_pv_service.sh', out / 'power', env=env)
        run(sys.executable, REPO / 'tools/re/patch_powerd_virtual_battery.py',
            a.powerd, out / 'powerd', env=env)
        # Reuse the preimage above; replacement retains the existing file mode.
        shutil.copyfile(out / 'powerd', payload / 'profile-powerd')
        writes.append((POWERD, 'profile-powerd', None, 0))
        info = subprocess.check_output(['codesign', '-d', '--verbose=4', str(out / 'powerd')],
                                        stderr=subprocess.STDOUT, text=True)
        hashes.append(re.search(r'^CDHash=([0-9a-f]{40})$', info, re.M).group(1))
        cached = original_launchd(a.launchd_cache)
        for name, binary in [('input', out / 'input/dvm-input'),
                             ('power-pv-service', out / 'power/power-pv-service')]:
            job = service(name)
            path = '/System/Library/LaunchDaemons/com.apple.dvm-' + name + '.plist'
            cached['LaunchDaemons'][path] = job
            (payload / ('profile-' + name + '.plist')).write_bytes(plistlib.dumps(job))
            shutil.copyfile(binary, payload / ('profile-' + name))
            additions += [(path, 'profile-' + name + '.plist', '644'),
                          ('/usr/local/libexec/dvm-' + name, 'profile-' + name, '755')]
        (payload / 'profile-launchd').write_bytes(plistlib.dumps(cached, fmt=plistlib.FMT_BINARY, sort_keys=False))
        writes.append((LAUNCHD, 'profile-launchd', None, 0))
        (out / 'hashes.txt').write_text('\n'.join(hashes) + '\n')
        run(sys.executable, REPO / 'build_tc.py', out / 'hashes.txt', out / 'patches.tc', env=env)
        run(sys.executable, REPO / 'tools/rootfs/merge_tc.py', out / 'system.tc', a.tc,
            out / 'patches.tc', out / 'input/helper.tc', out / 'power/power-pv-service.tc', env=env)
        run(sys.executable, REPO / 'tools/re/smp_pv_patch.py', a.firmware / 'bootkc', out / 'bootkc', env=env)
    else:
        shutil.copyfile(a.tc, out / 'system.tc')
        shutil.copyfile(a.firmware / 'bootkc', out / 'bootkc')
    (payload / 'dvm-profile-install.sh').write_text(installer_script(checks, writes, additions, a.profile == 'patched'))
    run('bash', '-n', payload / 'dvm-profile-install.sh', env=env)
    atomic_json(out / 'patch-inventory.json', records)
    for name, extra in [('restore', []), ('system', ['-enable', 'dcp'] +
                        (['-development-activation'] if config['development_activation'] else []))]:
        run(sys.executable, REPO / 'dt_fixup.py', a.dtree_raw, out / (name + '.dtree'),
            '-nvram', a.nvram, '-enable', 'ans', '-enable', 'smc', '-enable', 'sep',
            '-dram', '12G', '-enable', 'spmi', *extra, env=env)
    image = out / 'installer.dmg'
    shutil.copyfile(a.firmware / 'ramdisk.dmg', image)
    wrapper = REPO / 'tools/rootfs/safe_attach.sh'
    # This is the small disposable restore ramdisk, not the large System
    # volume. Its root-owned libexec directory must be writable by the host
    # user while staging the already checksummed installer payloads.
    mount = Path(subprocess.check_output([str(wrapper), 'attach', str(image), '--owners', 'off'], text=True).strip())
    try:
        for file in payload.iterdir():
            shutil.copyfile(file, mount / 'libexec' / file.name)
        run('sync', env=env)
    finally:
        run(wrapper, 'detach', mount, env=env)


def machine(a, out, disk, config, restore=False):
    firmware = a.firmware
    return [str(a.qemu), '-M', 'darwin', '-bootkc', str(firmware / 'bootkc' if restore else out / 'bootkc'),
            '-dtree', str(out / ('restore.dtree' if restore else 'system.dtree')),
            '-tc', str(firmware / 'ramdisk.tc' if restore else out / 'system.tc'),
            '-ramdisk', str(firmware / 'ramdisk.dmg'),
            '-args', ('rd=md0 ' if restore else 'rootdev=disk1s1 ') + BOOTARGS,
            '-display', 'none', '-m', '12G', '-sptm', str(firmware / 'sptm'),
            '-txm', str(firmware / 'txm'), '-smp', '1' if restore else str(config['cpus']),
            '-accel', 'tcg,thread=multi', '-fb', '1179x2556', '-fbmode', 'graphics',
            '-drive', f'if=none,id=ans,file={disk},format=qcow2']


def manifest(a, out, disk, config, destination):
    argv = machine(a, out, disk, config)
    paths = [a.qemu] + [Path(argv[argv.index(key) + 1])
                        for key in ('-bootkc', '-dtree', '-tc', '-ramdisk', '-sptm', '-txm')]
    atomic_json(destination, dict(format='darwin-vm-warm-disk-v1', bootstrap_profile=config,
        qemu_argv=argv, qemu_env=config['env'],
        qemu_inputs={str(p): dict(sha256=sha256(p), bytes=p.stat().st_size) for p in paths},
        disk=dict(path=str(disk), backing_chain=qcow2_backing_chain(a.qemu_img, disk))))


def verdict(serial, stderr, profile):
    required = ['BSD root: disk1s1', 'Early boot complete',
                'mount-complete volume Preboot', 'mount-complete volume Hardware',
                'disk1s5 mount-complete volume User', 'AppleARMRTC publishing service!',
                'AppleDialogSPMIPMURTC started!']
    missing = [s for s in required if s not in serial]
    if not re.search(r'/dev/disk1s2 on /private/var .*protect', serial):
        missing.append('protected Data mount')
    if not re.search(r'handle_mount:893: disk1s2 .*encrypted', serial):
        missing.append('encrypted Data mount')
    failures = [s for s in ('panic(cpu', 'rebooting due to critical process crashes',
                            'Copying ', 'rejected unsupported') if s in serial + stderr]
    display = 'iomfb: presented ' in stderr
    storage_pass = not missing and not failures
    return dict(storage_pass=storage_pass, missing=missing, failures=failures,
                validation_pass=storage_pass and (profile == 'native' or display),
                display_observed=display, profile=profile,
                interactive_stability='not certified; inspect frames, services and input separately')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--profile', required=True, choices=('native', 'patched'))
    source = p.add_mutually_exclusive_group(required=True)
    source.add_argument('--base-image', type=Path, help='merged raw System disk with Data/Preboot/Hardware slots; seed fresh Data')
    source.add_argument('--parent', type=Path, help='explicit read-only seeded qcow2; inherits its Data/Setup history')
    p.add_argument('--out', type=Path, required=True, help='new directory (short path for restore sockets)')
    p.add_argument('--tc', type=Path, required=True, help='original merged System/cryptex trust cache')
    p.add_argument('--cache-dir', type=Path, required=True, help='original exported .01/.13/.21 shared caches')
    p.add_argument('--powerd', type=Path, required=True, help='original exported 24A5430a powerd')
    p.add_argument('--launchd-cache', type=Path, required=True, help='original exported System/Library/xpc/launchd.plist')
    p.add_argument('--dtree-raw', type=Path, required=True)
    p.add_argument('--nvram', type=Path, default=REPO / 'nvram.bin')
    p.add_argument('--firmware', type=Path, default=REPO / 'firmware')
    p.add_argument('--exclave', type=Path, help='matching decrypted ExclaveOS payload; required with --base-image')
    p.add_argument('--qemu', type=Path, default=REPO / 'qemu-sptm/build/qemu-system-aarch64')
    p.add_argument('--qemu-img', type=Path, default=REPO / 'qemu-sptm/build/qemu-img')
    p.add_argument('--development-activation', action='store_true', help='opt into development DT activation; does not complete Setup')
    p.add_argument('--prepare-only', action='store_true', help='build guarded installer/configuration; do not boot or create disk children')
    p.add_argument('--boot-seconds', type=int, default=180, help='per normal-boot observation bound, 1..600')
    a = p.parse_args()
    for key, value in vars(a).items():
        if isinstance(value, Path):
            setattr(a, key, value.resolve())
    if a.out.exists() or not 1 <= a.boot_seconds <= 600:
        p.error('--out must be new and --boot-seconds must be 1..600')
    if len(os.fsencode(str(a.out / 'install/monitor.sock'))) >= 104 or re.search(r'[,\s]', str(a.out)):
        p.error('--out must fit macOS socket paths and contain no comma/whitespace')
    if any(c in str(a.base_image or a.parent) for c in ',\n\r'):
        p.error('source disk path must contain no comma/newline')
    required = [a.base_image or a.parent, a.tc, a.powerd, a.launchd_cache,
                a.dtree_raw, a.nvram, a.qemu, a.qemu_img]
    required += [a.firmware / name for name in ('bootkc', 'ramdisk.dmg', 'ramdisk.tc', 'sptm', 'txm')]
    required += [a.cache_dir / spec['cache_name'] for spec in specs()]
    if a.base_image:
        if not a.exclave:
            p.error('--base-image requires --exclave')
        required.append(a.exclave)
        # Storage stages currently use the canonical checkout firmware.
        if a.firmware != (REPO / 'firmware').resolve():
            p.error('--base-image currently requires the checkout firmware directory')
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        p.error('missing explicit inputs:\n' + '\n'.join(missing))
    if a.qemu_img.name != 'qemu-img':
        p.error('--qemu-img must be named qemu-img (shared installer resolves it through PATH)')
    info_command = [str(a.qemu_img), 'info', '--output=json']
    # The source is a raw APFS container whose historical .dmg suffix is not a
    # UDIF format declaration. qemu-img otherwise guesses UDIF from the suffix
    # and rejects the valid raw image before the explicit format check below.
    if a.base_image:
        info_command += ['-f', 'raw']
    info_command.append(str(a.base_image or a.parent))
    disk_info = json.loads(subprocess.check_output(info_command, text=True))
    expected_format = 'raw' if a.base_image else 'qcow2'
    if disk_info.get('format') != expected_format:
        p.error(f'selected source must have {expected_format} format')
    if a.parent and a.parent.stat().st_mode & 0o222:
        p.error('--parent must already be read-only; do not use a live writable disk')
    if sha256(a.firmware / 'bootkc') != KC_SHA256 or sha256(a.powerd) != SOURCE_SHA256:
        p.error('requires the reviewed 24A5430a bootkc and original powerd')
    binary = a.qemu.read_bytes()
    if any(name not in binary for name in (b'darwin-spmi', b'darwin-pmu')):
        p.error('QEMU binary lacks native SPMI/PMU RTC support; build the current pinned submodule')
    original_launchd(a.launchd_cache)
    # Validate all cache preimages before building or mounting anything.
    for spec in specs():
        cache_payload(a.cache_dir / spec['cache_name'], spec)
    env = clean_env()
    env['PATH'] = str(a.qemu_img.parent) + os.pathsep + env.get('PATH', '')
    env['BASE_TC'] = str(a.tc)
    config = profile_config(a.profile, a.development_activation)
    a.out.mkdir(parents=True)
    report = dict(config=config, source_kind='fresh-data-seed' if a.base_image else 'existing-seeded-parent',
                  inputs={str(path): dict(bytes=path.stat().st_size, sha256=sha256(path)) for path in required},
                  status='preparing', boots=[])
    report['source_revisions'] = {
        name: subprocess.check_output(['git', '-C', str(path), 'rev-parse', 'HEAD'], text=True).strip()
        for name, path in [('repo', REPO), ('qemu', REPO / 'qemu-sptm')]}
    report['source_status'] = subprocess.check_output(
        ['git', '-C', str(REPO), 'status', '--short'], text=True)
    atomic_json(a.out / 'bootstrap.json', report)
    try:
        prepare(a, a.out, config, env)
        report['status'] = 'prepared-not-installed'
        atomic_json(a.out / 'bootstrap.json', report)
        if a.prepare_only:
            print('Prepared only; no baseline disk created.', flush=True)
            return
        tag = 'BP_' + uuid.uuid4().hex[:10]
        if a.base_image:
            seed_env = dict(env, BASE_DMG=str(a.base_image), SRC=str(a.base_image), TC=str(a.tc),
                EXCLAVE=str(a.exclave), NVRAM=str(a.nvram), DTREE_RAW=str(a.dtree_raw),
                DVM_QEMU=str(a.qemu), QEMU_IMG=str(a.qemu_img), TAG_PREFIX=tag,
                SEED_ONLY='1', UPDATE_PARENT_LINK='0', START_AT='format', NATIVE_RTC='1')
            run('bash', REPO / 'tools/rootfs/rebuild_persistent_parent.sh', a.out / 'seed', env=seed_env)
            parent = a.out / 'seed/marker.qcow2'
            # All seed guests have stopped. Seal every newly owned chain member.
            for disk in (a.out / 'seed').glob('*.qcow2'):
                disk.chmod(0o444)
            (a.out / 'seed/base-exclave.dmg').chmod(0o444)
        else:
            parent = a.parent
        template = a.out / 'restore-launch.json'
        atomic_json(template, dict(format='darwin-vm-qemu-launch-v1',
                                  argv=machine(a, a.out, parent, config, restore=True), env={'DARWIN_RTC_PV': '0'}))
        run(sys.executable, REPO / 'tools/re/install_staged_helpers.py', '--template', template,
            '--parent', parent, '--ramdisk', a.out / 'installer.dmg', '--out', a.out / 'install',
            '--tag', tag + '_INSTALL', '--installer', '/libexec/dvm-profile-install.sh',
            '--install-marker', 'DVM_PROFILE_INSTALLED', env=env)
        disk = a.out / 'install/disk.qcow2'
        manifest(a, a.out, disk, config, a.out / 'candidate.json')
        report['status'] = 'installed-not-validated'
        atomic_json(a.out / 'bootstrap.json', report)
        # Chained fresh boots exercise writes from the first boot on the second.
        for index in (1, 2):
            boot_tag = tag + f'_BOOT{index}'
            boot_dir = a.out / f'boot{index}'
            boot_dir.mkdir()
            child = boot_dir / 'disk.qcow2'
            run(a.qemu_img, 'create', '-f', 'qcow2', '-F', 'qcow2', '-b', disk, child, env=env)
            boot_env = dict(env, **config['env'], DVM_QEMU=str(a.qemu))
            run('bash', REPO / 'tools/probe.sh', '--dtree', a.out / 'system.dtree',
                '--bootkc', a.out / 'bootkc', '--ramdisk', a.firmware / 'ramdisk.dmg',
                '--tc', a.out / 'system.tc', '--mem', '12G', '--secs', a.boot_seconds,
                '--tag', boot_tag, '--out', boot_dir, '--bootargs', 'rootdev=disk1s1 ' + BOOTARGS,
                '--launch-manifest', boot_dir / 'launch.json', '--',
                '-smp', config['cpus'], '-accel', 'tcg,thread=multi',
                '-sptm', a.firmware / 'sptm', '-txm', a.firmware / 'txm',
                '-fb', '1179x2556', '-fbmode', 'graphics',
                '-drive', f'if=none,id=ans,file={child},format=qcow2', env=boot_env)
            result = verdict((boot_dir / (boot_tag + '.serial.log')).read_text(errors='replace'),
                             (boot_dir / (boot_tag + '.stderr.log')).read_text(errors='replace'), a.profile)
            atomic_json(boot_dir / 'verdict.json', result)
            report['boots'].append(result)
            child.chmod(0o444)
            if not result['validation_pass']:
                raise RuntimeError(f'boot {index} failed storage/display validation; candidate retained')
            disk = child
        manifest(a, a.out, disk, config, a.out / 'baseline.json')
        report['status'] = 'profile-validated-two-boots; interactive-stability-not-certified'
        print(f'Baseline: {a.out / "baseline.json"}', flush=True)
    except BaseException as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
        raise
    finally:
        atomic_json(a.out / 'bootstrap.json', report)


if __name__ == '__main__':
    main()

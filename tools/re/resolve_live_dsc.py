#!/usr/bin/env python3
import glob
import json
import mmap
import os
import re
import socket
import struct
import sys
import time

sock_path = sys.argv[1]
cache_glob = sys.argv[2]
start_paused = len(sys.argv) > 3 and sys.argv[3] == '--cont'

def hmp(command):
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(20)
    s.connect(sock_path)
    buf = b''
    while not buf.rstrip().endswith(b'(qemu)'):
        buf += s.recv(65536)
    s.sendall(command.encode() + b'\n')
    buf = b''
    while not buf.rstrip().endswith(b'(qemu)'):
        buf += s.recv(65536)
    s.close()
    lines = []
    for line in buf.decode(errors='replace').replace('\r', '').split('\n'):
        if '\x1b[' in line or line.strip() == '(qemu)':
            continue
        lines.append(line)
    return '\n'.join(lines).strip()

def regs():
    out = hmp('info registers')
    pcm = re.search(r'PC=([0-9a-fA-F]+)', out)
    el0 = 'EL0t' in out
    return (int(pcm.group(1), 16) if pcm else None), el0, out

def read_words(pc):
    out = hmp(f'x/8wx 0x{pc:x}')
    words = [int(x, 16) for x in re.findall(r'(?<![0-9a-fA-F])0x([0-9a-fA-F]{8})(?![0-9a-fA-F])', out)]
    if len(words) != 8:
        raise RuntimeError(f'could not parse 8 words at {pc:#x}: {out}')
    return words, out

def mapping_for_offset(path, offset):
    with open(path, 'rb') as f:
        hdr = f.read(0x200)
        mapping_off, mapping_count = struct.unpack_from('<II', hdr, 0x10)
        f.seek(mapping_off)
        for _ in range(mapping_count):
            address, size, file_off, maxprot, initprot = struct.unpack('<QQQII', f.read(32))
            if file_off <= offset < file_off + size:
                return address + offset - file_off
    return None

deadline = time.time() + float(os.environ.get('RESOLVE_DEADLINE_SECS', '45'))
files = [p for p in glob.glob(cache_glob)
         if os.path.isfile(p) and not p.endswith(('.a2s', '.symbols', '.map'))]
attempts = []
if start_paused:
    hmp('cont')
while time.time() < deadline:
    pc, el0, raw = regs()
    if not el0 or pc is None or not (0x180000000 <= pc < 0x340000000):
        time.sleep(0.02)
        continue
    hmp('stop')
    pc, el0, raw = regs()
    if not el0 or pc is None or not (0x180000000 <= pc < 0x340000000):
        hmp('cont')
        continue
    words, dis = read_words(pc)
    pattern = struct.pack('<8I', *words)
    hits = []
    for path in files:
        with open(path, 'rb') as f:
            mm = mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ)
            pos = mm.find(pattern)
            while pos >= 0:
                hits.append((path, pos))
                pos = mm.find(pattern, pos + 1)
            mm.close()
    candidates = []
    for path, off in hits:
        static = mapping_for_offset(path, off)
        if static is None:
            continue
        slide = pc - static
        if 0 <= slide <= 0x20000000 and slide % 0x4000 == 0:
            candidates.append((path, off, static, slide))
    attempts.append({'pc': hex(pc), 'words': [hex(x) for x in words],
                     'hits': len(hits), 'candidates': len(candidates)})
    if len(candidates) == 1:
        path, off, static, slide = candidates[0]
        base = 0x180000000 + slide
        mapped = hmp(f'gva2gpa 0x{base:x}')
        result = {'runtime_pc': hex(pc), 'words': [hex(x) for x in words],
                  'cache_file': path, 'file_offset': hex(off),
                  'static_pc': hex(static), 'slide': hex(slide),
                  'runtime_cache_base': hex(base), 'gva2gpa': mapped,
                  'attempts': attempts}
        print(json.dumps(result, indent=2))
        sys.exit(0)
    hmp('cont')

print(json.dumps({'error': 'no unique cache-backed EL0 PC before deadline',
                  'attempts': attempts}, indent=2))
sys.exit(1)

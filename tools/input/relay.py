#!/usr/bin/env python3
"""Relay QEMU's captured mouse states to the native guest's UART protocol.

Coalesce consecutive motion states, preserve press/release transitions, and
permit one in-flight packet. ACK measures native submission, not UI response.
Never retry ambiguous submission; on timeout stop and report it.
"""
import argparse
from collections import deque
import json
import re
import select
import socket
import time


def enqueue(queue, event):
    if (type(event.get('down')) is not bool or
            any(type(event.get(k)) is not int or not 0 <= event[k] <= 32767
                for k in ('x', 'y'))):
        raise ValueError('invalid QEMU touch state')
    record = dict(event, observed=time.monotonic(), edge=True)
    if queue and queue[-1]['down'] == record['down'] and not queue[-1]['edge']:
        # The most recent position supersedes motion, but never a button edge.
        queue[-1] = dict(record, edge=False)
    else:
        if len(queue) >= 64:
            raise RuntimeError('input queue exceeded 64 button transitions')
        if queue and queue[-1]['down'] == record['down']:
            record['edge'] = False
        queue.append(record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--events', required=True)
    parser.add_argument('--uart', required=True)
    parser.add_argument('--log', required=True)
    parser.add_argument('--char-delay', type=float, default=0,
                        help='optional diagnostic pacing; fixed UART uses backpressure')
    parser.add_argument('--ack-timeout', type=float,
                        help='submission deadline: 30 s for native tap initialization, 10 s otherwise')
    parser.add_argument('--replay-existing', action='store_true')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--home', action='store_true')
    mode.add_argument('--ping', action='store_true', help='check helper without sending HID events')
    parser.add_argument('--ping-count', type=int, default=1)
    mode.add_argument('--release', type=int, nargs=2, metavar=('X', 'Y'),
                      help='release a finger after a failed relay; coordinates 0..32767')
    mode.add_argument('--tap', type=int, nargs=2, metavar=('X', 'Y'),
                      help='one bounded native tap; coordinates 0..32767')
    mode.add_argument('--swipe', type=int, nargs=4, metavar=('X1', 'Y1', 'X2', 'Y2'),
                      help='one buffered native 400 ms swipe; coordinates 0..32767')
    parser.add_argument('--backend', choices=('direct', 'recap'),
                        default='direct',
                        help='live HID (default) or diagnostic buffered Recap; '
                             '--tap defaults to native Recap tap (daemon v6)')
    args = parser.parse_args()
    if args.ack_timeout is None:
        # First native Recap tap initialization took 13.002 s in the bounded
        # SETTINGS_V6_TRACE2 run. This is a submission bound, not tap duration.
        args.ack_timeout = 30 if args.tap or args.swipe else 10
    if not 0 < args.ack_timeout <= 120:
        parser.error('--ack-timeout must be in (0, 120] seconds')
    if args.ping_count < 1 or (args.ping_count != 1 and not args.ping):
        parser.error('--ping-count must be positive and used with --ping')
    if (args.release or args.tap or args.swipe) and not all(0 <= v <= 32767 for v in (args.release or args.tap or args.swipe)):
        parser.error('touch coordinates must be 0..32767')
    swipe_steps = deque()
    if args.swipe:
        x1, y1, x2, y2 = args.swipe
        if (x1, y1) == (x2, y2):
            parser.error('swipe endpoints must differ')
        # Daemon R buffers moves with native 20 ms durations, then plays on
        # lift. UART ACK latency cannot stretch this into a long press.
        swipe_kind = 'R' if args.backend == 'recap' else 'T'
        swipe_steps.extend(dict(down=True, x=round(x1+(x2-x1)*i/20),
                                y=round(y1+(y2-y1)*i/20), kind=swipe_kind)
                           for i in range(21))
        swipe_steps.append(dict(down=False, x=x2, y=y2, kind=swipe_kind))
    queue, partial, replies = deque(), '', b''
    sequence = int(time.time() * 1000)
    with open(args.events) as events, open(args.log, 'a', buffering=1) as log, \
            socket.socket(socket.AF_UNIX) as uart:
        if not args.replay_existing:
            events.seek(0, 2)
        uart.connect(args.uart)
        uart.setblocking(False)
        pending = None
        # Finish any incomplete line left by an earlier disconnected writer.
        uart.sendall(b'\n')
        # Legacy R diagnostics request eight 20 ms stationary moves. This
        # sequence did not launch Settings; the default P packet uses Recap's
        # dedicated native tap generator and needs no host gesture timing.
        tap_steps = [1]*9 + [0] if args.backend == 'recap' else [0]
        home_steps = deque(tap_steps if args.tap else [1, 0] if args.home else [0]*args.ping_count if args.ping
                           else [0] if args.release else [])
        while True:
            partial += events.read()
            lines = partial.split('\n')
            partial = lines.pop()
            for line in lines:
                enqueue(queue, json.loads(line))
            if select.select([uart], [], [], .005)[0]:
                data = uart.recv(65536)
                if not data:
                    raise RuntimeError('guest UART disconnected')
                replies += data
                while b'\n' in replies:
                    line, replies = replies.split(b'\n', 1)
                    match = re.search(rb'DVM_INPUT_ACK (\d+) ([01])', line)
                    if not match or not pending or int(match[1]) != pending['sequence']:
                        continue
                    now = time.monotonic()
                    result = dict(pending, event='ack', success=match[2] == b'1',
                                  submit_ms=(now-pending['sent'])*1000,
                                  queue_to_ack_ms=(now-pending['observed'])*1000)
                    log.write(json.dumps(result)+'\n')
                    print(json.dumps(result), flush=True)
                    if match[2] != b'1':
                        raise RuntimeError('guest could not construct input event')
                    pending = None
                replies = replies[-65536:]
            if pending and time.monotonic()-pending['sent'] > args.ack_timeout:
                raise RuntimeError('native input ACK timed out: '+str(pending['sequence']))
            if pending:
                continue
            if swipe_steps:
                record = dict(swipe_steps.popleft(), observed=time.monotonic())
            elif home_steps:
                x, y = args.release or args.tap or (0, 0)
                record = dict(down=bool(home_steps.popleft()), x=x, y=y,
                              observed=time.monotonic(),
                              kind='H' if args.home else
                                   ('R' if args.backend == 'recap' else 'P') if args.tap else
                                   ('R' if args.backend == 'recap' else 'T') if args.release else 'S')
            elif queue:
                record = dict(queue.popleft(), kind='R' if args.backend == 'recap' else 'T')
            elif args.home or args.ping or args.release or args.tap or args.swipe:
                return
            else:
                continue
            sequence += 1
            pending = dict(record, sequence=sequence, sent=time.monotonic())
            packet = ('DVMINPUT1 %d %s %d %d %d\n' %
                      (sequence, record['kind'], record['down'], record['x'], record['y'])).encode()
            if args.char_delay:
                for byte in packet:
                    select.select([], [uart], [], args.ack_timeout)
                    uart.sendall(bytes([byte]))
                    time.sleep(args.char_delay)
            else:
                uart.sendall(packet)
            log.write(json.dumps(dict(pending, event='sent'))+'\n')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Drive and verify the native UART input transport (DARWIN_INPUT_UART=1).

Host events enter QEMU through QMP ``input-send-event`` (the same absolute
pointer path the Cocoa window uses), QEMU's darwin-input queue forwards them
to the guest helper, and this tool reads QEMU's status file to separate the
stages: queued -> sent -> acknowledged by the helper -> submitted to the HID
system -> first presented frame.  Screendumps before/after are the only
witness for "the UI did something"; an ACK never is.

Coordinates are QEMU's normalised 0..32767 range.  ``--px`` accepts
framebuffer pixels instead (1179x2556 portrait); the guest reports the
digitizer position as a 0..1 fraction of the display, so pixel (x, y) maps to
round(x * 32767 / 1179), round(y * 32767 / 2556).  Backboardd then scales the
fraction to the 1179x2556 display; UIKit points are half of that.
"""
import argparse
import hashlib
import json
import socket
import sys
import time
from pathlib import Path

FB_W, FB_H = 1179, 2556


class QMP:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(10)
        self.sock.connect(str(path))
        self.buf = b''
        self.resume_events = []
        self.recv()                                   # greeting
        self.execute('qmp_capabilities')

    def recv(self):
        while b'\n' not in self.buf:
            data = self.sock.recv(65536)
            if not data:
                raise RuntimeError('QMP closed')
            self.buf += data
        line, self.buf = self.buf.split(b'\n', 1)
        return json.loads(line)

    def execute(self, cmd, **args):
        self.sock.sendall(json.dumps(dict(execute=cmd, arguments=args)).encode() + b'\n')
        while True:
            reply = self.recv()
            if 'event' in reply:
                continue
            if 'error' in reply:
                raise RuntimeError(reply['error'])
            return reply.get('return')

    def ensure_running(self, timeout=10):
        """Resume a paused VM before input, but never revive shutdown."""
        status = self.execute('query-status')
        if status.get('running'):
            return status
        state = status.get('status')
        if state not in ('paused', 'prelaunch', 'debug'):
            raise RuntimeError('QEMU VM is not input-capable: %s' % status)
        self.resume_events.append(status)
        try:
            self.execute('cont')
        except RuntimeError:
            # A concurrent resume is harmless; confirm the state below.
            pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self.execute('query-status')
            if status.get('running'):
                return status
            if status.get('status') not in ('paused', 'prelaunch', 'debug'):
                break
            time.sleep(0.05)
        raise RuntimeError('QEMU did not resume for input: %s' % status)


class HMP:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(15)
        self.sock.connect(str(path))
        self.read()

    def read(self):
        buf = b''
        while not buf.rstrip().endswith(b'(qemu)'):
            chunk = self.sock.recv(65536)
            if not chunk:
                break
            buf += chunk
        return buf.decode(errors='replace')

    def command(self, text):
        self.sock.sendall(text.encode() + b'\n')
        return self.read()


def norm(value, size, px):
    if not px:
        return int(value)
    return round(int(value) * 32767 / size)


def read_status(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def wait_status(path, predicate, timeout, what):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = read_status(path)
        if last and predicate(last):
            return last
        time.sleep(0.05)
    raise TimeoutError(f'{what} not observed within {timeout}s; last status: {last}')


def send(qmp, x, y, down):
    qmp.ensure_running()
    qmp.execute('input-send-event', events=[
        dict(type='abs', data=dict(axis='x', value=x)),
        dict(type='abs', data=dict(axis='y', value=y)),
        dict(type='btn', data=dict(button='left', down=down)),
    ])


def move(qmp, x, y):
    qmp.ensure_running()
    qmp.execute('input-send-event', events=[
        dict(type='abs', data=dict(axis='x', value=x)),
        dict(type='abs', data=dict(axis='y', value=y)),
    ])


def screendump(hmp, path):
    hmp.command(f'screendump {path} -f png')
    data = Path(path).read_bytes()
    return hashlib.sha256(data).hexdigest()


def wait_dispatch_step(status_path, target_count, timeout):
    """Wait until the newly submitted DVMI2 edge/motion is HID-dispatched."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = read_status(status_path)
        if last and (last.get('dispatched', 0) +
                     last.get('dispatch_failed', 0)) >= target_count:
            return last
        if last and last.get('guest_state') != 'R':
            raise RuntimeError(
                'guest input helper left ready state during serialized gesture')
        time.sleep(0.01)
    raise TimeoutError('HID dispatch step %d not observed; last status: %s' %
                       (target_count, last))


def key_press(args, run, qcode, hold_ms, button=None):
    """Home through the F5 key (qcode) or through the right mouse button."""
    status_path = run / 'input-status.json'
    before = wait_status(status_path, lambda s: s['guest_state'] == 'R', args.ready_timeout,
                         'guest helper readiness')
    qmp = QMP(run / 'qmp.sock')
    vm_status = qmp.ensure_running()
    hmp = HMP(run / 'monitor.sock')
    frames = {}
    if args.frames:
        frames['before'] = screendump(hmp, args.frames + '-before.png')
    t0 = time.monotonic()
    for down in (True, False):
        if button:
            event = dict(type='btn', data=dict(button=button, down=down))
        else:
            event = dict(type='key', data=dict(down=down, key=dict(type='qcode', data=qcode)))
        qmp.ensure_running()
        qmp.execute('input-send-event', events=[event])
        if down:
            time.sleep(hold_ms / 1000)
    after = wait_status(status_path,
                        lambda s: s['inflight'] == 0 and s['queue_len'] == 0 and
                        s['sent'] >= before['sent'] + 2,
                        args.ack_timeout, 'button acknowledgement')
    result = dict(qemu_status_before_input=vm_status,
                  qemu_resume_events=qmp.resume_events,
                  stage_host_to_ack_complete_ms=round((time.monotonic() - t0) * 1000, 1),
                  records_sent=after['sent'] - before['sent'],
                  records_acked=after['acked'] - before['acked'],
                  ack_failed=after['ack_failed'] - before['ack_failed'],
                  timeouts=after['timeouts'] - before['timeouts'],
                  ack_last_us=after['ack_last_us'], guest_state=after['guest_state'])
    if args.frames:
        time.sleep(args.settle)
        frames['after'] = screendump(hmp, args.frames + '-after.png')
        result['frame_changed'] = frames['before'] != frames['after']
        result['frames'] = {k: dict(sha256=v) for k, v in frames.items()}
    return result


def wheel(args, run, x, y, notches):
    """Mouse wheel at (x, y): QEMU batches the notches into one W record and
    the guest performs a drag that ends at rest."""
    status_path = run / 'input-status.json'
    before = wait_status(status_path, lambda s: s['guest_state'] == 'R', args.ready_timeout,
                         'guest helper readiness')
    qmp = QMP(run / 'qmp.sock')
    vm_status = qmp.ensure_running()
    hmp = HMP(run / 'monitor.sock')
    frames = {}
    if args.frames:
        frames['before'] = screendump(hmp, args.frames + '-before.png')
    t0 = time.monotonic()
    move(qmp, x, y)
    name = 'wheel-up' if notches > 0 else 'wheel-down'
    for _ in range(abs(notches)):
        qmp.ensure_running()
        qmp.execute('input-send-event', events=[
            dict(type='btn', data=dict(button=name, down=True)),
            dict(type='btn', data=dict(button=name, down=False))])
    after = wait_status(status_path,
                        lambda s: s['inflight'] == 0 and s['queue_len'] == 0 and
                        s['sent'] > before['sent'] and s['contact_sent'] is False and
                        s['dispatched'] + s['dispatch_failed'] > before['dispatched'] + before['dispatch_failed'],
                        args.ack_timeout, 'wheel gesture dispatch')
    result = dict(qemu_status_before_input=vm_status,
                  qemu_resume_events=qmp.resume_events,
                  stage_host_to_dispatch_complete_ms=round((time.monotonic() - t0) * 1000, 1),
                  records_sent=after['sent'] - before['sent'],
                  dispatch_last_us=after['dispatch_last_us'],
                  dispatch_failed=after['dispatch_failed'] - before['dispatch_failed'],
                  timeouts=after['timeouts'] - before['timeouts'],
                  contact_sent=after['contact_sent'])
    if args.frames:
        time.sleep(args.settle)
        frames['after'] = screendump(hmp, args.frames + '-after.png')
        result['frame_changed'] = frames['before'] != frames['after']
        result['frames'] = {k: dict(sha256=v) for k, v in frames.items()}
    return result


def gesture(args, run, points, hold_ms):
    """Down at points[0], moves through the rest, up at the last point."""
    status_path = run / 'input-status.json'
    before = wait_status(status_path, lambda s: s['guest_state'] == 'R', args.ready_timeout,
                         'guest helper readiness')
    qmp = QMP(run / 'qmp.sock')
    vm_status = qmp.ensure_running()
    hmp = HMP(run / 'monitor.sock')
    frames = {}
    if args.frames:
        frames['before'] = screendump(hmp, args.frames + '-before.png')
    t0 = time.monotonic()
    dispatch_target = before['dispatched'] + before['dispatch_failed']

    def wait_for_step():
        nonlocal dispatch_target
        if args.dispatch_each_step:
            dispatch_target += 1
            wait_dispatch_step(status_path, dispatch_target,
                               args.dispatch_step_timeout)

    send(qmp, points[0][0], points[0][1], True)
    wait_for_step()
    step = hold_ms / max(1, len(points) - 1) / 1000 if len(points) > 1 else hold_ms / 1000
    for x, y in points[1:]:
        time.sleep(step)
        move(qmp, x, y)
        wait_for_step()
    if len(points) == 1:
        time.sleep(hold_ms / 1000)
    send(qmp, points[-1][0], points[-1][1], False)
    wait_for_step()
    sent_at = time.monotonic()
    # Every record of this gesture must be acknowledged, not merely queued.
    after = wait_status(status_path,
                        lambda s: s['inflight'] == 0 and s['queue_len'] == 0 and
                        s['sent'] > before['sent'] and s['contact_sent'] is False,
                        args.ack_timeout, 'gesture acknowledgement')
    ack_done = time.monotonic()
    # Stage 2: the helper's worker must have dispatched every touch record
    # (pings and cancels are not dispatched).
    expected = (after['sent'] - before['sent']) - (after['pings'] - before['pings'])
    def dispatched(s):
        return (s['dispatched'] + s['dispatch_failed']) - (before['dispatched'] + before['dispatch_failed'])
    try:
        after = wait_status(status_path, lambda s: dispatched(s) >= expected, args.ack_timeout,
                            'HID dispatch completion')
        dispatch_note = None
    except TimeoutError as error:
        dispatch_note = str(error).split(';')[0]
    result = dict(
        qemu_status_before_input=vm_status,
        qemu_resume_events=qmp.resume_events,
        stage_host_to_ack_complete_ms=round((ack_done - t0) * 1000, 1),
        stage_host_to_dispatch_complete_ms=round((time.monotonic() - t0) * 1000, 1),
        dispatched=dispatched(after), dispatch_failed=after['dispatch_failed'] - before['dispatch_failed'],
        dispatch_last_us=after['dispatch_last_us'], dispatch_max_us=after['dispatch_max_us'],
        delivery_last_ms=after['delivery_last_ms'], delivery_max_ms=after['delivery_max_ms'],
        dispatch_note=dispatch_note,
        records_sent=after['sent'] - before['sent'],
        records_acked=after['acked'] - before['acked'],
        ack_failed=after['ack_failed'] - before['ack_failed'],
        ack_not_ready=after['ack_not_ready'] - before['ack_not_ready'],
        ack_rejected=after['ack_rejected'] - before['ack_rejected'],
        timeouts=after['timeouts'] - before['timeouts'],
        coalesced=after['coalesced'] - before['coalesced'],
        dispatch_each_step=args.dispatch_each_step,
        ack_last_us=after['ack_last_us'], ack_max_us=after['ack_max_us'],
        guest_state=after['guest_state'], epoch=after['epoch'],
    )
    if args.frame_wait:
        try:
            probed = wait_status(status_path, lambda s: s['probe_open'] is False and
                                 s['input_to_present_samples'] > before['input_to_present_samples'],
                                 args.frame_wait, 'presented frame after input')
            result['input_to_present_ms'] = probed['input_to_present_ms']
        except TimeoutError as error:
            result['input_to_present_ms'] = None
            result['present_note'] = str(error).split(';')[0]
        time.sleep(args.settle)
    if args.frames:
        frames['after'] = screendump(hmp, args.frames + '-after.png')
        result['frame_changed'] = frames['before'] != frames['after']
        result['frames'] = {k: dict(sha256=v) for k, v in frames.items()}
    result['host_release_to_report_ms'] = round((time.monotonic() - sent_at) * 1000, 1)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--run', type=Path, required=True,
                   help='run directory holding qmp.sock, monitor.sock and input-status.json')
    p.add_argument('--px', action='store_true', help='coordinates are framebuffer pixels')
    p.add_argument('--hold-ms', type=int, default=80, help='tap hold / swipe duration')
    p.add_argument('--steps', type=int, default=12, help='swipe motion samples')
    p.add_argument('--dispatch-each-step', action='store_true',
                   help='wait for each HID dispatch before sending the next touch point')
    p.add_argument('--dispatch-step-timeout', type=float, default=30,
                   help='per-record timeout used with --dispatch-each-step')
    p.add_argument('--frames', help='prefix for before/after screendumps')
    p.add_argument('--frame-wait', type=float, default=5, help='seconds to wait for a presented frame')
    p.add_argument('--settle', type=float, default=1.5, help='seconds before the after-frame')
    p.add_argument('--ready-timeout', type=float, default=5)
    p.add_argument('--ack-timeout', type=float, default=15)
    p.add_argument('--log', type=Path, help='append the JSON result here')
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('tap'); s.add_argument('x'); s.add_argument('y')
    s = sub.add_parser('swipe'); [s.add_argument(n) for n in ('x1', 'y1', 'x2', 'y2')]
    s = sub.add_parser('home'); s.add_argument('--button', choices=('right',),
                                                help='use the right mouse button instead of F5')
    sub.add_parser('power', help='send one consumer-page Power button press')
    s = sub.add_parser('wheel'); s.add_argument('x'); s.add_argument('y')
    s.add_argument('notches', type=int, help='positive = wheel up (content scrolls up)')
    sub.add_parser('status')
    s = sub.add_parser('wait-ready'); s.add_argument('--timeout', type=float, default=600)
    s = sub.add_parser('frame'); s.add_argument('path')
    args = p.parse_args()
    if args.dispatch_step_timeout <= 0:
        p.error('--dispatch-step-timeout must be positive')
    run = args.run
    status_path = run / 'input-status.json'
    if args.cmd == 'status':
        print(json.dumps(read_status(status_path), indent=1))
        return
    if args.cmd == 'wait-ready':
        status = wait_status(status_path, lambda s: s['guest_state'] == 'R', args.timeout, 'readiness')
        print(json.dumps(dict(ready=True, guest_pid=status['guest_pid'], epoch=status['epoch'])))
        return
    if args.cmd == 'frame':
        print(json.dumps(dict(path=args.path, sha256=screendump(HMP(run / 'monitor.sock'), args.path))))
        return
    if args.cmd in ('home', 'power'):
        # F5 and Power are mapped to consumer-page Home/Power usages by
        # darwin_fb.c; both travel as B records with both edges.
        result = dict(command='home', run=str(run), unix=time.time())
        qcode = 'f5' if args.cmd == 'home' else 'power'
        if args.cmd == 'power':
            result['command'] = 'power'
        try:
            result.update(key_press(args, run, qcode, args.hold_ms,
                                    button=getattr(args, 'button', None)))
            result['ok'] = (result['records_acked'] >= result['records_sent'] and
                            not result['ack_failed'] and not result['timeouts'])
        except TimeoutError as error:
            result.update(ok=False, error=str(error))
        print(json.dumps(result, indent=1))
        if args.log:
            with args.log.open('a') as log:
                log.write(json.dumps(result) + '\n')
        sys.exit(0 if result.get('ok') else 1)
    if args.cmd == 'wheel':
        result = dict(command='wheel', notches=args.notches, run=str(run), unix=time.time())
        try:
            result.update(wheel(args, run, norm(args.x, FB_W, args.px), norm(args.y, FB_H, args.px),
                                args.notches))
            result['ok'] = not result['dispatch_failed'] and not result['timeouts'] and not result['contact_sent']
        except TimeoutError as error:
            result.update(ok=False, error=str(error))
        print(json.dumps(result, indent=1))
        if args.log:
            with args.log.open('a') as log:
                log.write(json.dumps(result) + '\n')
        sys.exit(0 if result.get('ok') else 1)
    if args.cmd == 'tap':
        points = [(norm(args.x, FB_W, args.px), norm(args.y, FB_H, args.px))]
    else:
        x1, y1 = norm(args.x1, FB_W, args.px), norm(args.y1, FB_H, args.px)
        x2, y2 = norm(args.x2, FB_W, args.px), norm(args.y2, FB_H, args.px)
        n = max(2, args.steps)
        points = [(round(x1 + (x2 - x1) * i / (n - 1)), round(y1 + (y2 - y1) * i / (n - 1)))
                  for i in range(n)]
    result = dict(command=args.cmd, points=points, hold_ms=args.hold_ms,
                  run=str(run), unix=time.time())
    try:
        result.update(gesture(args, run, points, args.hold_ms))
        # Pings sent between the gesture's records are acknowledged too.
        result['ok'] = (result['records_acked'] >= result['records_sent'] and
                        not result['ack_failed'] and not result['timeouts'] and
                        not result['dispatch_failed'] and result['dispatch_note'] is None)
    except TimeoutError as error:
        result.update(ok=False, error=str(error))
    print(json.dumps(result, indent=1))
    if args.log:
        with args.log.open('a') as log:
            log.write(json.dumps(result) + '\n')
    sys.exit(0 if result.get('ok') else 1)


if __name__ == '__main__':
    main()

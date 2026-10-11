"""Inspect one known XNU thread's saved state in a frozen QEMU guest."""

import json

import lldb


def _read(process, address, size):
    error = lldb.SBError()
    data = process.ReadMemory(address, size, error)
    if error.Fail() or len(data) != size:
        return b""
    return data


def _u64(data, offset):
    return int.from_bytes(data[offset:offset + 8], "little")


def inspect(debugger, thread_pointer):
    process = debugger.GetSelectedTarget().GetProcess()
    thread_pointer = int(thread_pointer)
    raw = _read(process, thread_pointer, 0x300)
    if not raw:
        raise RuntimeError("cannot read thread 0x%x" % thread_pointer)
    saved = _u64(raw, 0x110)
    if saved and saved >> 48 == 0:
        saved |= 0xFFFF000000000000
    state = _read(process, saved, 0x110) if saved else b""
    result = {
        "thread": thread_pointer,
        "wait_event": _u64(raw, 0x18),
        "continuation": _u64(raw, 0xE0),
        "kernel_stack": _u64(raw, 0xF0),
        "saved": saved,
        "thread_state": _u64(raw, 0x1F0),
        "thread_group": _u64(raw, 0x290),
        "saved_flavor": int.from_bytes(state[:4], "little") if state else None,
    }
    if len(state) == 0x110 and result["saved_flavor"] == 0x15:
        for label, offset in (("fp", 0xF0), ("lr", 0xF8),
                              ("sp", 0x100), ("pc", 0x108)):
            result[label] = _u64(state, offset)
        fp = result["fp"]
        frames = []
        seen = set()
        for _ in range(24):
            if not fp or fp in seen or fp & 7:
                break
            seen.add(fp)
            link = _read(process, fp, 16)
            if not link:
                break
            parent, lr = _u64(link, 0), _u64(link, 8)
            frames.append({"fp": fp, "lr": lr & 0x0000FFFFFFFFFFFF})
            fp = parent
        result["frames"] = frames
    print("LS_HOLDER_SAVED_STATE " + json.dumps(result, sort_keys=True),
          flush=True)
    return result

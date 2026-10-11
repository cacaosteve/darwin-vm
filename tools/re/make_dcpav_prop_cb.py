#!/usr/bin/env python3
"""Print a QEMU D126/D127/D128 callback script for one opaque DCPAV property.

The input is the serialized property body, without its terminating NUL. This
only packages the H17P wire transfer; it does not make the body a valid
OSUnserializeXML object or prove that DCP firmware would publish it.
"""

import argparse
import struct
from pathlib import Path


def callback_script(key: str, payload: bytes) -> str:
    key_bytes = key.encode("ascii")
    if not 1 <= len(key_bytes) <= 63 or b"\0" in key_bytes:
        raise ValueError("key must be 1-63 non-NUL ASCII bytes")
    if not payload:
        raise ValueError("serialized payload is empty")
    if payload.endswith(b"\0"):
        raise ValueError("payload must omit the terminating NUL counted by D126")
    if len(payload) >= 0xFFFFFFFF:
        raise ValueError("payload does not fit the D126 u32 size")

    parts = [f"D126:{struct.pack('<I', len(payload) + 1).hex()}:4"]
    for offset in range(0, len(payload), 0x1000):
        chunk = payload[offset:offset + 0x1000]
        request = (chunk.ljust(0x1000, b"\0")
                   + struct.pack("<II", offset, len(chunk)))
        parts.append(f"D127:{request.hex()}:4")
    parts.append(f"D128:{key_bytes.ljust(0x40, bytes(1)).hex()}:4")
    return ",".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("key", help="property key, such as ColorElements")
    parser.add_argument("payload", type=Path, help="serialized body without NUL")
    args = parser.parse_args()
    try:
        print(callback_script(args.key, args.payload.read_bytes()))
    except (UnicodeEncodeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

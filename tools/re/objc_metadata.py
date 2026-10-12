"""Small, testable helpers for read-only live Objective-C metadata probes."""


def cstring_pointer(value):
    """Strip high PAC/tag bits while preserving a C string's byte address.

    Object, class, and list pointers are commonly aligned, but a `char *` may
    point at any byte. Clearing its low bits can silently turn a name into a
    suffix and make otherwise-correct ivar metadata look corrupt.
    """
    return (int(value) & 0x0000FFFFFFFFFFFF) if value else 0

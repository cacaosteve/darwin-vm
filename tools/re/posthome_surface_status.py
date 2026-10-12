"""Pure classification for the known post-home fb_swap_set_layer x3 arg."""


def _looks_like_user_pointer(value):
    # Captured arm64e guest user heaps used by this probe are in 0x70..0x7f.
    return isinstance(value, int) and 0x7000000000 <= value < 0x800000000000


def classify_iosurface_x3(value, sample_available=True):
    """Classify x3 without treating null/invalid pointers as pixel samples."""
    if not value:
        return "no-surface-pointer-at-x3"
    if not _looks_like_user_pointer(value):
        return "x3-not-a-valid-user-pointer"
    if not sample_available:
        return "valid-user-pointer-sample-limit-reached"
    return "captured-or-read-attempted"

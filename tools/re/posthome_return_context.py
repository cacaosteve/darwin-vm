"""Pure helpers for pairing a sampled function entry to its return PC."""


def return_context_status(expected_thread, expected_sp,
                         actual_thread, actual_sp):
    """Classify a return-PC hit against a saved entry context.

    Exact TPIDR and SP equality is a strong same-invocation filter for these
    synchronous kernel callsites. It is an observation aid, not a general
    unwind proof; callers should retain raw values in the event record.
    """
    if expected_thread is None or expected_sp is None:
        return "entry-context-incomplete"
    if actual_thread is None or actual_sp is None:
        return "return-context-incomplete"
    if actual_thread != expected_thread:
        return "thread-mismatch"
    if actual_sp != expected_sp:
        return "stack-pointer-mismatch"
    return "matched-entry-thread-and-restored-sp"

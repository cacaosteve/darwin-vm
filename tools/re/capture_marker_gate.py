"""Small filesystem gates shared by the read-only capture probes."""

from pathlib import Path
import json


def marker_is_open(marker):
    """Return whether a capture marker, or its retained first copy, exists.

    The gesture callback keeps the first confirmed event in ``*.first.json``.
    Later lock-manager transitions may replace the active marker, but must not
    close independent post-transition pixel/CA samplers that observed the
    original accepted gesture.
    """
    if not marker:
        return True
    path = Path(marker)
    if path.exists():
        return True
    if path.suffix:
        retained = path.with_name(path.stem + ".first" + path.suffix)
        return retained.exists()
    return False


def accepted_transition_marker(marker):
    """Return a confirmed first/active user dismissal marker, if present.

    Gesture callbacks can observe a later lock-manager transition after Home
    and replace the active marker with an unconfirmed event. Prefer a valid
    active marker, then check the retained first confirmed transition.
    """
    if not marker:
        return None
    path = Path(marker)
    candidates = [path]
    if path.suffix:
        candidates.append(path.with_name(path.stem + ".first" + path.suffix))
    for candidate in candidates:
        try:
            value = json.loads(candidate.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        dismissal = value.get("dismissal_gesture")
        if (value.get("for_user_gesture") == 1 and
                value.get("gesture_sequence_confirmed") is True and
                isinstance(dismissal, dict) and
                dismissal.get("confirmed") is True):
            return value
    return None

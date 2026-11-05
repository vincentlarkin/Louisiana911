"""Timestamp repair shared by the Police-to-Citizen adapters (Lake Charles, Sulphur)."""

from datetime import timedelta


def repair_fall_back_start(started, ended, tz):
    """Undo the feed's offset mix-up in the repeated hour when clocks fall back.

    On the night daylight saving time ends, 1:00-1:59 AM happens twice. The
    public P2C feed labels a call that started in the first 1 AM hour (CDT,
    -05:00) with the standard-time offset (-06:00) when it ended in the second
    one, so the call appears to end before it started. Move the start back to
    daylight time only when that exact pattern fits: the local start time
    exists twice, and the corrected start is no later than the end. Anything
    else is returned unchanged so the caller still rejects it.
    """
    if not started or not ended or ended >= started:
        return started
    local = started.astimezone(tz).replace(tzinfo=None)
    first = local.replace(tzinfo=tz, fold=0)
    second = local.replace(tzinfo=tz, fold=1)
    shift = first.utcoffset() - second.utcoffset()
    if shift <= timedelta(0):
        return started  # this wall-clock time only happens once
    corrected = started - shift
    return corrected if corrected <= ended else started

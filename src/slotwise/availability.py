"""Slot availability: pure functions, no I/O, so they can be property-tested exhaustively.

    free slots = working hours − time off − existing bookings, respecting duration + buffer

Time rules (the part that bites in production):
- Working hours are *local wall-clock* times in the tenant's timezone ("09:00–17:00").
- Everything else (bookings, time off, results) is an aware UTC datetime.
- A local window is converted to UTC per day, so a 09:00 start is 03:30Z in Asia/Kolkata but
  13:00Z or 14:00Z in America/New_York depending on daylight saving time.
- Candidate starts step through the window in real (UTC) time, so every slot has its true length
  even on a DST-change day.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True, slots=True, order=True)
class Interval:
    """Half-open [start, end). Two intervals that only touch (a.end == b.start) do not overlap."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Interval datetimes must be timezone-aware")
        if self.end <= self.start:
            raise ValueError("Interval end must be after start")

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.end and other.start < self.end


def local_to_utc(day: date, wall: time, tz: ZoneInfo) -> datetime:
    """Local wall-clock time → UTC.

    For a wall time that doesn't exist (inside a spring-forward gap), zoneinfo interprets it with
    the pre-transition offset, which lands it just after the gap: the sensible "start when the
    clock allows" behaviour. For an ambiguous time (fall-back), fold=0 picks the first occurrence.
    """
    return datetime.combine(day, wall, tzinfo=tz).astimezone(UTC)


def day_bounds_utc(day: date, tz: ZoneInfo) -> Interval:
    """The UTC interval covering one local calendar day (23, 24 or 25 hours long)."""
    return Interval(
        local_to_utc(day, time(0), tz), local_to_utc(day + timedelta(days=1), time(0), tz)
    )


def windows_for_day(
    day: date, tz: ZoneInfo, working_hours: Iterable[tuple[time, time]]
) -> list[Interval]:
    windows = []
    for start, end in working_hours:
        s, e = local_to_utc(day, start, tz), local_to_utc(day, end, tz)
        if e > s:  # a window entirely inside a DST gap collapses to nothing
            windows.append(Interval(s, e))
    return sorted(windows)


def compute_slots(
    *,
    day: date,
    tz: ZoneInfo,
    working_hours: Iterable[tuple[time, time]],
    busy: Sequence[Interval],
    duration: timedelta,
    buffer: timedelta = timedelta(0),
    step: timedelta = timedelta(minutes=15),
    not_before: datetime | None = None,
) -> list[Interval]:
    """Free slots for one staff member on one local day.

    A slot [s, s + duration) is offered when:
      - it fits inside a working window (the trailing buffer may run past closing time),
      - its reserved block [s, s + duration + buffer) overlaps nothing in `busy`,
      - s >= not_before (no booking in the past).
    Busy intervals are existing bookings' reserved blocks (which already include their buffer)
    and time off.
    """
    if duration <= timedelta(0) or step <= timedelta(0):
        raise ValueError("duration and step must be positive")
    busy_sorted = sorted(busy)
    slots: list[Interval] = []
    for window in windows_for_day(day, tz, working_hours):
        start = window.start
        while start + duration <= window.end:
            block = Interval(start, start + duration + buffer)
            if (not_before is None or start >= not_before) and not _hits(block, busy_sorted):
                slots.append(Interval(start, start + duration))
            start += step
    return slots


def _hits(block: Interval, busy_sorted: Sequence[Interval]) -> bool:
    for b in busy_sorted:
        if b.start >= block.end:  # sorted by start: nothing later can overlap
            return False
        if b.overlaps(block):
            return True
    return False

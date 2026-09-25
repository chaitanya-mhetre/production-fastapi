"""Availability: example tests for DST edges + property-based tests against a brute-force oracle."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from slotwise.availability import Interval, compute_slots, day_bounds_utc, windows_for_day

NY = ZoneInfo("America/New_York")
IST = ZoneInfo("Asia/Kolkata")
H = timedelta(hours=1)
M = timedelta(minutes=1)


def utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


# --- examples ----------------------------------------------------------------


def test_ist_has_no_dst_and_half_hour_offset() -> None:
    [w] = windows_for_day(date(2026, 10, 1), IST, [(time(9), time(17))])
    assert w == Interval(utc(2026, 10, 1, 3, 30), utc(2026, 10, 1, 11, 30))


@pytest.mark.parametrize(
    ("day", "expected_start"),
    [
        (date(2026, 3, 7), utc(2026, 3, 7, 14)),  # EST, UTC-5
        (date(2026, 3, 9), utc(2026, 3, 9, 13)),  # EDT, UTC-4 (DST began 2026-03-08)
        (date(2026, 11, 2), utc(2026, 11, 2, 14)),  # back to EST (DST ended 2026-11-01)
    ],
)
def test_same_local_opening_moves_in_utc_across_dst(day: date, expected_start: datetime) -> None:
    [w] = windows_for_day(day, NY, [(time(9), time(17))])
    assert w.start == expected_start


def test_day_lengths_on_dst_change_days() -> None:
    spring = day_bounds_utc(date(2026, 3, 8), NY)
    fall = day_bounds_utc(date(2026, 11, 1), NY)
    assert spring.end - spring.start == 23 * H
    assert fall.end - fall.start == 25 * H


def test_window_across_spring_forward_gap_has_real_length() -> None:
    # 01:00–04:00 local on spring-forward night is only 2 real hours (02:00–03:00 is skipped).
    slots = compute_slots(
        day=date(2026, 3, 8),
        tz=NY,
        working_hours=[(time(1), time(4))],
        busy=[],
        duration=H,
        step=H,
    )
    assert len(slots) == 2
    assert all(s.end - s.start == H for s in slots)


def test_window_across_fall_back_has_extra_hour() -> None:
    slots = compute_slots(
        day=date(2026, 11, 1),
        tz=NY,
        working_hours=[(time(0), time(3))],
        busy=[],
        duration=H,
        step=H,
    )
    assert len(slots) == 4  # 00:00–03:00 local spans 4 real hours on fall-back night


def test_busy_and_buffer_are_respected() -> None:
    day = date(2026, 10, 1)
    booking = Interval(utc(2026, 10, 1, 4, 30), utc(2026, 10, 1, 5, 0))  # 10:00–10:30 IST
    slots = compute_slots(
        day=day,
        tz=IST,
        working_hours=[(time(9), time(12))],
        busy=[booking],
        duration=30 * M,
        buffer=15 * M,
        step=15 * M,
    )
    starts_ist = [s.start.astimezone(IST).strftime("%H:%M") for s in slots]
    # 09:30 start → block 09:30–10:15 hits the 10:00 booking; 09:15 → 09:15–10:00 just touches: OK.
    assert "09:15" in starts_ist and "09:30" not in starts_ist
    assert "10:00" not in starts_ist and "10:30" in starts_ist
    assert starts_ist[-1] == "11:30"  # 11:30–12:00 fits; its buffer may spill past closing


def test_not_before_hides_past_slots() -> None:
    slots = compute_slots(
        day=date(2026, 10, 1),
        tz=IST,
        working_hours=[(time(9), time(11))],
        busy=[],
        duration=H,
        step=H,
        not_before=utc(2026, 10, 1, 4, 0),  # 09:30 IST
    )
    assert [s.start for s in slots] == [utc(2026, 10, 1, 4, 30)]  # only 10:00 IST


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError):
        Interval(datetime(2026, 1, 1), datetime(2026, 1, 2))  # noqa: DTZ001


# --- properties ---------------------------------------------------------------

TIMEZONES = st.sampled_from(
    ["Asia/Kolkata", "America/New_York", "Europe/London", "Australia/Lord_Howe", "UTC"]
)
DAYS = st.dates(min_value=date(2026, 1, 1), max_value=date(2027, 12, 31))


@st.composite
def scenario(draw: st.DrawFn) -> dict[str, object]:
    tz = ZoneInfo(draw(TIMEZONES))
    day = draw(DAYS)
    open_min = draw(st.integers(0, 20 * 60))
    close_min = draw(st.integers(open_min + 5, 24 * 60 - 1))
    wh = [(time(open_min // 60, open_min % 60), time(close_min // 60, close_min % 60))]
    bounds = day_bounds_utc(day, tz)
    busy = []
    for _ in range(draw(st.integers(0, 6))):
        offset = draw(st.integers(0, int((bounds.end - bounds.start).total_seconds() // 60) - 1))
        length = draw(st.integers(1, 180))
        start = bounds.start + offset * M
        busy.append(Interval(start, start + length * M))
    return {
        "day": day,
        "tz": tz,
        "working_hours": wh,
        "busy": busy,
        "duration": draw(st.integers(5, 120)) * M,
        "buffer": draw(st.integers(0, 30)) * M,
        "step": draw(st.sampled_from([5, 10, 15, 30])) * M,
    }


def brute_force(sc: dict[str, object]) -> list[Interval]:
    """Oracle: check every minute-aligned start in the day, slowly and obviously."""
    [window] = windows_for_day(sc["day"], sc["tz"], sc["working_hours"])  # type: ignore[arg-type]
    duration, buffer, step = sc["duration"], sc["buffer"], sc["step"]
    assert isinstance(duration, timedelta) and isinstance(buffer, timedelta)
    assert isinstance(step, timedelta)
    out = []
    t = window.start
    while t + duration <= window.end:
        block = Interval(t, t + duration + buffer)
        if not any(block.overlaps(b) for b in sc["busy"]):  # type: ignore[attr-defined]
            out.append(Interval(t, t + duration))
        t += step
    return out


@settings(max_examples=300, deadline=None)
@given(scenario())
def test_matches_brute_force_oracle(sc: dict[str, object]) -> None:
    if not windows_for_day(sc["day"], sc["tz"], sc["working_hours"]):  # type: ignore[arg-type]
        return  # window swallowed by a DST gap
    assert compute_slots(**sc) == brute_force(sc)  # type: ignore[arg-type]


@settings(max_examples=300, deadline=None)
@given(scenario())
def test_invariants(sc: dict[str, object]) -> None:
    slots = compute_slots(**sc)  # type: ignore[arg-type]
    windows = windows_for_day(sc["day"], sc["tz"], sc["working_hours"])  # type: ignore[arg-type]
    buffer = sc["buffer"]
    assert isinstance(buffer, timedelta)
    for s in slots:
        assert s.end - s.start == sc["duration"]
        assert any(w.start <= s.start and s.end <= w.end for w in windows)
        block = Interval(s.start, s.end + buffer)
        assert not any(block.overlaps(b) for b in sc["busy"])  # type: ignore[attr-defined]
    assert slots == sorted(slots)

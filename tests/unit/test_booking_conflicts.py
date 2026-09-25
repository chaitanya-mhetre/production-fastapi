"""40P01 during a calendar write is reported as 409 slot_taken, never as a 500."""

import pytest
from sqlalchemy.exc import DBAPIError

from slotwise.errors import SlotTaken
from slotwise.services.bookings import BookingService


class _PgError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def _service() -> BookingService:
    return object.__new__(BookingService)  # only the conflict mapping is under test


@pytest.mark.parametrize("code", ["23P01", "40P01"])
async def test_overlap_and_overlap_deadlock_become_slot_taken(code: str) -> None:
    with pytest.raises(SlotTaken):
        async with _service()._overlap_is_409():
            raise DBAPIError("INSERT INTO bookings ...", {}, _PgError(code))


async def test_other_database_errors_are_not_masked() -> None:
    with pytest.raises(DBAPIError):
        async with _service()._overlap_is_409():
            raise DBAPIError("INSERT INTO bookings ...", {}, _PgError("23503"))

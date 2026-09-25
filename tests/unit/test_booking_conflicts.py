"""40P01 during a calendar write is reported as 409 slot_taken, never as a 500."""

from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy.exc import DBAPIError

from slotwise.errors import SlotTaken
from slotwise.services.bookings import BookingService


class _PgError(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


class _Bookings:
    def __init__(self) -> None:
        self.locked: list[Any] = []

    async def lock_staff_schedule(self, staff_id: Any) -> None:
        self.locked.append(staff_id)


def _service() -> tuple[BookingService, _Bookings]:
    service = object.__new__(BookingService)  # only the conflict mapping is under test
    fake = _Bookings()
    service.bookings = fake  # type: ignore[assignment]
    return service, fake


@pytest.mark.parametrize("code", ["23P01", "40P01"])
async def test_overlap_and_overlap_deadlock_become_slot_taken(code: str) -> None:
    service, fake = _service()
    staff = uuid4()
    with pytest.raises(SlotTaken):
        async with service._overlap_is_409(staff):
            raise DBAPIError("INSERT INTO bookings ...", {}, _PgError(code))
    assert fake.locked == [staff]  # the staff calendar lock is taken before the write


async def test_other_database_errors_are_not_masked() -> None:
    service, _ = _service()
    with pytest.raises(DBAPIError):
        async with service._overlap_is_409(uuid4()):
            raise DBAPIError("INSERT INTO bookings ...", {}, _PgError("23503"))

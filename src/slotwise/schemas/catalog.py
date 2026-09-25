from datetime import datetime, time
from typing import Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from slotwise.schemas.common import ORMModel


class ServiceIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    duration_min: int = Field(ge=5, le=720)
    price_paise: int = Field(ge=0)
    buffer_min: int = Field(default=0, ge=0, le=240)


class ServiceUpdateIn(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    duration_min: int | None = Field(default=None, ge=5, le=720)
    price_paise: int | None = Field(default=None, ge=0)
    buffer_min: int | None = Field(default=None, ge=0, le=240)
    active: bool | None = None


class ServiceOut(ORMModel):
    id: UUID
    name: str
    duration_min: int
    price_paise: int
    buffer_min: int
    active: bool


class StaffIn(BaseModel):
    display_name: str = Field(min_length=1, max_length=200)
    user_id: UUID | None = None
    service_ids: list[UUID] = []


class StaffUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    active: bool | None = None
    avatar_key: str | None = None
    service_ids: list[UUID] | None = None


class StaffOut(BaseModel):
    id: UUID
    display_name: str
    user_id: UUID | None
    avatar_key: str | None
    active: bool
    service_ids: list[UUID]


class WorkingHoursItem(BaseModel):
    weekday: int = Field(ge=0, le=6, description="0 = Monday")
    start_time: time
    end_time: time

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start_time >= self.end_time:
            raise ValueError("start_time must be before end_time")
        return self


class WorkingHoursIn(BaseModel):
    items: list[WorkingHoursItem]

    @model_validator(mode="after")
    def _no_overlap(self) -> Self:
        by_day: dict[int, list[WorkingHoursItem]] = {}
        for item in self.items:
            by_day.setdefault(item.weekday, []).append(item)
        for day_items in by_day.values():
            day_items.sort(key=lambda i: i.start_time)
            for a, b in zip(day_items, day_items[1:], strict=False):
                if b.start_time < a.end_time:
                    raise ValueError(f"overlapping working hours on weekday {a.weekday}")
        return self


class TimeOffIn(BaseModel):
    start: datetime
    end: datetime
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _valid(self) -> Self:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("start/end must include a timezone offset")
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class TimeOffOut(BaseModel):
    id: UUID
    staff_id: UUID
    start: datetime
    end: datetime
    reason: str | None


class SlotOut(BaseModel):
    staff_id: UUID
    start: datetime
    end: datetime

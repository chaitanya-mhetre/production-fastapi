"""Keyset (cursor) pagination on (created_at, id).

Why not OFFSET: `OFFSET 100000` makes Postgres read and throw away 100k rows, and rows shift
between pages when new ones are inserted. A keyset cursor is O(page size) and stable.
"""

import base64
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from slotwise.errors import ValidationFailed


@dataclass(frozen=True, slots=True)
class Cursor:
    created_at: datetime
    id: UUID

    def encode(self) -> str:
        raw = f"{self.created_at.isoformat()}|{self.id}"
        return base64.urlsafe_b64encode(raw.encode()).decode()

    @classmethod
    def decode(cls, value: str) -> "Cursor":
        try:
            created, id_ = base64.urlsafe_b64decode(value.encode()).decode().split("|")
            return cls(datetime.fromisoformat(created), UUID(id_))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationFailed("invalid cursor") from exc

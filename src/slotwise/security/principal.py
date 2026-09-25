"""Who is making the request: a user (via JWT) or an API key."""

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from slotwise.models import Role, TenantPlan
from slotwise.security.permissions import ROLE_PERMISSIONS, Permission


@dataclass(frozen=True, slots=True)
class Principal:
    actor_type: Literal["user", "api_key"]
    actor_id: UUID
    tenant_id: UUID | None
    role: Role | None = None
    scopes: frozenset[Permission] = frozenset()
    is_superadmin: bool = False
    plan: TenantPlan | None = None

    @property
    def permissions(self) -> frozenset[Permission]:
        if self.actor_type == "api_key":
            return self.scopes
        return ROLE_PERMISSIONS[self.role] if self.role else frozenset()

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

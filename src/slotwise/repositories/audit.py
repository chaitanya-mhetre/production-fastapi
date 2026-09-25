from typing import Any

from slotwise.context import request_id_var
from slotwise.models import AuditLog
from slotwise.observability.tracing import current_trace_id
from slotwise.repositories.base import TenantScopedRepository
from slotwise.security.principal import Principal


class AuditRepository(TenantScopedRepository):
    def record(
        self,
        principal: Principal | None,
        *,
        action: str,
        entity: str,
        entity_id: object,
        diff: dict[str, Any] | None = None,
    ) -> None:
        """Written in the caller's transaction, so the audit row exists iff the change committed."""
        self.session.add(
            AuditLog(
                tenant_id=self.tenant_id,
                actor_type=principal.actor_type if principal else "system",
                actor_id=str(principal.actor_id) if principal else "system",
                action=action,
                entity=entity,
                entity_id=str(entity_id),
                diff=diff or {},
                request_id=request_id_var.get(),
                trace_id=current_trace_id(),
            )
        )

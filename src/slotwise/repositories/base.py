"""Base for tenant-scoped repositories.

Tenant isolation, layer 1: a repository cannot be constructed without a tenant id, and every
query it builds filters on it. Layer 2 (RLS) catches anything this layer misses.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession


class TenantScopedRepository:
    def __init__(self, session: AsyncSession, tenant_id: UUID) -> None:
        if tenant_id is None:  # defensive: callers are typed, but this is a security boundary
            raise ValueError("tenant-scoped repository requires a tenant_id")
        self.session = session
        self.tenant_id = tenant_id

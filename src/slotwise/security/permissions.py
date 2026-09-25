"""RBAC as data: roles map to permission sets. API keys carry permissions directly as scopes."""

import enum

from slotwise.models import Role


class Permission(enum.StrEnum):
    TENANT_READ = "tenant:read"
    TENANT_MANAGE = "tenant:manage"
    MEMBERS_MANAGE = "members:manage"
    CUSTOMERS_READ = "customers:read"
    CUSTOMERS_WRITE = "customers:write"
    CATALOG_READ = "catalog:read"
    CATALOG_WRITE = "catalog:write"
    BOOKINGS_READ = "bookings:read"
    BOOKINGS_WRITE = "bookings:write"
    API_KEYS_MANAGE = "api_keys:manage"
    WEBHOOKS_MANAGE = "webhooks:manage"
    AUDIT_READ = "audit:read"
    UPLOADS_WRITE = "uploads:write"


ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.TENANT_ADMIN: frozenset(Permission),
    Role.RECEPTIONIST: frozenset(
        {
            Permission.TENANT_READ,
            Permission.CUSTOMERS_READ,
            Permission.CUSTOMERS_WRITE,
            Permission.CATALOG_READ,
            Permission.BOOKINGS_READ,
            Permission.BOOKINGS_WRITE,
            Permission.UPLOADS_WRITE,
        }
    ),
    Role.STAFF: frozenset(
        {Permission.TENANT_READ, Permission.CATALOG_READ, Permission.BOOKINGS_READ}
    ),
}

# Scopes an API key may be given. Admin-only powers (members, keys, webhooks) are never grantable
# to a key, so a leaked key can't be used to mint more keys or redirect webhooks.
API_KEY_GRANTABLE: frozenset[Permission] = frozenset(
    {
        Permission.TENANT_READ,
        Permission.CUSTOMERS_READ,
        Permission.CUSTOMERS_WRITE,
        Permission.CATALOG_READ,
        Permission.CATALOG_WRITE,
        Permission.BOOKINGS_READ,
        Permission.BOOKINGS_WRITE,
    }
)

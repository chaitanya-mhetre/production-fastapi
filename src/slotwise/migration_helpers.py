"""Helpers used by hand-written Alembic migrations."""

from alembic import op

APP_ROLES = ("slotwise_app", "slotwise_worker")

# NULLIF(...,'') matters: current_setting(..., true) returns '' (not NULL) once the setting has
# been used on a connection, and ''::uuid would raise instead of simply matching nothing.
TENANT_PREDICATE = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def grant_to_app_roles(table: str) -> None:
    for role in APP_ROLES:
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO {role}")


def grant_sequences() -> None:
    for role in APP_ROLES:
        op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}")


def enable_tenant_rls(table: str) -> None:
    """Turn on RLS so rows are only visible/insertable for the tenant in app.tenant_id."""
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY tenant_isolation ON {table} "
        f"USING ({TENANT_PREDICATE}) WITH CHECK ({TENANT_PREDICATE})"
    )
    grant_to_app_roles(table)

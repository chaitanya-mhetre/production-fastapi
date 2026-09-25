"""M4 auth hardening: rotating refresh tokens, API keys, quota index.

Revision ID: 0004
Revises: 0003
"""

from alembic import op

from slotwise.migration_helpers import grant_to_app_roles

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE refresh_tokens (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tenant_id uuid REFERENCES tenants(id) ON DELETE CASCADE,
            family_id uuid NOT NULL,
            token_hash text NOT NULL UNIQUE,
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            replaced_by uuid REFERENCES refresh_tokens(id),
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX ix_refresh_tokens_family ON refresh_tokens (family_id)")

    op.execute("""
        CREATE TABLE api_keys (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name text NOT NULL,
            prefix text NOT NULL UNIQUE,
            key_hash text NOT NULL,
            scopes text[] NOT NULL,
            rate_limit_per_min integer NOT NULL DEFAULT 600 CHECK (rate_limit_per_min > 0),
            created_by uuid REFERENCES users(id) ON DELETE SET NULL,
            last_used_at timestamptz,
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX ix_api_keys_tenant ON api_keys (tenant_id)")
    # Both are looked up *before* the tenant is known (by token hash / key prefix), so they are
    # global tables without RLS; every query filters explicitly.
    grant_to_app_roles("refresh_tokens")
    grant_to_app_roles("api_keys")

    op.execute("CREATE INDEX ix_bookings_tenant_created ON bookings (tenant_id, created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_bookings_tenant_created")
    op.execute("DROP TABLE IF EXISTS api_keys")
    op.execute("DROP TABLE IF EXISTS refresh_tokens")

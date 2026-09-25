"""M1 tenancy: tenants, users, memberships, customers, audit log + RLS.

Revision ID: 0001
Revises:
"""

from alembic import op

from slotwise.migration_helpers import enable_tenant_rls, grant_sequences, grant_to_app_roles

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute("CREATE TYPE tenant_plan AS ENUM ('free', 'pro')")
    op.execute("CREATE TYPE tenant_status AS ENUM ('active', 'suspended')")
    op.execute("CREATE TYPE tenant_role AS ENUM ('tenant_admin', 'receptionist', 'staff')")

    op.execute("""
        CREATE TABLE tenants (
            id uuid PRIMARY KEY,
            slug text NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9][a-z0-9-]{1,48}$'),
            name text NOT NULL,
            plan tenant_plan NOT NULL DEFAULT 'free',
            timezone text NOT NULL DEFAULT 'Asia/Kolkata',
            status tenant_status NOT NULL DEFAULT 'active',
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("""
        CREATE TABLE users (
            id uuid PRIMARY KEY,
            email citext NOT NULL UNIQUE,
            password_hash text NOT NULL,
            full_name text NOT NULL,
            is_superadmin boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("""
        CREATE TABLE tenant_memberships (
            user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            role tenant_role NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (user_id, tenant_id)
        )""")
    op.execute("CREATE INDEX ix_memberships_tenant ON tenant_memberships (tenant_id)")
    # Global tables: needed before the tenant is known (login), so no RLS. Access goes through
    # repositories that always filter explicitly.
    for table in ("tenants", "users", "tenant_memberships"):
        grant_to_app_roles(table)

    op.execute("""
        CREATE TABLE customers (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name text NOT NULL,
            phone text NOT NULL,
            email text,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (tenant_id, phone)
        )""")
    enable_tenant_rls("customers")

    op.execute("""
        CREATE TABLE audit_logs (
            id bigserial PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            actor_type text NOT NULL,
            actor_id text NOT NULL,
            action text NOT NULL,
            entity text NOT NULL,
            entity_id text NOT NULL,
            diff jsonb NOT NULL DEFAULT '{}',
            request_id text,
            trace_id text,
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX ix_audit_tenant_created ON audit_logs (tenant_id, created_at DESC)")
    op.execute("CREATE INDEX ix_audit_entity ON audit_logs (entity, entity_id)")
    enable_tenant_rls("audit_logs")
    grant_sequences()


def downgrade() -> None:
    for table in ("audit_logs", "customers", "tenant_memberships", "users", "tenants"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for typ in ("tenant_role", "tenant_status", "tenant_plan"):
        op.execute(f"DROP TYPE IF EXISTS {typ}")

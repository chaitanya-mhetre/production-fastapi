"""M2 catalogue: services, staff, staff_services, working_hours, time_off.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

from slotwise.migration_helpers import enable_tenant_rls

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE services (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            name text NOT NULL,
            duration_min integer NOT NULL CHECK (duration_min BETWEEN 5 AND 720),
            price_paise bigint NOT NULL CHECK (price_paise >= 0),
            buffer_min integer NOT NULL DEFAULT 0 CHECK (buffer_min BETWEEN 0 AND 240),
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX ix_services_tenant ON services (tenant_id) WHERE active")

    op.execute("""
        CREATE TABLE staff (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            user_id uuid REFERENCES users(id) ON DELETE SET NULL,
            display_name text NOT NULL,
            avatar_key text,
            active boolean NOT NULL DEFAULT true,
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX ix_staff_tenant ON staff (tenant_id)")

    op.execute("""
        CREATE TABLE staff_services (
            staff_id uuid NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
            service_id uuid NOT NULL REFERENCES services(id) ON DELETE CASCADE,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            PRIMARY KEY (staff_id, service_id)
        )""")
    op.execute("CREATE INDEX ix_staff_services_service ON staff_services (service_id)")

    op.execute("""
        CREATE TABLE working_hours (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            staff_id uuid NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
            weekday smallint NOT NULL CHECK (weekday BETWEEN 0 AND 6),  -- 0 = Monday (ISO - 1)
            start_time time NOT NULL,
            end_time time NOT NULL,
            CHECK (start_time < end_time)
        )""")
    op.execute("CREATE INDEX ix_working_hours_staff_day ON working_hours (staff_id, weekday)")

    op.execute("""
        CREATE TABLE time_off (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            staff_id uuid NOT NULL REFERENCES staff(id) ON DELETE CASCADE,
            period tstzrange NOT NULL CHECK (NOT isempty(period)),
            reason text,
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    # GiST index answers "which time-off overlaps this day?" (period && day_range) efficiently.
    op.execute("CREATE INDEX ix_time_off_staff_period ON time_off USING gist (staff_id, period)")

    for table in ("services", "staff", "staff_services", "working_hours", "time_off"):
        enable_tenant_rls(table)


def downgrade() -> None:
    for table in ("time_off", "working_hours", "staff_services", "staff", "services"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

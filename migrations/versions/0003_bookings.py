"""M3 bookings (exclusion constraint) + idempotency keys.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

from slotwise.migration_helpers import enable_tenant_rls

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE booking_status AS ENUM "
        "('pending_payment', 'confirmed', 'cancelled', 'no_show', 'completed')"
    )
    op.execute("""
        CREATE TABLE bookings (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            staff_id uuid NOT NULL REFERENCES staff(id),
            service_id uuid NOT NULL REFERENCES services(id),
            customer_id uuid NOT NULL REFERENCES customers(id),
            -- The *reserved block*: service duration + trailing buffer. The constraint below
            -- works on this, so buffers are enforced by the database too.
            period tstzrange NOT NULL CHECK (NOT isempty(period)),
            starts_at timestamptz NOT NULL,
            ends_at timestamptz NOT NULL,
            status booking_status NOT NULL,
            price_paise bigint NOT NULL,
            notes text,
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK (starts_at < ends_at),
            -- No two *active* bookings for the same staff member may overlap in time.
            -- Enforced atomically by Postgres, so two concurrent requests can't both win.
            CONSTRAINT no_double_booking EXCLUDE USING gist (staff_id WITH =, period WITH &&)
                WHERE (status IN ('pending_payment', 'confirmed'))
        )""")
    op.execute("CREATE INDEX ix_bookings_tenant_start ON bookings (tenant_id, starts_at, id)")
    op.execute("CREATE INDEX ix_bookings_customer ON bookings (customer_id)")
    enable_tenant_rls("bookings")

    op.execute("""
        CREATE TABLE idempotency_keys (
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            key text NOT NULL CHECK (length(key) BETWEEN 8 AND 255),
            request_hash text NOT NULL,
            response_status integer,
            response_body jsonb,
            locked_until timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (tenant_id, key)
        )""")
    op.execute("CREATE INDEX ix_idempotency_created ON idempotency_keys (created_at)")
    enable_tenant_rls("idempotency_keys")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS idempotency_keys")
    op.execute("DROP TABLE IF EXISTS bookings")
    op.execute("DROP TYPE IF EXISTS booking_status")

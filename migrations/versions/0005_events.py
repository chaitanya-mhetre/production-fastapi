"""M5 events: outbox, consumer inbox, webhooks + deliveries, payments, reminder tracking.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

from slotwise.migration_helpers import enable_tenant_rls, grant_sequences

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE outbox_events (
            id bigserial PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            aggregate_type text NOT NULL,
            aggregate_id text NOT NULL,
            event_type text NOT NULL,
            payload jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            published_at timestamptz,
            attempts integer NOT NULL DEFAULT 0,
            last_error text
        )""")
    # Partial index: the relay only ever scans unpublished rows, and that set stays tiny.
    op.execute(
        "CREATE INDEX ix_outbox_unpublished ON outbox_events (id) WHERE published_at IS NULL"
    )
    enable_tenant_rls("outbox_events")

    # Inbox: at-least-once delivery means consumers see duplicates. Each consumer records the
    # event ids it has processed and skips repeats (idempotent consumer pattern).
    op.execute("""
        CREATE TABLE consumer_inbox (
            consumer text NOT NULL,
            event_id bigint NOT NULL REFERENCES outbox_events(id) ON DELETE CASCADE,
            processed_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (consumer, event_id)
        )""")
    op.execute("GRANT SELECT, INSERT, DELETE ON consumer_inbox TO slotwise_worker")

    op.execute("""
        CREATE TABLE webhook_endpoints (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            url text NOT NULL CHECK (url ~ '^https?://'),
            secret_enc text NOT NULL,
            events text[] NOT NULL,
            active boolean NOT NULL DEFAULT true,
            consecutive_failures integer NOT NULL DEFAULT 0,
            disabled_reason text,
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute(
        "CREATE INDEX ix_webhook_endpoints_tenant ON webhook_endpoints (tenant_id) WHERE active"
    )
    enable_tenant_rls("webhook_endpoints")

    op.execute("CREATE TYPE delivery_status AS ENUM ('pending', 'delivered', 'dead')")
    op.execute("""
        CREATE TABLE webhook_deliveries (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            endpoint_id uuid NOT NULL REFERENCES webhook_endpoints(id) ON DELETE CASCADE,
            outbox_event_id bigint NOT NULL REFERENCES outbox_events(id) ON DELETE CASCADE,
            event_type text NOT NULL,
            status delivery_status NOT NULL DEFAULT 'pending',
            attempt integer NOT NULL DEFAULT 0,
            status_code integer,
            response_ms integer,
            last_error text,
            next_retry_at timestamptz,
            delivered_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (endpoint_id, outbox_event_id)
        )""")
    op.execute(
        "CREATE INDEX ix_deliveries_due ON webhook_deliveries (next_retry_at) WHERE status = 'pending'"
    )
    op.execute("CREATE INDEX ix_deliveries_tenant_status ON webhook_deliveries (tenant_id, status)")
    enable_tenant_rls("webhook_deliveries")

    op.execute("""
        CREATE TABLE payments (
            id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
            booking_id uuid NOT NULL REFERENCES bookings(id),
            provider text NOT NULL,
            provider_ref text NOT NULL,
            amount_paise bigint NOT NULL,
            status text NOT NULL,
            raw jsonb NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (provider, provider_ref)
        )""")
    enable_tenant_rls("payments")

    op.execute("ALTER TABLE bookings ADD COLUMN reminder_sent_at timestamptz")
    op.execute(
        "CREATE INDEX ix_bookings_reminder_due ON bookings (starts_at) "
        "WHERE reminder_sent_at IS NULL AND status IN ('pending_payment', 'confirmed')"
    )
    grant_sequences()


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_bookings_reminder_due")
    op.execute("ALTER TABLE bookings DROP COLUMN IF EXISTS reminder_sent_at")
    for table in (
        "payments",
        "webhook_deliveries",
        "webhook_endpoints",
        "consumer_inbox",
        "outbox_events",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    op.execute("DROP TYPE IF EXISTS delivery_status")

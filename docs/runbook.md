# Runbook

## Outbox backlog (`OutboxBacklog` alert)
1. `docker compose logs relay --tail 100`: is the relay running? Broker errors?
2. `redis-cli -p 56383 ping`: is the broker up?
3. Backlog size: `SELECT count(*) FROM outbox_events WHERE published_at IS NULL;`
4. The relay drains at full speed once the broker is back. Nothing is lost; rows wait in Postgres.

## Events published but never processed (Redis was lost)
Rows marked `published_at` whose tasks vanished with the broker have no deliveries/inbox rows. Re-drive:
```sql
UPDATE outbox_events e SET published_at = NULL
WHERE published_at > now() - interval '1 hour'
  AND NOT EXISTS (SELECT 1 FROM consumer_inbox i WHERE i.event_id = e.id);
```
Consumers are idempotent, so over-selecting is safe.

## Webhook endpoint disabled by the circuit breaker
`GET /v1/webhook-endpoints` shows `disabled_reason`. Once the tenant fixes the receiver:
`POST /v1/webhook-endpoints/{id}:enable`, then re-queue dead letters with
`POST /v1/webhook-deliveries/{id}:retry`.

## A tenant reports "slot taken" but the calendar looks free
Check for a `pending_payment` hold: those keep the slot for 15 minutes. `expire_unpaid_bookings` releases them.

## Rollback
Re-run the deploy workflow with the previous image tag (`/opt/slotwise/current_tag`). Migrations are
expand-only per release, so the previous image works against the current schema.

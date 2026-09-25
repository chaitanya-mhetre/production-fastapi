# ADR 0002: Postgres row-level security as a second tenant-isolation layer

**Status:** accepted

**Context.** Tenant isolation that relies only on developers remembering `WHERE tenant_id = …` fails the first
time someone forgets.

**Decision.** Two layers. (1) Repositories require a tenant id and always filter. (2) RLS policies on every
tenant table compare `tenant_id` with `current_setting('app.tenant_id')`, set per transaction with
`set_config(…, true)`. The API connects as a role without `BYPASSRLS`.

**Consequences.** The setting must be transaction-local, or it leaks through pooled connections to the next
request, so it's applied in an `after_begin` hook on every transaction. Global tables (users, API keys)
are read before the tenant is known, so they have no RLS and are handled explicitly. Workers use a separate BYPASSRLS role.

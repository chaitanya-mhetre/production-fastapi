# Security

## OWASP API Security Top 10 (2023): where each is handled
| # | Risk | Mitigation in Slotwise | Test |
|---|---|---|---|
| API1 | Broken object-level authorization | every repository filters by tenant; Postgres RLS as a second layer; cross-tenant IDs return 404 | `test_cross_tenant_access_returns_404`, `test_rls_hides_rows_even_without_where_clause` |
| API2 | Broken authentication | Argon2id; constant-time login path (dummy hash for unknown users); pinned HS256; 15-min access tokens; refresh rotation with reuse detection; login throttling | `test_tampered_expired_and_alg_none_tokens_rejected`, `test_reusing_a_rotated_token_revokes_the_whole_family`, `test_login_brute_force_is_throttled` |
| API3 | Broken object property-level authorization | explicit Pydantic input/output schemas; no ORM objects returned directly; secrets never in list responses | `test_api_key_auth_and_scopes` (no `key` in list) |
| API4 | Unrestricted resource consumption | per-tenant/per-key Redis rate limits, plan quotas, Nginx body/time limits, page size caps, 10 s webhook timeouts | `test_api_key_rate_limit_returns_429_with_headers`, `test_monthly_booking_quota` |
| API5 | Broken function-level authorization | RBAC permission table; superadmin-only tenant creation; admin scopes not grantable to API keys | `test_rbac_staff_cannot_write_customers`, `test_admin_scopes_are_not_grantable` |
| API6 | Unrestricted access to sensitive business flows | idempotency on booking POST; unpaid holds expire after 15 min | `test_unpaid_bookings_expire_and_free_the_slot` |
| API7 | Server-side request forgery | outbound webhook URLs resolved and rejected if private/loopback/link-local/reserved; no credentials in URLs; redirects not followed | `test_ssrf_targets_rejected` |
| API8 | Security misconfiguration | non-root container, no server tokens, security headers, `/metrics` allow-listed, secrets from env/SSM only | review |
| API9 | Improper inventory management | versioned `/v1` routes; OpenAPI generated from code | review |
| API10 | Unsafe consumption of APIs | inbound payment webhook: HMAC over raw bytes + timestamp window, idempotent on provider ref, amount cross-checked | `test_payment_webhook_rejects_bad_signatures`, `test_payment_amount_mismatch_does_not_confirm` |

## Secrets
- Passwords: Argon2id. API keys and refresh tokens: SHA-256 (256-bit random secrets, so a fast hash is safe).
- Webhook signing secrets: Fernet-encrypted (they must be recoverable to sign).
- Runtime secrets: environment variables, sourced from AWS SSM Parameter Store in production. `.env.docker`
  holds dev-only values and says so.

## Known gaps (honest list)
1. **DNS rebinding** against the SSRF guard: the hostname is resolved for validation, then resolved again by
   httpx when connecting. Fix: connect to the vetted IP (custom transport) or route egress via a policy proxy.
2. **Refresh-token race:** two legitimate concurrent refreshes with the same token trigger reuse detection
   and log the user out. A short grace window for the immediately previous token is a common mitigation;
   not implemented.
3. **Global tables have no RLS** (users, memberships, API keys, refresh tokens). They're read before the tenant is known;
   access is explicit and reviewed, but a bug there isn't caught by a second layer.
4. **Quota is not atomic** with the insert (can overshoot by one or two at the boundary).
5. **No audit log for reads**, only writes.

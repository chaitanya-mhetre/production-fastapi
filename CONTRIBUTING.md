# Contributing

1. `make up && uv sync && make migrate`
2. Branch from `main`; small commits with conventional messages (`feat:`, `fix:`, `test:`, `docs:`).
3. `make check` must pass: ruff, `mypy --strict`, all tests against real Postgres/Redis.
4. Schema changes: a new hand-written Alembic migration (never edit an applied one). Every new tenant table
   needs `tenant_id NOT NULL` + `enable_tenant_rls()`. Migrations must be expand/contract-safe (see `deploy/aws/README.md`).
5. New behaviour needs a test that fails without the change.
6. Never commit secrets. `.env.docker` is dev-only; real values live in SSM.

.PHONY: check lint type test fmt up down migrate run worker beat relay full-up full-down smoke

COMPOSE := docker compose

check: lint type test

lint:
	uv run ruff check .
	uv run ruff format --check .

type:
	uv run mypy

test:
	uv run pytest -q

fmt:
	uv run ruff format . && uv run ruff check --fix .

up:            ## start postgres, redis, mailpit
	$(COMPOSE) up -d postgres redis mailpit

down:
	$(COMPOSE) down

migrate:
	uv run alembic upgrade head

run:
	uv run uvicorn slotwise.main:app --reload --port 18081

worker:
	uv run celery -A slotwise.worker.celery_app worker -l info

beat:
	uv run celery -A slotwise.worker.celery_app beat -l info

relay:
	uv run python -m slotwise.outbox.relay

full-up:       ## whole system: api, workers, relay, nginx, jaeger, prometheus, grafana
	docker compose --profile full up -d --build

full-down:
	docker compose --profile full down

smoke:         ## end-to-end check against the running full stack (through Nginx)
	docker compose exec -T api python -m slotwise.cli create-superadmin \
		--email root@slotwise.example.com --password smoke-root-password
	SMOKE_BASE_URL=http://localhost:58088 SMOKE_MAILPIT_API=http://localhost:58025/api/v1 \
	SMOKE_SUPERADMIN_EMAIL=root@slotwise.example.com SMOKE_SUPERADMIN_PASSWORD=smoke-root-password \
		uv run python scripts/smoke.py

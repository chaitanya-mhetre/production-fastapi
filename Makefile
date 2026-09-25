.PHONY: check lint type test fmt up down migrate run worker beat relay bootstrap

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

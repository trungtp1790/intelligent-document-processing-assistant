.PHONY: bootstrap up down migrate seed-demo test-unit test-integration test-provider eval-fixture lint typecheck smoke

bootstrap:
	python scripts/bootstrap.py

up:
	docker compose up --build -d

down:
	docker compose down

migrate:
	docker compose exec api alembic upgrade head
	docker compose exec api python -m app.workflows.checkpoint_setup

seed-demo:
	docker compose exec api python -m app.seed

test-unit:
	docker compose exec api python -m pytest -q tests/unit

test-integration:
	docker compose --profile test rm -sf postgres-test
	docker compose --profile test up -d --wait postgres-test
	docker compose run --rm --no-deps -e DATABASE_URL=postgresql+psycopg://idp:idp-test-only@postgres-test:5432/idp_test -e REDIS_URL=redis://redis:6379/15 api alembic upgrade head
	docker compose run --rm --no-deps -e DATABASE_URL=postgresql+psycopg://idp:idp-test-only@postgres-test:5432/idp_test -e REDIS_URL=redis://redis:6379/15 api python -m app.workflows.checkpoint_setup
	docker compose run --rm --no-deps -e DATABASE_URL=postgresql+psycopg://idp:idp-test-only@postgres-test:5432/idp_test -e TEST_DATABASE_URL=postgresql+psycopg://idp:idp-test-only@postgres-test:5432/idp_test -e REDIS_URL=redis://redis:6379/15 -e DOCUMENT_STORAGE_PATH=/tmp/idp-integration-documents api python -m pytest -q tests/integration
	docker compose --profile test rm -sf postgres-test

test-provider:
	docker compose exec api python -m pytest -q tests/provider_contract

eval-fixture:
	docker compose exec api python scripts/evaluate_fixtures.py

lint:
	docker compose exec api ruff check app tests

typecheck:
	docker compose exec api mypy app

smoke:
	python scripts/smoke.py --demo-login


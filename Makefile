.PHONY: db stack test api worker

db:
	docker compose up -d --wait postgres

stack:
	docker compose up -d --build

test:
	docker compose --profile test up -d --wait postgres_test
	TEST_DATABASE_URL=postgresql+psycopg://durable:durable@localhost:5433/durable_runs_test .venv/bin/pytest

api:
	.venv/bin/uvicorn app.main:app --reload

worker:
	.venv/bin/python -m app.worker


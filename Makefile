.PHONY: db test api worker

db:
	docker compose up -d --wait

test:
	.venv/bin/pytest

api:
	.venv/bin/uvicorn app.main:app --reload

worker:
	.venv/bin/python -m app.worker


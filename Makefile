.PHONY: format format-check lint typecheck test pre-commit check db-up db-down db-logs db-status

format:
	uv run ruff format .

format-check:
	uv run ruff format --check .

lint:
	uv run ruff check .

typecheck:
	uv run mypy

test:
	uv run pytest

pre-commit:
	uv run pre-commit run --all-files

check: format-check lint typecheck test

db-up:
	docker compose up --detach --wait postgres

db-down:
	docker compose down

db-logs:
	docker compose logs --follow postgres

db-status:
	docker compose ps postgres

.PHONY: format format-check lint typecheck test pre-commit check db-up db-down db-logs db-status migration-up migration-down migration-current radar radar-refresh

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

migration-up:
	uv run alembic upgrade head

migration-down:
	uv run alembic downgrade -1

migration-current:
	uv run alembic current

radar:
	PYTHONDONTWRITEBYTECODE=1 uv run python -B -m career_os.api.radar_server

radar-refresh:
	@set -a; if [ -f .env.radar.local ]; then . ./.env.radar.local; fi; set +a; \
	PYTHONDONTWRITEBYTECODE=1 uv run career-os collect \
		--config config/sources.market-iteration.yaml \
		--radar-panel-manifest config/radar/panel.mvp.v1.yaml

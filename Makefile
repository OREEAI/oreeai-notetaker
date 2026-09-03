UV ?= uv

.PHONY: setup dev test lint format typecheck makemigrations migrate downgrade docker-up docker-down docker-logs docker-rebuild clean

setup:
	$(UV) sync

dev:
	$(UV) run uvicorn oreeai_nt.main:app --reload --port 8000

test:
	$(UV) run pytest

lint:
	$(UV) run ruff check .
	$(UV) run ruff format --check .

format:
	$(UV) run ruff check --fix .
	$(UV) run ruff format .

typecheck:
	$(UV) run mypy

makemigrations:
	$(UV) run alembic revision --autogenerate -m "$(m)"

migrate:
	$(UV) run alembic upgrade head

downgrade:
	$(UV) run alembic downgrade -1

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f api

docker-rebuild:
	docker compose build --no-cache api

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache dist build *.egg-info

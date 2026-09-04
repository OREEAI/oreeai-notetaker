UV ?= uv

.PHONY: setup dev test lint format typecheck makemigrations migrate downgrade docker-up docker-down docker-logs docker-rebuild bot-build bot-run clean

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

bot-build:
	docker build -t oreeai-bot:local -f bot/Dockerfile .

bot-run:
	mkdir -p bot/audio bot/debug && chmod 777 bot/audio bot/debug
	docker run --rm --init --shm-size=1g --name oreeai-bot-spike \
		-e MEETING_URL -e BOT_NAME -e CONSENT_ACK -e CALL_ID -e LOG_LEVEL -e DEBUG_DIR=/debug \
		-v $(CURDIR)/bot/audio:/audio \
		-v $(CURDIR)/bot/debug:/debug \
		oreeai-bot:local

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache dist build *.egg-info

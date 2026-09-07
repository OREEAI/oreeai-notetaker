UV ?= uv

.PHONY: setup dev test lint format typecheck makemigrations migrate downgrade docker-up docker-down docker-logs docker-rebuild bot-build bot-run bot-probe clean

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
	docker build --build-arg GIT_SHA=$(GIT_SHA) -t oreeai-bot:local -f bot/Dockerfile .

# Image is stamped with the building commit; bot-run rebuilds first (cached:
# seconds when unchanged) so a gate log always identifies the code under test.
# The -dirty suffix marks images built from an uncommitted working tree.
GIT_SHA ?= $(shell sha=$$(git rev-parse --short HEAD 2>/dev/null || echo unknown); test -n "$$(git status --porcelain 2>/dev/null)" && sha=$${sha}-dirty; echo $$sha)

bot-run: bot-build
	mkdir -p bot/audio bot/debug $(BOT_PROFILE) && chmod 777 bot/audio bot/debug && chmod 700 $(BOT_PROFILE)
	docker run --rm --init --shm-size=1g --name oreeai-bot-spike \
		$(GPU_FLAGS) \
		-e MEETING_URL -e BOT_NAME -e CONSENT_ACK -e CALL_ID -e LOG_LEVEL -e TZ=$(TZ) -e DEBUG_DIR=/debug \
		-e BOT_WAITING_ROOM_TIMEOUT -e BOT_EMPTY_ROOM_TIMEOUT -e BOT_ALONE_GRACE -e BOT_MAX_RECORD_DURATION \
		-e BOT_SILENCE_RMS_FLOOR -e PAREC_DEVICE -e BOT_AUTH_MODE -e BOT_PROFILE_DIR \
		-v $(CURDIR)/bot/audio:/audio \
		-v $(CURDIR)/bot/debug:/debug \
		-v $(BOT_PROFILE):/profile \
		oreeai-bot:local

# Persistent Chrome profile backing authenticated joins. Mounted at /profile
# inside the container; holds the Google session (credentials), so it is
# gitignored and never baked into the image. Override per host, e.g.
# BOT_PROFILE=/var/lib/oreeai/chrome-profile.
BOT_PROFILE ?= $(CURDIR)/bot/chrome-profile

# One-time interactive sign-in for the persistent profile. noVNC is published
# on 127.0.0.1:7900 only; open http://127.0.0.1:7900 in a browser and sign in
# with the dedicated bot account.
bot-login: bot-build
	mkdir -p $(BOT_PROFILE) && chmod 700 $(BOT_PROFILE)
	docker run --rm --init --shm-size=1g --name oreeai-bot-login \
		$(GPU_FLAGS) \
		-p 127.0.0.1:7900:7900 \
		-e BOT_ENTRY_MODE=login -e LOG_LEVEL -e TZ=$(TZ) -e BOT_LOGIN_TIMEOUT -e BOT_PROFILE_DIR \
		-v $(BOT_PROFILE):/profile \
		oreeai-bot:local

# Real-GPU passthrough for the WebGL fingerprint; no-op on hosts without /dev/dri.
GPU_FLAGS := $(shell test -e /dev/dri && echo "--device /dev/dri --group-add 44 --group-add 104")
# Container timezone (Intl API is fingerprint-visible); override per host, e.g. TZ=UTC.
TZ ?= Africa/Lagos

bot-probe: bot-build
	docker run --rm --shm-size=1g $(GPU_FLAGS) -e TZ=$(TZ) --entrypoint bash oreeai-bot:local -c 'Xvfb :99 -screen 0 2400x1350x24 -nolisten tcp & sleep 1; export XDG_RUNTIME_DIR=/tmp/runtime-$$(id -u); mkdir -p $$XDG_RUNTIME_DIR; pulseaudio --start --exit-idle-time=-1 --disable-shm; pactl load-module module-null-sink sink_name=virtual_speaker >/dev/null; pactl load-module module-remap-source master=virtual_speaker.monitor source_name=virtual_mic >/dev/null; DISPLAY=:99 python -m bot.probe_browser'

clean:
	rm -rf .venv .pytest_cache .mypy_cache .ruff_cache dist build *.egg-info

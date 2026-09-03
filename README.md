# oreeai-notetaker

AI note taker for Google Meet and Zoom meetings. FastAPI + Postgres + Redis, managed with `uv`.

## Quickstart

```bash
# 1. Install dependencies (requires uv: https://docs.astral.sh/uv/)
make setup

# 2. Start Postgres + Redis
docker compose up -d db redis

# 3. Configure
cp .env.example .env

# 4. Run migrations
make migrate

# 5. Run the API
make dev
```

Interactive docs: http://localhost:8000/docs

## Full stack via Docker

```bash
make docker-up      # postgres + redis + api (hot reload)
make docker-logs
make docker-down
```

## Common commands

| Command | Purpose |
| --- | --- |
| `make dev` | dev server with hot reload |
| `make test` | run tests (SQLite, no services needed) |
| `make lint` / `make format` | ruff check / autofix |
| `make typecheck` | mypy strict |
| `make makemigrations m="..."` | create alembic migration |
| `make migrate` | apply migrations |

See [AGENTS.md](AGENTS.md) for architecture, conventions, and how to add features.

## Layout

```
src/oreeai_nt/
├── api/            # routers + dependency injection (views)
├── services/       # business logic
├── repositories/   # data access
├── models/         # SQLAlchemy models
├── schemas/        # Pydantic DTOs
├── enums/          # shared StrEnum types (per domain)
├── core/           # config, cache, exceptions, logging
├── db/             # engine, session, base/mixins
├── integrations/   # Google Meet / Zoom clients
└── workers/        # background job hooks
plans/              # local planning docs (gitignored)
```

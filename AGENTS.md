# AGENTS.md

Guidance for AI agents (and humans) working in this repository.

## Project

AI note taker for Google Meet and Zoom meetings. FastAPI + Postgres + Redis, managed with `uv`.

- **Python**: 3.13 (pinned in `pyproject.toml`)
- **Package manager**: `uv` — never use pip directly; always commit `uv.lock` changes
- **Source layout**: `src/oreeai_nt/` (installed as editable package by `uv sync`)

## Commands

```bash
make setup            # install/sync dependencies
make dev              # run dev server with hot reload (localhost:8000)
make test             # pytest (uses in-memory SQLite, no Postgres needed)
make lint             # ruff check + format check
make format           # ruff autofix + format
make typecheck        # mypy (strict, src/ only)
make makemigrations m="add x"   # autogenerate alembic migration (needs DB running)
make migrate          # alembic upgrade head
make docker-up        # postgres + redis + api via docker compose
make docker-down
make docker-logs
```

Infrastructure (Postgres, Redis) for local dev: `make docker-up`, or run only `docker compose up -d db redis`.

## Architecture (strict layering)

```
api/  ->  services/  ->  repositories/  ->  models (SQLAlchemy)
```

- **`api/` (views)**: HTTP concerns only. Routers, request/response models, status codes. Never imports SQLAlchemy models or touches the session for queries. Wires dependencies via `api/deps.py`.
- **`services/`**: All business logic, validation rules, cache orchestration, cross-repo coordination. Returns Pydantic schemas (`schemas/`), receives repositories via constructor. This is where feature behavior lives.
- **`repositories/`**: Data access only. Generic `BaseRepository` provides get/list/create/update/delete; subclasses add entity-specific queries. Returns ORM models. Never contains business rules.
- **`models/`**: SQLAlchemy ORM models. Mixins in `db/base.py` (`UUIDPrimaryKeyMixin`, `TimestampMixin`) — reuse them for new tables.
- **`schemas/`**: Pydantic DTOs. `*Create`, `*Update` (all-optional patch semantics via `model_dump(exclude_unset=True)`), `*Read` (with `from_attributes`).
- **`core/`**: Cross-cutting: `config.py` (pydantic-settings; add new env vars here), `cache.py` (`CacheService`, Redis-backed, degrades gracefully to no-op when Redis is down), `exceptions.py` (`AppError` subclasses are mapped to HTTP responses automatically in `main.py`).
- **`integrations/`**: External platform clients (Google Meet, Zoom). Implement the `MeetingPlatformClient` protocol in `integrations/base.py`. Adapters only — no business logic here.
- **`workers/`**: Background job hooks (meeting bots, transcription, summarization). Currently process-local placeholders; swap call sites to a queue (Celery/ARQ) later without touching services.

**Transaction policy**: repositories flush but never commit. `get_db` in `api/deps.py` commits on request success and rolls back on any exception. Services can therefore compose multiple repo calls atomically.

**Dependency injection**: everything is wired through `Annotated[..., Depends(...)]` aliases in `api/deps.py` (`SessionDep`, `CacheDep`, `MeetingServiceDep`, ...). In tests, override the dependency with `app.dependency_overrides[...]`.

## Adding a new feature (e.g. `transcripts`)

1. Model in `models/transcript.py`, export it in `models/__init__.py`
2. Schemas in `schemas/transcript.py`
3. Repository: subclass `BaseRepository[Transcript]` in `repositories/transcript.py`
4. Service in `services/transcript.py` — inject repository + cache, raise `NotFoundError`/`ConflictError` for expected failures
5. Router in `api/v1/transcripts.py`, register it in `api/v1/router.py`, add a `TranscriptServiceDep` in `api/deps.py`
6. Migration: `make makemigrations m="add transcripts"` (requires DB up)
7. Tests mirroring the layout: `tests/api/test_transcripts.py`, `tests/services/test_transcript_service.py`

Use the existing Meetings feature (`models/meeting.py` → `api/v1/meetings.py`) as the reference pattern.

## Conventions

- mypy **strict** passes on `src/` — keep it that way; annotate everything
- ruff is the formatter and linter (line length 100); run `make format` before committing
- Tests run on in-memory SQLite via fixtures in `tests/conftest.py`; no external services required
- Config comes from environment variables (see `.env.example`); never hardcode credentials; never commit `.env`
- Logging via stdlib `logging` (`setup_logging` in `core/logging.py`); no print statements

## Git workflow

- **Feature branching with squash & merge**: branch off `main` as `feat/<name>`, `fix/<name>`, or `chore/<name>`; PRs are squash-merged to `main`, so keep branch history noisy but write a clean squash commit message
- CI (`.github/workflows/ci.yml`) must pass: ruff, mypy, pytest

## Plans folder

`plans/` is gitignored and holds working design documents (`*.md`, `*.pdf`, `*.txt`). Put architecture plans, specs, and notes there — never commit them.

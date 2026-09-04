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
make typecheck        # mypy strict; src/ today, src/ + bot/ once PR 1 lands
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
- **`enums/`**: Shared `StrEnum` types (e.g. `MeetingPlatform`, `MeetingStatus`) referenced across models, schemas, services, and integrations. Never define an enum inside `models/` if anything outside the model layer needs it — put it here, one file per domain (`enums/meeting.py`), re-exported in `enums/__init__.py`.
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

- mypy **strict** passes on `src/` (and on `bot/` once PR 1 lands — PR 1 adds `bot` to `tool.mypy.files`) — keep it that way; annotate everything
- ruff is the formatter and linter (line length 100); run `make format` before committing
- Tests run on in-memory SQLite via fixtures in `tests/conftest.py`; no external services required
- Config comes from environment variables (see `.env.example`); never hardcode credentials; never commit `.env`
- Logging via stdlib `logging` (`setup_logging` in `core/logging.py`); no print statements

## Git workflow

- **Feature branching with squash & merge**: branch off `main` as `feat/<name>`, `fix/<name>`, or `chore/<name>`; PRs are squash-merged to `main`, so keep branch history noisy but write a clean squash commit message
- CI must pass before merge: **Lint** (`.github/workflows/lint.yml` — ruff, mypy) and **CI** (`.github/workflows/ci.yml` — pytest)

## Plans folder

`plans/` is gitignored and holds working design documents (`*.md`, `*.pdf`, `*.txt`). Put architecture plans, specs, and notes there — never commit them.

## Work ledger

`plans/note-taker-prs.md` is the authoritative work queue for phase 1 (call note taker). **Read its Status Ledger before picking up work**; update the ledger and the relevant chunk's Handoff Notes whenever a chunk starts, finishes, or gets blocked. The doc also defines the shared contracts (exit codes, status machine, webhook spec, env-var inventory) that all call-service work cross-references — do not redefine them in a PR.

## Bot (`bot/`)

- `bot/` is a **separate deployable**, not part of the `oreeai_nt` Python package. It must never import `src/oreeai_nt`, and the service must never import `bot/`. The only contract between them is the container boundary and the bot's exit-code table (see `bot/README.md` and the Shared contracts in `plans/note-taker-prs.md`).
- Bot code uses the **sync Playwright API** (it's a standalone process; async buys nothing here).
- All Meet DOM selectors live in `bot/selectors.py` (aria-label/role based, `en-US` locale forced). A Meet UI change should be a one-file fix, not a hunt through call sites.
- Bot scripts are held to the same ruff + mypy standards as `src/` (PR 1 extends `tool.mypy.files` to include `bot/`).

## Standing rules

- **Never log audio bytes or recording paths paired with `user_ref`.** Encryption at rest lands in PR 6; until then treat any local audio file as sensitive.
- **`user_ref` is an opaque string:** never parsed, enriched, foreign-keyed, or joined across systems. This service is a strict emitter; OreeAI's database is never written to.
- **The transcription provider must support both batch and realtime on one account** (Deepgram or AssemblyAI). **Never Whisper** — it streams poorly and the phase 3 live-trainer needs realtime.
- **Production compose never publishes ports to `0.0.0.0`** — loopback or internal network only. Lesson from OreeAI PR #48. (The dev `docker-compose.yml` is exempt; `docker-compose.prod.yml` is not.)
- **All `/api/v1` routes require `X-API-Key`** matching the `API_KEY` env var, including `/health`. Keep this even though the API runs on a loopback/internal network in phase 1; auth-uniform is easier to reason about than exemptions. Webhook receivers verify HMAC separately — that is unrelated to API auth.
- **Bot-runner, not the API process, owns the docker socket.** The standalone `bot-runner` service is the only thing that spawns bot containers; the API process never gets the docker socket. See the orchestration architecture diagram in `plans/note-taker-prs.md` under Shared contracts.

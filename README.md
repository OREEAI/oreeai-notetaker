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

## Data retention

The product is the transcript; the audio is not. This section is the
written policy — an outsider should be able to read it and know exactly

what happens to a meeting recording.

**What is stored, where.** Each call produces one audio file (a 16 kHz
mono WAV, ~115 MB/hour) and one transcript. The transcript lives in the
service's own database and is kept. The audio is uploaded to a private
S3-compatible bucket (`S3_BUCKET`) — the same code works with AWS S3,
Cloudflare R2, Hetzner Object Storage, Backblaze B2, or MinIO; switching
provider is an environment-variable change, not a code change. The bot
writes the WAV to local scratch space first (`AUDIO_HOST_PATH`); once
the upload lands, the local file is deleted.

**Where the audio lives (object key layout):**

```
s3://<bucket>/calls/<call_id>/audio.wav
```

A sample listing:

```bash
aws s3 ls s3://<bucket>/calls/
# PRE 018f3c2e-0f3f-4c40-9f3f-3ee69c1d0b1a/
aws s3 ls s3://<bucket>/calls/018f3c2e-0f3f-4c40-9f3f-3ee69c1d0b1a/
# 2026-09-15 10:14:02  1351680 audio.wav
```

**Encryption at rest.** Every upload requests server-side encryption
(`S3_SSE`, default `AES256` — S3-managed keys). Upgrading to
`aws:kms` is possible by setting `S3_SSE=aws:kms` and adding a KMS key
(see the checklist below). You can verify what a stored object actually
got with:

```bash
aws s3api head-object --bucket <bucket> --key calls/<call_id>/audio.wav
# → "ServerSideEncryption": "AES256"
```

**Serving.** Audio is only ever served through presigned, time-limited
GET URLs (`S3_PRESIGN_TTL_SECONDS`, default 3600) generated against the
configured endpoint. There are no public links, and the bucket must
stay private. A webhook consumer that wants the audio fetches it through
a presigned URL within the TTL.

**How long audio is kept.**

| Call outcome | Audio kept for | Configured by |
| --- | --- | --- |
| `done` (meeting transcribed) | `AUDIO_RETENTION_DAYS` after done — default **0: deleted immediately** | `AUDIO_RETENTION_DAYS` |
| `failed` (operational failure) | `FAILED_AUDIO_RETENTION_DAYS` after the failure — default 7 days | `FAILED_AUDIO_RETENTION_DAYS` |

`AUDIO_RETENTION_DAYS=0` means the transcript is the product and the
raw audio is a scratch artifact: the moment a call is done, the stored
object is deleted and the call's `audio_url` is cleared. The webhook
payload carries the object URI (`s3://...`) as a *snapshot* taken at
`done` — with immediate retention the object will already be gone if a
receiver tries to fetch it later (fetch promptly, or treat the audio as
not part of your contract), which is intended. Transcripts are retained
regardless of audio retention; how long transcripts are kept is a
separate product decision, not set by this section.

**Enforcement.** The retention worker (inside the bot-runner process
for phase 1) runs every 60 seconds and once immediately after each
`done`: it deletes expired objects and nulls `audio_url`, leaving
transcripts untouched. A provider-side bucket lifecycle rule is set up
as **defense-in-depth** (so objects disappear even if the worker is
down) — the worker remains the policy owner because it also clears
`audio_url` in the database.

**Nothing about audio in logs.** The service never logs audio bytes,
recording paths, or object keys in a way that could be correlated with
a user reference, and never logs webhook secrets. Local scratch WAVs
live only on the host volume, are deleted right after a successful
upload, and are never shipped anywhere.

### Bucket-setup checklist

One-time setup per provider. The bucket must be **private**, must
enforce **server-side encryption**, and should carry **lifecycle
rules** with the same periods as the app configuration (minimum
granularity is 1 day on most providers; with `AUDIO_RETENTION_DAYS=0`
the app-side sweep is the sole enforcement and a 1-day rule is the
closest fallback). Also add an **incomplete-multipart-upload abort
rule** — a hard crash mid-multipart would otherwise strand orphaned
parts provider-side, which normal deletes cannot clean up.

AWS-style CLI (works for AWS; Hetzner exposes the same API):

```bash
aws s3api create-bucket --bucket <bucket> --region <region> \
  --create-bucket-configuration LocationConstraint=<region>
aws s3api put-public-access-block --bucket <bucket> \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
aws s3api put-bucket-encryption --bucket <bucket> \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
# Lifecycle: expire objects at AUDIO_RETENTION_DAYS (here: example 7)
# and abort incomplete multipart uploads after 7 days.
aws s3api put-bucket-lifecycle-configuration --bucket <bucket> \
  --lifecycle-configuration '{"Rules":[{"ID":"expire-audio","Status":"Enabled","Filter":{"Prefix":"calls/"},"Expiration":{"Days":7}},{"ID":"abort-multipart","Status":"Enabled","Filter":{"Prefix":"calls/"},"AbortIncompleteMultipartUpload":{"DaysAfterInitiation":7}}]}'
```

MinIO (`mc`):

```bash
mc alias set oreeai https://<minio-host>:9000 <ACCESS_KEY> <SECRET_KEY>
mc mb oreeai/<bucket>
mc anonymous set none oreeai/<bucket>                     # private
mc encrypt set sse-s3 oreeai/<bucket>                     # requires a KMS (KES or MINIO_KMS_AUTO_KMS=1)
mc ilm rule add oreeai/<bucket> --prefix "calls/" --expire-days 7
mc ilm rule add oreeai/<bucket> --expire-abort-incomplete-mupload-days 7   # MinIO's flag name — not a typo
```

Cloudflare R2 (no egress fees; lifecycle via dashboard or `wrangler`,
S3 API for everything else) and Backblaze B2 (lifecycle via their
console/API) follow the same four rules: private, encrypted, expire
`calls/` objects at `AUDIO_RETENTION_DAYS`, abort incomplete multipart
uploads.

Note for KMS: the stronger `aws:kms` option additionally requires a
KMS key at the provider and one more setting (added when that option
is actually used) — for phase 1, `AES256` is enough.

**Deletion requests.** Explicit per-call deletion (GDPR-style erasure
beyond the retention policy) is not implemented yet — it needs its own
endpoint and will be a follow-up. The retention policy above is the
baseline.

## Transcription

Recordings are transcribed by **Deepgram** (`nova-3`, pre-recorded
batch) with speaker diarization (`diarize_model=latest`); the segments
land in `Call.transcript` (JSONB) and in the webhook payload. Provider
choice, request shape, error mapping, and the pending realtime
confirmation are documented in
[docs/transcription.md](docs/transcription.md). Locally (no
credentials), `TRANSCRIPTION_PROVIDER` unset falls back to a stub that
lands every call `done` with an empty transcript — production
fail-fasts without a real provider.

## Layout

```
src/oreeai_notetaker/
├── api/            # routers + dependency injection (views)
├── services/       # business logic
├── repositories/   # data access
├── models/         # SQLAlchemy models
├── schemas/        # Pydantic DTOs
├── enums/          # shared StrEnum types (per domain)
├── core/           # config, cache, exceptions, logging
├── db/             # engine, session, base/mixins
├── integrations/   # external platform clients (Google Meet; object storage)
└── workers/        # background job hooks
plans/              # local planning docs (gitignored)
```

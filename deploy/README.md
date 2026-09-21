# Deploy — the note taker on the VPS

The production stack is **one `docker compose`**: `api`, `db`, `redis`,
`bot-runner` (plus a build-only `bot` service behind the `build` profile).
It runs **standalone**: this service does not deploy alongside or share
state with OreeAI's stack. It may sit on the *same host* — coexistence is
intentional and covered by the pre-flight and verification steps below —
but it has its own compose project, its own volume, its own network, and
its own database.

Standing rules the stack enforces:

| Rule | Where |
| --- | --- |
| No port is published on any host interface beyond loopback (`api` is `127.0.0.1` only; `db`/`redis` unpublished) | `docker-compose.prod.yml`, locked by `tests/deploy/test_prod_compose_docker.py` |
| The docker socket is mounted on `bot-runner` **only** | same file; the api process never gets it |
| Every service has a healthcheck, `restart: unless-stopped`, and 10m × 5 json-file log caps | same file |
| Audio is served only through presigned time-limited URLs | `README.md` → Data retention |

## Which image carries the runner

The `bot-runner` service runs **the same image as `api`** (`oreeai-api:<API_IMAGE_TAG>`),
with its command overridden to
`python -m oreeai_notetaker.workers.bot_runner`. Reasons:

- the runtime image already ships the docker CLI (to drive the mounted
  socket), the redis client (heartbeats), alembic, and the whole service
  package — no second Python environment to build or keep in sync;
- one `up --build` builds one image and tags it once; api and runner
  can never drift across releases.

The **bot** image is separate on purpose (`bot/Dockerfile`, Chromium +
Playwright), built by the profile-gated `bot` service:

```bash
docker compose -f docker-compose.prod.yml --profile build build bot
```

## Prerequisites

- A VPS (Debian/Ubuntu) where OreeAI's stack already runs, with root or
  sudo access.
- Docker Engine + the compose plugin.
- `git`.
- No domain or TLS is needed: the API is internal-only in phase 1 (reach
  it over SSH; see [If you ever need to expose it](#if-you-ever-need-to-expose-it)).

## 0. Pre-deploy: swap

Run [../ops/swap.md](../ops/swap.md) once **before** the first deploy. The
host is memory-committed under a full bot load; swap makes the kernel's
OOM-killer degrade gracefully and prefer a capped bot container over
OreeAI's Postgres/Redis.

## 1. Coexistence pre-flight (OreeAI shares this host)

```bash
ss -tlnp | grep -E ':(8000|5432|6379)\b' || true   # is 8000 taken on loopback?
docker network ls | grep oreeai_internal || true   # must be ours or absent
docker volume ls | grep oreeai_pgdata || true      # must be ours or absent
free -h
```

- Our `db` and `redis` publish no host ports, so 5432/6379 conflicts are
  impossible; only `api`'s loopback port can collide. If 8000 is taken,
  set `API_HOST_PORT` in `.env` to a free loopback port.
- `oreeai_internal` and `oreeai_pgdata` must not already belong to
  another stack. If they do, stop and sort the ownership out before
  `up` — the runner spawns bots onto exactly `oreeai_internal`.

## 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # log out and back in
docker compose version
```

## 3. Clone and check out the release tag

```bash
sudo mkdir -p /opt/oreeai && sudo chown "$USER" /opt/oreeai
git clone <repo-url> /opt/oreeai
cd /opt/oreeai
git checkout v0.1.0
```

## 4. Configure `.env`

```bash
cd /opt/oreeai
cp deploy/env.example .env
chmod 600 .env
```

`deploy/env.example` is a symlink to the root `.env.example`; every
variable the stack reads is in it. Fill at least:

| Variable | Value |
| --- | --- |
| `ENVIRONMENT` | `production` (never `local` on this host) |
| `POSTGRES_PASSWORD` | `openssl rand -hex 24` |
| `API_KEY` | `openssl rand -hex 32` |
| `API_IMAGE_TAG` / `BOT_IMAGE_TAG` | `v0.1.0` (the release tag) |
| `API_HOST_PORT` | `8000` unless step 1 found it taken |
| `DOCKER_GID` | `stat -c '%g' /var/run/docker.sock` |
| `S3_ENDPOINT_URL` / `S3_REGION` / `S3_BUCKET` / `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | bucket credentials (the runner refuses to start without them in production) |
| `TRANSCRIPTION_PROVIDER` | `deepgram` (the runner refuses `stub`/unset in production) |
| `DEEPGRAM_API_KEY` | the provider key |
| `AUDIO_HOST_PATH` | `/var/lib/oreeai/audio` (host path == container path) |
| `BOT_PROFILE` | `/var/lib/oreeai/chrome-profile` |
| `BACKUP_KEEP_DAYS` | `30` unless a different retention is wanted |

Notes:

- The example is shared with dev: `POSTGRES_HOST_PORT`, `REDIS_HOST_PORT`,
  and the `localhost` `DATABASE_URL`/`REDIS_URL` lines are dev-only and
  ignored by the prod compose (it builds in-network URLs from
  `POSTGRES_*`).
- Keep the defaults for `AUDIO_HOST_PATH`/`BOT_PROFILE` unless the host
  paths genuinely differ; the runner passes `AUDIO_HOST_PATH` to spawned
  bot mounts, so host and container views must agree.
- The compose `environment:` block is authoritative on this host; `.env`
  is interpolation input. Change `.env`, then recreate:
  `docker compose -f docker-compose.prod.yml up -d`.

## 5. Host directories

```bash
sudo mkdir -p /var/lib/oreeai/audio /var/lib/oreeai/backups /var/lib/oreeai/chrome-profile
```

The containers run as the non-root `app` user. After the first build
(step 6), learn its uid and give it ownership of the audio dir it writes:

```bash
docker compose -f docker-compose.prod.yml run --rm --entrypoint id api
# uid=999(app) gid=999(app) ...
sudo chown -R 999:999 /var/lib/oreeai/audio
sudo chmod 700 /var/lib/oreeai/chrome-profile
```

`/var/lib/oreeai/backups` stays root-owned: host-side tooling (root cron
running `deploy/backup.sh`) writes it, and the db container only reads it.
The runner copies the Chrome profile into the audio volume per call; the
profile directory itself is root-only (it holds a Google session — treat
it like a password).

## 6. Build the images

```bash
docker compose -f docker-compose.prod.yml --profile build build bot
docker compose -f docker-compose.prod.yml build api
```

## 7. Start the stack

```bash
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml ps
```

Expect four running services, all `healthy` (give `api` up to ~30 s and
`bot-runner` up to ~90 s: the runner fail-fasts its storage/transcription
config, sweeps orphans, then writes its first heartbeat).

## 8. Migrate

```bash
docker compose -f docker-compose.prod.yml exec api alembic upgrade head
```

Fresh deployments run the full migration chain (including the `calls`
table and the `transcript` JSONB column). **Never hand-patch the schema on
the host** — add a migration and ship a release.

## 9. Smoke test

```bash
deploy/smoke.sh
```

Asserts `GET /api/v1/health` returns 200 with the `X-API-Key` header and
reports `database`, `cache`, and `runner` all `up`. Non-zero exit on any
failure. `ENV_FILE`/`BASE_URL` override the defaults.

## 10. Verify the runner heartbeat

```bash
docker compose -f docker-compose.prod.yml exec redis redis-cli exists oreeai:runner:heartbeat
# → 1
```

The runner writes this key every 10 s; the API's health endpoint reports
`runner: down` within a minute of the heartbeat disappearing.

## 11. Verify OreeAI is untouched

```bash
docker ps --format 'table {{.Names}}\t{{.Status}}'
docker compose -f docker-compose.prod.yml restart
docker ps --format 'table {{.Names}}\t{{.Status}}'   # OreeAI names/status unchanged
```

Also run this service's first real call (PR 4's deferred host-level test):
under a bot OOM-kill, OreeAI's containers must keep running and its
Postgres must stay up.

## 12. Backups (nightly, 03:00 host time)

Install the cron line on the host:

```bash
sudo mkdir -p /var/log/oreeai
crontab -e    # paste the line from /opt/oreeai/deploy/backup.cron
crontab -l    # verify
```

Prove it now, not at 03:00:

```bash
/opt/oreeai/deploy/backup.sh
ls -lh /var/lib/oreeai/backups/
```

Rehearse a restore against a **throwaway** database (the default target;
the live database is never touched):

```bash
/opt/oreeai/deploy/restore.sh /var/lib/oreeai/backups/oreeai-<timestamp>.sql.gz
```

A deliberate product restore (real disaster recovery) is explicit and
stops/re-starts the services around the restore:

```bash
/opt/oreeai/deploy/restore.sh /var/lib/oreeai/backups/<file> --target-prod --yes
```

Operations:

- Schedule and retention: `BACKUP_CRON` (documented in `backup.cron`) and
  `BACKUP_KEEP_DAYS` (default 30; the newest dump is never deleted). Cron
  fires on the **host clock**; keep the VPS on UTC so 03:00 means 03:00 UTC.
- Disk check: `du -sh /var/lib/oreeai/backups /var/lib/oreeai/audio`.
- Cron survives reboot with the OS cron service; after a reboot verify
  with `crontab -l` and `systemctl status cron`.
- Avoid deploys across 03:00 (host time); `pg_dump` does not lock writes,
  but a quiet box makes a clean dump easier to reason about.

## Tagging and roll-forward

Releases are git tags (`v0.1.0`, `v0.1.1`, …); the first deploy is
`v0.1.0`. Create the tag on the merged commit and push it:

```bash
git checkout main && git pull
git tag -a v0.1.0 -m "first production deploy"
git push origin v0.1.0
```

Both image tags come from `.env`:

```bash
git fetch --tags
git checkout v0.1.1
# .env: API_IMAGE_TAG=v0.1.1  BOT_IMAGE_TAG=v0.1.1
docker compose -f docker-compose.prod.yml --profile build build bot
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec api alembic upgrade head
deploy/smoke.sh
```

A registry (GHCR / Docker Hub) is the natural next step when the deploy
grows past one host: replace the `build:` blocks with `image:` pulls from
the registry under the same tags. Not needed for phase 1.

**Rolling back** is a documented, repeatable action — see
[rollback.md](rollback.md).

## The docker socket is mounted on purpose

`bot-runner` mounts `/var/run/docker.sock` because it is the only thing
that spawns bot containers. This makes it the most privileged container
in the stack — deliberately: the API process never gets the socket, so
if the API is compromised the attacker still does not get the host's
docker control plane. Do not "fix" this by mounting the socket on `api`
or by moving spawning into the API process.

## If you ever need to expose it

Phase 1 is internal-only: nothing outside the VPS needs to call this API,
and OreeAI (the caller) runs on the same host. If that ever changes, the
Traefik addition is a small, reviewed patch — this compose deliberately
does not ship it, and the CI guard that forbids wildcard bindings would
need an explicit exception for Traefik's 80/443. The shape:

```yaml
  traefik:
    image: traefik:v3
    restart: unless-stopped
    command:
      - --providers.docker=true
      - --entrypoints.web.address=:80
      - --entrypoints.websecure.address=:443
      - --certificatesresolvers.le.acme.tlschallenge=true
      - --certificatesresolvers.le.acme.email=<email>
      - --certificatesresolvers.le.acme.storage=/letsencrypt/acme.json
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - traefik_letsencrypt:/letsencrypt
    networks: [oreeai_internal]

  api:
    labels:
      - traefik.enable=true
      - traefik.http.routers.oreeai.rule=Host(`<domain>`)
      - traefik.http.routers.oreeai.entrypoints=websecure
      - traefik.http.routers.oreeai.tls.certresolver=le
      - traefik.http.services.oreeai.loadbalancer.server.port=8000
```

The API keeps its `X-API-Key` auth; TLS is not a substitute. Decide with
the team before adding a public surface.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| `api` unhealthy, runner `down` | `docker compose -f docker-compose.prod.yml logs bot-runner` — a fail-fast storage/transcription error is logged at startup |
| bot spawns die with exit 125 | `oreeai_internal` missing or renamed: `docker network ls` |
| bot containers exit immediately | `BOT_PROFILE`/profile sign-in (below) and `CONSENT_ACK` |
| runner can't reach the socket | `DOCKER_GID` does not match `stat -c '%g' /var/run/docker.sock` |
| runner can't write call WAVs | host `/var/lib/oreeai/audio` not owned by the container's `app` uid (step 5) |
| smoke fails on `cache` | `redis` container down, or `CACHE_ENABLED=false` |

Authenticated joins need a one-time sign-in on the VPS (the profile is
host state, not an image artifact):

```bash
# From your laptop: tunnel the noVNC port
ssh -L 7900:127.0.0.1:7900 <user>@<vps>
# On the VPS: publishes 127.0.0.1:7900 only
cd /opt/oreeai && make bot-login
# open http://127.0.0.1:7900 and sign in with the dedicated bot account
```

`make bot-login` builds an `oreeai-bot:local` image for the sign-in; the
production bot image stays `oreeai-bot:${BOT_IMAGE_TAG}`.

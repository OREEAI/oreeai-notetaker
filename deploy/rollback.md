# Rollback — the note-taker stack

A rollback is a **documented, repeatable action**: check out the previous
release tag, rebuild both images under that tag, bring the stack up,
re-run migrations **only if the release's notes say so**, and smoke test.
Never edit running containers or hand-patch the database.

## Preconditions

- Know the last good release tag (`git tag --sort=-v:refname | head`).
- Take a backup first, always:

  ```bash
  /opt/oreeai/deploy/backup.sh
  ```

- Read the release notes of the release you are rolling back: did it add
  a migration? Migrations are forward-only by default. An **additive**
  migration is compatible with the older code (keep the schema, roll the
  code back). A destructive/rewriting migration needs its documented
  `alembic downgrade <revision>` or a restore from the backup.

## Procedure

```bash
cd /opt/oreeai
git fetch --tags
git checkout <previous-good-tag>

# .env — point both image tags at the previous release:
#   API_IMAGE_TAG=<previous-good-tag>
#   BOT_IMAGE_TAG=<previous-good-tag>

docker compose -f docker-compose.prod.yml --profile build build bot
docker compose -f docker-compose.prod.yml up -d --build
deploy/smoke.sh
```

If (and only if) the release notes say the migration must be reverted:

```bash
docker compose -f docker-compose.prod.yml exec api alembic downgrade <revision>
```

Then verify a call end-to-end (`POST /api/v1/calls`, watch the statuses
and the webhook) before declaring the rollback done.

## Worked example — `v0.1.0` → broken `v0.1.1` → back to `v0.1.0`

The stack runs `v0.1.0` and a call completes. `v0.1.1` ships a deliberate
bug (say, a bad SQL query in the status transition). Observed:

```bash
deploy/smoke.sh
# smoke: FAIL — http://127.0.0.1:8000/api/v1/health returned HTTP 503
docker compose -f docker-compose.prod.yml logs --tail 50 api
# ... ERROR ... mark_recording failed: column "recording_started_at" does not exist
```

Roll back:

```bash
cd /opt/oreeai
/opt/oreeai/deploy/backup.sh
# backup: OK — /var/lib/oreeai/backups/oreeai-20260921T041500Z.sql.gz (214K); retention 30 days

git fetch --tags
git checkout v0.1.0

sed -i 's/^API_IMAGE_TAG=.*/API_IMAGE_TAG=v0.1.0/; s/^BOT_IMAGE_TAG=.*/BOT_IMAGE_TAG=v0.1.0/' .env
grep -E '^(API|BOT)_IMAGE_TAG=' .env
# API_IMAGE_TAG=v0.1.0
# BOT_IMAGE_TAG=v0.1.0

docker compose -f docker-compose.prod.yml --profile build build bot
docker compose -f docker-compose.prod.yml up -d --build
docker compose -f docker-compose.prod.yml exec api alembic current
# b81e0f4c7d25 (head)          # v0.1.1 shipped no migration: schema stays, code rolls back

deploy/smoke.sh
# smoke: PASS — http://127.0.0.1:8000/api/v1/health 200; database=up cache=up runner=up
```

(If `v0.1.1` had shipped a destructive migration, this step would
instead be `alembic downgrade <revision>` — when its release notes
document a safe downgrade — or a `restore.sh` recovery.)

Confirm the fix with a real call, then forward-fix on a new tag
(`v0.1.2`); do not roll forward again onto `v0.1.1`.

## If the database itself is wrong (not the code)

Use the product restore path — it stops `api` + `bot-runner`, restores
the chosen dump into the live database, restarts the services and runs
the smoke test:

```bash
/opt/oreeai/deploy/restore.sh /var/lib/oreeai/backups/<file> --target-prod --yes
```

Rehearse the default throwaway path first if there is any doubt:
`deploy/restore.sh <file>` restores into `oreeai_restore_test` and leaves
the live database untouched.

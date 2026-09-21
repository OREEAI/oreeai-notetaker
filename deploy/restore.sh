#!/usr/bin/env bash
# Restore a pg_dump backup into Postgres (PR 8).
#
# Default target is a THROWAWAY database (oreeai_restore_test): the dump
# is dropped, recreated and restored there so the rehearsal proves the
# backup is restorable without touching the live database. Restoring the
# live database is an explicit, confirmed action (--target-prod --yes)
# that also stops the api + bot-runner services first and re-runs
# deploy/smoke.sh afterwards.
#
# Usage:
#   deploy/restore.sh /var/lib/oreeai/backups/oreeai-20260921T030000Z.sql.gz
#   deploy/restore.sh <dump> --target oreeai_restore_ci
#   deploy/restore.sh <dump> --target-prod --yes
#
# Overridable (environment first, then .env):
#   COMPOSE_FILE, POSTGRES_USER, POSTGRES_DB, DB_SERVICE, RESTORE_TEST_DB
#
# Exit codes: 0 success; 1 restore failed; 2 usage/configuration error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/docker-compose.prod.yml}"
DB_SERVICE="${DB_SERVICE:-db}"

usage() {
  echo "usage: $(basename "$0") <backup.sql.gz> [--target DB] [--target-prod --yes]" >&2
  exit 2
}

DUMP_FILE=""
TARGET=""
TARGET_PROD=0
CONFIRMED=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      [[ $# -ge 2 ]] || usage
      TARGET="$2"
      shift 2
      ;;
    --target-prod)
      TARGET_PROD=1
      shift
      ;;
    --yes)
      CONFIRMED=1
      shift
      ;;
    --help | -h)
      usage
      ;;
    *)
      [[ -z "${DUMP_FILE}" ]] || usage
      DUMP_FILE="$1"
      shift
      ;;
  esac
done
[[ -n "${DUMP_FILE}" ]] || usage

env_value() {
  # env_value NAME — environment wins, else the last .env assignment
  # (compose's dotenv semantics). Surrounding quotes are stripped to match
  # compose interpolation; shell values are used literally.
  local name="$1"
  if [[ -n "${!name:-}" ]]; then
    printf '%s' "${!name}"
    return 0
  fi
  [[ -f "${ENV_FILE}" ]] || return 0
  local value
  value="$(sed -n "s/^${name}=//p" "${ENV_FILE}" | tail -n 1 | tr -d '\r')"
  value="${value#\"}"
  value="${value%\"}"
  value="${value#\'}"
  value="${value%\'}"
  printf '%s' "${value}"
}

POSTGRES_USER="${POSTGRES_USER:-$(env_value POSTGRES_USER)}"
POSTGRES_USER="${POSTGRES_USER:-oreeai}"
POSTGRES_DB="${POSTGRES_DB:-$(env_value POSTGRES_DB)}"
POSTGRES_DB="${POSTGRES_DB:-oreeai}"

if ((TARGET_PROD)); then
  if [[ -n "${TARGET}" || "${CONFIRMED}" -ne 1 ]]; then
    echo "restore: --target-prod requires --yes and cannot be combined with --target" >&2
    usage
  fi
  TARGET="${POSTGRES_DB}"
else
  TARGET="${TARGET:-${RESTORE_TEST_DB:-oreeai_restore_test}}"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "restore: FAIL — docker is required" >&2
  exit 2
fi
if [[ ! -f "${DUMP_FILE}" ]]; then
  echo "restore: FAIL — dump file not found: ${DUMP_FILE}" >&2
  exit 2
fi
if ! gzip -t "${DUMP_FILE}" 2>/dev/null; then
  echo "restore: FAIL — not a valid gzip stream: ${DUMP_FILE}" >&2
  exit 2
fi
if ! docker compose -f "${COMPOSE_FILE}" ps --status running -q "${DB_SERVICE}" | grep -q .; then
  echo "restore: FAIL — service '${DB_SERVICE}' is not running in ${COMPOSE_FILE}" >&2
  exit 2
fi

# Never drop and recreate the live database (or a reserved maintenance
# database) through the throwaway path: the product restore is
# --target-prod --yes, which stops services and smoke-tests afterwards.
if ((!TARGET_PROD)); then
  case "${TARGET}" in
    "${POSTGRES_DB}" | postgres | template0 | template1 | template*)
      echo "restore: FAIL — refusing to restore into reserved database '${TARGET}' via --target;" >&2
      echo "         use --target-prod --yes for a deliberate product restore," >&2
      echo "         or a throwaway database name" >&2
      exit 2
      ;;
  esac
fi

compose() {
  docker compose -f "${COMPOSE_FILE}" "$@"
}

leave_stopped_note() {
  if ((TARGET_PROD)); then
    echo "restore: NOTE — api and bot-runner were stopped and remain stopped;" >&2
    echo "         fix the cause and re-run, or restore an earlier dump" >&2
  fi
}

if ((TARGET_PROD)); then
  echo "restore: stopping api + bot-runner before touching ${TARGET} (product)"
  compose stop api bot-runner >/dev/null
fi

echo "restore: dropping and recreating database ${TARGET}"
compose exec -T "${DB_SERVICE}" psql -U "${POSTGRES_USER}" -d postgres -v ON_ERROR_STOP=1 \
  -c "DROP DATABASE IF EXISTS \"${TARGET}\" WITH (FORCE);" \
  -c "CREATE DATABASE \"${TARGET}\";" >/dev/null

echo "restore: loading ${DUMP_FILE} into ${TARGET}"
if ! gunzip -c "${DUMP_FILE}" | compose exec -T "${DB_SERVICE}" \
  psql -U "${POSTGRES_USER}" -d "${TARGET}" -v ON_ERROR_STOP=1 --quiet >/dev/null; then
  echo "restore: FAIL — psql restore failed for ${TARGET}" >&2
  leave_stopped_note
  exit 1
fi

# Structural sanity: the product table must exist and be queryable.
calls="$(compose exec -T "${DB_SERVICE}" psql -U "${POSTGRES_USER}" -d "${TARGET}" \
  -tAc "SELECT count(*) FROM calls;" 2>/dev/null || true)"
if [[ ! "${calls}" =~ ^[0-9]+$ ]]; then
  echo "restore: FAIL — restored database has no queryable calls table" >&2
  leave_stopped_note
  exit 1
fi

if ((TARGET_PROD)); then
  echo "restore: starting api + bot-runner"
  compose start api bot-runner >/dev/null
  if ! "${SCRIPT_DIR}/smoke.sh"; then
    echo "restore: FAIL — smoke test failed after restoring ${TARGET}" >&2
    exit 1
  fi
  echo "restore: OK — ${TARGET} restored (${calls} calls); smoke green"
else
  echo "restore: OK — throwaway ${TARGET} restored (${calls} calls); live database untouched"
fi

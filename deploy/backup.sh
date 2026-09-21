#!/usr/bin/env bash
# Nightly Postgres backup for the note-taker stack (PR 8).
#
# pg_dump of the app database, gzip-compressed to
# ${BACKUP_DIR}/oreeai-<UTC-timestamp>.sql.gz, then local retention
# cleanup: dumps older than BACKUP_KEEP_DAYS are deleted, except the
# newest one (never delete the last surviving backup).
#
# Runs on the HOST (cron), streaming `docker compose exec -T db pg_dump`
# through the host's gzip — no tooling assumptions inside the container.
# The db service must be running; the script does not start the stack.
#
# Overridable (environment first, then .env):
#   COMPOSE_FILE        default: repo docker-compose.prod.yml
#   BACKUP_DIR          default: /var/lib/oreeai/backups
#   BACKUP_KEEP_DAYS    default: 30 (0 disables cleanup)
#   POSTGRES_USER/DB    default: oreeai
#   DB_SERVICE          default: db
#
# Exit codes: 0 success; 1 backup failed; 2 configuration/precondition error.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"
COMPOSE_FILE="${COMPOSE_FILE:-${REPO_ROOT}/docker-compose.prod.yml}"
BACKUP_DIR="${BACKUP_DIR:-/var/lib/oreeai/backups}"
DB_SERVICE="${DB_SERVICE:-db}"

env_value() {
  local name="$1"
  local current="${!name:-}"
  if [[ -n "${current}" ]]; then
    printf '%s' "${current}"
    return 0
  fi
  if [[ -f "${ENV_FILE}" ]]; then
    sed -n "s/^${name}=//p" "${ENV_FILE}" | tail -n 1 | tr -d '\r'
  fi
}

POSTGRES_USER="${POSTGRES_USER:-$(env_value POSTGRES_USER)}"
POSTGRES_USER="${POSTGRES_USER:-oreeai}"
POSTGRES_DB="${POSTGRES_DB:-$(env_value POSTGRES_DB)}"
POSTGRES_DB="${POSTGRES_DB:-oreeai}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-$(env_value BACKUP_KEEP_DAYS)}"
BACKUP_KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"

if ! command -v docker >/dev/null 2>&1; then
  echo "backup: FAIL — docker is required" >&2
  exit 2
fi
if [[ ! -f "${COMPOSE_FILE}" ]]; then
  echo "backup: FAIL — compose file not found: ${COMPOSE_FILE}" >&2
  exit 2
fi
if ! docker compose -f "${COMPOSE_FILE}" ps --status running -q "${DB_SERVICE}" | grep -q .; then
  echo "backup: FAIL — service '${DB_SERVICE}' is not running in ${COMPOSE_FILE}" >&2
  exit 2
fi

mkdir -p "${BACKUP_DIR}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="${BACKUP_DIR}/oreeai-${timestamp}.sql.gz"

# Pipe through the host gzip. pipefail makes a failed pg_dump fail the
# whole pipeline; the partial file is removed rather than left as a
# plausible-looking corrupt dump.
if ! docker compose -f "${COMPOSE_FILE}" exec -T "${DB_SERVICE}" \
  pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" | gzip -c >"${out}"; then
  rm -f "${out}"
  echo "backup: FAIL — pg_dump failed for database ${POSTGRES_DB}" >&2
  exit 1
fi

# Sanity: the gzip stream must be valid and must be a plain-format
# pg_dump (an empty database still carries the header).
if ! gzip -t "${out}" 2>/dev/null; then
  rm -f "${out}"
  echo "backup: FAIL — dump is not a valid gzip stream" >&2
  exit 1
fi
set +o pipefail
header="$(gzip -cd "${out}" | head -n 5 || true)"
set -o pipefail
if [[ "${header}" != *"PostgreSQL database dump"* ]]; then
  rm -f "${out}"
  echo "backup: FAIL — dump is truncated or not a pg_dump archive" >&2
  exit 1
fi

if [[ "${BACKUP_KEEP_DAYS}" =~ ^[0-9]+$ ]] && ((BACKUP_KEEP_DAYS > 0)); then
  newest="$(find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'oreeai-*.sql.gz' \
    -printf '%T@ %p\n' | sort -rn | head -n 1 | cut -d' ' -f2-)"
  find "${BACKUP_DIR}" -maxdepth 1 -type f -name 'oreeai-*.sql.gz' \
    -mtime +"${BACKUP_KEEP_DAYS}" -not -path "${newest}" -delete
fi

echo "backup: OK — ${out} ($(du -h "${out}" | cut -f1)); retention ${BACKUP_KEEP_DAYS} days in ${BACKUP_DIR}"

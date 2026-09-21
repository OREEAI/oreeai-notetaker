#!/usr/bin/env bash
# Health smoke test for the deployed note-taker stack (PR 8).
#
# Asserts the api answers 200 on GET /api/v1/health *with* the X-API-Key
# header, and that the response reports every component up:
# components.database, components.cache, components.runner.
#
# Reads configuration from the environment first, then from .env (never
# sources it — values are extracted with sed, so a hostile .env cannot
# execute code here). API_KEY is never printed.
#
# Usage:
#   deploy/smoke.sh
#   BASE_URL=http://127.0.0.1:9000 deploy/smoke.sh
#   ENV_FILE=/opt/oreeai/.env deploy/smoke.sh
#
# Exit codes:
#   0  stack healthy
#   2  missing configuration (API_KEY) or missing tooling
#   3  HTTP request failed or returned a non-200 status
#   4  health payload reports a component not up
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
ENV_FILE="${ENV_FILE:-${REPO_ROOT}/.env}"

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

API_KEY="$(env_value API_KEY)"
if [[ -z "${API_KEY}" ]]; then
  echo "smoke: FAIL — API_KEY is not set and not found in ${ENV_FILE}" >&2
  exit 2
fi

if [[ -z "${BASE_URL:-}" ]]; then
  API_HOST_PORT="$(env_value API_HOST_PORT)"
  BASE_URL="http://127.0.0.1:${API_HOST_PORT:-8000}"
fi
HEALTH_URL="${BASE_URL%/}/api/v1/health"

if ! command -v curl >/dev/null 2>&1; then
  echo "smoke: FAIL — curl is required" >&2
  exit 2
fi

body="$(mktemp)"
trap 'rm -f "${body}"' EXIT

http_code="$(curl -sS -o "${body}" -w '%{http_code}' \
  --connect-timeout 5 --max-time 15 \
  -H "X-API-Key: ${API_KEY}" "${HEALTH_URL}" || true)"

if [[ "${http_code}" != "200" ]]; then
  echo "smoke: FAIL — ${HEALTH_URL} returned HTTP ${http_code}" >&2
  echo "smoke: response body: $(cat "${body}")" >&2
  exit 3
fi

parse_components() {
  local file="$1"
  if command -v python3 >/dev/null 2>&1; then
    python3 - "${file}" <<'PY'
import json
import sys

try:
    with open(sys.argv[1]) as handle:
        payload = json.load(handle)
except Exception:
    print("parse_error parse_error parse_error")
    raise SystemExit(0)

components = payload.get("components") or {}
print(" ".join(str(components.get(key, "missing")) for key in ("database", "cache", "runner")))
PY
    return 0
  fi
  # Degraded fallback without python3: "up" only if the exact pair appears.
  local database cache runner
  grep -q '"database":"up"' "${file}" && database="up" || database="not-up"
  grep -q '"cache":"up"' "${file}" && cache="up" || cache="not-up"
  grep -q '"runner":"up"' "${file}" && runner="up" || runner="not-up"
  printf '%s %s %s\n' "${database}" "${cache}" "${runner}"
}

read -r database cache runner <<<"$(parse_components "${body}")"

failed=0
for component in "database:${database}" "cache:${cache}" "runner:${runner}"; do
  name="${component%%:*}"
  state="${component#*:}"
  if [[ "${state}" != "up" ]]; then
    echo "smoke: FAIL — ${name} is ${state}" >&2
    failed=1
  fi
done
if [[ "${failed}" -ne 0 ]]; then
  echo "smoke: response body: $(cat "${body}")" >&2
  exit 4
fi

echo "smoke: PASS — ${HEALTH_URL} 200; database=up cache=up runner=up"

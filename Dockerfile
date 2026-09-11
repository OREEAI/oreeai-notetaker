FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
RUN uv sync --frozen --no-dev


FROM python:3.13-slim-bookworm AS runtime

# docker CLI only (no daemon): the bot-runner service drives bot containers
# through the mounted host socket. The API process never gets that socket.
RUN apt-get update \
    && install -m 0755 -d /etc/apt/keyrings \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --system app && useradd --system --gid app --create-home app

WORKDIR /app
COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER app
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=5 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(urllib.request.Request('http://localhost:8000/api/v1/health', headers={'X-API-Key': os.environ.get('API_KEY', '')}))" || exit 1

CMD ["uvicorn", "oreeai_notetaker.main:app", "--host", "0.0.0.0", "--port", "8000"]

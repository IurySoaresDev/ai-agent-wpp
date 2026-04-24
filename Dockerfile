# syntax=docker/dockerfile:1.7

# ---------- Stage 1: build ----------
FROM python:3.12-slim-bookworm AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install dependencies first (cached layer when only code changes).
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

# Copy project source and install the package itself.
COPY src ./src
COPY prompts ./prompts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


# ---------- Stage 2: runtime ----------
FROM python:3.12-slim-bookworm AS runtime

# OCI labels for image provenance.
LABEL org.opencontainers.image.title="ai-wpp" \
      org.opencontainers.image.description="WhatsApp AI agent (Mistral + LangGraph + W-API)" \
      org.opencontainers.image.source="https://example.com/ai-wpp"

# Minimal runtime tools. curl is used by the healthcheck.
RUN apt-get update \
 && apt-get install -y --no-install-recommends curl tini \
 && rm -rf /var/lib/apt/lists/*

# Unprivileged user — never run app code as root.
RUN groupadd --system --gid 1001 app \
 && useradd  --system --uid 1001 --gid 1001 --home /app --shell /usr/sbin/nologin app

WORKDIR /app

# Bring the prepared virtualenv + source over, owned by `app`.
COPY --from=builder --chown=app:app /app /app

# Checkpoint DB lives here; mount a volume for durability.
RUN mkdir -p /app/data && chown -R app:app /app/data
VOLUME ["/app/data"]

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    AGENT_CHECKPOINT_DB=/app/data/checkpoints.sqlite

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${PORT}/health" || exit 1

USER app

# tini reaps zombies and forwards signals cleanly (graceful shutdown on SIGTERM).
ENTRYPOINT ["/usr/bin/tini", "--"]

# Single worker on purpose: the SQLite checkpointer and the in-process dedupe
# cache are not safe across workers. For horizontal scaling, move state to
# Postgres (langgraph-checkpoint-postgres) + Redis and raise --workers.
CMD ["sh", "-c", "uvicorn ai_wpp.main:app --host ${HOST} --port ${PORT} --workers 1 --proxy-headers --forwarded-allow-ips=* --no-server-header --timeout-graceful-shutdown 30"]

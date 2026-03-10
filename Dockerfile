# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

# ---------------------------------------------------------------------------
# Dependencies layer (cached unless pyproject.toml or uv.lock changes)
# ---------------------------------------------------------------------------
FROM base AS deps

COPY pyproject.toml ./
# Copy lockfile if it exists (will be generated on first build)
COPY uv.lock* ./

# Install dependencies into a virtual environment at /app/.venv
RUN uv sync --frozen --no-install-project --no-dev

# ---------------------------------------------------------------------------
# Runtime image
# ---------------------------------------------------------------------------
FROM base AS runtime

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Copy venv from deps stage
COPY --from=deps /app/.venv /app/.venv

# Copy application source
COPY src/ ./src/
COPY pyproject.toml ./

# Install the project itself (no deps, already installed)
RUN uv sync --frozen --no-dev

EXPOSE 8000

# Healthcheck for Docker / Kubernetes
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')"

CMD ["incidentflow-mcp", "serve", "--host", "0.0.0.0", "--port", "8000"]

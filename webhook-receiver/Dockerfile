# Dockerfile for the webhook receiver (standalone Alertmanager ingress)
FROM python:3.12-slim

WORKDIR /app

# Install uv for fast, reliable dependency resolution (pinned for reproducibility)
COPY --from=ghcr.io/astral-sh/uv:0.5.25 /uv /bin/uv

# Copy project files (explicit uv.lock required for --frozen)
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# Install production dependencies only
RUN uv sync --frozen --no-dev

# Ensure logs reach the container log driver immediately
ENV PYTHONUNBUFFERED=1
ENV ALERTS_JSONL_PATH=/var/hermes/alerts.jsonl
ENV INCIDENTS_DB_PATH=/var/hermes/incidents.db

# Shared volume with the hermes-agent container (single-writer boundary:
# this process writes alerts.jsonl only, never the SQLite database)
RUN mkdir -p /var/hermes

# Webhook ingress + health probe
EXPOSE 8090

# --no-sync: the image already synced at build time; never re-resolve
# (or require the network) at container start.
CMD ["uv", "run", "--no-sync", "python", "-m", "webhook_receiver.server"]

# Dockerfile for Hermes Agent
FROM python:3.12-slim

WORKDIR /app

# Install uv for fast, reliable dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy project files (explicit uv.lock required for --frozen)
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# Install production dependencies only
RUN uv sync --frozen --no-dev

# Ensure logs reach the container log driver immediately
ENV PYTHONUNBUFFERED=1
ENV ALERTS_JSONL_PATH=/var/hermes/alerts.jsonl
ENV ALERT_OFFSET_PATH=/var/hermes/alerts.jsonl.offset
ENV METRICS_PORT=8091

RUN mkdir -p /var/hermes

# metrics endpoint — implemented in task 2.7
EXPOSE 8091

CMD ["uv", "run", "python", "-m", "agentic_node_ops.processor"]
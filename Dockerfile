# Dockerfile for Hermes Agent
FROM python:3.12-slim

WORKDIR /app

# Install uv for fast, reliable dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy project files
COPY pyproject.toml uv.lock* ./
COPY src/ ./src/

# Install dependencies and the package
RUN uv sync --frozen --no-dev

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV ALERTS_JSONL_PATH=/var/hermes/alerts.jsonl
ENV ALERT_OFFSET_PATH=/var/hermes/alerts.jsonl.offset
ENV METRICS_PORT=8091

# Create directory for persistent state
RUN mkdir -p /var/hermes

# Expose metrics port
EXPOSE 8091

# Run the processor loop as the main entry point
CMD ["uv", "run", "python", "-m", "agentic_node_ops.processor"]

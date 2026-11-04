# Thread server image for Fly.io. Build: docker build -t thread .
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_SYNC=1 \
    PYTHONUNBUFFERED=1

# Dependencies first for layer caching, then the app.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    THREAD_HOST=0.0.0.0 \
    THREAD_PORT=8080 \
    THREAD_DATA_DIR=/data \
    THREAD_CONFIG_DIR=/data/config

# Shared config in /data/config; per-account data in /data/users/<id>/.
RUN mkdir -p /root/.config && ln -s /data/config /root/.config/thread

EXPOSE 8080

# Starts with no accounts; create invites with: fly ssh console -C "thread-admin invite"
CMD ["sh", "-c", "mkdir -p \"$THREAD_DATA_DIR/users\" \"$THREAD_CONFIG_DIR\"; exec /app/.venv/bin/thread-online"]

# One image for every service in compose.yaml (the gateway now; the dashboard later). Each service
# sets its own command. Libraries come from uv.lock; .dockerignore keeps data, keys and .env out.
FROM docker.io/library/python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
RUN UV_PYTHON_DOWNLOADS=never uv sync --locked --no-cache --no-install-project --extra gateway
COPY comms comms
COPY dashboard dashboard
COPY detection detection
COPY edge edge
COPY gateway gateway
COPY hal hal
ENV PATH=/app/.venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

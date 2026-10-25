# One image for every part of the live simulation (timekeeper, control panel, gateway, nodes).
# Only the Python libraries live in the image; the code is mounted from simulation/ (see compose.yaml).
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.16 /uv /bin/uv
WORKDIR /deps
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project
ENV PATH=/deps/.venv/bin:$PATH PYTHONUNBUFFERED=1
WORKDIR /sim

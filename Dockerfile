FROM node:22-bookworm-slim AS frontend
WORKDIR /web
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/src.jsx frontend/wind.mjs ./
RUN npm run build

FROM ghcr.io/astral-sh/uv:0.6.1-python3.12-bookworm AS builder

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
COPY --from=frontend /src/tg_drop_bot/static/frog/assets/app.js /app/src/tg_drop_bot/static/frog/assets/app.js
RUN uv sync --frozen --no-dev

FROM python:3.12-slim

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /app /app
RUN chmod +x /app/docker/entrypoint.sh

ENTRYPOINT ["/app/docker/entrypoint.sh"]

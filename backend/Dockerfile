# Build with the repository root as the context so the published OpenAPI file
# can be copied beside the application at its configured runtime path.
FROM ghcr.io/astral-sh/uv:0.13.5@sha256:32a7bb39b496babbd997b0a39a4d66c126bdaf5661b302bf6f06abda8deca6ff AS uv

FROM python:3.14.4-slim-bookworm@sha256:fc74d22ffd0d5ac395a4b7bdda75a4539758862c49ebf3005647084631e63789 AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

WORKDIR /app

COPY --from=uv /uv /uvx /usr/local/bin/
RUN groupadd --system app && useradd --system --gid app --home-dir /app --no-create-home app

# Install the locked runtime dependencies before copying frequently changing
# application code so Docker can reuse this layer during normal development.
COPY backend/pyproject.toml backend/uv.lock ./backend/
RUN cd /app/backend && uv sync --locked --no-dev --no-install-project

COPY --chown=app:app backend/src ./backend/src
COPY --chown=app:app backend/db ./backend/db
COPY --chown=app:app docs/openapi.yaml ./docs/openapi.yaml
COPY --chown=app:app backend/pyproject.toml backend/uv.lock ./backend/
RUN cd /app/backend && uv sync --locked --no-dev

ENV PYTHONPATH=/app/backend/src:/app/backend/src/generated \
    OPENAPI_PATH=/app/docs/openapi.yaml

USER app:app
EXPOSE 8000

CMD ["uvicorn", "credit.main:app", "--host", "0.0.0.0", "--port", "8000"]

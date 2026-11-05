# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .
COPY alembic.ini ./
COPY migrations ./migrations

# Образ для линтера и тестов.
FROM base AS dev
RUN pip install ".[dev]"
COPY tests ./tests

FROM base AS runtime
RUN useradd --system --uid 10001 coord
USER coord
EXPOSE 8000
CMD ["uvicorn", "coordinator.api.main:app", "--host", "0.0.0.0", "--port", "8000"]

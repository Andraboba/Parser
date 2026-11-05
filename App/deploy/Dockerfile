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
COPY stub ./stub
COPY docs ./docs
COPY scripts ./scripts

FROM base AS runtime
RUN useradd --system --uid 10001 coord
USER coord
EXPOSE 8000
# Перед api стоит только nginx, порт наружу не открыт: адресу клиента из его заголовков можно верить.
CMD ["uvicorn", "coordinator.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*", "--no-access-log"]

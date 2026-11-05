# Образ агента рабочего узла.
#
# Требования из плана: пользователь без прав root, запись только в том,
# корневая файловая система может быть только для чтения, конфигурация
# монтируется снаружи, секреты приходят через окружение.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

COPY agent ./agent
COPY config ./config
COPY README.md ./

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin agent \
    && mkdir -p /data/videos \
    && chown -R agent:agent /data/videos /app

USER agent

VOLUME ["/data/videos"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-m", "agent", "check-config", "-c", "/etc/agent/agent.yaml"]

ENTRYPOINT ["python", "-m", "agent"]
CMD ["run", "--config", "/etc/agent/agent.yaml"]

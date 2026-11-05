FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY agent_stub.py ./
RUN useradd --system --uid 10001 stub && mkdir -p /data/videos && chown -R stub /data/videos
USER stub
VOLUME ["/data/videos"]
CMD ["python", "agent_stub.py"]

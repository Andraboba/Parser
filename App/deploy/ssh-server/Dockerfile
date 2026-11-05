# «Рабочий сервер» стенда: контейнер ведёт себя как отдельная машина с SSH (ТЗ, п. 4).
# Агента в образе нет: его ставит и запускает координатор по SSH.
FROM python:3.13-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server procps \
    && rm -rf /var/lib/apt/lists/*
# Без пароля, но не заблокирована: заблокированную учётную запись sshd не пускает и по ключу.
RUN useradd --create-home --uid 10001 --shell /bin/sh --password '*' agent \
    && mkdir -p /data/videos /run/sshd /etc/ssh/keys
COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh
EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]

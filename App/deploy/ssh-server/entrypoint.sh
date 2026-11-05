#!/bin/sh
set -eu

# Ключ хоста живёт на томе: пересозданный сервер сохраняет свою идентичность,
# и runner не примет его за подмену.
keys=/etc/ssh/keys
[ -f "$keys/ssh_host_ed25519_key" ] || ssh-keygen -q -t ed25519 -N "" -f "$keys/ssh_host_ed25519_key"

public_key="${SSH_AUTHORIZED_KEY:-}"
if [ -z "$public_key" ] && [ -f /run/secrets/ssh_client_key_pub ]; then
  public_key="$(cat /run/secrets/ssh_client_key_pub)"
fi
if [ -z "$public_key" ]; then
  echo "нет открытого ключа координатора: вход по SSH будет невозможен" >&2
fi

install -d -o agent -g agent -m 700 /home/agent/.ssh
printf '%s\n' "$public_key" > /home/agent/.ssh/authorized_keys
chown agent:agent /home/agent/.ssh/authorized_keys
chmod 600 /home/agent/.ssh/authorized_keys
chown -R agent:agent /data/videos

exec /usr/sbin/sshd -D -e

#!/usr/bin/env bash
# Готовит стенд к первому запуску: случайные секреты в secrets/ и сертификаты TLS в deploy/tls/.
# Существующие файлы не перезаписывает. Обе папки в git не попадают.
# С флагом --renew-tls перевыпускает сертификаты (ротация). После этого: docker compose up -d --force-recreate
set -euo pipefail
cd "$(dirname "$0")/.."

secret() { od -An -N24 -tx1 /dev/urandom | tr -d ' \n'; }

mkdir -p secrets deploy/tls
chmod 700 secrets

# Контейнеры работают от непривилегированных пользователей и читают файл через bind mount,
# поэтому файл доступен на чтение, а закрыт каталог, в котором он лежит.
write_secret() {
  local name="$1" value="$2"
  [ -f "secrets/$name" ] && return 0
  printf '%s' "$value" > "secrets/$name"
  chmod 644 "secrets/$name"
  echo "создан secrets/$name"
}

write_secret coord_admin_password "$(secret)"
write_secret coord_runner_token "$(secret)"
for n in 01 02 03; do
  write_secret "worker_${n}_token" "$(secret)"
done
if [ ! -f secrets/coord_agent_tokens ]; then
  write_secret coord_agent_tokens \
    "worker-01=$(cat secrets/worker_01_token),worker-02=$(cat secrets/worker_02_token),worker-03=$(cat secrets/worker_03_token)"
fi

[ -f .env ] || cp .env.example .env

if [ "${1:-}" = "--renew-tls" ] || [ ! -f deploy/tls/server.crt ]; then
  # Иначе Git Bash на Windows превратит -subj "/CN=..." в путь к файлу.
  export MSYS_NO_PATHCONV=1
  cd deploy/tls
  # Расширения обязательны: Python 3.13 и новее проверяет цепочку в строгом режиме X.509.
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 -sha256 \
    -keyout ca.key -out ca.crt -subj "/CN=Coordinator lab CA" \
    -addext "basicConstraints=critical,CA:TRUE" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -keyout server.key -out server.csr \
    -subj "/CN=coordinator" 2>/dev/null
  cat > server.ext <<'EXT'
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectKeyIdentifier=hash
authorityKeyIdentifier=keyid,issuer
subjectAltName=DNS:localhost,DNS:web,DNS:coordinator,IP:127.0.0.1
EXT
  openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 -sha256 \
    -extfile server.ext -out server.crt 2>/dev/null
  rm -f server.csr server.ext ca.srl
  chmod 644 ca.crt server.crt
  chmod 600 server.key ca.key
  echo "созданы сертификаты в deploy/tls"
fi

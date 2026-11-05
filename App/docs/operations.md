# Установка, обновление, масштабирование

## Установка на чистую машину

Требуются Docker с Compose v2, bash и openssl. На Windows подойдёт Git Bash. Минимум для стенда:
4 vCPU и 8 ГБ памяти.

```bash
git clone https://github.com/Andraboba/Ushastiy.git
```

```bash
cd Ushastiy/App
```

```bash
bash scripts/init-env.sh
```

```bash
docker compose up -d --build
```

`init-env.sh` создаёт случайные секреты в `secrets/` и сертификаты TLS в `deploy/tls/`. Обе папки в
git не попадают. Существующие файлы скрипт не перезаписывает, запускать его можно повторно.

Проверка: `docker compose ps` показывает `healthy` у `db`, `redis`, `api`, `web`, затем откройте
https://localhost:8443 и войдите как `admin` с паролем из `secrets/coord_admin_password`.

## Настройки

Переменные окружения с приставкой `COORD_`, полный список в `src/coordinator/config.py`. Основные:

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `COORD_GLOBAL_RPS_LIMIT` | 20, на стенде 3000 | Общий лимит запросов. **Для настоящего сайта обязательно снизьте** в `.env` |
| `COORD_HEARTBEAT_INTERVAL` | 5 | Интервал heartbeat, секунды |
| `COORD_LEASE_TTL` | 60 | Срок аренды блока, секунды |
| `COORD_WORKER_OFFLINE_AFTER` | 11 | Через сколько секунд без heartbeat узел недоступен |
| `COORD_MAX_CHUNK_ATTEMPTS` | 5 | После скольких попыток блок становится `failed` |
| `COORD_DEFAULT_MIN_FREE_SPACE_GB` | 1 | Резерв места, ниже которого узел не получает блоки |
| `COORD_RUNNER_IMAGES` | `coord/agent-stub:*,lab/agent:*` | Allowlist образов |
| `COORD_TERMINAL_IDLE_SECONDS` | 300 | Автозавершение терминала без ввода |
| `COORD_API_WORKERS`, `COORD_API_CPUS` | 1, 1.0 | Процессы `api` и лимит CPU для них, только в `.env` для compose |

## Обновление платформы

```bash
git pull
```

```bash
docker compose up -d --build
```

Миграции применятся сами. Работающие агенты переживут перезапуск `api`: подтверждённое не теряется,
неотправленное они повторят. Перед обновлением схемы сделайте резервную копию:

```bash
docker compose exec -T db pg_dump -U coord coord > backup.sql
```

## Подключение настоящего агента

Заглушки заменяются настоящими агентами из `../Agent` без правок платформы, с управляемым тестовым
сайтом вместо внешнего:

```bash
docker compose -f compose.yaml -f deploy/compose.real-agent.yaml up -d --build
```

Для другого набора узлов агенту нужны адрес координатора `https://web`, переменная `SSL_CERT_FILE` с
файлом CA, токен узла и, если нужен доступ к внешнему сайту, правильный лимит запросов.

## Масштабирование

**Из интерфейса** (нужна роль administrator): «Узлы» → «Добавить узел». Токен выпускается
автоматически и попадает только в окружение контейнера.

**Через compose:** добавьте сервис `worker-0N` по образцу имеющихся: своё имя, лимиты и том. Токен
узла положите в `secrets/worker_0N_token` и допишите в `secrets/coord_agent_tokens`, затем:

```bash
docker compose up -d --build
```

Добавлять и убирать узлы можно не останавливая задание. Узел начинает получать блоки сразу после
регистрации, его доля общего лимита рассчитывается на следующем проходе диспетчера.

Узкое место при росте числа агентов — один процесс `api` с лимитом 1 CPU. Начиная примерно с 30 агентов
задайте `COORD_API_WORKERS` и `COORD_API_CPUS` в `.env`, цифры в [отчёте о нагрузке](load-report.md).

## Ротация секретов

**Токен агента без остановки узла.** «Пользователи» → «Токены агентов» → «Выпустить токен». Передайте
токен агенту, перезапустите его и отзовите старый. Пока оба действуют, узел работает.

**Токен runner-а.** Запишите новое значение в `secrets/coord_runner_token`, затем:

```bash
docker compose up -d --force-recreate api dispatcher runner
```

**Сертификаты TLS.** Сессии пользователей и работа узлов продолжатся после перезапуска:

```bash
bash scripts/init-env.sh --renew-tls
```

```bash
docker compose up -d --force-recreate
```

**Пароль пользователя** меняется в разделе «Пользователи». Все сессии этого пользователя при этом
завершаются. Файл `secrets/coord_admin_password` используется только при самом первом запуске.

## Резервное копирование

Состояние системы целиком в базе: `pg_dump`, как выше. Файлы лежат в томах `videos-*` на узлах и
копируются отдельно.

## Режим разработки без TLS

Интерфейс на http://localhost:8080 и API напрямую на http://localhost:8000. Пароли и токены при этом
идут открытым текстом, для настоящей работы режим не подходит.

```bash
docker compose -f compose.yaml -f deploy/compose.dev.yaml up -d --build
```

# План реализации платформы и текущий прогресс

Источник: `ПЛАН_2_Платформа.docx` (этапы Э0–Э13). Работа разбита на tracer-bullet тикеты по skill
[`to-tickets`](https://github.com/mattpocock/skills/tree/main/skills/engineering/to-tickets) (Matt Pocock):
каждый тикет проходит через все слои (схема → API → заглушка агента → React-интерфейс → тесты)
и проверяется сам по себе. Трекер не ведём, статусы — в этом файле.

**Стек:** Python 3.13, FastAPI, SQLAlchemy 2 (async, psycopg 3), Alembic, PostgreSQL 17, Redis 7.
Фронт — React (Vite + TypeScript). Python-пакет называется `coordinator`
(имя `platform` конфликтует со стандартной библиотекой).

**Git:** репозиторий в родительской папке (`C:\Ushastiy`), из этой сессии к нему доступа нет.
Коммиты делаются вручную.

---

## Где остановился (05.11.2026)

### Тикет 01 «Каркас стенда и CI»: код готов, полный зелёный прогон CI ещё не получен

- Сделано: структура `src/coordinator/{api,dispatcher,domain,storage}`, `migrations/`, `tests/`,
  `deploy/`, `web/`, `scripts/`; `GET /health` проверяет db и redis и при сбое возвращает 503;
  базовая миграция 0001; `compose.yaml` (db, redis, migrate, api); `deploy/Dockerfile`
  (стадии base, dev, runtime); `scripts/ci.sh` (сборка → ruff → миграции на чистой базе → pytest);
  `deploy/compose.ci.yaml` (CI без публикации портов); шаблон GitHub Actions
  `deploy/ci/github-actions.yml`. Его нужно скопировать в `<корень репо>/.github/workflows/`.
- Прогоны CI:
  1. Образы собрались, ruff нашёл ASYNC109 в `api/app.py`. **Исправлено.**
  2. Упал при скачивании образа postgres из Docker Hub (`unexpected EOF`, сеть). Код не виноват.
     После этого `postgres:17-alpine` докачан, `redis:7-alpine` докачивался.
- **Первое действие при продолжении:** `bash scripts/ci.sh` должен закончиться строкой `CI OK`.
  Затем `docker compose up -d --build` и `curl localhost:8000/health` → `{"status":"ok",...}`.

### Тикет 02 «Tracer bullet»: начат, готово примерно 40 %

Готово (ещё ни разу не запускалось):
- `domain/statuses.py`: статусы задания, блока, элемента и узла (минимальный набор).
- `domain/chunks.py`: `split_range()`, разбиение конечного диапазона на блоки размером 1..10 000.
- `storage/models.py`: ORM-модели workers, jobs, range_chunks, items.
  Миграция `0002_core_tables.py`, `migrations/env.py` смотрит на `Base.metadata`.
- `dispatcher/leasing.py`:
  - `lease_chunk()`: атомарная выдача через `FOR UPDATE SKIP LOCKED`;
  - `complete_chunk()`: идемпотентный upsert элементов, закрытие блока, завершение задания,
    когда закрыт последний блок.
- `storage/jobs.py`: `create_job()`, `start_job()`, `get_job_view()` / `list_job_views()`
  (прогресс и счётчики элементов).
- `storage/workers.py`: `register_worker()` (upsert по имени), `list_worker_views()`.
- `config.py`: добавлены `heartbeat_interval`, `lease_ttl`, `default_rps_share`, `protocol_version`.
- `storage/db.py`: `make_sessionmaker()`.

Осталось в тикете 02:
1. `api/schemas.py`: pydantic-схемы запросов и ответов по разделу 5 плана.
2. Роутеры, подключить в `api/app.py` (sessionmaker положить в `app.state`):
   - `POST /api/jobs` (range_from, range_to, chunk_size), `POST /api/jobs/{id}/start`,
     `GET /api/jobs`, `GET /api/jobs/{id}`;
   - `POST /api/workers/register` → 201, `POST /api/workers/{id}/lease` → 200 или 204,
     `POST /api/workers/{id}/complete`, `GET /api/workers`;
   - ошибки: `LeaseConflict` → 409, `InvalidResults` → 422, `JobNotFound` → 404,
     `JobStateConflict` → 409, неизвестный узел → 404.
3. Заглушка агента `stub/agent_stub.py`: только stdlib, свой маленький Dockerfile. Цикл:
   register → lease → результаты (`found`, если id % 7 == 0, иначе `not_found`) → complete;
   при 204 — пауза.
4. React-интерфейс в `web/` (Vite + TS): список заданий, форма создания и запуска,
   страница задания с прогресс-баром (опрос каждые 2 с), список узлов.
   Dockerfile: сборка на node → раздача через nginx с проксированием `/api` на api.
5. `compose.yaml`: сервисы `web` (порт 8080) и `worker-01..03` (заглушки с разными
   threads и лимитами из таблицы 2.1 плана).
6. Тесты:
   - unit для `split_range`;
   - интеграционный полный цикл (создать → запустить → register → lease до 204 → complete →
     задание `completed`, число элементов совпадает, повторный complete не создаёт дублей);
   - фикстура `TRUNCATE ... RESTART IDENTITY CASCADE` между интеграционными тестами.
7. Прогнать `bash scripts/ci.sh`, поднять стенд, глазами проверить интерфейс.

Принятые решения, которые стоит помнить:
- `worker_id` — целое число. В примере плана была строка `"w-01"`; окончательно контракт
  фиксируется в тикете 03.
- `complete` требует `next_id == range_end + 1`. Частичный прогресс пойдёт через `progress`
  в тикете 06.
- `complete_chunk` блокирует строку задания (`FOR UPDATE`), иначе два параллельных закрытия
  не увидят друг друга и задание не перейдёт в `completed`.
- Узел регистрируется по имени (upsert), а не по адресу: пересозданный контейнер сохраняет id.

### Особенности окружения (Windows)
- `python` / `python3` не работают (заглушка Microsoft Store), нужно использовать `py -3.13`.
- Локальный `.venv` создан, но `pip install -e ".[dev]"` завис из-за сети и не доустановился.
  Можно переустановить или работать только через `scripts/ci.sh` в Docker.
- Сеть до Docker Hub и PyPI нестабильна. При обрыве повторить `docker pull`.

---

## Тикеты

| # | Тикет | Зависит от | Статус |
|---|---|---|---|
| 01 | Каркас стенда и CI | — | код готов, ждёт зелёного CI |
| 02 | Tracer bullet: задание → блок → заглушка → результат → прогресс в UI | 01 | в работе (~40 %) |
| 03 | Фиксация контракта агентского API: версия протокола, токен, коды ошибок, OpenAPI, контрактные тесты | 02 | |
| 04 | Формат диапазона (ТЗ, п. 3) и разбиение на блоки, форма создания задания | 02 | |
| 05 | Жизненный цикл задания: start, мягкая pause, resume, stop, таблица переходов (п. 19) | 02 | |
| 06 | Heartbeat, аренда, возврат блока с next_id, progress; заглушка «падает по команде» | 02 | |
| 07 | Идемпотентный приём результатов и файлов: Idempotency-Key, метаданные, проверка пути, список элементов | 02 | |
| 08 | Ошибки и `fail`: классификация, повтор ошибок из UI | 06, 07 | |
| 09 | Балансировка по ресурсам узла: метрики, лимиты, нехватка места | 06 | |
| 10 | Централизованный лимит запросов: rps_share через Redis, реакция на 429 | 06 | |
| 11 | Бесконечный диапазон (п. 10) | 04, 05 | |
| 12 | Восстановление после сбоев: kill координатора, БД недоступна, новый IP, OOM | 06, 07 | |
| 13 | Runner: управление контейнерами из UI, allowlist | 06 | |
| 14 | Пользователи, роли viewer/operator/admin, CSRF/XSS, аудит | 05 | |
| 15 | Журнал событий: events, поиск, маскирование секретов | 08 | |
| 16 | Главная панель полностью: скорость, ETA, автообновление ≤5 с | 07, 09 | |
| 17 | Мониторинг и алерты: Prometheus и Grafana, предупреждения из п. 12 | 09, 10, 15 | |
| 18 | TLS и секреты | 14 | |
| 19 | Терминал в контейнер (docker exec), только admin | 13, 14 | |
| 20 | Нагрузочные и отказные тесты, отчёт | 09, 10, 12 | |
| 21 | Развёртывание, документация, демонстрация (чек-лист раздела 16 плана) | все | |

После тикета 02 параллельно можно вести тикеты 03–07, 09 и 10.

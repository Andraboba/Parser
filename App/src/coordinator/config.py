from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Секреты приходят файлами (docker secrets): в окружении контейнера их видно через docker inspect.
_SECRETS_DIR = Path("/run/secrets")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="COORD_", env_file=".env", extra="ignore",
        secrets_dir=_SECRETS_DIR if _SECRETS_DIR.is_dir() else None,
    )

    database_url: str = "postgresql+psycopg://coord:coord@localhost:5432/coord"
    redis_url: str = "redis://localhost:6379/0"
    # Таймаут проверки зависимостей в /health, секунды.
    health_timeout: float = 2.0

    # Параметры, которые агент получает при регистрации, секунды.
    heartbeat_interval: int = 5
    lease_ttl: int = 60
    # Узел без heartbeat считается недоступным чуть позже двух интервалов (ТЗ, п. 18).
    worker_offline_after: int = 11
    # Сколько ждать реакции агента на немедленную остановку.
    stop_grace_seconds: int = 10
    # Сколько открытый диапазон ждёт появления узлов, прежде чем завершиться (ТЗ, п. 10).
    no_workers_grace_seconds: int = 300
    max_chunk_attempts: int = 5
    dispatcher_interval: float = 1.0
    job_sample_interval: float = 3.0
    idempotency_ttl_hours: int = 24
    metrics_retention_hours: int = 24
    # Общий лимит запросов стенда: контейнеры выходят в сеть через один IP (ТЗ, п. 9).
    global_rps_limit: float = 20.0
    # Балансировка (ТЗ, п. 6.3).
    default_min_free_space_gb: float = 1.0
    cpu_overload_ratio: float = 0.95
    memory_overload_ratio: float = 0.97
    error_ratio_limit: float = 0.5
    balance_window_seconds: int = 60
    threads_per_chunk: int = 16
    threads_per_cpu: int = 8
    protocol_version: int = 1
    # Старейшая версия протокола, с которой координатор ещё совместим (ТЗ, п. 18).
    min_protocol_version: int = 1
    # Пользователи. Начальный администратор создаётся при bootstrap, если задан пароль.
    admin_login: str = "admin"
    admin_password: str = ""
    session_ttl_minutes: int = 480
    # С TLS cookie передаётся только по защищённому соединению (тикет 18).
    cookie_secure: bool = False
    events_retention_days: int = 30
    # Runner: пустой адрес — минимальный режим, контейнеры поднимает compose (Э6 плана).
    runner_url: str = ""
    runner_token: str = ""
    runner_images: str = "coord/agent-stub:*,lab/agent:*"
    runner_network: str = "coord_default"
    # Куда стучатся агенты: через nginx и TLS, порт api наружу не открыт.
    agent_coordinator_url: str = "https://web"
    image_update_timeout: int = 45
    container_sync_interval: float = 5.0
    # Терминал: короткая сессия и автозавершение без активности (ТЗ, п. 11.4).
    terminal_idle_seconds: int = 300
    terminal_max_seconds: int = 1800
    # Токены агентов для начальной загрузки: worker-01=секрет,*=общий-секрет.
    agent_tokens: str = ""

    def runner_image_patterns(self) -> list[str]:
        return [p.strip() for p in self.runner_images.split(",") if p.strip()]

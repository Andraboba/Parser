from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="COORD_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://coord:coord@localhost:5432/coord"
    redis_url: str = "redis://localhost:6379/0"
    # Таймаут проверки зависимостей в /health, секунды.
    health_timeout: float = 2.0

    # Параметры, которые агент получает при регистрации, секунды.
    heartbeat_interval: int = 5
    lease_ttl: int = 60
    # Доля общего лимита запросов на узел; динамическое деление — тикет 10.
    default_rps_share: float = 5.0
    protocol_version: int = 1

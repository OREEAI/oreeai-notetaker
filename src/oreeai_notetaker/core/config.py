from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    project_name: str = "oreeai-notetaker"
    environment: str = "local"
    debug: bool = True
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"

    database_url: str = "postgresql+asyncpg://oreeai:oreeai@localhost:5433/oreeai"
    db_echo: bool = False
    db_pool_size: int = 10
    db_max_overflow: int = 20

    redis_url: str = "redis://localhost:6380/0"
    cache_enabled: bool = True
    cache_prefix: str = "oreeai"
    cache_ttl_seconds: int = 300

    cors_origins: list[str] = []


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

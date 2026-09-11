from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,
    )

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

    api_key: str
    call_concurrency_limit: int = 3
    bot_image_tag: str = "local"
    bot_docker_network: str = "oreeai_internal"
    bot_profile: str = ""
    bot_max_record_duration: int = 10800
    audio_host_path: str = "/var/lib/oreeai/audio"
    webhook_http_timeout: int = 10
    webhook_max_attempts: int = 5
    webhook_timestamp_skew: int = 300

    cors_origins: list[str] = []


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

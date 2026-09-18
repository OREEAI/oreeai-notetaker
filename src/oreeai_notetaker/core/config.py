from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

TranscriptionProvider = Literal["deepgram", "stub"]


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

    # PR 6 — object storage (S3 API; provider = env change, e.g. R2,
    # Hetzner, Backblaze, MinIO). Optional so the API process starts
    # without them; validated when the adapter is built (runner startup).
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    s3_bucket: str | None = None
    s3_access_key_id: str | None = None
    s3_secret_access_key: str | None = None
    s3_sse: str = "AES256"
    s3_presign_ttl_seconds: int = 3600
    audio_retention_days: int = 0
    failed_audio_retention_days: int = 7

    # PR 7 — transcription. Provider is settled (Deepgram, nova-3 batch);
    # "stub" is the dev/staging stand-in (honest empty transcript, no
    # network). Optional so the API process starts without it: unset means
    # dev/staging falls back to stub with a loud warning, production
    # fail-fasts when the transcription service is built (runner startup).
    transcription_provider: TranscriptionProvider | None = None
    deepgram_api_key: str | None = None
    audio_max_bytes: int = 2147483648

    cors_origins: list[str] = []


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

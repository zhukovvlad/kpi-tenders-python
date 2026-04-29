from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # App
    app_env: Literal["local", "dev", "staging", "prod"] = "local"
    app_port: int = 8000
    log_level: str = "INFO"

    # Go internal API
    go_service_url: str = "http://localhost:8080"
    service_token: str = Field(..., min_length=1)

    # Go internal API (continued)
    go_client_timeout: float = 10.0

    # MinIO
    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = Field(..., min_length=1)
    minio_secret_key: str = Field(..., min_length=1)
    minio_bucket: str = "tenders"
    minio_use_ssl: bool = False

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"

    # PostgreSQL (direct access for AI/ML tables)
    database_url: str = Field(..., min_length=1)

    # Gemini
    gemini_api_key: str = ""
    gemini_light_model: str = "gemini-2.5-flash"
    gemini_heavy_model: str = "gemini-3.1-pro-preview"


@lru_cache
def get_settings() -> Settings:
    return Settings()

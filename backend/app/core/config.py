"""Конфигурация приложения.

Все значения читаются из переменных окружения / .env через pydantic-settings.
Хардкод URL-ов запрещён: внутри Docker используются имена сервисов
(postgres, redis, qdrant, ollama, reranker).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, SecretStr, computed_field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Типизированные настройки сервиса.

    Экземпляр создаётся один раз и кешируется (см. :func:`get_settings`).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    # ─────────────────────────────── APP ───────────────────────────────
    APP_NAME: str = "BR Document Assistant"
    APP_ENV: Literal["local", "production"] = "production"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    LOG_LEVEL: Literal["debug", "info", "warning", "error", "critical"] = "info"
    LOG_FORMAT: Literal["json", "console"] = "json"
    TZ: str = "Europe/Moscow"
    API_V1_PREFIX: str = "/api/v1"
    API_ROOT_PATH: str = ""
    UVICORN_WORKERS: int = 2

    # ─────────────────────────────── SECURITY ───────────────────────────────
    SECRET_KEY: SecretStr = Field(
        ...,
        description="Ключ подписи JWT. Сгенерировать: openssl rand -hex 32",
    )
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    JWT_ISSUER: str = "br-assistant"
    PASSWORD_MIN_LENGTH: int = 10
    CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["http://localhost"])
    RATE_LIMIT_DEFAULT: str = "60/minute"
    RATE_LIMIT_CHAT: str = "20/minute"
    RATE_LIMIT_LOGIN: str = "5/minute"
    BOOTSTRAP_ADMIN_EMAIL: str = "admin@cbr.local"
    BOOTSTRAP_ADMIN_PASSWORD: SecretStr = SecretStr("change_me")
    PII_REDACTION_ENABLED: bool = True

    # ─────────────────────────────── POSTGRES ───────────────────────────────
    POSTGRES_USER: str = "br_user"
    POSTGRES_PASSWORD: SecretStr = SecretStr("change_me")
    POSTGRES_DB: str = "br_assistant"
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    DATABASE_URL: str | None = None
    DATABASE_URL_SYNC: str | None = None
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_RECYCLE_SECONDS: int = 1800
    DB_ECHO: bool = False

    # ─────────────────────────────── REDIS ───────────────────────────────
    REDIS_PASSWORD: SecretStr = SecretStr("change_me")
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_URL: str | None = None
    CELERY_BROKER_URL: str | None = None
    CELERY_RESULT_BACKEND: str | None = None
    CELERY_CONCURRENCY: int = 2
    CACHE_TTL_SECONDS: int = 3600
    SESSION_TTL_SECONDS: int = 86400

    # ─────────────────────────────── QDRANT ───────────────────────────────
    QDRANT_HOST: str = "qdrant"
    QDRANT_PORT: int = 6333
    QDRANT_GRPC_PORT: int = 6334
    QDRANT_URL: str = "http://qdrant:6333"
    QDRANT_API_KEY: SecretStr = SecretStr("")
    QDRANT_COLLECTION: str = "br_regulations"
    QDRANT_PREFER_GRPC: bool = False
    VECTOR_SIZE: int = 1024
    HYBRID_SEARCH_ENABLED: bool = True

    # ─────────────────────────────── OLLAMA / LLM ───────────────────────────────
    OLLAMA_BASE_URL: str = "http://ollama:11434"
    OLLAMA_LLM_MODEL: str = "qwen2.5:7b-instruct"
    OLLAMA_FT_MODEL: str = "br-assistant"
    OLLAMA_MODEL_ACTIVE: str = "br-assistant"
    OLLAMA_EMBED_MODEL: str = "bge-m3"
    OLLAMA_SYNTH_MODEL: str = "qwen2.5:14b-instruct"
    OLLAMA_KEEP_ALIVE: str = "24h"
    LLM_TEMPERATURE: float = 0.1
    LLM_TOP_P: float = 0.9
    LLM_MAX_TOKENS: int = 1536
    LLM_CONTEXT_WINDOW: int = 8192
    LLM_TIMEOUT_SECONDS: int = 120
    LLM_MAX_RETRIES: int = 3
    LLM_RETRY_BACKOFF_BASE: float = 1.5
    LLM_FALLBACK_ENABLED: bool = True

    # ─────────────────────────────── RERANKER ───────────────────────────────
    RERANKER_ENABLED: bool = True
    RERANKER_URL: str = "http://reranker:7997"
    RERANKER_MODEL: str = "BAAI/bge-reranker-v2-m3"
    RERANKER_TOP_N: int = 5
    RERANKER_TIMEOUT_SECONDS: int = 30

    # ─────────────────────────────── RAG ───────────────────────────────
    CHUNK_SIZE: int = 900
    CHUNK_OVERLAP: int = 150
    RETRIEVER_TOP_K: int = 20
    RETRIEVER_SCORE_THRESHOLD: float = 0.35
    ANSWER_MIN_RELEVANCE: float = 0.45
    CRITIC_ENABLED: bool = True
    CRITIC_MAX_REGENERATIONS: int = 1
    MAX_HISTORY_MESSAGES: int = 10
    DATA_DIR: Path = Path("/app/data")
    INSTRUCTIONS_DIR: Path = Path("/app/data/instructions")
    TEMPLATES_DIR: Path = Path("/app/data/templates")
    ARTIFACTS_DIR: Path = Path("/app/artifacts")
    PDF_DROP_WATERMARK: bool = True
    PDF_WATERMARK_MIN_LIGHTNESS: float = 0.3
    MAX_UPLOAD_SIZE_MB: int = 50
    ALLOWED_UPLOAD_EXTENSIONS: list[str] = Field(
        default_factory=lambda: [".pdf", ".docx", ".txt", ".md", ".html"]
    )

    # ─────────────────────────────── OBSERVABILITY ───────────────────────────────
    METRICS_ENABLED: bool = True

    # ─────────────────────────── ВАЛИДАТОРЫ ───────────────────────────
    @field_validator("CORS_ORIGINS", "ALLOWED_UPLOAD_EXTENSIONS", mode="before")
    @classmethod
    def _split_csv(cls, value: Any) -> Any:
        """Разрешает задавать списки строкой через запятую в .env."""
        if isinstance(value, str):
            if value.strip().startswith("["):
                return value
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("SECRET_KEY")
    @classmethod
    def _validate_secret(cls, value: SecretStr) -> SecretStr:
        """Не даёт стартовать с дефолтным или слишком коротким ключом."""
        raw = value.get_secret_value()
        if len(raw) < 32:
            raise ValueError("SECRET_KEY должен содержать минимум 32 символа")
        if raw.startswith("CHANGE_ME"):
            raise ValueError("SECRET_KEY не заполнен: сгенерируйте openssl rand -hex 32")
        return value

    @model_validator(mode="after")
    def _build_dsn(self) -> Settings:
        """Собирает DSN-строки, если они не заданы в .env явно."""
        pg_pass = self.POSTGRES_PASSWORD.get_secret_value()
        redis_pass = self.REDIS_PASSWORD.get_secret_value()
        pg_tail = (
            f"{self.POSTGRES_USER}:{pg_pass}@"
            f"{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )
        redis_head = f"redis://:{redis_pass}@{self.REDIS_HOST}:{self.REDIS_PORT}"

        if not self.DATABASE_URL:
            self.DATABASE_URL = f"postgresql+asyncpg://{pg_tail}"
        if not self.DATABASE_URL_SYNC:
            self.DATABASE_URL_SYNC = f"postgresql+psycopg2://{pg_tail}"
        if not self.REDIS_URL:
            self.REDIS_URL = f"{redis_head}/0"
        if not self.CELERY_BROKER_URL:
            self.CELERY_BROKER_URL = f"{redis_head}/1"
        if not self.CELERY_RESULT_BACKEND:
            self.CELERY_RESULT_BACKEND = f"{redis_head}/2"
        return self

    @model_validator(mode="after")
    def _check_root_path(self) -> Settings:
        """Не даёт задать root_path, конфликтующий с префиксом API.

        Если прокси срезает префикс, маршруты обязаны быть объявлены уже без
        него, иначе ни один запрос не найдёт обработчик.
        """
        root = self.API_ROOT_PATH.rstrip("/")
        if root and self.API_V1_PREFIX.startswith(root):
            raise ValueError(
                "API_ROOT_PATH срезается прокси до сопоставления маршрута, "
                f"поэтому API_V1_PREFIX ({self.API_V1_PREFIX}) не должен "
                f"начинаться с него ({root})"
            )
        return self

    # ─────────────────────────── ВЫЧИСЛЯЕМЫЕ ───────────────────────────
    @computed_field  # type: ignore[prop-decorator]
    @property
    def max_upload_size_bytes(self) -> int:
        """Максимальный размер загружаемого файла в байтах."""
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        """True, если сервис работает в продакшн-режиме."""
        return self.APP_ENV == "production"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def qdrant_endpoint(self) -> str:
        """Базовый URL Qdrant с учётом выбранного транспорта."""
        if self.QDRANT_PREFER_GRPC:
            return f"{self.QDRANT_HOST}:{self.QDRANT_GRPC_PORT}"
        return self.QDRANT_URL

    def ensure_dirs(self) -> None:
        """Создаёт рабочие каталоги, если их нет (идемпотентно)."""
        for directory in (
            self.DATA_DIR,
            self.INSTRUCTIONS_DIR,
            self.TEMPLATES_DIR,
            self.ARTIFACTS_DIR,
        ):
            directory.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Возвращает singleton настроек.

    Используется как FastAPI-зависимость и в Celery-воркерах.

    Returns:
        Закешированный экземпляр :class:`Settings`.
    """
    return Settings()  # type: ignore[call-arg]


settings: Settings = get_settings()

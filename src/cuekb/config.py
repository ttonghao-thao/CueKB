from functools import lru_cache

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUEKB_", env_file=".env", extra="ignore")

    env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"
    backend: str = "memory"
    process_role: str = "api"
    database_url: str = "postgresql+psycopg://cuekb:cuekb@localhost:5432/cuekb"
    opensearch_url: str = "http://localhost:9200"
    opensearch_index_prefix: str = "cuekb"
    opensearch_timeout_ms: int = Field(default=500, ge=20, le=5000)
    keyword_candidates: int = Field(default=50, ge=1, le=500)
    vector_candidates: int = Field(default=50, ge=1, le=500)
    rerank_candidates: int = Field(default=30, ge=1, le=100)
    storage_path: str = "data/originals"
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    worker_poll_seconds: float = Field(default=1.0, ge=0.1, le=60)
    worker_lease_seconds: int = Field(default=300, ge=30, le=3600)
    worker_max_attempts: int = Field(default=3, ge=1, le=20)
    api_key_pepper: str = ""
    bootstrap_api_key: str = ""
    model_config_key: SecretStr = SecretStr("")
    vector_dimension: int = Field(default=1024, ge=1)
    search_deadline_ms: int = Field(default=900, ge=100, le=10000)
    model_timeout_ms: int = Field(default=400, ge=20, le=5000)
    worker_model_timeout_ms: int = Field(default=120000, ge=1000, le=600000)
    rerank_min_remaining_ms: int = Field(default=180, ge=10, le=5000)
    rerank_enabled: bool = True
    relation_candidates: int = Field(default=30, ge=1, le=100)
    relation_timeout_ms: int = Field(default=100, ge=10, le=1000)
    context_max_chars: int = Field(default=12000, ge=1200, le=50000)
    context_per_hit_chars: int = Field(default=4000, ge=1200, le=10000)
    context_max_chunks: int = Field(default=8, ge=1, le=20)
    exact_identifier_pattern: str = (
        r"^(?:[A-Za-z][A-Za-z0-9_.:/+-]*\d[A-Za-z0-9_.:/+-]*|[A-Z][A-Z0-9_./:-]{2,})$"
    )

    @model_validator(mode="after")
    def validate_production(self) -> "Settings":
        if self.backend not in {"memory", "production"}:
            raise ValueError("backend must be memory or production")
        if self.process_role not in {"api", "worker"}:
            raise ValueError("process_role must be api or worker")
        if self.backend == "production" and self.process_role == "api":
            if not self.api_key_pepper or len(self.api_key_pepper) < 32:
                raise ValueError(
                    "production requires CUEKB_API_KEY_PEPPER with at least 32 characters"
                )
            if not self.bootstrap_api_key or len(self.bootstrap_api_key) < 24:
                raise ValueError(
                    "production requires CUEKB_BOOTSTRAP_API_KEY with at least 24 characters"
                )
        if self.backend == "production" and len(self.model_config_key.get_secret_value()) < 32:
            raise ValueError(
                "production requires CUEKB_MODEL_CONFIG_KEY with at least 32 characters"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()

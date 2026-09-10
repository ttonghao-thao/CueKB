from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CUEKB_", env_file=".env", extra="ignore")

    env: str = "development"
    host: str = "127.0.0.1"
    port: int = 8080
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://cuekb:cuekb@localhost:5432/cuekb"
    opensearch_url: str = "http://localhost:9200"
    opensearch_index_prefix: str = "cuekb"
    default_top_k: int = Field(default=8, ge=1, le=20)
    max_top_k: int = Field(default=20, ge=1, le=100)
    keyword_candidates: int = Field(default=50, ge=1, le=500)
    vector_candidates: int = Field(default=50, ge=1, le=500)
    rerank_candidates: int = Field(default=30, ge=1, le=100)


@lru_cache
def get_settings() -> Settings:
    return Settings()

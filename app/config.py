from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Magic Stores"
    database_url: str = "sqlite:///./store_engine.db"
    secret_key: str = "YOUR_SECRET_KEY_HERE"


@lru_cache
def get_settings() -> Settings:
    return Settings()

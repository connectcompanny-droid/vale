from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./vale.db"
    fernet_key: str = ""

    anthropic_api_key: str = ""
    claude_model: str = "claude-sonnet-5-5"

    uazapi_base_url: str = ""
    uazapi_instance_token: str = ""
    webhook_secret: str = ""

    monitor_min_interval_minutes: int = 10
    monitor_max_concurrency: int = 2
    offer_ttl_minutes: int = 10
    headless: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()

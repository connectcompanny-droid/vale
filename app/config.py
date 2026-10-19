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

    # Speech-to-text for voice notes (OpenAI-compatible /audio/transcriptions endpoint).
    stt_api_url: str = "https://api.openai.com/v1/audio/transcriptions"
    stt_api_key: str = ""
    stt_model: str = "whisper-1"

    # Text-to-speech for voice replies: none | elevenlabs | openai (see app/voice/tts.py).
    tts_provider: str = "none"
    tts_api_url: str = "https://api.openai.com/v1/audio/speech"
    tts_api_key: str = ""
    tts_model: str = ""
    tts_voice_pt: str = ""
    tts_voice_es: str = ""
    tts_voice_en: str = ""
    # Longest reply sent as audio; longer ones go as text (voice notes > ~1 min get skipped).
    tts_max_chars: int = 600

    monitor_min_interval_minutes: int = 10
    monitor_max_concurrency: int = 2
    offer_ttl_minutes: int = 10
    headless: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./vale.db"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    # Dev/tests only. In production the schema is managed by Alembic (`alembic upgrade head`).
    auto_create_tables: bool = True
    fernet_key: str = ""

    # Job queue. "inline" = process in the API process (dev/tests). "redis" = arq workers (production).
    queue_mode: str = "inline"
    redis_url: str = "redis://localhost:6379/0"
    # Jobs handled concurrently by one worker process (conversations, documents, audio).
    worker_concurrency: int = 20
    # Simultaneous Claude calls per worker process (protects the API rate limit).
    llm_concurrency: int = 8
    # Unique id of this process; browser sessions are owned by the process that opened them.
    instance_id: str = ""
    # Optional token for /metrics (sent as ?token=...). Empty = metrics disabled.
    metrics_token: str = ""
    log_json: bool = False

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
    # Browsers open at the same time in one monitor process.
    monitor_max_concurrency: int = 2
    # GLOBAL politeness limit: minimum seconds between two visits to the government site, across
    # ALL monitor processes together. This, not the number of clients, sets the site traffic:
    # 30 s = at most 2 visits/min (2,880/day). Adding servers never raises it.
    site_min_gap_seconds: int = 30
    offer_ttl_minutes: int = 10
    headless: bool = True
    # "chrome" = real Google Chrome (current certificate root store, auto-updated). Empty = Playwright's
    # bundled Chromium, whose root store is frozen at its build date and can reject valid certificates.
    browser_channel: str = "auto"  # auto: Chrome on x86_64, bundled Chromium on ARM
    # Pause all checks for this long after an untrusted-certificate error (never bypass TLS).
    tls_pause_minutes: int = 30
    # Pause all checks for this long when the site's firewall rejects us ("Request Rejected").
    # Vale never tries to evade it (no proxy rotation, no fingerprint spoofing).
    blocked_pause_minutes: int = 360


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.instance_id:
        import os
        import socket

        s.instance_id = f"{socket.gethostname()}-{os.getpid()}"
    return s

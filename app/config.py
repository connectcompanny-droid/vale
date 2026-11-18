from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite+aiosqlite:///./vale.db"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    # Dev/tests only. In production the schema is managed by Alembic (`alembic upgrade head`).
    auto_create_tables: bool = True
    fernet_key: str = ""

    @field_validator("database_url")
    @classmethod
    def _async_driver(cls, v: str) -> str:
        """Hosted Postgres (Railway, Supabase, Render...) hands out postgres:// or postgresql://
        URLs; the app needs the asyncpg driver."""
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+asyncpg://" + v[len(prefix):]
        return v

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

    # WhatsApp provider: "uazapi" (paid) or "evolution" (free: Evolution API or Vale's own bridge in bridge/whatsapp).
    whatsapp_provider: str = "uazapi"
    evolution_base_url: str = "http://127.0.0.1:8081"
    evolution_api_key: str = ""
    evolution_instance: str = "vale"

    # WhatsApp: answer new people only when their message is about Vale's services (the number may
    # also get personal or unrelated messages). The website chat always answers.
    reply_only_relevant: bool = True
    classifier_model: str = "claude-haiku-5-5"
    # Language of the fixed messages until the client shows another one.
    default_lang: str = "es"

    # Automatic checks of the government sites by the robot. Off while a site refuses automated
    # access: Vale then works in assisted mode (client books with a kit, or an operator books).
    auto_checks: bool = True
    # Who books when the robot does not: "self" (Vale prepares, client books) or "operator"
    # (a person on Vale's side books from the operator panel). Clients can ask for the other one.
    default_booking_mode: str = "self"
    operator_enabled: bool = True
    # WhatsApp number (digits) told when a client confirms a slot the operator offered.
    operator_phone: str = ""
    self_nudge_days: int = 14

    # Appointment systems Vale books on by itself: icp (extranjería), aeat (identity appointment for
    # the digital certificate). Enable one only after validating it on the real site.
    enabled_sites: str = "icp"

    # Provinces open to clients (comma separated keys from the catalog). Start with the validated ones.
    active_provinces: str = "granada"

    # Website chat (second channel). Served by the API at /chat.
    web_chat_enabled: bool = True
    web_history_days: int = 30
    # Vale's WhatsApp number, digits only with country code: shown on the chat page as the other way in.
    whatsapp_number: str = ""

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
    # Local troubleshooting: save a screenshot of the government page when a check fails BEFORE any
    # personal data is typed (province and trámite steps only). Off on servers.
    debug_snapshots: bool = False
    # "chrome" = real Google Chrome (current certificate root store, auto-updated). Empty = Playwright's
    # bundled Chromium, whose root store is frozen at its build date and can reject valid certificates.
    browser_channel: str = "auto"  # auto: Chrome on x86_64, bundled Chromium on ARM
    # Pause all checks for this long after an untrusted-certificate error (never bypass TLS).
    tls_pause_minutes: int = 30
    # Pause all checks for this long when the site's firewall rejects us ("Request Rejected").
    # Vale never tries to evade it (no proxy rotation, no fingerprint spoofing).
    blocked_pause_minutes: int = 360

    # Public address of this API (e.g. https://vale.up.railway.app); used for payment return pages.
    public_base_url: str = ""
    # Legal texts linked from the consent message (shown only when set).
    terms_url: str = ""
    privacy_url: str = ""
    # Operator dashboard /admin/summary?token=... (disabled when empty).
    admin_token: str = ""

    # Billing with Stripe Checkout. Empty STRIPE_SECRET_KEY = free beta: no payment step at all.
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    activation_fee_cents: int = 990   # charged before monitoring starts
    success_fee_cents: int = 2900     # charged after the cita is booked
    founder_slots: int = 50           # first N paying clients get the founder discount
    founder_discount_pct: int = 50
    # Day-before reminder is sent from this local hour on (Europe/Madrid).
    reminder_hour: int = 10


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if not s.instance_id:
        import os
        import socket

        s.instance_id = f"{socket.gethostname()}-{os.getpid()}"
    return s

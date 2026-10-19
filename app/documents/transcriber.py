"""Voice-note transcription through any OpenAI-compatible speech-to-text endpoint
(OpenAI, Groq Whisper, a self-hosted faster-whisper server...). The audio is sent once and
never stored."""

import httpx

from app.config import get_settings

EXT = {"audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/aac": "aac", "audio/wav": "wav", "audio/webm": "webm"}


class TranscriptionError(Exception):
    pass


async def transcribe(audio: bytes, mime: str, lang: str | None = None) -> str:
    s = get_settings()
    if not s.stt_api_key:
        raise TranscriptionError("stt_not_configured")
    base_mime = mime.split(";")[0].strip()
    files = {"file": (f"voice.{EXT.get(base_mime, 'ogg')}", audio, base_mime or "audio/ogg")}
    data = {"model": s.stt_model, "response_format": "json"}
    if lang:
        data["language"] = lang
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(s.stt_api_url, headers={"Authorization": f"Bearer {s.stt_api_key}"}, files=files, data=data)
    if r.status_code >= 400:
        raise TranscriptionError(f"stt_http_{r.status_code}")
    text = (r.json().get("text") or "").strip()
    if not text:
        raise TranscriptionError("empty_transcript")
    return text

"""Text-to-speech, pluggable. Vale can answer with voice notes; which engine produces them is
configuration, so ElevenLabs, OpenAI or any other provider can be dropped in later.

To add a provider: subclass Synthesizer, implement `synthesize`, and register it in
`get_synthesizer`. Set TTS_PROVIDER in .env. With TTS_PROVIDER=none (default) Vale only writes.
"""

import logging
from abc import ABC, abstractmethod

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


class SynthesisError(Exception):
    pass


class Synthesizer(ABC):
    enabled = True

    @abstractmethod
    async def synthesize(self, text: str, lang: str) -> tuple[bytes, str]:
        """Return (audio bytes, mime type) for `text` spoken in `lang` (pt/es/en)."""


class NoSynthesizer(Synthesizer):
    enabled = False

    async def synthesize(self, text: str, lang: str) -> tuple[bytes, str]:
        raise SynthesisError("tts_disabled")


class ElevenLabsSynthesizer(Synthesizer):
    """ElevenLabs text-to-speech. Verify model id and voice ids in your ElevenLabs account."""

    def __init__(self):
        s = get_settings()
        self.key, self.model = s.tts_api_key, s.tts_model or "eleven_multilingual_v2"
        self.voices = {"pt": s.tts_voice_pt, "es": s.tts_voice_es, "en": s.tts_voice_en}

    async def synthesize(self, text: str, lang: str) -> tuple[bytes, str]:
        voice = self.voices.get(lang) or self.voices.get("pt")
        if not (self.key and voice):
            raise SynthesisError("elevenlabs_not_configured")
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                headers={"xi-api-key": self.key, "accept": "audio/mpeg"},
                json={"text": text, "model_id": self.model},
            )
        if r.status_code >= 400:
            raise SynthesisError(f"elevenlabs_http_{r.status_code}")
        return r.content, "audio/mpeg"


class OpenAICompatibleSynthesizer(Synthesizer):
    """Any OpenAI-style /audio/speech endpoint (OpenAI, or self-hosted servers that mimic it)."""

    def __init__(self):
        s = get_settings()
        self.url, self.key, self.model = s.tts_api_url, s.tts_api_key, s.tts_model or "gpt-4o-mini-tts"
        self.voices = {"pt": s.tts_voice_pt, "es": s.tts_voice_es, "en": s.tts_voice_en}

    async def synthesize(self, text: str, lang: str) -> tuple[bytes, str]:
        if not (self.url and self.key):
            raise SynthesisError("openai_tts_not_configured")
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.post(
                self.url, headers={"Authorization": f"Bearer {self.key}"},
                json={"model": self.model, "voice": self.voices.get(lang) or "alloy", "input": text, "response_format": "mp3"},
            )
        if r.status_code >= 400:
            raise SynthesisError(f"tts_http_{r.status_code}")
        return r.content, "audio/mpeg"


_synth: Synthesizer | None = None


def get_synthesizer() -> Synthesizer:
    global _synth
    if _synth is None:
        provider = get_settings().tts_provider.lower()
        _synth = {"elevenlabs": ElevenLabsSynthesizer, "openai": OpenAICompatibleSynthesizer}.get(provider, NoSynthesizer)()
    return _synth


def set_synthesizer(synth: Synthesizer) -> None:
    global _synth
    _synth = synth

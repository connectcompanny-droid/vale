from unittest.mock import patch

import pytest
from sqlalchemy import select

from app.db import session_scope
from app.models import User
from app.router import handle
from app.voice.tts import SynthesisError, Synthesizer, set_synthesizer


class FakeSynth(Synthesizer):
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    async def synthesize(self, text, lang):
        self.calls.append((text, lang))
        if self.fail:
            raise SynthesisError("boom")
        return b"mp3data", "audio/mpeg"


async def _ready(make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))


async def _voice(channel, make_msg, transcript="qual o status?"):
    channel.media = (b"ogg", "audio/ogg")
    with patch("app.router.transcribe", return_value=transcript):
        await handle(make_msg(media=True, kind="audio"))


async def _set_mode(mode):
    async with session_scope() as s:
        (await s.scalar(select(User))).reply_mode = mode


@pytest.mark.asyncio
async def test_without_tts_everything_stays_text(channel, make_msg):
    await _ready(make_msg)
    await _voice(channel, make_msg)
    assert channel.last[1].startswith("agent:")


@pytest.mark.asyncio
async def test_mirror_answers_voice_with_voice_and_text_with_text(channel, make_msg):
    synth = FakeSynth()
    set_synthesizer(synth)
    await _ready(make_msg)
    await _voice(channel, make_msg)
    assert channel.last[1].startswith("<audio audio/mpeg")
    assert synth.calls[-1][1] == "pt"
    await handle(make_msg("e agora?"))
    assert channel.last[1] == "agent:e agora?"


@pytest.mark.asyncio
async def test_text_mode_never_speaks_and_audio_mode_always_does(channel, make_msg):
    set_synthesizer(FakeSynth())
    await _ready(make_msg)
    await _set_mode("text")
    await _voice(channel, make_msg)
    assert channel.last[1].startswith("agent:")
    await _set_mode("audio")
    await handle(make_msg("oi de novo"))
    assert channel.last[1].startswith("<audio")


@pytest.mark.asyncio
async def test_tts_failure_falls_back_to_text(channel, make_msg):
    set_synthesizer(FakeSynth(fail=True))
    await _ready(make_msg)
    await _voice(channel, make_msg)
    assert channel.last[1].startswith("agent:")


@pytest.mark.asyncio
async def test_readback_stays_text_with_buttons_even_after_voice(channel, make_msg):
    set_synthesizer(FakeSynth())
    await _ready(make_msg)
    await _voice(channel, make_msg, "DATA full_name=JOAO PEREIRA;document_number=GH1")
    _, text, buttons = channel.last
    assert "JOAO PEREIRA" in text and buttons  # exact spelling must be read, not heard

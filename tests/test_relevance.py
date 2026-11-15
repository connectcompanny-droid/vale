import pytest
from sqlalchemy import func, select

from app import router as vale_router
from app.agent import relevance
from app.config import get_settings
from app.db import session_scope
from app.models import User


@pytest.fixture
def gate(monkeypatch):
    monkeypatch.setattr(get_settings(), "reply_only_relevant", True)
    monkeypatch.setattr(get_settings(), "default_lang", "es")
    asked = []

    async def fake(text):
        asked.append(text)
        return "papeles" in text.lower()

    monkeypatch.setattr(relevance, "is_about_services", fake)
    return asked


def test_keywords_are_specific():
    assert relevance.keyword_hit("Hola, necesito cita previa para el NIE")
    assert relevance.keyword_hit("preciso marcar a toma de huellas")
    for unrelated in ("Vale, nos vemos mañana", "tengo una cita con mi novia", "já visto, obrigado", "hola!"):
        assert not relevance.keyword_hit(unrelated), unrelated


async def _users() -> int:
    async with session_scope() as s:
        return await s.scalar(select(func.count()).select_from(User))


@pytest.mark.asyncio
async def test_unrelated_newcomers_get_no_reply_and_leave_no_trace(channel, make_msg, gate):
    await vale_router.handle(make_msg("Oye, ¿cenamos el sábado?"))
    assert channel.sent == [] and await _users() == 0
    await vale_router.handle(make_msg("Hola, tengo dudas con mis papeles de residencia"))
    assert "Acepto" in [b.label for b in channel.last[2]] and await _users() == 1  # Spanish by default


@pytest.mark.asyncio
async def test_known_people_and_the_website_chat_skip_the_gate(channel, make_msg, gate):
    await vale_router.handle(make_msg("Hola, tengo dudas con mis papeles"))
    asked = len(gate)
    await vale_router.handle(make_msg("cualquier cosa"))  # already in a conversation with Vale
    assert len(gate) == asked
    web = make_msg("hola")
    web.phone = "web:" + "b" * 28
    await vale_router.handle(web)
    assert len(gate) == asked and channel.sent


@pytest.mark.asyncio
async def test_agent_skip_sends_nothing(channel):
    from app.router import _deliver

    async with session_scope() as s:
        await _deliver(s, None, "34600000001", "[SKIP]", client_spoke=True)
    assert channel.sent == []


@pytest.mark.asyncio
async def test_classifier_failure_stays_silent(monkeypatch):
    class Broken:
        @property
        def messages(self):
            raise RuntimeError("api down")

    monkeypatch.setattr(relevance.llm, "client", lambda: Broken())
    assert await relevance.is_about_services("hola qué tal") is False
    assert await relevance.is_about_services("necesito el NIE") is True  # keyword, no API needed


def test_spanish_is_the_default_language(monkeypatch):
    from app.i18n import guess_lang, t

    monkeypatch.setattr(get_settings(), "default_lang", "es")
    assert guess_lang("?") == "es" and guess_lang("Olá, preciso de ajuda") == "pt" and guess_lang("I need help please") == "en"
    assert t("consent_yes", "xx") == "Acepto"

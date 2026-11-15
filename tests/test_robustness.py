"""Things that must hold when WhatsApp or the monitor process fail at a bad moment."""

from datetime import datetime, timedelta, timezone

import pytest

from app.citas import service
from app.citas.monitor import Monitor
from app.citas.providers.base import CheckResult, Slot
from app.db import session_scope
from app.models import CitaRequest, OfferState, RequestState, SlotOffer

PROFILE = {"full_name": "MARIA SILVA", "document_number": "FX123456", "nationality": "BRASIL"}


async def _monitoring_request(phone="34600000001") -> int:
    async with session_scope() as s:
        user, _ = await service.get_or_create_user(s, phone, None, "pt")
        await service.give_consent(s, user)
        doc = await service.save_pending_data(s, user, PROFILE, source="text")
        await service.confirm_pending(s, user, doc.id)
        return (await service.create_request(s, user, "granada", "nie")).id


async def _approved_offer(req_id: int, hold_key: str | None, expired: bool = False) -> int:
    async with session_scope() as s:
        req = await s.get(CitaRequest, req_id)
        offer = await service.create_offer(s, req, "CNP Granada", "20/11/2026 10:00", {"hold_key": hold_key})
        offer.state, req.state = OfferState.approved, RequestState.booking
        if expired:
            offer.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        return offer.id


class Down(Exception):
    pass


def _break_whatsapp(channel):
    async def fail(*a, **k):
        raise Down("bridge offline")
    channel.send_text = channel.send_buttons = fail


@pytest.mark.asyncio
async def test_a_booked_cita_is_saved_even_if_the_message_fails(channel, provider):
    req_id = await _monitoring_request()
    await _approved_offer(req_id, "k1")
    provider.owns = lambda key: True
    _break_whatsapp(channel)
    await Monitor(provider).book_approved()
    async with session_scope() as s:
        req = await s.get(CitaRequest, req_id)
        assert req.state == RequestState.booked and req.note == "Justificante 123"


@pytest.mark.asyncio
async def test_booking_orphaned_by_a_restart_goes_back_to_the_queue(channel, provider):
    req_id = await _monitoring_request()
    offer_id = await _approved_offer(req_id, "old-process:1", expired=True)
    provider.owns = lambda key: False  # the browser session died with the old process
    await Monitor(provider).book_approved()
    async with session_scope() as s:
        assert (await s.get(CitaRequest, req_id)).state == RequestState.monitoring
        assert (await s.get(SlotOffer, offer_id)).state == OfferState.expired
    assert "expirou" in channel.last[1]


@pytest.mark.asyncio
async def test_booking_in_another_process_is_left_alone_while_the_offer_is_valid(channel, provider):
    req_id = await _monitoring_request()
    await _approved_offer(req_id, "other-process:1")
    provider.owns = lambda key: False
    await Monitor(provider).book_approved()
    async with session_scope() as s:
        assert (await s.get(CitaRequest, req_id)).state == RequestState.booking
    assert channel.sent == []


@pytest.mark.asyncio
async def test_expired_offers_are_closed_even_if_the_message_fails(channel, provider):
    req_id = await _monitoring_request()
    async with session_scope() as s:
        req = await s.get(CitaRequest, req_id)
        offer = await service.create_offer(s, req, "CNP Granada", "20/11/2026 10:00", {"hold_key": "k"})
        offer.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        offer_id = offer.id
    _break_whatsapp(channel)
    await Monitor(provider).tick()  # must not raise
    async with session_scope() as s:
        assert (await s.get(SlotOffer, offer_id)).state == OfferState.expired


@pytest.mark.asyncio
async def test_an_offer_that_cannot_be_delivered_is_undone_and_the_slot_freed(channel, provider):
    req_id = await _monitoring_request()
    provider.next_check = CheckResult("slots", slots=[Slot("CNP Granada", "20/11/2026 10:00", {})], hold_key="k9")
    _break_whatsapp(channel)
    await Monitor(provider).tick()  # must not raise
    assert provider.released == ["k9"]
    async with session_scope() as s:
        req = await s.get(CitaRequest, req_id, populate_existing=True)
        assert req.state == RequestState.monitoring
        assert await service.latest_pending_offer_id(s, req.user_id) is None


@pytest.mark.asyncio
async def test_language_is_corrected_before_consent(channel, make_msg):
    from app.models import User
    from app.router import handle
    from sqlalchemy import select

    await handle(make_msg("?"))  # no sign: starts in Portuguese
    assert "Você aceita" in channel.last[1]
    await handle(make_msg("Hola, necesito una cita"))
    assert "Acepto" in channel.last[2][0].label
    async with session_scope() as s:
        assert (await s.scalar(select(User))).lang == "es"


@pytest.mark.asyncio
async def test_digital_certificate_interest_is_recorded_once_and_shown_to_the_operator(channel, monkeypatch):
    from fastapi.testclient import TestClient

    from app.agent.orchestrator import TOOLS, Agent
    from app.agent.prompts import SYSTEM_PROMPT
    from app.config import get_settings
    from app.models import User

    assert "sede.fnmt.gob.es" in SYSTEM_PROMPT and any(t["name"] == "register_interest" for t in TOOLS)
    async with session_scope() as s:
        user, _ = await service.get_or_create_user(s, "34600000001", None, "pt")
        agent = Agent(client=object())
        for _ in range(2):
            assert await agent._run_tool(s, user, "register_interest", {"topic": "digital_certificate"}, None, None) == {"noted": True}
        assert "error" in await agent._run_tool(s, user, "register_interest", {"topic": "other"}, None, None)
    monkeypatch.setattr(get_settings(), "admin_token", "adm")
    from app.main import app

    with TestClient(app) as client:
        assert client.get("/admin/summary?token=adm").json()["digital_certificate_interest"] == 1
        assert "certificado digital" in client.get("/admin?token=adm").text

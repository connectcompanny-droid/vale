"""Assisted mode: the robot never touches the site; the client books with a kit, or an operator books."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import assisted
from app.citas import service
from app.citas.monitor import Monitor
from app.config import get_settings
from app.db import session_scope
from app.models import CitaRequest, OfferState, RequestState, SlotOffer, User

PROFILE = {"full_name": "Maria Silva", "document_number": "FX123456", "nationality": "Brasil"}


@pytest.fixture
def assisted_on(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "auto_checks", False)
    monkeypatch.setattr(s, "admin_token", "adm")
    monkeypatch.setattr(s, "operator_phone", "34699000000")


async def _ready(phone="34600000001", mode=None):
    async with session_scope() as s:
        user, _ = await service.get_or_create_user(s, phone, None, "es")
        await service.give_consent(s, user)
        doc = await service.save_pending_data(s, user, PROFILE, source="text")
        await service.confirm_pending(s, user, doc.id)
        req = await service.create_request(s, user, "granada", "nie")
        if mode:
            req.mode = mode
        return req.id


@pytest.mark.asyncio
async def test_robot_stays_away_and_self_mode_gets_a_kit_then_a_daily_nudge(channel, provider, assisted_on):
    req_id = await _ready()
    calls = []
    orig = provider.check

    async def spy(*a, **k):
        calls.append(a)
        return await orig(*a, **k)

    provider.check = spy
    m = Monitor(provider)
    await m.tick()
    assert calls == []  # the robot never visits the site in assisted mode
    kit = channel.last[1]
    assert "icp.administracionelectronica.gob.es" in kit and "ASIGNACIÓN NIE" in kit and "FX123456" in kit and "MARIA SILVA" in kit
    sent = len(channel.sent)
    await m.tick()
    assert len(channel.sent) == sent  # the kit goes once
    async with session_scope() as s:
        r = await s.get(CitaRequest, req_id)
        r.kit_sent_at = datetime.now(timezone.utc) - timedelta(days=1)
    madrid_noon = datetime.now(timezone.utc).replace(hour=11, minute=0)
    assert await assisted.send_nudges(madrid_noon) == 1 and "Asignación de NIE" in channel.last[1]
    assert await assisted.send_nudges(madrid_noon) == 0


@pytest.mark.asyncio
async def test_client_reports_the_booking_and_gets_the_reminder_flow(channel, assisted_on):
    from app.agent.orchestrator import Agent

    req_id = await _ready()
    async with session_scope() as s:
        user = await s.get(User, (await s.get(CitaRequest, req_id)).user_id)
        agent = Agent(client=object())
        status = await agent._run_tool(s, user, "get_my_status", {}, None, None)
        assert status["requests"][0]["who_books"] == "self"
        kit = await agent._run_tool(s, user, "resend_booking_kit", {"request_id": req_id}, None, None)
        assert "FX123456" in kit["kit"]
        out = await agent._run_tool(s, user, "report_booked_by_client",
                                    {"request_id": req_id, "starts_at": "10/12/2030 09:30", "office": "CNP Granada"}, None, None)
        assert out["recorded"] == req_id
        assert (await s.get(CitaRequest, req_id)).state == RequestState.booked


@pytest.mark.asyncio
async def test_operator_offers_client_confirms_operator_books(channel, provider, assisted_on, make_msg):
    from app.router import handle

    req_id = await _ready(mode="operator")
    await Monitor(provider).tick()
    assert "nuestro equipo se encarga" in channel.last[1]
    from app.main import app

    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        assert c.get("/admin/operador?token=adm").status_code == 200
        assert c.get("/admin/operador?token=adm", headers={"x-forwarded-for": "1.2.3.4"}).status_code == 404  # never via tunnel
        assert c.get("/admin/operador?token=bad").status_code == 404
        page = c.get("/admin/operador?token=adm").text
        assert "FX123456" in page and "Proponer al cliente" in page
        r = c.post(f"/admin/operador/{req_id}/offer", data={"token": "adm", "office": "CNP Granada", "starts_at": "20/12/2030 10:00"},
                   follow_redirects=False)
        assert r.status_code == 303 and "Hay hueco" in channel.last[1]
        offer_button = channel.last[2][0].id
        # booking before the client says yes is impossible
        assert c.post(f"/admin/operador/{req_id}/booked", data={"token": "adm", "reference": "X"}).status_code == 409
    msg = make_msg(button=offer_button)
    msg.phone = "34600000001"
    await handle(msg)
    assert any("nuestro equipo la está reservando" in txt.lower() for p, txt, _ in channel.sent if p == "34600000001")
    assert any(p == "34699000000" and "confirmó" in txt for p, txt, _ in channel.sent)  # operator told
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        assert "CONFIRMÓ" in c.get("/admin/operador?token=adm").text
        assert c.post(f"/admin/operador/{req_id}/booked", data={"token": "adm", "reference": "JUST-1"}, follow_redirects=False).status_code == 303
    assert "Reservado" in channel.last[1] and "JUST-1" in channel.last[1]
    async with session_scope() as s:
        assert (await s.get(CitaRequest, req_id)).state == RequestState.booked


@pytest.mark.asyncio
async def test_operator_can_release_a_slot_that_vanished(channel, assisted_on):
    req_id = await _ready(mode="operator")
    async with session_scope() as s:
        await service.operator_offer(s, req_id, "CNP Granada", "20/12/2030 10:00")
        req = await service.operator_slot_gone(s, req_id)
        assert req.state == RequestState.monitoring

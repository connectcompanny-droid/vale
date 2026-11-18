"""Copilot API: local only, token in a header, and the same rules as the panel (no booking before the yes)."""

import pytest
from fastapi.testclient import TestClient

from app.citas import service
from app.config import get_settings
from app.db import session_scope
from app.models import CitaRequest, RequestState

PROFILE = {"full_name": "Ana Souza", "document_number": "FX9", "nationality": "Brasil", "nie": "Y1234567X"}


@pytest.fixture
def operator(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "auto_checks", False)
    monkeypatch.setattr(s, "admin_token", "adm")
    monkeypatch.setattr(s, "default_booking_mode", "operator")


async def _ready(phone="34600000011"):
    async with session_scope() as s:
        user, _ = await service.get_or_create_user(s, phone, None, "es")
        await service.give_consent(s, user)
        doc = await service.save_pending_data(s, user, PROFILE, source="text")
        await service.confirm_pending(s, user, doc.id)
        return (await service.create_request(s, user, "granada", "nie")).id


H = {"x-vale-token": "adm"}


@pytest.mark.asyncio
async def test_copilot_flow(channel, operator, make_msg):
    from app.main import app
    from app.router import handle

    req_id = await _ready()
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        assert c.get("/admin/copilot/queue").status_code == 404
        assert c.get("/admin/copilot/queue", headers={**H, "x-forwarded-for": "8.8.8.8"}).status_code == 404
        q = c.get("/admin/copilot/queue", headers=H).json()
        item = q["items"][0]
        assert item["id"] == req_id and item["client"]["doc_type"] == "nie" and item["client"]["number"] == "Y1234567X"
        assert item["match"] == ["ASIGNACION NIE"] and item["province"] == "Granada"
        assert c.post("/admin/copilot/checked", headers=H, json={"tramite_code": "nie"}).json()["marked"] == 1
        assert c.post(f"/admin/copilot/{req_id}/booked", headers=H, json={"reference": "X"}).status_code == 409
        assert c.post(f"/admin/copilot/{req_id}/offer", headers=H, json={"office": "CNP Granada", "starts_at": "20/12/2030 10:00"}).json() == {"ok": True}
        button = channel.last[2][0].id
    msg = make_msg(button=button)
    msg.phone = "34600000011"
    await handle(msg)
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        q = c.get("/admin/copilot/queue", headers=H).json()
        assert q["items"][0]["state"] == "booking" and q["items"][0]["offer"]["starts_at"] == "20/12/2030 10:00"
        assert c.post(f"/admin/copilot/{req_id}/booked", headers=H, json={"reference": "JUST99"}).json() == {"ok": True}
        hints = c.get("/admin/copilot/hints", headers=H).json()
        assert hints["slots_found"] == 1 and hints["enough_data"] is False
    assert "JUST99" in channel.last[1]
    async with session_scope() as s:
        assert (await s.get(CitaRequest, req_id)).state == RequestState.booked


@pytest.mark.asyncio
async def test_certificate_appointment_goes_to_the_operator_in_assisted_mode(channel, operator, provider):
    from app.citas.monitor import Monitor
    from app.main import app

    async with session_scope() as s:
        user, _ = await service.get_or_create_user(s, "34600000022", None, "es")
        await service.give_consent(s, user)
        doc = await service.save_pending_data(s, user, PROFILE, source="text")
        await service.confirm_pending(s, user, doc.id)
        req = await service.create_request(s, user, "granada", "acreditacion_certificado")
        assert req.state == RequestState.monitoring and req.mode == "operator"
    await Monitor(provider).tick()
    assert "nuestro equipo se encarga" in channel.last[1]
    with TestClient(app, client=("127.0.0.1", 5000)) as c:
        item = c.get("/admin/copilot/queue", headers=H).json()["items"][0]
        assert item["site"] == "aeat" and item["client"]["nie"] == "Y1234567X"

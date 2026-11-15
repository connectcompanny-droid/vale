import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import billing, notifications
from app.citas import service
from app.citas.monitor import Monitor
from app.config import get_settings
from app.db import session_scope
from app.models import CitaRequest, OfferState, RequestState, SlotOffer, User

PROFILE = {"full_name": "MARIA SILVA", "document_number": "FX123456", "nationality": "BRASIL"}


class FakeStripe(billing.StripeClient):
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def create_checkout(self, *, amount_cents, name, request_id, kind, lang):
        if self.fail:
            raise RuntimeError("stripe down")
        self.calls.append((kind, amount_cents, request_id))
        return f"https://checkout.test/{kind}/{request_id}"


@pytest.fixture
def billing_on(monkeypatch):
    cfg = get_settings()
    monkeypatch.setattr(cfg, "stripe_secret_key", "sk_test_x")
    monkeypatch.setattr(cfg, "stripe_webhook_secret", "whsec_test")
    fake = FakeStripe()
    billing.set_client(fake)
    yield fake
    billing.set_client(None)


async def _ready_user(phone="34600000001", lang="pt") -> int:
    async with session_scope() as s:
        user, _ = await service.get_or_create_user(s, phone, None, lang)
        await service.give_consent(s, user)
        doc = await service.save_pending_data(s, user, PROFILE, source="text")
        await service.confirm_pending(s, user, doc.id)
        return user.id


async def _new_request(user_id: int, tramite="nie") -> CitaRequest:
    async with session_scope() as s:
        user = await s.get(User, user_id)
        return await service.create_request(s, user, "granada", tramite)


def _event(kind: str, req_id: int, event_id="evt_1", paid=True) -> dict:
    return {"id": event_id, "type": "checkout.session.completed",
            "data": {"object": {"payment_status": "paid" if paid else "unpaid", "amount_total": 990,
                                "metadata": {"kind": kind, "request_id": str(req_id)}}}}


def _sign(body: bytes, secret="whsec_test", ts=None) -> str:
    ts = ts or int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


# ---------- free beta ----------

@pytest.mark.asyncio
async def test_without_stripe_key_requests_go_straight_to_monitoring(channel):
    uid = await _ready_user()
    req = await _new_request(uid)
    assert req.state == RequestState.monitoring
    assert await billing.send_pending_links() == 0


# ---------- activation ----------

@pytest.mark.asyncio
async def test_activation_link_then_payment_starts_monitoring(channel, provider, billing_on):
    uid = await _ready_user()
    req = await _new_request(uid)
    assert req.state == RequestState.awaiting_payment

    await Monitor(provider).tick()
    phone, text, _ = channel.last
    assert "https://checkout.test/activation/" in text
    assert "€4,95" in text and "fundador" in text  # first clients get 50% off
    assert billing_on.calls == [("activation", 495, req.id)]

    await Monitor(provider).tick()
    assert len(billing_on.calls) == 1  # never sent twice

    assert await billing.handle_event(_event("activation", req.id)) == "activation_paid"
    assert "Pagamento confirmado" in channel.last[1]
    async with session_scope() as s:
        r = await s.get(CitaRequest, req.id)
        assert r.state == RequestState.monitoring and r.activation_paid_at is not None
    assert await billing.handle_event(_event("activation", req.id)) == "duplicate"


@pytest.mark.asyncio
async def test_founder_discount_ends_after_the_slots(channel, provider, billing_on, monkeypatch):
    monkeypatch.setattr(get_settings(), "founder_slots", 1)
    first = await _new_request(await _ready_user("34600000001"))
    second = await _new_request(await _ready_user("34600000002"))
    await billing.send_pending_links()
    amounts = {rid: cents for _, cents, rid in billing_on.calls}
    assert amounts == {first.id: 495, second.id: 990}


@pytest.mark.asyncio
async def test_stripe_failure_is_retried_on_the_next_tick(channel, billing_on):
    req = await _new_request(await _ready_user())
    billing_on.fail = True
    assert await billing.send_pending_links() == 0
    billing_on.fail = False
    assert await billing.send_pending_links() == 1
    async with session_scope() as s:
        assert (await s.get(CitaRequest, req.id)).activation_link_url.startswith("https://")


@pytest.mark.asyncio
async def test_status_returns_the_payment_link(channel, billing_on):
    from app.agent.orchestrator import Agent

    uid = await _ready_user()
    req = await _new_request(uid)
    await billing.send_pending_links()
    async with session_scope() as s:
        user = await s.get(User, uid)
        out = await Agent(client=object())._run_tool(s, user, "get_my_status", {}, None, None)
    assert out["requests"][0]["payment_link"] == f"https://checkout.test/activation/{req.id}"


# ---------- success fee ----------

@pytest.mark.asyncio
async def test_success_fee_link_after_booking_and_payment(channel, billing_on):
    req = await _new_request(await _ready_user())
    await billing.send_pending_links()  # activation link (founder price)
    await billing.handle_event(_event("activation", req.id, "evt_a"))
    async with session_scope() as s:
        r = await s.get(CitaRequest, req.id)
        await service.mark_booked(s, r, "Justificante 1")
    await billing.send_pending_links()
    assert f"https://checkout.test/success/{req.id}" in channel.last[1]
    assert ("success", 1450, req.id) in billing_on.calls  # founder price kept on the success fee
    assert await billing.handle_event(_event("success", req.id, "evt_s")) == "success_paid"
    async with session_scope() as s:
        assert (await s.get(CitaRequest, req.id)).success_paid_at is not None


# ---------- webhook ----------

def test_signature_checks():
    body = b'{"x":1}'
    billing.verify_signature(body, _sign(body), "whsec_test")
    for header, secret in ((_sign(body, "other"), "whsec_test"), (_sign(body, ts=int(time.time()) - 3600), "whsec_test"),
                           ("garbage", "whsec_test"), ("t=abc,v1=00", "whsec_test"), (_sign(body), "")):
        with pytest.raises(billing.SignatureError):
            billing.verify_signature(body, header, secret)


def test_stripe_webhook_endpoint_rejects_unsigned_and_accepts_signed(channel, billing_on):
    from app.main import app

    body = json.dumps({"id": "evt_x", "type": "customer.created", "data": {"object": {}}}).encode()
    with TestClient(app) as client:
        assert client.post("/webhook/stripe", content=body).status_code == 400
        r = client.post("/webhook/stripe", content=body, headers={"stripe-signature": _sign(body)})
        assert r.status_code == 200 and r.json()["result"] == "ignored"


# ---------- reminder ----------

async def _booked_with_offer(starts_at: str, phone="34600000001") -> int:
    req = await _new_request(await _ready_user(phone))
    async with session_scope() as s:
        r = await s.get(CitaRequest, req.id)
        s.add(SlotOffer(request_id=r.id, office="CNP Granada", starts_at=starts_at, state=OfferState.approved,
                        expires_at=datetime.now(timezone.utc)))
        await service.mark_booked(s, r, "J-1")
    return req.id


@pytest.mark.asyncio
async def test_day_before_reminder_is_sent_once_after_the_reminder_hour(channel):
    madrid_morning = datetime(2026, 11, 15, 7, 0, tzinfo=timezone.utc)  # 08:00 in Madrid
    await _booked_with_offer("16/11/2026 09:30")
    assert await notifications.send_reminders(madrid_morning) == 0  # before 10:00
    later = madrid_morning + timedelta(hours=3)
    assert await notifications.send_reminders(later) == 1
    assert "amanhã" in channel.last[1] and "16/11/2026 09:30" in channel.last[1]
    assert await notifications.send_reminders(later) == 0


@pytest.mark.asyncio
async def test_no_reminder_for_other_days(channel):
    await _booked_with_offer("20/11/2026 09:30")
    assert await notifications.send_reminders(datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)) == 0


def test_appointment_date_parsing():
    assert str(notifications.appointment_date("16/11/2026 09:30")) == "2026-11-16"
    assert str(notifications.appointment_date("Lunes 3-2-2027")) == "2027-02-03"
    assert notifications.appointment_date("mañana") is None
    assert notifications.appointment_date("31/02/2026") is None


# ---------- pause notice ----------

@pytest.mark.asyncio
async def test_pause_notice_once_a_day_one_message_per_person(channel):
    uid = await _ready_user()
    await _new_request(uid, "nie")
    await _new_request(uid, "huellas")
    now = datetime.now(timezone.utc)
    assert await notifications.notify_pause(6, now) == 1
    assert "6 h" in channel.last[1]
    assert await notifications.notify_pause(6, now + timedelta(hours=2)) == 0
    assert await notifications.notify_pause(6, now + timedelta(hours=21)) == 1


# ---------- consent and admin ----------

@pytest.mark.asyncio
async def test_consent_shows_terms_links_when_configured(channel, make_msg, monkeypatch):
    from app.router import handle

    monkeypatch.setattr(get_settings(), "terms_url", "https://vale.test/termos")
    monkeypatch.setattr(get_settings(), "privacy_url", "https://vale.test/privacidade")
    await handle(make_msg("oi"))
    assert "https://vale.test/termos" in channel.last[1] and "https://vale.test/privacidade" in channel.last[1]


def test_admin_summary_requires_token_and_masks_phones(channel, monkeypatch):
    from app.main import _mask, app

    assert _mask("34600123456") == "3460•••••56"
    monkeypatch.setattr(get_settings(), "admin_token", "adm")
    with TestClient(app) as client:
        assert client.get("/admin/summary").status_code == 404
        assert client.get("/admin/summary?token=wrong").status_code == 404
        r = client.get("/admin/summary?token=adm")
        assert r.status_code == 200 and r.json()["users"] == 0
        assert client.get("/admin?token=wrong").status_code == 404
        assert "Vale · painel" in client.get("/admin?token=adm").text


@pytest.mark.asyncio
async def test_clients_from_the_free_beta_are_never_billed(channel, monkeypatch):
    req = await _new_request(await _ready_user())  # billing off: free beta
    async with session_scope() as s:
        await service.mark_booked(s, await s.get(CitaRequest, req.id), "J-1")
    monkeypatch.setattr(get_settings(), "stripe_secret_key", "sk_test_x")  # billing turned on later
    fake = FakeStripe()
    billing.set_client(fake)
    try:
        assert await billing.send_pending_links() == 0 and fake.calls == []
    finally:
        billing.set_client(None)

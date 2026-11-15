"""Two appointment systems side by side: extranjería (icp) and the certificate identity appointment (aeat)."""

import pytest

from app.citas import service, site_guard
from app.citas.catalog import TRAMITES, codes_for, tramite_enabled
from app.citas.monitor import Monitor
from app.citas.providers.base import CheckResult, Slot
from app.config import get_settings
from app.db import session_scope
from app.models import CitaRequest, RequestState
from tests.conftest import FakeProvider

PROFILE = {"full_name": "MARIA SILVA", "document_number": "FX123456", "nationality": "BRASIL", "nie": "Y1234567X"}


async def _user(phone="34600000001"):
    async with session_scope() as s:
        user, _ = await service.get_or_create_user(s, phone, None, "es")
        await service.give_consent(s, user)
        doc = await service.save_pending_data(s, user, PROFILE, source="text")
        await service.confirm_pending(s, user, doc.id)
        return user.id


async def _request(uid, code):
    async with session_scope() as s:
        from app.models import User
        return (await service.create_request(s, await s.get(User, uid), "granada", code)).id


def test_certificate_appointment_belongs_to_aeat_and_is_off_by_default(monkeypatch):
    assert TRAMITES["acreditacion_certificado"].site == "aeat" and "nie" in codes_for("icp")
    monkeypatch.setattr(get_settings(), "enabled_sites", "icp")
    assert tramite_enabled("nie") and not tramite_enabled("acreditacion_certificado")
    monkeypatch.setattr(get_settings(), "enabled_sites", "icp, aeat, nonsense")
    assert tramite_enabled("acreditacion_certificado")


@pytest.mark.asyncio
async def test_request_is_refused_while_the_site_is_not_enabled(channel, monkeypatch):
    monkeypatch.setattr(get_settings(), "enabled_sites", "icp")
    uid = await _user()
    with pytest.raises(service.RuleError, match="not_bookable_yet"):
        await _request(uid, "acreditacion_certificado")


@pytest.mark.asyncio
async def test_each_monitor_checks_only_its_own_site_and_pauses_alone(channel, monkeypatch):
    monkeypatch.setattr(get_settings(), "enabled_sites", "icp,aeat")
    uid = await _user()
    nie_id = await _request(uid, "nie")
    cert_id = await _request(uid, "acreditacion_certificado")

    icp, aeat = FakeProvider(), FakeProvider()
    icp.next_check = CheckResult("error", reason="blocked_by_site")
    aeat.next_check = CheckResult("slots", slots=[Slot("AEAT Granada", "20/11/2026 10:00", {})], hold_key="a1")
    m_icp, m_aeat = Monitor(icp, site="icp"), Monitor(aeat, site="aeat", primary=False)
    await m_icp.tick()
    await m_aeat.tick()

    assert await site_guard.paused_until("icp") is not None and await site_guard.paused_until("aeat") is None
    async with session_scope() as s:
        assert (await s.get(CitaRequest, nie_id)).state == RequestState.monitoring
        assert (await s.get(CitaRequest, cert_id)).state == RequestState.offer_pending
    offers = [text for _, text, buttons in channel.sent if buttons]
    assert len(offers) == 1 and "AEAT Granada" in offers[0]

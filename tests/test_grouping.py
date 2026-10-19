import pytest
from sqlalchemy import select

from app.citas import service
from app.citas.monitor import Monitor
from app.citas.providers.base import CheckResult, Slot
from app.db import session_scope
from app.documents.extractor import ExtractedDocument
from app.models import CitaRequest, RequestState, User
from app.router import handle


async def _client(make_msg, phone, province="granada", tramite="nie"):
    await handle(make_msg("oi", phone=phone))
    await handle(make_msg(button="consent:yes", phone=phone))
    async with session_scope() as s:
        user = await s.scalar(select(User).where(User.phone == phone))
        doc = ExtractedDocument(document_kind="passport", full_name="A B", document_number=phone, nationality="BRASIL", confidence="high")
        await service.confirm_pending(s, user, (await service.store_document(s, user, doc)).id)
        return (await service.create_request(s, user, province, tramite)).id


@pytest.mark.asyncio
async def test_one_site_visit_for_many_clients_of_the_same_procedure(channel, provider, make_msg):
    first = await _client(make_msg, "34600000001")
    second = await _client(make_msg, "34600000002")
    other = await _client(make_msg, "34600000003", tramite="huellas")
    calls = []

    async def check(request_id, *a):
        calls.append(request_id)
        return CheckResult("no_slots")

    provider.check = check
    await Monitor(provider).tick()
    assert sorted(calls) == sorted([first, other])  # 2 visits for 3 clients
    async with session_scope() as s:
        assert (await s.get(CitaRequest, second)).last_checked_at is not None  # shared result

    calls.clear()
    await Monitor(provider).tick()
    assert calls == []  # nobody is due again before the interval


@pytest.mark.asyncio
async def test_slots_go_to_the_client_who_waited_longest(channel, provider, make_msg):
    first = await _client(make_msg, "34600000001")
    second = await _client(make_msg, "34600000002")
    provider.next_check = CheckResult("slots", slots=[Slot("Granada", "23/10 10:00", {"radio_id": "r"})], hold_key="h")
    await Monitor(provider).tick()
    async with session_scope() as s:
        assert (await s.get(CitaRequest, first)).state == RequestState.offer_pending
        second_req = await s.get(CitaRequest, second)
        assert second_req.state == RequestState.monitoring and second_req.last_checked_at is None  # next in line, checked right away
    assert channel.last[0] == "34600000001"

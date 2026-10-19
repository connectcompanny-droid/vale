"""Behaviour that must hold when several API/worker/monitor processes run side by side."""

import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from app.citas import service, site_guard
from app.citas.monitor import Monitor
from app.citas.providers.base import CheckResult, Slot
from app.db import session_scope
from app.documents.extractor import ExtractedDocument
from app.models import CitaRequest, ProcessedMessage, RequestState, User
from app.router import handle


async def _client(make_msg, phone, tramite="nie"):
    await handle(make_msg("oi", phone=phone))
    await handle(make_msg(button="consent:yes", phone=phone))
    async with session_scope() as s:
        user = await s.scalar(select(User).where(User.phone == phone))
        doc = ExtractedDocument(document_kind="passport", full_name="A B", document_number=phone, nationality="BRASIL", confidence="high")
        await service.confirm_pending(s, user, (await service.store_document(s, user, doc)).id)
        return (await service.create_request(s, user, "granada", tramite)).id


@pytest.mark.asyncio
async def test_global_pace_holds_across_processes(channel, provider, make_msg):
    for i, tramite in enumerate(["nie", "huellas", "certificados", "recogida_tie"]):
        await _client(make_msg, f"3460000000{i}", tramite)
    calls = []

    async def check(request_id, *a):
        calls.append(request_id)
        return CheckResult("no_slots")

    provider.check = check
    with patch("app.citas.site_guard.get_settings") as gs:
        gs.return_value.site_min_gap_seconds = 3600
        # Two monitor "processes" ticking at the same moment.
        await asyncio.gather(Monitor(provider).tick(), Monitor(provider).tick())
    assert len(calls) == 1  # one visit allowed in the whole cluster


@pytest.mark.asyncio
async def test_same_group_is_never_checked_twice_concurrently(channel, provider, make_msg):
    await _client(make_msg, "34600000001")
    await _client(make_msg, "34600000002")
    calls = []

    async def check(request_id, *a):
        calls.append(request_id)
        await asyncio.sleep(0.05)
        return CheckResult("no_slots")

    provider.check = check
    await asyncio.gather(*(Monitor(provider).tick() for _ in range(5)))
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_only_the_owning_process_books_a_held_session(channel, provider, make_msg):
    rid = await _client(make_msg, "34600000001")
    provider.next_check = CheckResult("slots", slots=[Slot("Granada", "x", {"radio_id": "r"})], hold_key="procA:1")
    await Monitor(provider).tick()
    await handle(make_msg(button=channel.last[2][0].id))

    booked = []

    async def book(*a):
        booked.append(a)
        from app.citas.providers.base import BookResult
        return BookResult("booked", reference="R1")

    provider.book = book
    provider.owns = lambda key: key.startswith("procB:")
    await Monitor(provider).tick()
    assert booked == []  # process B leaves it alone
    provider.owns = lambda key: key.startswith("procA:")
    await Monitor(provider).tick()
    assert len(booked) == 1
    async with session_scope() as s:
        assert (await s.get(CitaRequest, rid)).state == RequestState.booked


@pytest.mark.asyncio
async def test_duplicate_delivery_is_processed_once(channel, make_msg):
    m = make_msg("oi", mid="dup-1")
    await asyncio.gather(handle(m), handle(m), handle(m))
    assert len(channel.sent) == 1
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(ProcessedMessage)) == 1


@pytest.mark.asyncio
async def test_burst_from_one_client_is_handled_in_order(channel, make_msg):
    await handle(make_msg("oi"))
    await asyncio.gather(*(handle(make_msg(button="consent:yes" if i == 0 else None, text=f"msg {i}")) for i in range(5)))
    async with session_scope() as s:
        assert await s.scalar(select(func.count()).select_from(User)) == 1


@pytest.mark.asyncio
async def test_pause_is_never_skipped_even_for_approved_bookings(channel, provider, make_msg):
    await site_guard.pause(site_guard.ICP, 60, "blocked_by_site")
    assert not await site_guard.acquire_visit(priority=True)

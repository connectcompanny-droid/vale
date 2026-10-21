from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.citas import service
from app.citas import site_guard
from app.citas.monitor import Monitor
from app.citas.providers.base import CheckResult
from app.citas.providers.icp import classify_error
from app.db import session_scope
from app.documents.extractor import ExtractedDocument
from app.models import User
from app.router import handle


def test_classify_tls_errors():
    assert classify_error(Exception("Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://x")) == "tls_untrusted"
    assert classify_error(Exception("net::ERR_SSL_PROTOCOL_ERROR")) == "tls_untrusted"
    assert classify_error(Exception("net::ERR_CONNECTION_RESET")) == "network"


@pytest.mark.asyncio
async def test_untrusted_certificate_pauses_all_checks(channel, provider, make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))
    async with session_scope() as s:
        user = await s.scalar(select(User))
        doc = ExtractedDocument(document_kind="passport", full_name="A B", document_number="X1", nationality="BRASIL", confidence="high")
        await service.confirm_pending(s, user, (await service.store_document(s, user, doc)).id)
        await service.create_request(s, user, "granada", "nie")

    calls = []

    async def check(*a):
        calls.append(a)
        return CheckResult("error", reason="tls_untrusted")

    provider.check = check
    m = Monitor(provider)
    await m.tick()
    assert len(calls) == 1 and await m.paused()
    sent_before = len(channel.sent)

    async with session_scope() as s:  # make the request due again
        from app.models import CitaRequest
        (await s.scalar(select(CitaRequest))).last_checked_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await m.tick()
    assert len(calls) == 1  # paused: no new request to the site
    assert len(channel.sent) == sent_before  # the client is not bothered with infrastructure errors

    await site_guard.resume()
    async with session_scope() as s:
        from app.models import CitaRequest
        (await s.scalar(select(CitaRequest))).last_checked_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await m.tick()
    assert len(calls) == 2  # resumes after the pause


@pytest.mark.asyncio
async def test_firewall_block_pauses_for_hours(channel, provider, make_msg):
    await handle(make_msg("oi"))
    await handle(make_msg(button="consent:yes"))
    async with session_scope() as s:
        user = await s.scalar(select(User))
        doc = ExtractedDocument(document_kind="passport", full_name="A B", document_number="X1", nationality="BRASIL", confidence="high")
        await service.confirm_pending(s, user, (await service.store_document(s, user, doc)).id)
        await service.create_request(s, user, "granada", "nie")

    async def check(*a):
        return CheckResult("error", reason="blocked_by_site")

    provider.check = check
    m = Monitor(provider)
    await m.tick()
    until = await site_guard.paused_until()
    assert until and until - datetime.now(timezone.utc) > timedelta(hours=5)
    assert await Monitor(provider).paused()  # every other process sees the same pause


def test_browser_channel_auto_picks_chrome_on_x86_and_chromium_on_arm():
    from app.citas.providers.icp import resolve_channel

    assert resolve_channel("auto", "x86_64") == "chrome"
    assert resolve_channel("auto", "aarch64") is None
    assert resolve_channel("auto", "arm64") is None
    assert resolve_channel("chrome", "aarch64") == "chrome"
    assert resolve_channel("", "x86_64") is None


def test_hosted_postgres_urls_get_the_async_driver():
    from app.config import Settings

    assert Settings(database_url="postgresql://u:p@h:5432/d").database_url == "postgresql+asyncpg://u:p@h:5432/d"
    assert Settings(database_url="postgres://u:p@h/d").database_url == "postgresql+asyncpg://u:p@h/d"
    assert Settings(database_url="sqlite+aiosqlite:///x.db").database_url == "sqlite+aiosqlite:///x.db"

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.citas import service
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
    assert len(calls) == 1 and m.paused()
    sent_before = len(channel.sent)

    async with session_scope() as s:  # make the request due again
        from app.models import CitaRequest
        (await s.scalar(select(CitaRequest))).last_checked_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await m.tick()
    assert len(calls) == 1  # paused: no new request to the site
    assert len(channel.sent) == sent_before  # the client is not bothered with infrastructure errors

    m.paused_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    await m.tick()
    assert len(calls) == 2  # resumes after the pause

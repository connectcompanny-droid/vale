"""Billing with Stripe Checkout: activation fee before monitoring, success fee after booking.

No Stripe SDK: two HTTPS calls and an HMAC check. With STRIPE_SECRET_KEY empty the whole
module is off (free beta): requests go straight to monitoring and no fee is ever asked.

Flow
- A request with complete data moves to `awaiting_payment` (service.ready_state).
- The monitor's tick calls `send_pending_links()`: it claims each request atomically, creates a
  Checkout Session and sends the link on WhatsApp. Failures release the claim and retry later.
- Stripe calls /webhook/stripe; `handle_event()` verifies the signature, records the payment and
  starts monitoring (activation) or thanks the client (success fee). Events are idempotent.
"""

import hashlib
import hmac
import logging
import time
from datetime import datetime, timezone

import httpx
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app import audit
from app.citas.catalog import TRAMITES
from app.config import get_settings
from app.db import session_scope
from app.i18n import t
from app.models import CitaRequest, ProcessedMessage, RequestState, User

log = logging.getLogger(__name__)
API = "https://api.stripe.com/v1"
SENDING = "sending"


class SignatureError(Exception):
    pass


def enabled() -> bool:
    return bool(get_settings().stripe_secret_key)


def price_text(cents: int) -> str:
    return f"€{cents // 100},{cents % 100:02d}"


def amount(kind: str, founder: bool) -> int:
    s = get_settings()
    base = s.activation_fee_cents if kind == "activation" else s.success_fee_cents
    return base * (100 - s.founder_discount_pct) // 100 if founder else base


# ---------- Stripe API (replaceable in tests) ----------

class StripeClient:
    async def create_checkout(self, *, amount_cents: int, name: str, request_id: int, kind: str, lang: str) -> str:
        s = get_settings()
        base = (s.public_base_url or "https://example.com").rstrip("/")
        data = {
            "mode": "payment",
            "line_items[0][quantity]": "1",
            "line_items[0][price_data][currency]": "eur",
            "line_items[0][price_data][unit_amount]": str(amount_cents),
            "line_items[0][price_data][product_data][name]": name,
            "client_reference_id": str(request_id),
            "metadata[request_id]": str(request_id),
            "metadata[kind]": kind,
            "payment_intent_data[metadata][request_id]": str(request_id),
            "payment_intent_data[metadata][kind]": kind,
            "locale": {"pt": "pt", "es": "es", "en": "en"}.get(lang, "auto"),
            "success_url": f"{base}/pay/done",
            "cancel_url": f"{base}/pay/cancelled",
        }
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{API}/checkout/sessions", data=data, auth=(s.stripe_secret_key, ""))
            r.raise_for_status()
            return r.json()["url"]


_client: StripeClient | None = None


def client() -> StripeClient:
    global _client
    if _client is None:
        _client = StripeClient()
    return _client


def set_client(c: StripeClient) -> None:
    global _client
    _client = c


# ---------- sending links ----------

async def _claim(field, req_id: int) -> bool:
    async with session_scope() as s:
        res = await s.execute(update(CitaRequest).where(CitaRequest.id == req_id, field.is_(None)).values({field.key: SENDING}))
        return res.rowcount == 1


async def _release(field, req_id: int) -> None:
    async with session_scope() as s:
        await s.execute(update(CitaRequest).where(CitaRequest.id == req_id, field == SENDING).values({field.key: None}))


async def _founder_available() -> bool:
    s = get_settings()
    if s.founder_slots <= 0:
        return False
    async with session_scope() as db:
        taken = await db.scalar(select(func.count()).select_from(CitaRequest).where(CitaRequest.founder.is_(True)))
    return (taken or 0) < s.founder_slots


async def send_pending_links() -> int:
    """Send activation links (awaiting_payment) and success-fee links (booked). Returns links sent."""
    if not enabled():
        return 0
    from app.channels import get_channel

    async with session_scope() as s:
        act = [r.id for r in (await s.scalars(select(CitaRequest).where(
            CitaRequest.state == RequestState.awaiting_payment, CitaRequest.activation_link_url.is_(None)))).all()]
        suc = [r.id for r in (await s.scalars(select(CitaRequest).where(
            CitaRequest.state == RequestState.booked, CitaRequest.success_paid_at.is_(None),
            CitaRequest.activation_paid_at.is_not(None),  # never bill clients served during the free beta
            CitaRequest.success_link_url.is_(None)))).all()]
    sent = 0
    for kind, field, ids in (("activation", CitaRequest.activation_link_url, act), ("success", CitaRequest.success_link_url, suc)):
        for req_id in ids:
            if not await _claim(field, req_id):
                continue  # another process took it
            try:
                async with session_scope() as s:
                    req = await s.get(CitaRequest, req_id)
                    expected = RequestState.awaiting_payment if kind == "activation" else RequestState.booked
                    if req.state != expected:  # cancelled or changed since it was listed
                        setattr(req, field.key, None)
                        continue
                    user = await s.get(User, req.user_id)
                    if kind == "activation" and not req.founder and await _founder_available():
                        req.founder = True
                    cents = amount(kind, req.founder)
                    label = TRAMITES[req.tramite_code].label.get(user.lang, req.tramite_code)
                    name = f"Vale · {'Ativação' if kind == 'activation' else 'Taxa de sucesso'} · {label}"
                    url = await client().create_checkout(amount_cents=cents, name=name, request_id=req.id, kind=kind, lang=user.lang)
                    setattr(req, field.key, url)
                    audit.record(s, f"{kind}_link_sent", user.id, req.id, amount_cents=cents, founder=req.founder)
                    phone, lang, founder = user.phone, user.lang, req.founder
                if kind == "activation":
                    text = t("activation_link", lang, tramite=label, price=price_text(cents),
                             founder=t("founder_note", lang) if founder else "", url=url)
                else:
                    text = t("success_link", lang, price=price_text(cents), url=url)
                await get_channel().send_text(phone, text)
                sent += 1
            except Exception:
                log.exception("could not send %s link for request %s; will retry", kind, req_id)
                await _release(field, req_id)
    return sent


# ---------- webhook ----------

def verify_signature(payload: bytes, header: str, secret: str, tolerance: int = 300, now: float | None = None) -> None:
    """Stripe-Signature: t=<ts>,v1=<hex hmac-sha256 of "<ts>.<payload>">."""
    if not secret:
        raise SignatureError("no webhook secret configured")
    pairs = [p.strip().split("=", 1) for p in header.split(",") if "=" in p]
    ts = next((v for k, v in pairs if k == "t"), None)
    sigs = [v for k, v in pairs if k == "v1"]
    if not ts or not ts.isdigit() or not sigs:
        raise SignatureError("malformed header")
    if abs((now or time.time()) - int(ts)) > tolerance:
        raise SignatureError("timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, sig) for sig in sigs):
        raise SignatureError("signature mismatch")


async def handle_event(event: dict) -> str:
    """Apply a verified Stripe event. Returns what happened (for logs/tests)."""
    from app.channels import get_channel

    if event.get("type") not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return "ignored"
    obj = event.get("data", {}).get("object", {})
    if obj.get("payment_status") != "paid":
        return "not_paid"
    meta = obj.get("metadata") or {}
    kind, req_id = meta.get("kind"), meta.get("request_id")
    if kind not in ("activation", "success") or not str(req_id or "").isdigit():
        return "unknown"
    try:
        async with session_scope() as s:
            s.add(ProcessedMessage(message_id=f"stripe:{event.get('id')}"))
    except IntegrityError:
        return "duplicate"

    now = datetime.now(timezone.utc)
    async with session_scope() as s:
        req = await s.get(CitaRequest, int(req_id))
        if req is None:
            return "unknown_request"
        user = await s.get(User, req.user_id)
        if kind == "activation":
            req.activation_paid_at = req.activation_paid_at or now
            if req.state == RequestState.awaiting_payment:
                req.state = RequestState.monitoring
            audit.record(s, "activation_paid", req.user_id, req.id, amount_cents=obj.get("amount_total"))
            text = t("activation_paid", user.lang)
        else:
            req.success_paid_at = req.success_paid_at or now
            audit.record(s, "success_fee_paid", req.user_id, req.id, amount_cents=obj.get("amount_total"))
            text = t("success_paid", user.lang)
        phone = user.phone
    await get_channel().send_text(phone, text)
    return f"{kind}_paid"

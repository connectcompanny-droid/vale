"""Operator panel: /admin/operador?token=ADMIN_TOKEN

Where a person on Vale's side works the assisted-mode queue:
1. "Confirmed by the client": book these NOW in a normal browser, then press "Reservado".
2. "Offer sent": waiting for the client's yes (the offer expires on its own).
3. "Waiting for a slot": look on the official site; when there is one, propose it to the client.

The page shows the clients' personal details (needed to fill the official form), so it only opens
from the computer running Vale: requests that come through the public tunnel are refused.
"""

import hmac
from html import escape

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import selectinload

from app.citas import service
from app.citas.catalog import ALL_PROVINCES, TRAMITES
from app.citas.providers.icp import BASE_URL
from app.config import get_settings
from app.db import session_scope
from app.i18n import t
from app.models import CitaRequest, OfferState, RequestState

router = APIRouter()
LOCAL = {"127.0.0.1", "::1", "localhost"}


def _guard(request: Request, token: str) -> None:
    expected = get_settings().admin_token
    if not expected or not hmac.compare_digest(token or "", expected):
        raise HTTPException(status_code=404)
    via_tunnel = any(h in request.headers for h in ("x-forwarded-for", "x-forwarded-host", "ngrok-trace-id", "cf-connecting-ip"))
    if via_tunnel or (request.client and request.client.host not in LOCAL):
        raise HTTPException(status_code=404)


def _copy(label: str, value: str) -> str:
    v = escape(value or "")
    return (f'<div class="f"><span>{escape(label)}</span><code>{v}</code>'
            f'<button type="button" onclick="navigator.clipboard.writeText(this.previousElementSibling.textContent)">copiar</button></div>')


def _card(r: CitaRequest, profile: dict, token: str) -> str:
    tr = TRAMITES[r.tramite_code]
    phone = service.gov_phone(r.user.phone, profile)
    offer = next((o for o in reversed(r.offers) if o.state in (OfferState.pending, OfferState.approved)), None)
    fields = "".join([
        _copy("Provincia", ALL_PROVINCES.get(r.province, r.province)),
        _copy("Trámite (opción con)", tr.match[0]),
        _copy("NIE" if profile.get("nie") else "Pasaporte", str(profile.get("nie") or profile.get("document_number") or "")),
        _copy("Nombre", str(profile.get("full_name", "")).upper()),
        _copy("País", str(profile.get("nationality", "")).upper()),
        _copy("Teléfono", phone),
        _copy("Email", str(profile.get("email", ""))),
    ])
    tok = escape(token)
    if r.state == RequestState.booking:
        head = f'<p class="hot">El cliente CONFIRMÓ: {escape(offer.office)} · {escape(offer.starts_at)}. Resérvalo ya.</p>'
        actions = (f'<form method="post" action="/admin/operador/{r.id}/booked"><input type="hidden" name="token" value="{tok}">'
                   f'<input name="reference" placeholder="Código del justificante" required><button>Reservado ✓</button></form>'
                   f'<form method="post" action="/admin/operador/{r.id}/gone"><input type="hidden" name="token" value="{tok}">'
                   f'<button class="ghost">Ya no estaba</button></form>')
    elif r.state == RequestState.offer_pending:
        head = f'<p>Oferta enviada: {escape(offer.office)} · {escape(offer.starts_at)}. Esperando el sí del cliente.</p>'
        actions = (f'<form method="post" action="/admin/operador/{r.id}/gone"><input type="hidden" name="token" value="{tok}">'
                   f'<button class="ghost">Retirar oferta</button></form>')
    else:
        last = service.aware(r.last_checked_at).strftime("%d/%m %H:%M") if r.last_checked_at else "nunca"
        head = f'<p>Esperando hueco · última revisión: {last} UTC · revisiones: {r.checks}</p>'
        actions = (f'<form method="post" action="/admin/operador/{r.id}/offer"><input type="hidden" name="token" value="{tok}">'
                   f'<input name="office" placeholder="Oficina" required><input name="starts_at" placeholder="DD/MM/AAAA HH:MM" required>'
                   f'<button>Proponer al cliente</button></form>'
                   f'<form method="post" action="/admin/operador/{r.id}/checked"><input type="hidden" name="token" value="{tok}">'
                   f'<button class="ghost">Revisado, sin huecos</button></form>')
    return (f'<article><h3>#{r.id} · {escape(tr.label["es"])}</h3>{head}<div class="fields">{fields}</div>'
            f'<div class="actions">{actions}</div></article>')


@router.get("/admin/operador", response_class=HTMLResponse)
async def panel(request: Request, token: str = Query("")) -> str:
    _guard(request, token)
    async with session_scope() as s:
        queue = await service.operator_queue(s)
        cards = []
        for r in queue:
            await s.refresh(r, ["offers"])
            cards.append(_card(r, await service.get_profile(s, r.user_id), token))
    body = "".join(cards) or "<p>No hay nada pendiente.</p>"
    return f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60"><title>Vale · operador</title><style>
body{{font-family:system-ui,sans-serif;background:#f6f1e7;color:#10234a;margin:0;padding:16px;max-width:900px;margin:auto}}
h1{{font-size:22px}} article{{background:#fff;border:2px solid #10234a;padding:14px;margin:14px 0}} h3{{margin:0 0 6px}}
.hot{{background:#3ddc97;padding:8px;font-weight:700}} .fields{{display:grid;gap:6px;margin:10px 0}}
.f{{display:grid;grid-template-columns:150px 1fr auto;gap:8px;align-items:center}} .f span{{color:#4a5878;font-size:13px}}
code{{background:#ede6d8;padding:4px 6px;overflow-wrap:anywhere}} button{{background:#10234a;color:#fff;border:0;padding:8px 12px;cursor:pointer}}
button.ghost{{background:#fff;color:#10234a;border:2px solid #10234a}} .actions form{{display:flex;flex-wrap:wrap;gap:8px;margin-top:8px}}
input{{padding:8px;border:2px solid #10234a}} a{{color:#10234a}}</style></head><body>
<h1>Vale · cola del operador</h1>
<p>Web oficial: <a href="{BASE_URL}" target="_blank" rel="noopener">{BASE_URL}</a> · se actualiza cada minuto.
Nunca reserves sin el sí del cliente.</p>{body}</body></html>"""


async def _act(request: Request, token: str, fn) -> RedirectResponse:
    _guard(request, token)
    try:
        await fn()
    except service.RuleError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return RedirectResponse(f"/admin/operador?token={token}", status_code=303)


@router.post("/admin/operador/{request_id}/offer")
async def offer(request: Request, request_id: int, token: str = Form(""), office: str = Form(...), starts_at: str = Form(...)):
    from app.channels import Button, get_channel

    async def run():
        async with session_scope() as s:
            o = await service.operator_offer(s, request_id, office, starts_at)
            req = await s.get(CitaRequest, request_id, options=[selectinload(CitaRequest.user)])
            phone, lang, oid, off, when = req.user.phone, req.user.lang, o.id, o.office, o.starts_at
        await get_channel().send_buttons(phone, t("offer", lang, office=off, starts_at=when, ttl=get_settings().offer_ttl_minutes), [
            Button(f"offer:{oid}:confirm", t("confirm", lang)), Button(f"offer:{oid}:reject", t("reject", lang))])
    return await _act(request, token, run)


@router.post("/admin/operador/{request_id}/booked")
async def booked(request: Request, request_id: int, token: str = Form(""), reference: str = Form("")):
    from app.channels import get_channel

    async def run():
        async with session_scope() as s:
            req, o = await service.operator_booked(s, request_id, reference)
            await s.refresh(req, ["user"])
            phone, lang, off, when, ref = req.user.phone, req.user.lang, o.office, o.starts_at, req.note or ""
        await get_channel().send_text(phone, t("booked", lang, office=off, starts_at=when, reference=ref))
    return await _act(request, token, run)


@router.post("/admin/operador/{request_id}/gone")
async def gone(request: Request, request_id: int, token: str = Form("")):
    from app.channels import get_channel

    async def run():
        async with session_scope() as s:
            req = await service.operator_slot_gone(s, request_id)
            await s.refresh(req, ["user"])
            phone, lang = req.user.phone, req.user.lang
        await get_channel().send_text(phone, t("offer_expired", lang))
    return await _act(request, token, run)


@router.post("/admin/operador/{request_id}/checked")
async def checked(request: Request, request_id: int, token: str = Form("")):
    async def run():
        async with session_scope() as s:
            await service.operator_checked(s, request_id)
    return await _act(request, token, run)


# ---------------- Copilot API (used by the Vale Copilot Chrome extension) ----------------
# Same guard as the panel: local only, ADMIN_TOKEN in the X-Vale-Token header. The extension
# never acts on the government site by itself: it fills fields and reads pages when the
# operator presses its buttons, and the operator presses the site's own buttons.

from collections import Counter  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402
from sqlalchemy import select  # noqa: E402

from fastapi import Header  # noqa: E402

from app.models import AuditEvent  # noqa: E402


def _guard_api(request: Request, token: str | None) -> None:
    _guard(request, token or "")


def _client(r: CitaRequest, profile: dict) -> dict:
    nie = str(profile.get("nie") or "")
    return {
        "doc_type": "nie" if nie else "passport",
        "nie": nie,
        "number": nie or str(profile.get("document_number") or ""),
        "name": str(profile.get("full_name", "")).upper(),
        "nationality": str(profile.get("nationality", "")).upper(),
        "phone": service.gov_phone(r.user.phone, profile),
        "email": str(profile.get("email", "")),
    }


@router.get("/admin/copilot/queue")
async def copilot_queue(request: Request, x_vale_token: str | None = Header(default=None)) -> dict:
    _guard_api(request, x_vale_token)
    async with session_scope() as s:
        items = []
        for r in await service.operator_queue(s):
            await s.refresh(r, ["offers"])
            tr = TRAMITES[r.tramite_code]
            offer = next((o for o in reversed(r.offers) if o.state in (OfferState.pending, OfferState.approved)), None)
            items.append({
                "id": r.id, "state": r.state.value, "tramite_code": r.tramite_code, "site": tr.site,
                "tramite": tr.label["es"], "match": list(tr.match), "province": ALL_PROVINCES.get(r.province, r.province),
                "waiting_since": service.aware(r.created_at).isoformat(), "checks": r.checks,
                "client": _client(r, await service.get_profile(s, r.user_id)),
                "offer": {"office": offer.office, "starts_at": offer.starts_at, "expires_at": service.aware(offer.expires_at).isoformat()}
                if offer else None,
            })
    return {"items": items, "site": BASE_URL}


class OfferBody(BaseModel):
    office: str = Field(min_length=2, max_length=200)
    starts_at: str = Field(min_length=5, max_length=60)


class BookedBody(BaseModel):
    reference: str = Field(default="", max_length=120)


class CheckedBody(BaseModel):
    tramite_code: str


async def _offer(request_id: int, office: str, starts_at: str) -> None:
    from app.channels import Button, get_channel

    async with session_scope() as s:
        o = await service.operator_offer(s, request_id, office, starts_at)
        req = await s.get(CitaRequest, request_id, options=[selectinload(CitaRequest.user)])
        phone, lang, oid, off, when = req.user.phone, req.user.lang, o.id, o.office, o.starts_at
    await get_channel().send_buttons(phone, t("offer", lang, office=off, starts_at=when, ttl=get_settings().offer_ttl_minutes), [
        Button(f"offer:{oid}:confirm", t("confirm", lang)), Button(f"offer:{oid}:reject", t("reject", lang))])


async def _booked(request_id: int, reference: str) -> None:
    from app.channels import get_channel

    async with session_scope() as s:
        req, o = await service.operator_booked(s, request_id, reference)
        await s.refresh(req, ["user"])
        phone, lang, off, when, ref = req.user.phone, req.user.lang, o.office, o.starts_at, req.note or ""
    await get_channel().send_text(phone, t("booked", lang, office=off, starts_at=when, reference=ref))


async def _gone(request_id: int) -> None:
    from app.channels import get_channel

    async with session_scope() as s:
        req = await service.operator_slot_gone(s, request_id)
        await s.refresh(req, ["user"])
        phone, lang = req.user.phone, req.user.lang
    await get_channel().send_text(phone, t("offer_expired", lang))


async def _api(request: Request, token: str | None, coro_fn) -> dict:
    _guard_api(request, token)
    try:
        await coro_fn()
    except service.RuleError as e:
        raise HTTPException(status_code=409, detail=str(e)) from None
    return {"ok": True}


@router.post("/admin/copilot/{request_id}/offer")
async def copilot_offer(request: Request, request_id: int, body: OfferBody, x_vale_token: str | None = Header(default=None)) -> dict:
    return await _api(request, x_vale_token, lambda: _offer(request_id, body.office, body.starts_at))


@router.post("/admin/copilot/{request_id}/booked")
async def copilot_booked(request: Request, request_id: int, body: BookedBody, x_vale_token: str | None = Header(default=None)) -> dict:
    return await _api(request, x_vale_token, lambda: _booked(request_id, body.reference))


@router.post("/admin/copilot/{request_id}/gone")
async def copilot_gone(request: Request, request_id: int, x_vale_token: str | None = Header(default=None)) -> dict:
    return await _api(request, x_vale_token, lambda: _gone(request_id))


@router.post("/admin/copilot/checked")
async def copilot_checked(request: Request, body: CheckedBody, x_vale_token: str | None = Header(default=None)) -> dict:
    """The operator looked and found nothing for this trámite: one check for everyone waiting on it."""
    _guard_api(request, x_vale_token)
    async with session_scope() as s:
        n = 0
        for r in await service.operator_queue(s):
            if r.tramite_code == body.tramite_code and r.state == RequestState.monitoring:
                await service.operator_checked(s, r.id)
                n += 1
    return {"ok": True, "marked": n}


@router.get("/admin/copilot/hints")
async def copilot_hints(request: Request, x_vale_token: str | None = Header(default=None)) -> dict:
    """When slots have been found so far (Madrid hour), from the operator's own history. Plain
    counts, no guesswork: with little data it says so."""
    from app.notifications import MADRID

    _guard_api(request, x_vale_token)
    async with session_scope() as s:
        rows = (await s.execute(select(AuditEvent.action, AuditEvent.created_at, AuditEvent.detail)
                                .where(AuditEvent.action.in_(("offer_created", "checked"))))).all()
    found, looked = Counter(), Counter()
    for action, at, detail in rows:
        hour = service.aware(at).astimezone(MADRID).hour
        (found if action == "offer_created" else looked)[hour] += 1
    total_found = sum(found.values())
    best = [{"hour": h, "found": n, "checks": looked[h] + n} for h, n in found.most_common(3)]
    return {"slots_found": total_found, "best_hours": best,
            "enough_data": total_found >= 5,
            "note": "Con pocos datos todavía; revisa a distintas horas para que Vale aprenda." if total_found < 5 else ""}

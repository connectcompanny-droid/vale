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

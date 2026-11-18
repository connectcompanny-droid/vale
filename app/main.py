import hmac
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy import func, select, text

from app.channels import get_channel
from app.config import get_settings
from app.db import create_all, engine, session_scope
from app.logging_setup import setup_logging
from app.models import AuditEvent, CitaRequest, SiteState, User
from app.queue import enqueue_message, queue_depth

setup_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if get_settings().auto_create_tables:
        await create_all()
    yield


app = FastAPI(title="Vale", lifespan=lifespan)

from app.web_chat import router as web_chat_router  # noqa: E402

app.include_router(web_chat_router)

from app.operator_panel import router as operator_router  # noqa: E402

app.include_router(operator_router)


@app.get("/health")
async def health() -> dict:
    """Liveness: the process is up."""
    return {"ok": True}


@app.get("/ready")
async def ready() -> dict:
    """Readiness: dependencies reachable. Load balancers should route only to ready replicas."""
    try:
        async with engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        await queue_depth()
    except Exception as e:
        raise HTTPException(status_code=503, detail=type(e).__name__)
    return {"ok": True}


@app.get("/metrics", response_class=PlainTextResponse)
async def metrics(token: str = Query("")) -> str:
    """Prometheus text format. Counts only, never personal data."""
    expected = get_settings().metrics_token
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=404)
    lines = []
    async with session_scope() as s:
        for state, n in (await s.execute(select(CitaRequest.state, func.count()).group_by(CitaRequest.state))).all():
            lines.append(f'vale_requests{{state="{state.value}"}} {n}')
        for st in (await s.scalars(select(SiteState))).all():
            until = st.paused_until.replace(tzinfo=timezone.utc) if st.paused_until and st.paused_until.tzinfo is None else st.paused_until
            paused = 1 if until and until > datetime.now(timezone.utc) else 0
            lines.append(f'vale_site_paused{{site="{st.site}"}} {paused}')
            lines.append(f'vale_site_visits_total{{site="{st.site}"}} {st.visits}')
    depth = await queue_depth()
    if depth is not None:
        lines.append(f"vale_queue_depth {depth}")
    return "\n".join(lines) + "\n"


@app.post("/webhook/uazapi")
async def uazapi_webhook(request: Request, background: BackgroundTasks, secret: str = Query("")) -> dict:
    expected = get_settings().webhook_secret
    if not expected or not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=401)
    payload = await request.json()
    msg = get_channel().parse_webhook(payload)
    if msg is not None:
        # Acknowledge fast; UAZAPI retries slow webhooks. In redis mode this only enqueues.
        background.add_task(enqueue_message, msg)
    return {"ok": True}


@app.post("/webhook/evolution")
async def evolution_webhook(request: Request, background: BackgroundTasks, secret: str = Query("")) -> dict:
    """Evolution API, or Vale's free bridge (bridge/whatsapp), which uses the same format."""
    from app.channels.evolution import parse_evolution

    expected = get_settings().webhook_secret
    if not expected or not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=401)
    msg = parse_evolution(await request.json())
    if msg is not None:
        background.add_task(enqueue_message, msg)
    return {"ok": True}


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request) -> dict:
    from app import billing

    payload = await request.body()
    try:
        billing.verify_signature(payload, request.headers.get("stripe-signature", ""), get_settings().stripe_webhook_secret)
    except billing.SignatureError:
        raise HTTPException(status_code=400)
    import json

    result = await billing.handle_event(json.loads(payload))
    return {"ok": True, "result": result}


_PAGE = """<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vale</title><body style="font-family:system-ui,sans-serif;max-width:28rem;margin:4rem auto;padding:0 1rem;line-height:1.5">
<h1 style="font-size:1.4rem">{title}</h1><p>{body}</p></body></html>"""


@app.get("/pay/done", response_class=HTMLResponse)
async def pay_done() -> str:
    return _PAGE.format(title="✓", body="Pago recibido / Pagamento recebido / Payment received.<br>"
                        "Vuelve a WhatsApp / Volte ao WhatsApp / Go back to WhatsApp.")


@app.get("/pay/cancelled", response_class=HTMLResponse)
async def pay_cancelled() -> str:
    return _PAGE.format(title="Vale", body="El pago no se completó. Puedes usar el mismo enlace de WhatsApp cuando quieras.<br>"
                        "O pagamento não foi concluído. Use o mesmo link do WhatsApp quando quiser.")


def _mask(phone: str) -> str:
    return phone[:4] + "•" * max(0, len(phone) - 6) + phone[-2:] if len(phone) > 6 else "•••"


@app.get("/admin/summary")
async def admin_summary(token: str = Query("")) -> dict:
    """Operator view: counts, revenue events, site state and the latest requests (phones masked)."""
    expected = get_settings().admin_token
    if not expected or not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=404)
    async with session_scope() as s:
        by_state = {st.value: n for st, n in (await s.execute(select(CitaRequest.state, func.count()).group_by(CitaRequest.state))).all()}
        users = await s.scalar(select(func.count()).select_from(User))
        consented = await s.scalar(select(func.count()).select_from(User).where(User.consent_at.is_not(None)))
        activations = await s.scalar(select(func.count()).select_from(CitaRequest).where(CitaRequest.activation_paid_at.is_not(None)))
        successes = await s.scalar(select(func.count()).select_from(CitaRequest).where(CitaRequest.success_paid_at.is_not(None)))
        cert_interest = await s.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "interest:digital_certificate"))
        sites = [{"site": st.site, "paused_until": st.paused_until.isoformat() if st.paused_until else None,
                  "reason": st.pause_reason, "visits": st.visits} for st in (await s.scalars(select(SiteState))).all()]
        rows = (await s.execute(select(CitaRequest, User.phone).join(User, User.id == CitaRequest.user_id)
                                .order_by(CitaRequest.id.desc()).limit(50))).all()
        latest = [{"id": r.id, "phone": _mask(phone), "province": r.province, "tramite": r.tramite_code, "state": r.state.value,
                   "checks": r.checks, "last_checked_at": r.last_checked_at.isoformat() if r.last_checked_at else None,
                   "activation_paid": bool(r.activation_paid_at), "success_paid": bool(r.success_paid_at),
                   "created_at": r.created_at.isoformat() if r.created_at else None} for r, phone in rows]
    return {"users": users, "consented": consented, "requests_by_state": by_state,
            "activations_paid": activations, "success_fees_paid": successes, "digital_certificate_interest": cert_interest, "sites": sites, "latest_requests": latest}


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(token: str = Query("")) -> str:
    """The same summary as a simple page for the phone or browser."""
    from html import escape

    data = await admin_summary(token)
    states = "".join(f"<tr><td>{escape(k)}</td><td>{v}</td></tr>" for k, v in sorted(data["requests_by_state"].items()))
    sites = "".join(f"<tr><td>{escape(s['site'])}</td><td>{escape(str(s['paused_until'] or 'ativo'))}</td>"
                    f"<td>{escape(str(s['reason'] or ''))}</td><td>{s['visits']}</td></tr>" for s in data["sites"])
    rows = "".join(
        f"<tr><td>{r['id']}</td><td>{escape(r['phone'])}</td><td>{escape(r['province'])}</td><td>{escape(r['tramite'])}</td>"
        f"<td>{escape(r['state'])}</td><td>{r['checks']}</td><td>{'✓' if r['activation_paid'] else ''}</td>"
        f"<td>{'✓' if r['success_paid'] else ''}</td></tr>" for r in data["latest_requests"])
    css = ("body{font-family:system-ui,sans-serif;margin:1rem;max-width:60rem}table{border-collapse:collapse;width:100%;"
           "margin-bottom:1.5rem;font-size:.9rem}td,th{border-bottom:1px solid #ddd;padding:.35rem .5rem;text-align:left}"
           "h1{font-size:1.3rem}h2{font-size:1rem;margin-top:1.5rem}.k{display:flex;gap:1.5rem;flex-wrap:wrap}"
           ".k div{background:#f3f4f6;padding:.6rem .9rem;border-radius:8px}")
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>Vale admin</title><style>{css}</style><h1>Vale · painel</h1><div class=k>"
            f"<div>Usuários<br><b>{data['users']}</b></div><div>Com consentimento<br><b>{data['consented']}</b></div>"
            f"<div>Ativações pagas<br><b>{data['activations_paid']}</b></div><div>Taxas de sucesso pagas<br><b>{data['success_fees_paid']}</b></div>"
            f"<div>Interessados em certificado digital<br><b>{data['digital_certificate_interest']}</b></div></div>"
            f"<h2>Pedidos por estado</h2><table><tr><th>Estado</th><th>Qtd</th></tr>{states}</table>"
            f"<h2>Site do governo</h2><table><tr><th>Site</th><th>Pausado até</th><th>Motivo</th><th>Visitas</th></tr>{sites}</table>"
            f"<h2>Últimos pedidos</h2><table><tr><th>#</th><th>Telefone</th><th>Província</th><th>Trâmite</th><th>Estado</th>"
            f"<th>Verificações</th><th>Ativação</th><th>Sucesso</th></tr>{rows}</table>")

import hmac
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select, text

from app.channels import get_channel
from app.config import get_settings
from app.db import create_all, engine, session_scope
from app.logging_setup import setup_logging
from app.models import CitaRequest, SiteState
from app.queue import enqueue_message, queue_depth

setup_logging()


@asynccontextmanager
async def lifespan(_: FastAPI):
    if get_settings().auto_create_tables:
        await create_all()
    yield


app = FastAPI(title="Vale", lifespan=lifespan)


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

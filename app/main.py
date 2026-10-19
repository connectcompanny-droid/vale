import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Request

from app.channels import get_channel
from app.config import get_settings
from app.db import create_all
from app.router import handle

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI):
    await create_all()
    yield


app = FastAPI(title="Vale", lifespan=lifespan)


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/webhook/uazapi")
async def uazapi_webhook(request: Request, background: BackgroundTasks, secret: str = Query("")) -> dict:
    expected = get_settings().webhook_secret
    if not expected or not hmac.compare_digest(secret, expected):
        raise HTTPException(status_code=401)
    payload = await request.json()
    msg = get_channel().parse_webhook(payload)
    if msg is not None:
        # Acknowledge fast; UAZAPI retries slow webhooks.
        background.add_task(handle, msg)
    return {"ok": True}

"""Website chat: the second way into Vale, next to WhatsApp.

GET  /chat                 the chat page
POST /chat/api/session     -> {token}; the browser keeps it and sends it as X-Vale-Session
POST /chat/api/send        {text | button_id | media} -> queued for the same router WhatsApp uses
GET  /chat/api/messages    ?after=<id> -> messages of this session (the page polls)

The token is the only credential, so the server stores just a hash of it (as the user's key).
Abuse limits are per session and per address, in memory: enough for one API process.
"""

import base64
import hashlib
import secrets
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.channels import web
from app.channels.base import IncomingMessage
from app.config import get_settings
from app.queue import enqueue_message

router = APIRouter()
PAGE = (Path(__file__).parent / "static" / "chat.html").read_text(encoding="utf-8")

MAX_TEXT = 2000
MAX_MEDIA_BYTES = 6 * 1024 * 1024
MEDIA_KINDS = {"image/jpeg": "image", "image/png": "image", "image/webp": "image", "application/pdf": "document",
               "audio/webm": "audio", "audio/ogg": "audio", "audio/mp4": "audio", "audio/mpeg": "audio", "audio/wav": "audio"}
LIMITS = {"send": (30, 600), "session": (20, 3600), "poll": (120, 60)}  # name: (events, per seconds)
_events: dict[tuple[str, str], deque] = defaultdict(deque)


def _allow(kind: str, who: str) -> bool:
    limit, window = LIMITS[kind]
    q, now = _events[(kind, who)], time.monotonic()
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    if len(_events) > 50_000:  # forget idle keys
        for key in [k for k, v in _events.items() if not v or now - v[-1] > 3600]:
            del _events[key]
    return True


def _address(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def _key(token: str | None) -> str:
    if not get_settings().web_chat_enabled:
        raise HTTPException(status_code=404)
    if not token or not 20 <= len(token) <= 128:
        raise HTTPException(status_code=401, detail="session")
    return web.PREFIX + hashlib.sha256(token.encode()).hexdigest()[:28]


class Media(BaseModel):
    b64: str
    mime: str = Field(max_length=60)


class SendBody(BaseModel):
    text: str = Field(default="", max_length=MAX_TEXT)
    button_id: str | None = Field(default=None, max_length=80)
    button_label: str | None = Field(default=None, max_length=80)
    media: Media | None = None
    hidden: bool = False  # the automatic first greeting: answered, but not shown in the history


@router.get("/chat", response_class=HTMLResponse)
async def chat_page() -> HTMLResponse:
    cfg = get_settings()
    if not cfg.web_chat_enabled:
        raise HTTPException(status_code=404)
    number = "".join(c for c in cfg.whatsapp_number if c.isdigit())
    html = PAGE.replace("__WHATSAPP__", number).replace("__TERMS__", cfg.terms_url).replace("__PRIVACY__", cfg.privacy_url)
    return HTMLResponse(html, headers={
        "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
                                   "img-src 'self' data: blob:; media-src blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'",
        "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
    })


@router.post("/chat/api/session")
async def new_session(request: Request) -> dict:
    if not get_settings().web_chat_enabled:
        raise HTTPException(status_code=404)
    if not _allow("session", _address(request)):
        raise HTTPException(status_code=429)
    return {"token": secrets.token_urlsafe(32)}


@router.post("/chat/api/send")
async def send(body: SendBody, background: BackgroundTasks, x_vale_session: str | None = Header(default=None)) -> dict:
    key = _key(x_vale_session)
    if not _allow("send", key):
        raise HTTPException(status_code=429)
    text = body.text.strip()
    kind = mime = None
    raw: dict = {}
    if body.media:
        mime = body.media.mime.split(";")[0].strip().lower()
        kind = MEDIA_KINDS.get(mime)
        if kind is None:
            raise HTTPException(status_code=415)
        if len(body.media.b64) > MAX_MEDIA_BYTES * 4 // 3 + 8:
            raise HTTPException(status_code=413)
        try:
            base64.b64decode(body.media.b64, validate=True)
        except Exception:
            raise HTTPException(status_code=400) from None
        raw = {"web_media": {"b64": body.media.b64, "mime": mime}}
    if not (text or body.button_id or kind):
        raise HTTPException(status_code=400)
    if not body.hidden:
        shown = {"text": body.button_label or text} if not kind else {"text": text, "kind": kind}
        await web.store(key, "in", shown)
    msg = IncomingMessage(phone=key, message_id=f"web-{uuid.uuid4().hex}", text=text, button_id=body.button_id,
                          has_media=kind is not None, media_kind=kind, media_mime=mime, raw=raw)
    background.add_task(enqueue_message, msg)
    return {"ok": True}


@router.get("/chat/api/messages")
async def messages(after: int = 0, x_vale_session: str | None = Header(default=None)) -> dict:
    key = _key(x_vale_session)
    if not _allow("poll", key):
        raise HTTPException(status_code=429)
    return {"messages": await web.history(key, max(0, after))}

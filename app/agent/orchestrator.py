"""Claude tool-use loop for free-text conversation.

The agent can open, inspect and cancel requests and start data deletion. It cannot
approve offers or book: approvals arrive only as button taps handled in app.router.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from anthropic import AsyncAnthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import SYSTEM_PROMPT
from app.citas import service
from app.citas.catalog import PROVINCES, TRAMITES, find_province
from app.config import get_settings
from app.models import User

log = logging.getLogger(__name__)
MAX_HISTORY = 16
MAX_STEPS = 6

TOOLS = [
    {
        "name": "create_cita_request",
        "description": "Open a request to watch for appointment slots for the user.",
        "input_schema": {
            "type": "object",
            "properties": {
                "province": {"type": "string", "description": f"One of: {', '.join(PROVINCES)}"},
                "tramite_code": {"type": "string", "enum": list(TRAMITES)},
            },
            "required": ["province", "tramite_code"],
        },
    },
    {
        "name": "get_my_status",
        "description": "Active requests with state and which personal fields are still missing.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "cancel_cita_request",
        "description": "Cancel one of the user's active requests.",
        "input_schema": {"type": "object", "properties": {"request_id": {"type": "integer"}}, "required": ["request_id"]},
    },
    {
        "name": "save_contact_email",
        "description": "Store the email where the government sends the appointment receipt, when the user gives it.",
        "input_schema": {"type": "object", "properties": {"email": {"type": "string"}}, "required": ["email"]},
    },
    {
        "name": "submit_personal_data",
        "description": (
            "Record personal details the user TYPED or SAID (voice transcript) instead of sending a photo, "
            "or a correction to details already read. Pass only the fields they gave; the user then gets a "
            "read-back with Confirm/Fix buttons, so do not repeat the details yourself afterwards."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "full_name": {"type": "string", "description": "Full name exactly as on the document"},
                "document_number": {"type": "string", "description": "Passport or ID number, no spaces"},
                "nie": {"type": "string", "description": "Spanish NIE if they have one, e.g. X1234567L"},
                "nationality": {"type": "string", "description": "Country name in Spanish, e.g. BRASIL"},
            },
        },
    },
    {
        "name": "set_reply_mode",
        "description": "Change how Vale answers when the user asks: 'audio' (always voice notes), 'text' (never voice), 'mirror' (voice only when they send voice).",
        "input_schema": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["audio", "text", "mirror"]}}, "required": ["mode"]},
    },
    {
        "name": "request_data_deletion",
        "description": "Ask the user to confirm deletion of all their data (sends a confirmation button).",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class Agent:
    def __init__(self, client: AsyncAnthropic | None = None):
        self.client = client or AsyncAnthropic(api_key=get_settings().anthropic_api_key)

    async def _run_tool(self, session: AsyncSession, user: User, name: str, args: dict, on_delete: Callable[[], Awaitable[None]],
                        on_data: Callable[[dict], Awaitable[None]] | None) -> Any:
        try:
            if name == "create_cita_request":
                province = find_province(args.get("province", "")) or args.get("province", "")
                req = await service.create_request(session, user, province, args.get("tramite_code", ""))
                profile = await service.get_profile(session, user.id)
                return {"request_id": req.id, "state": req.state.value, "missing": service.missing_fields(profile, req.tramite_code)}
            if name == "get_my_status":
                profile = await service.get_profile(session, user.id)
                return {
                    "has_email": bool(profile.get("email")),
                    "requests": [
                        {"request_id": r.id, "province": r.province, "tramite": r.tramite_code, "state": r.state.value,
                         "checks": r.checks, "missing": service.missing_fields(profile, r.tramite_code)}
                        for r in await service.active_requests(session, user.id)
                    ],
                }
            if name == "cancel_cita_request":
                req = await service.cancel_request(session, user, int(args["request_id"]))
                return {"cancelled": req.id}
            if name == "save_contact_email":
                email = str(args.get("email", "")).strip()
                if "@" not in email or len(email) > 160:
                    return {"error": "invalid_email"}
                await service.set_contact_email(session, user, email)
                return {"saved": True}
            if name == "submit_personal_data":
                if on_data is None:
                    return {"error": "not_available"}
                await on_data({k: v for k, v in args.items() if v})
                return {"readback_sent": True}
            if name == "set_reply_mode":
                mode = args.get("mode")
                if mode not in ("audio", "text", "mirror"):
                    return {"error": "invalid_mode"}
                user.reply_mode = mode
                return {"reply_mode": mode}
            if name == "request_data_deletion":
                await on_delete()
                return {"confirmation_sent": True}
        except service.RuleError as e:
            return {"error": str(e)}
        return {"error": f"unknown_tool {name}"}

    async def reply(self, session: AsyncSession, user: User, text: str, on_delete: Callable[[], Awaitable[None]],
                    on_data: Callable[[dict], Awaitable[None]] | None = None) -> str:
        system = SYSTEM_PROMPT.format(
            lang=user.lang,
            provinces=", ".join(PROVINCES.values()),
            tramites="; ".join(f"{t.code} = {t.label.get(user.lang, t.label['pt'])}" for t in TRAMITES.values()),
        )
        history = list(user.history or [])[-MAX_HISTORY:]
        messages: list[dict] = [*history, {"role": "user", "content": text}]
        final = ""
        for _ in range(MAX_STEPS):
            resp = await self.client.messages.create(
                model=get_settings().claude_model, max_tokens=700, system=system, tools=TOOLS, messages=messages,
            )
            messages.append({"role": "assistant", "content": [b.model_dump() for b in resp.content]})
            if resp.stop_reason != "tool_use":
                final = "".join(b.text for b in resp.content if b.type == "text").strip()
                break
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    out = await self._run_tool(session, user, block.name, block.input or {}, on_delete, on_data)
                    results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(out, ensure_ascii=False)})
            messages.append({"role": "user", "content": results})
        # Store only plain-text turns; tool payloads stay out of the long-lived history.
        user.history = [*history, {"role": "user", "content": text}, {"role": "assistant", "content": final or "…"}][-MAX_HISTORY:]
        return final

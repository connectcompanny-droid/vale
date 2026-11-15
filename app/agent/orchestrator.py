"""Claude tool-use loop for free-text conversation.

The agent can open, inspect and cancel requests and start data deletion. It cannot
approve offers or book: approvals arrive only as button taps handled in app.router.
"""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from anthropic import AsyncAnthropic
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import audit, llm
from app.agent.prompts import CERT_BOOKING_OFF, CERT_BOOKING_ON, SYSTEM_PROMPT, WEB_NOTE, WHATSAPP_NOTE
from app.citas import service
from app.citas.catalog import PROVINCES, TRAMITES, find_province, tramite_enabled
from app.config import get_settings
from app.models import AuditEvent, ExternalAppointment, User

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
                "province": {"type": "string", "description": "One of the supported provinces listed in the instructions"},
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
        "name": "save_contact_phone",
        "description": "Store the mobile number the government can reach the user on. Only needed when 'phone' is listed as missing (website chat users).",
        "input_schema": {"type": "object", "properties": {"phone": {"type": "string", "description": "Digits, with country code if not Spanish"}}, "required": ["phone"]},
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
        "name": "confirm_personal_data",
        "description": (
            "Confirm the details shown in the latest read-back message (the one with the 'It's right' / 'Fix it' options). "
            "Call it ONLY when the person clearly says in their own words that those details are correct "
            "(e.g. 'está tudo certo', 'sí, correcto', 'yes that's right'). Never call it on your own initiative, "
            "never when they want to change something (use submit_personal_data for corrections)."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "save_appointment_reminder",
        "description": "Remember an appointment the person booked themselves (for example the identity check for the digital certificate) so Vale reminds them the day before. Use the date exactly as they said it, written as DD/MM/YYYY HH:MM.",
        "input_schema": {"type": "object", "properties": {
            "what": {"type": "string", "description": "Short description, e.g. 'Acreditación certificado digital'"},
            "starts_at": {"type": "string", "description": "DD/MM/YYYY HH:MM"},
            "place": {"type": "string", "description": "Office name or address, if given"},
        }, "required": ["what", "starts_at"]},
    },
    {
        "name": "set_language",
        "description": "Switch the language of Vale's fixed messages (read-backs, slot offers, reminders) when the person writes in another language than the current one.",
        "input_schema": {"type": "object", "properties": {"lang": {"type": "string", "enum": ["pt", "es", "en"]}}, "required": ["lang"]},
    },
    {
        "name": "set_reply_mode",
        "description": "Change how Vale answers when the user asks: 'audio' (always voice notes), 'text' (never voice), 'mirror' (voice only when they send voice).",
        "input_schema": {"type": "object", "properties": {"mode": {"type": "string", "enum": ["audio", "text", "mirror"]}}, "required": ["mode"]},
    },
    {
        "name": "register_interest",
        "description": "Note, once, that the user is interested in a service Vale guides but does not book yet.",
        "input_schema": {"type": "object", "properties": {"topic": {"type": "string", "enum": ["digital_certificate"]}}, "required": ["topic"]},
    },
    {
        "name": "request_data_deletion",
        "description": "Ask the user to confirm deletion of all their data (sends a confirmation button).",
        "input_schema": {"type": "object", "properties": {}},
    },
]


class Agent:
    def __init__(self, client: AsyncAnthropic | None = None):
        self.client = client or llm.client()

    async def _run_tool(self, session: AsyncSession, user: User, name: str, args: dict, on_delete: Callable[[], Awaitable[None]],
                        on_data: Callable[[dict], Awaitable[None]] | None) -> Any:
        try:
            if name == "create_cita_request":
                province = find_province(args.get("province", "")) or args.get("province", "")
                req = await service.create_request(session, user, province, args.get("tramite_code", ""))
                profile = await service.get_profile(session, user.id)
                out = {"request_id": req.id, "state": req.state.value, "missing": service.missing_fields(profile, req.tramite_code, user.phone)}
                if req.state.value == "awaiting_payment":
                    out["note"] = "The activation payment link is sent automatically in a separate message within seconds. Monitoring starts once it is paid."
                return out
            if name == "get_my_status":
                profile = await service.get_profile(session, user.id)
                return {
                    "has_email": bool(profile.get("email")),
                    "details_waiting_for_confirmation": bool(await service.latest_pending(session, user.id)),
                    "requests": [
                        {"request_id": r.id, "province": r.province, "tramite": r.tramite_code, "state": r.state.value,
                         "checks": r.checks, "missing": service.missing_fields(profile, r.tramite_code, user.phone),
                         **({"payment_link": r.activation_link_url} if r.state.value == "awaiting_payment" and r.activation_link_url
                            and r.activation_link_url.startswith("http") else {})}
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
            if name == "save_contact_phone":
                digits = "".join(c for c in str(args.get("phone", "")) if c.isdigit())
                if not 9 <= len(digits) <= 15:
                    return {"error": "invalid_phone"}
                promoted = await service.set_contact_phone(session, user, digits)
                return {"saved": True, "now_monitoring": [r.id for r in promoted]}
            if name == "submit_personal_data":
                if on_data is None:
                    return {"error": "not_available"}
                await on_data({k: v for k, v in args.items() if v})
                return {"readback_sent": True}
            if name == "confirm_personal_data":
                pending = await service.latest_pending(session, user.id)
                if not pending:
                    return {"error": "nothing_to_confirm"}
                promoted = await service.confirm_pending(session, user, pending.id)
                audit.record(session, "data_confirmed_in_words", user.id)
                return {"confirmed": True, "now_monitoring": [r.id for r in promoted]}
            if name == "save_appointment_reminder":
                from app.notifications import appointment_date

                when = str(args.get("starts_at", ""))[:60]
                day = appointment_date(when)
                if day is None:
                    return {"error": "date_not_understood", "hint": "Ask for the date as DD/MM/YYYY"}
                if day < service.utcnow().date():
                    return {"error": "date_in_the_past"}
                session.add(ExternalAppointment(user_id=user.id, what=str(args.get("what", ""))[:120], starts_at=when,
                                                place=str(args.get("place", "") or "")[:200]))
                audit.record(session, "external_appointment_saved", user.id)
                return {"saved": True, "reminder": "the day before"}
            if name == "set_language":
                lang = args.get("lang")
                if lang not in ("pt", "es", "en"):
                    return {"error": "invalid_lang"}
                user.lang = lang
                return {"lang": lang}
            if name == "set_reply_mode":
                mode = args.get("mode")
                if mode not in ("audio", "text", "mirror"):
                    return {"error": "invalid_mode"}
                user.reply_mode = mode
                return {"reply_mode": mode}
            if name == "register_interest":
                topic = args.get("topic")
                if topic not in ("digital_certificate",):
                    return {"error": "unknown_topic"}
                action = f"interest:{topic}"
                seen = await session.scalar(select(AuditEvent.id).where(AuditEvent.user_id == user.id, AuditEvent.action == action).limit(1))
                if not seen:
                    audit.record(session, action, user.id)
                return {"noted": True}
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
            channel=WEB_NOTE.format(ttl=get_settings().offer_ttl_minutes) if service.is_web(user.phone) else WHATSAPP_NOTE,
            provinces=", ".join(PROVINCES.values()),
            tramites="; ".join(f"{t.code} = {t.label.get(user.lang, t.label['es'])}" for t in TRAMITES.values() if tramite_enabled(t.code)),
            cert_booking=CERT_BOOKING_ON if tramite_enabled("acreditacion_certificado") else CERT_BOOKING_OFF,
        )
        history = [h for h in (user.history or []) if h.get("role") in ("user", "assistant")][-MAX_HISTORY:]
        messages: list[dict] = [*history, {"role": "user", "content": text}]
        final = ""
        for _ in range(MAX_STEPS):
            async with llm.limiter():
                resp = await self.client.messages.create(
                    model=get_settings().claude_model, max_tokens=900, system=system, tools=TOOLS, messages=messages,
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
        if final == "[SKIP]":
            return final  # not for Vale: nothing is sent and nothing goes into the history
        # Store only plain-text turns; tool payloads stay out of the long-lived history.
        user.history = [*history, {"role": "user", "content": text}, {"role": "assistant", "content": final or "…"}][-MAX_HISTORY:]
        return final

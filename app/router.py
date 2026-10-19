"""Routes every incoming WhatsApp message.

Order matters: safety-critical paths (consent, approvals, deletion) are handled by plain
code before anything reaches the LLM.
"""

import logging
from collections import OrderedDict

from app import audit
from app.agent.orchestrator import Agent
from app.channels import Button, IncomingMessage, get_channel
from app.citas import service
from app.citas.catalog import TRAMITES, normalize
from app.db import session_scope
from app.documents.extractor import extract_document
from app.i18n import MESSAGES, guess_lang, t

log = logging.getLogger(__name__)

DELETE_WORDS = ("APAGA TUDO", "APAGAR TUDO", "APAGUE TUDO", "BORRALO TODO", "BORRAR TODO", "DELETE EVERYTHING", "DELETE MY DATA")
FIELD_LABELS = {
    "full_name": {"pt": "nome completo", "es": "nombre completo", "en": "full name"},
    "document_number": {"pt": "número do documento", "es": "número de documento", "en": "document number"},
    "nationality": {"pt": "nacionalidade", "es": "nacionalidad", "en": "nationality"},
}

_seen: OrderedDict[str, None] = OrderedDict()
_agent: Agent | None = None


def _get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent()
    return _agent


def set_agent(agent: Agent) -> None:
    global _agent
    _agent = agent


def _duplicate(message_id: str) -> bool:
    if not message_id:
        return False
    if message_id in _seen:
        return True
    _seen[message_id] = None
    if len(_seen) > 5000:
        _seen.popitem(last=False)
    return False


def _button_from_text(text: str) -> str | None:
    """Let users who can't see buttons answer by typing the label."""
    n = normalize(text).strip(" .!")
    for key, payload in (("consent_yes", "consent:yes"), ("consent_no", "consent:no"), ("delete_yes", "delete:yes")):
        if n in {normalize(v) for v in MESSAGES[key].values()}:
            return payload
    return None


async def handle(msg: IncomingMessage) -> None:
    if _duplicate(msg.message_id):
        return
    ch = get_channel()
    try:
        async with session_scope() as s:
            user, is_new = await service.get_or_create_user(s, msg.phone, msg.push_name, guess_lang(msg.text))
            lang = user.lang
            button = msg.button_id or _button_from_text(msg.text)

            # 1) Consent gate: nothing is processed before an explicit yes.
            if button == "consent:yes":
                await service.give_consent(s, user)
                await ch.send_text(msg.phone, await _get_agent().reply(s, user, "(the user just accepted the privacy terms; greet and ask what they need)", lambda: _ask_delete(msg.phone, lang)))
                return
            if button == "consent:no":
                await service.delete_user_data(s, user)
                await ch.send_text(msg.phone, t("consent_declined", lang))
                return
            if user.consent_at is None:
                await ch.send_buttons(msg.phone, t("consent", lang), [
                    Button("consent:yes", t("consent_yes", lang)), Button("consent:no", t("consent_no", lang)),
                ])
                return

            # 2) Deletion: confirm, then erase.
            if button == "delete:yes":
                await service.delete_user_data(s, user)
                await ch.send_text(msg.phone, t("deleted", lang))
                return
            if button == "delete:no":
                await ch.send_text(msg.phone, "👍")
                return
            if any(w in normalize(msg.text) for w in DELETE_WORDS):
                await _ask_delete(msg.phone, lang)
                return

            # 3) Offer approval: the only route to a booking.
            if button and button.startswith("offer:"):
                _, offer_id, action = button.split(":")
                try:
                    if action == "confirm":
                        await service.approve_offer(s, user, int(offer_id))
                        await ch.send_text(msg.phone, t("booking", lang))
                    else:
                        await service.reject_offer(s, user, int(offer_id))
                        await ch.send_text(msg.phone, t("offer_rejected", lang))
                except service.RuleError:
                    await ch.send_text(msg.phone, t("offer_expired", lang))
                return

            # 4) Documents.
            if msg.has_media:
                await _handle_document(s, user, msg)
                return

            # 5) Everything else: conversation.
            if msg.text:
                reply = await _get_agent().reply(s, user, msg.text, lambda: _ask_delete(msg.phone, lang))
                if reply:
                    await ch.send_text(msg.phone, reply)
    except Exception:
        log.exception("failed to handle message %s", msg.message_id)
        try:
            await ch.send_text(msg.phone, t("error", "pt"))
        except Exception:
            log.exception("failed to send error message")


async def _ask_delete(phone: str, lang: str) -> None:
    await get_channel().send_buttons(phone, t("delete_confirm", lang), [
        Button("delete:yes", t("delete_yes", lang)), Button("delete:no", t("delete_no", lang)),
    ])


async def _handle_document(s, user, msg: IncomingMessage) -> None:
    ch = get_channel()
    lang = user.lang
    image, mime = await ch.download_media(msg)
    doc = await extract_document(image, mime)
    del image  # never persisted
    if doc.document_kind == "unreadable" or doc.confidence == "low":
        audit.record(s, "document_unreadable", user.id, issues=doc.issues)
        await ch.send_text(msg.phone, {
            "pt": "Não consegui ler bem esse documento. Mande uma foto nítida da página com a sua foto, sem reflexo e com os quatro cantos aparecendo.",
            "es": "No he podido leer bien el documento. Mándame una foto nítida de la página con tu foto, sin reflejos y con las cuatro esquinas visibles.",
            "en": "I couldn't read that document well. Send a sharp photo of the photo page, no glare, all four corners visible.",
        }[lang])
        return
    promoted = await service.store_document(s, user, doc)
    profile = await service.get_profile(s, user.id)
    lines = [f"✓ {doc.full_name or ''}".strip(), f"{doc.document_number or ''} · {doc.nationality or ''}".strip(" ·")]
    if "expired" in doc.issues:
        lines.append({"pt": "⚠️ Parece que este documento está vencido.", "es": "⚠️ Parece que este documento está caducado.", "en": "⚠️ This document looks expired."}[lang])
    actives = await service.active_requests(s, user.id)
    missing = sorted({f for r in actives for f in service.missing_fields(profile, r.tramite_code)})
    if promoted:
        names = ", ".join(TRAMITES[r.tramite_code].label[lang] for r in promoted)
        lines.append({"pt": f"Pronto! Já estou vigiando as vagas de: {names}.", "es": f"¡Listo! Ya vigilo los huecos de: {names}.", "en": f"Done! Now watching slots for: {names}."}[lang])
    elif missing:
        fields = ", ".join(FIELD_LABELS[f][lang] for f in missing)
        lines.append({"pt": f"Ainda falta: {fields}.", "es": f"Aún falta: {fields}.", "en": f"Still missing: {fields}."}[lang])
    elif not actives:
        lines.append({"pt": "Qual cita você precisa e em qual província?", "es": "¿Qué cita necesitas y en qué provincia?", "en": "Which appointment do you need, and in which province?"}[lang])
    await ch.send_text(msg.phone, "\n".join(lines))

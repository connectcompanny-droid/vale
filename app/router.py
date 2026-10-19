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
from app.documents.transcriber import TranscriptionError, transcribe
from app.i18n import MESSAGES, format_fields, guess_lang, t
from app.voice.tts import SynthesisError, get_synthesizer

log = logging.getLogger(__name__)

DELETE_WORDS = ("APAGA TUDO", "APAGAR TUDO", "APAGUE TUDO", "BORRALO TODO", "BORRAR TODO", "DELETE EVERYTHING", "DELETE MY DATA")
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
    for key, payload in (("consent_yes", "consent:yes"), ("consent_no", "consent:no"), ("delete_yes", "delete:yes"),
                         ("readback_ok", "data:latest:ok"), ("readback_fix", "data:latest:fix")):
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
                reply = await _get_agent().reply(s, user, "(the user just accepted the privacy terms; greet and ask what they need)", lambda: _ask_delete(msg.phone, lang), _data_hook(s, user, msg.phone, "text"))
                await _deliver(s, user, msg.phone, reply, client_spoke=False)
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

            # 3) Read-back of personal data: nothing is used until the user confirms it.
            if button and button.startswith("data:"):
                _, doc_id, action = button.split(":")
                pending = await service.latest_pending(s, user.id)
                if not pending or (doc_id != "latest" and int(doc_id) != pending.id):
                    await ch.send_text(msg.phone, t("fix_prompt", lang) if action == "fix" else t("data_saved", lang))
                    return
                if action == "ok":
                    promoted = await service.confirm_pending(s, user, pending.id)
                    await ch.send_text(msg.phone, t("data_saved", lang) + _next_step(lang, promoted, await service.active_requests(s, user.id)))
                else:
                    await ch.send_text(msg.phone, t("fix_prompt", lang))
                return

            # 4) Offer approval: the only route to a booking.
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

            # 5) Voice notes: transcribe, show the client what was understood, then treat it as text.
            text, source = msg.text, "text"
            if msg.has_media and msg.media_kind == "audio":
                audio, mime = await ch.download_media(msg)
                try:
                    text = await transcribe(audio, mime, lang)
                except TranscriptionError as e:
                    audit.record(s, "audio_failed", user.id, reason=str(e))
                    await ch.send_text(msg.phone, t("audio_failed", lang))
                    return
                finally:
                    del audio  # never persisted
                await ch.send_text(msg.phone, t("transcript", lang, text=text))
                text, source = f"[voice note] {text}", "audio"
            elif msg.has_media:
                # 6) Document photos.
                await _handle_document(s, user, msg)
                return

            # 7) Everything else: conversation (typed or spoken details go through submit_personal_data).
            if text:
                reply = await _get_agent().reply(s, user, text, lambda: _ask_delete(msg.phone, lang), _data_hook(s, user, msg.phone, source))
                if reply:
                    await _deliver(s, user, msg.phone, reply, client_spoke=source == "audio")
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


def _should_speak(user, client_spoke: bool, text: str) -> bool:
    from app.config import get_settings

    if not get_synthesizer().enabled or len(text) > get_settings().tts_max_chars:
        return False
    mode = getattr(user, "reply_mode", "mirror") or "mirror"
    return mode == "audio" or (mode == "mirror" and client_spoke)


async def _deliver(s, user, phone: str, text: str, client_spoke: bool) -> None:
    """Send a conversational reply as a voice note when appropriate, otherwise as text.
    Read-backs, offers and legal texts never go through here: they stay as text with buttons."""
    if not text:
        return
    ch = get_channel()
    if _should_speak(user, client_spoke, text):
        try:
            audio, mime = await get_synthesizer().synthesize(text, user.lang)
            await ch.send_audio(phone, audio, mime)
            audit.record(s, "voice_reply_sent", user.id, chars=len(text))
            return
        except (SynthesisError, Exception) as e:  # never lose the message because of TTS
            log.warning("voice reply failed, falling back to text: %s", e)
    await ch.send_text(phone, text)


def _data_hook(s, user, phone: str, source: str):
    async def on_data(fields: dict) -> None:
        doc = await service.save_pending_data(s, user, fields, source=source)
        await _send_readback(phone, user.lang, doc)
    return on_data


async def _send_readback(phone: str, lang: str, doc) -> None:
    fields = service.pending_fields(doc)
    await get_channel().send_buttons(phone, t("readback", lang, fields=format_fields(fields, lang)), [
        Button(f"data:{doc.id}:ok", t("readback_ok", lang)), Button(f"data:{doc.id}:fix", t("readback_fix", lang)),
    ])


def _next_step(lang: str, promoted: list, actives: list) -> str:
    if promoted:
        names = ", ".join(TRAMITES[r.tramite_code].label[lang] for r in promoted)
        return "\n" + {"pt": f"Já estou vigiando as vagas de: {names}.", "es": f"Ya vigilo los huecos de: {names}.", "en": f"Now watching slots for: {names}."}[lang]
    if not actives:
        return "\n" + {"pt": "Qual cita você precisa e em qual província?", "es": "¿Qué cita necesitas y en qué provincia?", "en": "Which appointment do you need, and in which province?"}[lang]
    return ""


async def _handle_document(s, user, msg: IncomingMessage) -> None:
    ch = get_channel()
    lang = user.lang
    image, mime = await ch.download_media(msg)
    doc = await extract_document(image, mime)
    del image  # never persisted
    if doc.document_kind == "unreadable" or doc.confidence == "low":
        audit.record(s, "document_unreadable", user.id, issues=doc.issues)
        await ch.send_text(msg.phone, {
            "pt": "Não consegui ler bem esse documento. Mande uma foto nítida da página com a sua foto, sem reflexo e com os quatro cantos aparecendo. Se preferir, pode escrever ou mandar um áudio com os dados.",
            "es": "No he podido leer bien el documento. Mándame una foto nítida de la página con tu foto, sin reflejos y con las cuatro esquinas visibles. Si prefieres, escríbeme o mándame un audio con los datos.",
            "en": "I couldn't read that document well. Send a sharp photo of the photo page, no glare, all four corners visible. Or type your details, or send a voice note.",
        }[lang])
        return
    if "expired" in doc.issues:
        await ch.send_text(msg.phone, {"pt": "⚠️ Parece que este documento está vencido.", "es": "⚠️ Parece que este documento está caducado.", "en": "⚠️ This document looks expired."}[lang])
    pending = await service.store_document(s, user, doc)
    await _send_readback(msg.phone, lang, pending)

"""Routes every incoming WhatsApp message.

Order matters: safety-critical paths (consent, approvals, deletion) are handled by plain
code before anything reaches the LLM.
"""

import logging

from app import audit
from app.agent import relevance
from app.agent.orchestrator import Agent
from app.channels import web as web_channel
from app.config import get_settings
from app.channels import Button, IncomingMessage, get_channel
from app.citas import service
from app.citas.catalog import TRAMITES, normalize
from sqlalchemy.exc import IntegrityError

from app.db import session_scope
from app.models import ProcessedMessage
from app.queue import user_lock
from app.documents.extractor import extract_document
from app.documents.transcriber import TranscriptionError, transcribe
from app.i18n import MESSAGES, detect_lang, format_fields, guess_lang, t
from app.voice.tts import SynthesisError, get_synthesizer

log = logging.getLogger(__name__)
SKIP = "[SKIP]"

DELETE_WORDS = ("APAGA TUDO", "APAGAR TUDO", "APAGUE TUDO", "BORRALO TODO", "BORRAR TODO", "BORRA TODO", "DELETE EVERYTHING", "DELETE MY DATA")
_agent: Agent | None = None


def _get_agent() -> Agent:
    global _agent
    if _agent is None:
        _agent = Agent()
    return _agent


def set_agent(agent: Agent) -> None:
    global _agent
    _agent = agent


async def _first_time(message_id: str) -> bool:
    """Record the message id; False if any replica already processed it (provider retries)."""
    if not message_id:
        return True
    try:
        async with session_scope() as s:
            s.add(ProcessedMessage(message_id=message_id))
        return True
    except IntegrityError:
        return False


def _button_from_text(text: str) -> str | None:
    """Let users who can't see buttons answer by typing the label."""
    n = normalize(text).strip(" .!")
    for key, payload in (("consent_yes", "consent:yes"), ("consent_no", "consent:no"), ("delete_yes", "delete:yes"),
                         ("readback_ok", "data:latest:ok"), ("readback_fix", "data:latest:fix")):
        if n in {normalize(v) for v in MESSAGES[key].values()}:
            return payload
    return None


async def _offer_from_text(s, user_id: int, text: str) -> str | None:
    """Channels without buttons: a typed "Confirmar" / "Não quero" answers the newest pending offer."""
    n = normalize(text or "").strip(" .!")
    action = next((a for key, a in (("confirm", "confirm"), ("reject", "reject"))
                   if n in {normalize(v) for v in MESSAGES[key].values()}), None)
    if not action:
        return None
    offer_id = await service.latest_pending_offer_id(s, user_id)
    return f"offer:{offer_id}:{action}" if offer_id else None


async def handle(msg: IncomingMessage) -> None:
    if not await _first_time(msg.message_id):
        return
    async with user_lock(msg.phone):
        await _handle(msg)


async def _meant_for_vale(msg: IncomingMessage) -> bool:
    """Someone Vale has never talked to: answer only if they want something Vale offers.
    Nothing about an ignored message is stored (only its id, to drop provider retries)."""
    if web_channel.is_web(msg.phone) or not get_settings().reply_only_relevant:
        return True
    async with session_scope() as s:
        if await service.find_user(s, msg.phone):
            return True
    text = msg.text or ""
    if msg.has_media and msg.media_kind == "audio" and not text:
        try:
            audio, mime = await get_channel().download_media(msg)
            text = await transcribe(audio, mime, None)
            del audio
        except Exception:
            return False
    if await relevance.is_about_services(text):
        return True
    log.info("ignored a message not related to Vale's services")
    return False


async def _handle(msg: IncomingMessage) -> None:
    ch = get_channel()
    if not await _meant_for_vale(msg):
        return
    lang = guess_lang(msg.text)
    try:
        async with session_scope() as s:
            user, is_new = await service.get_or_create_user(s, msg.phone, msg.push_name, lang)
            if user.consent_at is None and (seen := detect_lang(msg.text)) and seen != user.lang:
                user.lang = seen  # a first "?" or a photo says nothing; the next message usually does
            lang = user.lang
            button = msg.button_id or _button_from_text(msg.text) or await _offer_from_text(s, user.id, msg.text)

            # 1) Consent gate: nothing is processed before an explicit yes.
            if button == "consent:yes":
                await service.give_consent(s, user)
                first = next((h.get("content") for h in (user.history or []) if h.get("role") == "first"), None)
                user.history = []
                note = ("(the person just accepted the privacy terms. Before accepting they had written: \"" + first[:600] + "\". "
                        "Greet them briefly by answering that message as a person would; if it already says what they need, act on it.)"
                        if first else "(the person just accepted the privacy terms; greet them warmly in one or two sentences and ask what brings them here)")
                reply = await _get_agent().reply(s, user, note, lambda: _ask_delete(msg.phone, lang), _data_hook(s, user, msg.phone, "text"))
                await _deliver(s, user, msg.phone, reply, client_spoke=False)
                return
            if button == "consent:no":
                await service.delete_user_data(s, user)
                await ch.send_text(msg.phone, t("consent_declined", lang))
                return
            if user.consent_at is None:
                if msg.text and not msg.has_media and not button:
                    # Kept only until they answer: used to reply to it after "yes", erased with the user on "no".
                    user.history = [{"role": "first", "content": msg.text[:600]}]
                await ch.send_buttons(msg.phone, _consent_text(lang), [
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
                # 6) Document photos. A caption ("cita de NIE com este documento") is answered too.
                if await _handle_document(s, user, msg) and msg.text:
                    note = (f"(the person sent a photo of a document with this message: \"{msg.text[:600]}\". "
                            "The details read from it were already sent to them separately for confirmation; do not repeat them. "
                            "Act on what they asked, e.g. open the request, and ask only what is still missing.)")
                    reply = await _get_agent().reply(s, user, note, lambda: _ask_delete(msg.phone, lang), _data_hook(s, user, msg.phone, "text"))
                    await _deliver(s, user, msg.phone, reply, client_spoke=False)
                return

            # 7) Everything else: conversation (typed or spoken details go through submit_personal_data).
            if text:
                reply = await _get_agent().reply(s, user, text, lambda: _ask_delete(msg.phone, lang), _data_hook(s, user, msg.phone, source))
                if reply:
                    await _deliver(s, user, msg.phone, reply, client_spoke=source == "audio")
    except Exception:
        log.exception("failed to handle message %s", msg.message_id)
        try:
            await ch.send_text(msg.phone, t("error", lang))
        except Exception:
            log.exception("failed to send error message")


def _consent_text(lang: str) -> str:
    from app.config import get_settings

    cfg = get_settings()
    text = t("consent", lang)
    if cfg.terms_url or cfg.privacy_url:
        text += t("consent_links", lang, terms=cfg.terms_url or "-", privacy=cfg.privacy_url or "-")
    return text


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
    if not text or text.strip() == SKIP:
        return  # the agent saw nothing for Vale in this message
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


UNREADABLE = {
    "pt": "Não consegui ler bem esse documento. Mande uma foto nítida da página com a sua foto, sem reflexo e com os quatro cantos aparecendo. Se preferir, pode escrever ou mandar um áudio com os dados.",
    "es": "No he podido leer bien el documento. Mándame una foto nítida de la página con tu foto, sin reflejos y con las cuatro esquinas visibles. Si prefieres, escríbeme o mándame un audio con los datos.",
    "en": "I couldn't read that document well. Send a sharp photo of the photo page, no glare, all four corners visible. Or type your details, or send a voice note.",
}
DOC_FAILED = {
    "pt": "Recebi a foto, mas não consegui abri-la agora. Pode mandar de novo? Se preferir, escreva ou mande um áudio com o nome completo, o número do passaporte e a nacionalidade.",
    "es": "He recibido la foto, pero no he podido abrirla ahora. ¿Me la mandas otra vez? Si prefieres, escríbeme o mándame un audio con el nombre completo, el número de pasaporte y la nacionalidad.",
    "en": "I got the photo but couldn't open it just now. Could you send it again? Or type or record your full name, passport number and nationality.",
}


async def _handle_document(s, user, msg: IncomingMessage) -> bool:
    """Read a document photo and send the read-back. True when the read-back went out."""
    ch = get_channel()
    lang = user.lang
    try:
        image, mime = await ch.download_media(msg)
        doc = await extract_document(image, mime)
        del image  # never persisted
    except Exception:
        log.exception("document photo from a client could not be read")
        audit.record(s, "document_failed", user.id)
        await ch.send_text(msg.phone, DOC_FAILED.get(lang, DOC_FAILED["es"]))
        return False
    if doc.document_kind == "unreadable" or doc.confidence == "low":
        audit.record(s, "document_unreadable", user.id, issues=doc.issues)
        await ch.send_text(msg.phone, UNREADABLE.get(lang, UNREADABLE["es"]))
        return False
    if "expired" in doc.issues:
        await ch.send_text(msg.phone, {"pt": "⚠️ Parece que este documento está vencido.", "es": "⚠️ Parece que este documento está caducado.", "en": "⚠️ This document looks expired."}[lang])
    pending = await service.store_document(s, user, doc)
    await _send_readback(msg.phone, lang, pending)
    return True

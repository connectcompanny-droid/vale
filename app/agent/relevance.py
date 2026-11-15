"""Decides whether a WhatsApp message from someone new is meant for Vale.

The WhatsApp number Vale uses can also receive personal or unrelated messages. Vale only answers
people who want something it offers (immigration appointments in Spain, the documents and steps
around them, the digital certificate). Everyone else gets no reply and nothing is stored about them.

Order: an obvious keyword decides for free; otherwise a small model answers YES or NO. The
message is not stored either way. If the classifier fails, Vale stays silent (a missed lead is
better than answering the owner's personal contacts).
"""

import logging
import re

from app import llm
from app.citas.catalog import normalize
from app.config import get_settings

log = logging.getLogger(__name__)

# Words that only show up when someone is asking about Vale's services (accents removed, upper case).
KEYWORDS = (
    # Only words that are specific to Vale's world. Everyday words that also mean other things
    # ("vale" = ok, "cita" = date, "visto" = seen, "agendar") are left to the classifier.
    "NIE", "TIE", "CITA PREVIA", "PEDIR CITA", "MARCAR CITA", "CITA DE EXTRANJERIA", "CITA PARA EL NIE",
    "HUELLAS", "TOMA DE HUELLAS", "EXTRANJERIA", "ESTRANGEIRIA", "CERTIFICADO DIGITAL", "FNMT",
    "CERTIFICADO DE REGISTRO", "REGISTRO DE CIUDADANO", "CERTIFICADO DE RESIDENCIA", "ARRAIGO",
    "PERMISO DE RESIDENCIA", "AUTORIZACION DE RESIDENCIA", "TARJETA DE RESIDENCIA", "TARJETA ROJA",
    "NUMERO DE IDENTIDAD DE EXTRANJERO", "ICP", "SEDE ELECTRONICA",
)
_KW = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(set(KEYWORDS), key=len, reverse=True)) + r")\b")

PROMPT = (
    "You screen WhatsApp messages for Vale, a service that gets immigration appointments (cita previa de "
    "extranjería: NIE, TIE, fingerprints, EU registration certificate, residence certificates) for foreigners in "
    "Spain, helps with the documents for them, and guides people to get the Spanish digital certificate. "
    "The same number also receives unrelated personal or business messages, which must be ignored. "
    "Answer YES only if the message shows the sender wants or asks about something Vale offers or about Vale "
    "itself (including vague openers like 'hi, I saw your service' or questions about papers or appointments in "
    "Spain). Answer NO for anything else: greetings with no context, personal chat, other businesses, spam, "
    "deliveries, family. Reply with exactly one word: YES or NO."
)


def keyword_hit(text: str) -> bool:
    return bool(_KW.search(normalize(text or "")))


async def is_about_services(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return False
    if keyword_hit(text):
        return True
    try:
        async with llm.limiter():
            resp = await llm.client().messages.create(
                model=get_settings().classifier_model, max_tokens=5, system=PROMPT,
                messages=[{"role": "user", "content": text[:1000]}],
            )
        answer = "".join(b.text for b in resp.content if b.type == "text").strip().upper()
        return answer.startswith("YES") or answer.startswith("SI") or answer.startswith("SÍ")
    except Exception as e:
        log.warning("relevance check failed, staying silent: %s", e)
        return False

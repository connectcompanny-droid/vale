"""Fixed system messages. Conversational replies come from the agent; these are the
legally relevant or safety-critical texts, kept deterministic in all three languages."""

MESSAGES: dict[str, dict[str, str]] = {
    "consent": {
        "pt": (
            "Olá! Eu sou o Vale 👋 Eu vigio as vagas de cita previa de extranjería e reservo a sua, "
            "sempre com a sua confirmação.\n\n"
            "Para isso preciso ler os seus documentos. Seus dados ficam criptografados na União Europeia, "
            "são usados só para a SUA cita e você pode apagar tudo a qualquer momento escrevendo \"apaga tudo\".\n\n"
            "Você aceita?"
        ),
        "es": (
            "¡Hola! Soy Vale 👋 Vigilo los huecos de cita previa de extranjería y reservo la tuya, "
            "siempre con tu confirmación.\n\n"
            "Para ello necesito leer tus documentos. Tus datos se guardan cifrados en la Unión Europea, "
            "se usan solo para TU cita y puedes borrarlo todo cuando quieras escribiendo \"bórralo todo\".\n\n"
            "¿Aceptas?"
        ),
        "en": (
            "Hi! I'm Vale 👋 I watch for immigration cita previa slots and book yours, "
            "always with your confirmation.\n\n"
            "To do that I need to read your documents. Your data is encrypted in the European Union, "
            "used only for YOUR appointment, and you can delete everything anytime by writing \"delete everything\".\n\n"
            "Do you agree?"
        ),
    },
    "consent_yes": {"pt": "Aceito", "es": "Acepto", "en": "I agree"},
    "consent_no": {"pt": "Não aceito", "es": "No acepto", "en": "No thanks"},
    "consent_declined": {
        "pt": "Tudo bem. Não guardei nenhum dado seu. Se mudar de ideia, é só escrever de novo.",
        "es": "De acuerdo. No he guardado ningún dato tuyo. Si cambias de idea, escríbeme otra vez.",
        "en": "No problem. I haven't stored any of your data. If you change your mind, just write again.",
    },
    "offer": {
        "pt": "🎉 Abriu vaga!\n\n📍 {office}\n🗓 {starts_at}\n\nPosso reservar no seu nome? A vaga fica guardada para você por {ttl} minutos.",
        "es": "🎉 ¡Hay hueco!\n\n📍 {office}\n🗓 {starts_at}\n\n¿La reservo a tu nombre? Te la guardo {ttl} minutos.",
        "en": "🎉 A slot opened!\n\n📍 {office}\n🗓 {starts_at}\n\nShall I book it in your name? It's held for you for {ttl} minutes.",
    },
    "confirm": {"pt": "Confirmar", "es": "Confirmar", "en": "Confirm"},
    "reject": {"pt": "Não quero", "es": "No la quiero", "en": "Skip"},
    "offer_expired": {
        "pt": "Essa vaga expirou, mas continuo vigiando e te aviso da próxima.",
        "es": "Ese hueco ha caducado, pero sigo vigilando y te aviso del siguiente.",
        "en": "That slot expired, but I'm still watching and will tell you about the next one.",
    },
    "offer_rejected": {
        "pt": "Combinado, deixei essa passar. Continuo vigiando.",
        "es": "Entendido, la dejo pasar. Sigo vigilando.",
        "en": "Got it, skipping that one. Still watching.",
    },
    "booking": {
        "pt": "Reservando agora… ⏳",
        "es": "Reservando ahora… ⏳",
        "en": "Booking now… ⏳",
    },
    "booked": {
        "pt": "Reservado ✓\n\n📍 {office}\n🗓 {starts_at}\n{reference}\n\nLeve o passaporte original e o justificante. Te lembro na véspera.",
        "es": "Reservado ✓\n\n📍 {office}\n🗓 {starts_at}\n{reference}\n\nLleva el pasaporte original y el justificante. Te aviso el día antes.",
        "en": "Booked ✓\n\n📍 {office}\n🗓 {starts_at}\n{reference}\n\nBring your original passport and the receipt. I'll remind you the day before.",
    },
    "needs_human": {
        "pt": "O site do governo pediu uma verificação que só você pode fazer. Abra este link e conclua por lá; os seus dados já estão aqui à mão:\n{url}",
        "es": "La web de la administración pide una verificación que solo tú puedes hacer. Abre este enlace y termina allí; tus datos los tienes aquí:\n{url}",
        "en": "The government site asked for a check only you can do. Open this link and finish there; your details are right here:\n{url}",
    },
    "delete_confirm": {
        "pt": "Quer mesmo apagar todos os seus dados e cancelar seus pedidos? Isso não pode ser desfeito.",
        "es": "¿Seguro que quieres borrar todos tus datos y cancelar tus solicitudes? No se puede deshacer.",
        "en": "Do you really want to delete all your data and cancel your requests? This can't be undone.",
    },
    "delete_yes": {"pt": "Apagar tudo", "es": "Borrar todo", "en": "Delete all"},
    "delete_no": {"pt": "Cancelar", "es": "Cancelar", "en": "Cancel"},
    "deleted": {
        "pt": "Pronto. Apaguei todos os seus dados. Se precisar de novo, é só escrever.",
        "es": "Hecho. He borrado todos tus datos. Si me necesitas, escríbeme.",
        "en": "Done. I've deleted all your data. If you need me again, just write.",
    },
    "readback": {
        "pt": "Confere se está tudo certinho? Um erro numa letra pode fazer a cita não ser atendida.\n\n{fields}",
        "es": "¿Está todo correcto? Un error en una letra puede hacer que no te atiendan.\n\n{fields}",
        "en": "Is everything exactly right? One wrong letter can get the appointment refused.\n\n{fields}",
    },
    "readback_ok": {"pt": "Está certo", "es": "Está bien", "en": "It's right"},
    "readback_fix": {"pt": "Corrigir", "es": "Corregir", "en": "Fix it"},
    "fix_prompt": {
        "pt": "Me diga o que está errado, por texto ou áudio. Ex.: \"o sobrenome é SOUZA com Z\".",
        "es": "Dime qué está mal, por texto o audio. Ej.: \"el apellido es SOUZA con Z\".",
        "en": "Tell me what's wrong, by text or voice note. E.g. \"the surname is SOUZA with a Z\".",
    },
    "data_saved": {
        "pt": "Perfeito, dados confirmados ✓",
        "es": "Perfecto, datos confirmados ✓",
        "en": "Great, details confirmed ✓",
    },
    "transcript": {
        "pt": "🎙️ Entendi assim: \"{text}\"",
        "es": "🎙️ Te he entendido así: \"{text}\"",
        "en": "🎙️ Here's what I heard: \"{text}\"",
    },
    "audio_failed": {
        "pt": "Não consegui entender o áudio. Pode mandar de novo, falando mais perto do microfone, ou escrever?",
        "es": "No he podido entender el audio. ¿Puedes mandarlo otra vez, más cerca del micrófono, o escribirlo?",
        "en": "I couldn't make out the voice note. Could you send it again, closer to the mic, or type it?",
    },
    "error": {
        "pt": "Tive um problema agora. Já estou olhando; tente de novo em instantes.",
        "es": "He tenido un problema. Ya lo estoy mirando; inténtalo en un momento.",
        "en": "I hit a problem. I'm on it; please try again shortly.",
    },
}


FIELD_NAMES = {
    "full_name": {"pt": "👤 Nome", "es": "👤 Nombre", "en": "👤 Name"},
    "document_number": {"pt": "🪪 Documento", "es": "🪪 Documento", "en": "🪪 Document"},
    "nie": {"pt": "🔢 NIE", "es": "🔢 NIE", "en": "🔢 NIE"},
    "nationality": {"pt": "🌍 Nacionalidade", "es": "🌍 Nacionalidad", "en": "🌍 Nationality"},
    "birth_date": {"pt": "🎂 Nascimento", "es": "🎂 Nacimiento", "en": "🎂 Date of birth"},
}


def format_fields(fields: dict, lang: str) -> str:
    rows = [f"{FIELD_NAMES[k].get(lang, FIELD_NAMES[k]['pt'])}: {fields[k]}" for k in FIELD_NAMES if fields.get(k)]
    return "\n".join(rows)


def t(key: str, lang: str, **kwargs) -> str:
    table = MESSAGES[key]
    return table.get(lang, table["pt"]).format(**kwargs)


def guess_lang(text: str) -> str:
    s = f" {text.lower()} "
    es = sum(w in s for w in (" hola", " necesito", " quiero", " gracias", " cita ", "¿", " por favor"))
    pt = sum(w in s for w in (" olá", " oi ", " preciso", " quero", " obrigad", " você", " vocês"))
    en = sum(w in s for w in (" hello", " hi ", " need", " want", " thanks", " appointment", " please"))
    best = max((pt, "pt"), (es, "es"), (en, "en"))
    return best[1] if best[0] > 0 else "pt"

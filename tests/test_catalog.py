"""Real trámite options captured from the Granada cita previa page (Oct 2026)."""

from app.citas.catalog import TRAMITES, label_matches

GRANADA = [
    "SOLICITUD DE AUTORIZACIONES",
    "POLICIA - CERTIFICADOS CONCORDANCIA",
    "POLICIA - RECOGIDA DE TARJETA DE IDENTIDAD DE EXTRANJERO (TIE)",
    "POLICIA - TÍTULOS DE VIAJE",
    "POLICIA- RESGUARDO PRÓRROGA DE DERECHOS POR RECURSO ADMINISTRATIVO O JUDICIAL ASILO",
    "POLICIA- SOLICITUD ASILO",
    "POLICIA-AUTORIZACIÓN DE REGRESO",
    "POLICIA-CARTA DE INVITACIÓN",
    "POLICIA-CERTIFICADO DE REGISTRO DE CIUDADANO DE LA U.E.",
    "POLICIA-CERTIFICADOS Y ASIGNACION NIE",
    "POLICÍA - CÉDULA DE INSCRIPCIÓN",
    "POLICÍA - PRORROGA DE ESTANCIA CON VISADO",
    "POLICÍA - PRORROGA DE ESTANCIA SIN VISADO",
    "POLICÍA - SOLICITUD DE APATRIDA",
    "POLICÍA-DECLARACIÓN DE ENTRADA",
    "POLICÍA-SOLICITUD TARJETA ROJA",
    "POLICÍA-TOMA DE HUELLAS (EXPEDICIÓN DE TARJETA) INICIAL, RENOVACIÓN, DUPLICADO Y LEY 14/2013",
]


def first_match(code: str) -> str | None:
    return next((o for o in GRANADA if label_matches(TRAMITES[code], o)), None)


def test_granada_tramites_resolve_to_the_right_option():
    assert first_match("nie") == "POLICIA-CERTIFICADOS Y ASIGNACION NIE"
    assert first_match("huellas").startswith("POLICÍA-TOMA DE HUELLAS")
    assert first_match("certificado_ue") == "POLICIA-CERTIFICADO DE REGISTRO DE CIUDADANO DE LA U.E."
    assert first_match("certificados") == "POLICIA - CERTIFICADOS CONCORDANCIA"
    assert first_match("recogida_tie").startswith("POLICIA - RECOGIDA DE TARJETA")


def test_other_wordings_still_match():
    assert label_matches(TRAMITES["nie"], "POLICÍA-ASIGNACIÓN DE NIE")
    assert not label_matches(TRAMITES["nie"], "POLICIA - TÍTULOS DE VIAJE")

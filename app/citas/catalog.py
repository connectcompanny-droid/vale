"""Procedures (trámites) and provinces Vale supports.

`match` holds alternatives searched in the trámite dropdowns of the cita previa site
(case-insensitive, accents and punctuation ignored). An option matches an alternative when
it contains ALL of its words, so "ASIGNACION NIE" matches both "POLICIA-CERTIFICADOS Y
ASIGNACION NIE" (Granada) and "POLICÍA-ASIGNACIÓN DE NIE". Labels vary by province: validate
with scripts/try_icp.py before enabling one.
"""

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Tramite:
    code: str
    label: dict[str, str]
    match: tuple[str, ...]
    needs: tuple[str, ...]  # personal fields required to request it


TRAMITES: dict[str, Tramite] = {
    "nie": Tramite(
        code="nie",
        label={"pt": "Atribuição de NIE", "es": "Asignación de NIE", "en": "NIE assignment"},
        match=("ASIGNACION NIE",),
        needs=("full_name", "document_number", "nationality"),
    ),
    "huellas": Tramite(
        code="huellas",
        label={"pt": "Toma de huellas (TIE)", "es": "Toma de huellas (TIE)", "en": "Fingerprints (TIE)"},
        match=("TOMA DE HUELLAS",),
        needs=("full_name", "document_number", "nationality"),
    ),
    "certificado_ue": Tramite(
        code="certificado_ue",
        label={"pt": "Certificado de registro UE", "es": "Certificado de registro UE", "en": "EU registration certificate"},
        match=("CERTIFICADO REGISTRO UE", "CERTIFICADO REGISTRO CIUDADANO"),
        needs=("full_name", "document_number", "nationality"),
    ),
    "certificados": Tramite(
        code="certificados",
        label={"pt": "Certificados (residência, concordância)", "es": "Certificados (residencia, concordancia)", "en": "Certificates (residence, concordance)"},
        match=("CERTIFICADOS CONCORDANCIA", "CERTIFICADOS RESIDENCIA"),
        needs=("full_name", "document_number", "nationality"),
    ),
    "recogida_tie": Tramite(
        code="recogida_tie",
        label={"pt": "Recolha do cartão TIE", "es": "Recogida de la TIE", "en": "TIE card collection"},
        match=("RECOGIDA TARJETA",),
        needs=("full_name", "document_number", "nationality"),
    ),
}

# Launch provinces (see the go-to-market plan): start narrow, expand after validation.
PROVINCES: dict[str, str] = {
    "granada": "Granada",
    "madrid": "Madrid",
    "barcelona": "Barcelona",
}


def normalize(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.upper()) if unicodedata.category(c) != "Mn")


def _words(text: str) -> set[str]:
    return set(re.sub(r"[^A-Z0-9]+", " ", normalize(text)).split())


def find_province(value: str) -> str | None:
    v = normalize(value).lower()
    for key, name in PROVINCES.items():
        if v in (key, normalize(name).lower()):
            return key
    return None


def label_matches(tramite: Tramite, option_text: str) -> bool:
    words = _words(option_text)
    return any(_words(alt) <= words for alt in tramite.match)

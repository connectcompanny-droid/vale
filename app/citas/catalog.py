"""Procedures (trámites) and provinces Vale supports.

`match` holds keywords searched (case-insensitive, accents ignored) in the trámite
dropdown of the cita previa site, because the visible labels and internal ids vary
by province. Validate each entry against the live site before enabling a province.
"""

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
        match=("ASIGNACION DE NIE",),
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
        match=("CERTIFICADO DE REGISTRO",),
        needs=("full_name", "document_number", "nationality"),
    ),
    "certificados": Tramite(
        code="certificados",
        label={"pt": "Certificados (residência, concordância)", "es": "Certificados (residencia, concordancia)", "en": "Certificates (residence, concordance)"},
        match=("CERTIFICADOS",),
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


def find_province(value: str) -> str | None:
    v = normalize(value).lower()
    for key, name in PROVINCES.items():
        if v in (key, normalize(name).lower()):
            return key
    return None


def label_matches(tramite: Tramite, option_text: str) -> bool:
    text = normalize(option_text)
    return all(normalize(k) in text for k in tramite.match)

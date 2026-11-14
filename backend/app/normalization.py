"""Normalisation déterministe des valeurs extraites (nombres, dates, identifiants).

La sortie a toujours la forme complète du schéma : chaque clé est présente,
les valeurs absentes valent `None`, les tableaux sont des listes.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from .schema_registry import FieldDefinition, SchemaDefinition, build_pydantic_model

Warning = dict[str, Any]

CODE_INVALID_FORMAT = "format_invalide"

_FRENCH_MONTHS = {
    "janvier": 1, "janv": 1, "jan": 1,
    "fevrier": 2, "fevr": 2, "fev": 2,
    "mars": 3,
    "avril": 4, "avr": 4,
    "mai": 5,
    "juin": 6,
    "juillet": 7, "juil": 7,
    "aout": 8,
    "septembre": 9, "sept": 9, "sep": 9,
    "octobre": 10, "oct": 10,
    "novembre": 11, "nov": 11,
    "decembre": 12, "dec": 12,
}

_TRUE_WORDS = {"true", "vrai", "oui", "yes", "1", "x"}
_FALSE_WORDS = {"false", "faux", "non", "no", "0"}


def strip_accents(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


def make_warning(
    code: str,
    severity: str,
    field: str | None,
    message: str,
    expected: Any = None,
    actual: Any = None,
) -> Warning:
    return {
        "code": code,
        "severity": severity,
        "field": field,
        "message": message,
        "expected": expected,
        "actual": actual,
    }


# --------------------------------------------------------------------------
# Analyse des valeurs scalaires
# --------------------------------------------------------------------------


def parse_number(value: Any) -> Decimal:
    """Convertit `1 234,56 €`, `1.234,56`, `1,234.56` ou `1234.56` en Decimal."""
    if isinstance(value, bool):
        raise ValueError("booléen")
    if isinstance(value, (int, float, Decimal)):
        number = Decimal(str(value))
        if not number.is_finite():
            raise ValueError("nombre non fini")
        return number
    text = re.sub(r"[\s  ']", "", str(value))
    match = re.search(r"[-+]?\d[\d.,]*", text)
    if not match:
        raise ValueError("aucun chiffre")
    token = match.group(0).rstrip(".,")
    if "," in token and "." in token:
        decimal_separator = "," if token.rfind(",") > token.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        token = token.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif token.count(",") == 1:
        token = token.replace(",", ".")
    elif token.count(",") > 1:
        token = token.replace(",", "")
    elif token.count(".") > 1:
        token = token.replace(".", "")
    try:
        return Decimal(token)
    except InvalidOperation as exc:
        raise ValueError("nombre illisible") from exc


def parse_date(value: Any) -> str:
    """Convertit une date française ou ISO en chaîne `AAAA-MM-JJ`."""
    text = str(value).strip()
    match = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = (int(group) for group in match.groups())
        return date(year, month, day).isoformat()
    match = re.match(r"^(\d{1,2})[/.\-\s](\d{1,2})[/.\-\s](\d{2}|\d{4})$", text)
    if match:
        day, month, year = (int(group) for group in match.groups())
        if year < 100:
            year += 2000
        return date(year, month, day).isoformat()
    match = re.match(r"^(?:\w+\s+)?(\d{1,2})(?:er)?\s+([a-z]+)\.?\s+(\d{4})$", strip_accents(text.lower()))
    if match and match.group(2) in _FRENCH_MONTHS:
        return date(int(match.group(3)), _FRENCH_MONTHS[match.group(2)], int(match.group(1))).isoformat()
    raise ValueError("date illisible")


def parse_boolean(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    word = strip_accents(str(value).strip().lower())
    if word in _TRUE_WORDS:
        return True
    if word in _FALSE_WORDS:
        return False
    raise ValueError("booléen illisible")


# --------------------------------------------------------------------------
# Identifiants
# --------------------------------------------------------------------------


def luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        number = int(char)
        if index % 2 == 1:
            number *= 2
            if number > 9:
                number -= 9
        total += number
    return total % 10 == 0


def _check_siren(value: str) -> tuple[str, str | None]:
    compact = re.sub(r"[\s.\-]", "", value)
    if not re.fullmatch(r"\d{9}", compact):
        return value, "un SIREN comporte exactement 9 chiffres"
    if not luhn_valid(compact):
        return compact, "clé de contrôle SIREN invalide (algorithme de Luhn)"
    return compact, None


def _check_siret(value: str) -> tuple[str, str | None]:
    compact = re.sub(r"[\s.\-]", "", value)
    if not re.fullmatch(r"\d{14}", compact):
        return value, "un SIRET comporte exactement 14 chiffres"
    # Cas particulier officiel : les établissements de La Poste (SIREN 356000000)
    # sont valides si la somme de leurs chiffres est un multiple de 5.
    if compact.startswith("356000000") and sum(int(char) for char in compact) % 5 == 0:
        return compact, None
    if not luhn_valid(compact):
        return compact, "clé de contrôle SIRET invalide (algorithme de Luhn)"
    return compact, None


def _check_tva_intracom(value: str) -> tuple[str, str | None]:
    compact = re.sub(r"[\s.\-]", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}[0-9A-Z]{2,13}", compact):
        return value, "numéro de TVA intracommunautaire mal formé"
    if compact.startswith("FR"):
        if not re.fullmatch(r"FR[0-9A-Z]{2}\d{9}", compact):
            return compact, "un numéro de TVA français s'écrit FR + 2 caractères + SIREN (9 chiffres)"
        key, siren = compact[2:4], compact[4:]
        if key.isdigit() and int(key) != (12 + 3 * (int(siren) % 97)) % 97:
            return compact, "clé du numéro de TVA incohérente avec le SIREN"
    return compact, None


def _check_iban(value: str) -> tuple[str, str | None]:
    compact = re.sub(r"[\s.\-]", "", value).upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[0-9A-Z]{10,30}", compact):
        return value, "IBAN mal formé"
    rearranged = compact[4:] + compact[:4]
    numeric = "".join(str(int(char, 36)) for char in rearranged)
    if int(numeric) % 97 != 1:
        return compact, "clé de contrôle IBAN invalide"
    return compact, None


def _check_code_postal(value: str) -> tuple[str, str | None]:
    compact = re.sub(r"\s", "", value)
    if not re.fullmatch(r"\d{5}", compact):
        return value, "un code postal français comporte 5 chiffres"
    return compact, None


FORMAT_CHECKERS = {
    "siren": _check_siren,
    "siret": _check_siret,
    "tva_intracom": _check_tva_intracom,
    "iban": _check_iban,
    "code_postal_fr": _check_code_postal,
}


# --------------------------------------------------------------------------
# Normalisation d'un document complet
# --------------------------------------------------------------------------


def _normalize_scalar(field: FieldDefinition, value: Any, path: str, warnings: list[Warning]) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, (dict, list)):
        warnings.append(
            make_warning(CODE_INVALID_FORMAT, "error", path, f"{field.label} : valeur structurée inattendue, ignorée")
        )
        return None

    if field.type in ("string", "text"):
        text = str(value).strip()
        if field.type == "string":
            text = re.sub(r"\s+", " ", text)
        if field.format:
            text, problem = FORMAT_CHECKERS[field.format](text)
            if problem:
                warnings.append(
                    make_warning(CODE_INVALID_FORMAT, "error", path, f"{field.label} : {problem}", actual=text)
                )
        return text

    if field.type in ("number", "integer"):
        try:
            number = parse_number(value)
        except ValueError:
            warnings.append(
                make_warning(
                    CODE_INVALID_FORMAT, "error", path,
                    f"{field.label} : « {value} » n'est pas un nombre exploitable", actual=str(value),
                )
            )
            return None
        if field.type == "integer":
            if number != number.to_integral_value():
                warnings.append(
                    make_warning(
                        CODE_INVALID_FORMAT, "error", path,
                        f"{field.label} : un nombre entier est attendu", actual=float(number),
                    )
                )
                return None
            return int(number)
        return float(round(number, 6))

    if field.type == "date":
        try:
            return parse_date(value)
        except ValueError:
            text = str(value).strip()
            warnings.append(
                make_warning(
                    CODE_INVALID_FORMAT, "error", path,
                    f"{field.label} : « {text} » n'est pas une date valide (AAAA-MM-JJ attendu)", actual=text,
                )
            )
            return text

    try:
        return parse_boolean(value)
    except ValueError:
        warnings.append(
            make_warning(
                CODE_INVALID_FORMAT, "error", path,
                f"{field.label} : « {value} » n'est pas un booléen", actual=str(value),
            )
        )
        return None


def _is_empty_row(item: Any) -> bool:
    if not isinstance(item, dict):
        return True
    return all(cell is None or (isinstance(cell, str) and not cell.strip()) for cell in item.values())


def _normalize_object(
    fields: list[FieldDefinition], raw: Any, prefix: str, warnings: list[Warning]
) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    result: dict[str, Any] = {}
    for field in fields:
        path = f"{prefix}{field.name}"
        value = source.get(field.name)
        if field.type == "object":
            result[field.name] = _normalize_object(field.fields, value, f"{path}.", warnings)
        elif field.type == "array":
            items = [item for item in value if not _is_empty_row(item)] if isinstance(value, list) else []
            result[field.name] = [
                _normalize_object(field.fields, item, f"{path}[{index}].", warnings)
                for index, item in enumerate(items)
            ]
        else:
            result[field.name] = _normalize_scalar(field, value, path, warnings)
    return result


def normalize_document(definition: SchemaDefinition, raw: Any) -> tuple[dict[str, Any], list[Warning]]:
    """Ramène des données brutes (LLM ou saisie humaine) à la forme du schéma.

    Retourne les données normalisées et les alertes de format rencontrées. Les
    lignes de tableau entièrement vides sont ignorées.
    """
    warnings: list[Warning] = []
    data = _normalize_object(definition.fields, raw, "", warnings)
    build_pydantic_model(definition).model_validate(data)
    return data, warnings

"""Moteur de vérification déterministe exécuté après l'extraction.

Aucun appel au LLM ici : uniquement de l'arithmétique décimale et des règles
métier déclarées dans le schéma. Chaque écart produit une alerte précise
(`validation_warnings`) rattachée au champ concerné.

Niveaux de sévérité :
* `error`   — champ obligatoire absent, format d'identifiant ou de date invalide ;
* `warning` — incohérence mathématique ou métier entre plusieurs champs ;
* `info`    — valeur introuvable telle quelle dans le texte source (à contrôler à l'œil).
"""

from __future__ import annotations

import re
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .normalization import Warning, make_warning, normalize_document, strip_accents
from .schema_registry import FieldDefinition, RuleDefinition, SchemaDefinition

CODE_REQUIRED = "champ_obligatoire_manquant"
CODE_UNEXPECTED_VALUE = "valeur_inattendue"
CODE_NOT_IN_SOURCE = "valeur_non_retrouvee"
CODE_DUPLICATE = "doublon_possible"
CODE_EXTRACTION_FAILED = "extraction_echouee"
CODE_UNREADABLE = "texte_illisible"
CODE_HIDDEN_TEXT = "texte_cache"

BLOCKING_SEVERITIES = ("error", "warning")
CENT = Decimal("0.01")

_FRENCH_MONTH_NAMES = (
    "janvier", "fevrier", "mars", "avril", "mai", "juin",
    "juillet", "aout", "septembre", "octobre", "novembre", "decembre",
)


def _to_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return Decimal(str(value))


def _get(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _as_number(amount: Decimal) -> float:
    return float(amount)


def _fmt(amount: Decimal) -> str:
    text = f"{amount:.2f}" if amount == amount.quantize(CENT) else f"{amount.normalize():f}"
    return text.replace(".", ",")


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _object_is_empty(value: Any) -> bool:
    if isinstance(value, dict):
        return all(_object_is_empty(child) for child in value.values())
    if isinstance(value, list):
        return len(value) == 0
    return _is_blank(value)


# --------------------------------------------------------------------------
# Champs obligatoires et valeurs autorisées
# --------------------------------------------------------------------------


def _check_fields(fields: list[FieldDefinition], data: dict[str, Any], prefix: str, warnings: list[Warning]) -> None:
    for field in fields:
        path = f"{prefix}{field.name}"
        value = data.get(field.name)

        if field.type == "object":
            if field.required or not _object_is_empty(value):
                _check_fields(field.fields, value or {}, f"{path}.", warnings)
            continue

        if field.type == "array":
            items = value or []
            minimum = max(field.min_items, 1 if field.required else 0)
            if len(items) < minimum:
                warnings.append(
                    make_warning(
                        CODE_REQUIRED, "error", path,
                        f"{field.label} : au moins {minimum} ligne(s) attendue(s), {len(items)} trouvée(s)",
                        expected=minimum, actual=len(items),
                    )
                )
            for index, item in enumerate(items):
                _check_fields(field.fields, item, f"{path}[{index}].", warnings)
            continue

        if _is_blank(value):
            if field.required:
                warnings.append(
                    make_warning(CODE_REQUIRED, "error", path, f"{field.label} : champ obligatoire absent")
                )
            continue

        if field.allowed_values is not None and field.type != "date":
            if field.type in ("number", "integer"):
                number = _to_decimal(value)
                allowed = any(
                    number == Decimal(str(option)) for option in field.allowed_values if not isinstance(option, str)
                )
            else:
                allowed = value in field.allowed_values
            if not allowed:
                options = ", ".join(str(option) for option in field.allowed_values)
                warnings.append(
                    make_warning(
                        CODE_UNEXPECTED_VALUE, "warning", path,
                        f"{field.label} : valeur inhabituelle « {value} » (valeurs attendues : {options})",
                        actual=value,
                    )
                )


# --------------------------------------------------------------------------
# Règles de cohérence
# --------------------------------------------------------------------------


def _scopes(rule: RuleDefinition, data: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """Portées d'évaluation : la racine, ou chaque élément du tableau ciblé."""
    if not rule.array:
        return [(data, "")]
    items = _get(data, rule.array) or []
    return [(item, f"{rule.array}[{index}].") for index, item in enumerate(items) if isinstance(item, dict)]


def _mismatch(
    rule: RuleDefinition, field: str, expected: Decimal, actual: Decimal, detail: str
) -> Warning:
    return make_warning(
        rule.code, rule.severity, field,
        f"{rule.label} : attendu {_fmt(expected)}, lu {_fmt(actual)} (écart {_fmt(abs(expected - actual))}). {detail}",
        expected=_as_number(expected), actual=_as_number(actual),
    )


def _rule_product_equals(rule: RuleDefinition, data: dict[str, Any], tolerance: Decimal) -> list[Warning]:
    warnings: list[Warning] = []
    for scope, prefix in _scopes(rule, data):
        factors = [_to_decimal(_get(scope, name)) for name in rule.factors]
        actual = _to_decimal(_get(scope, rule.result))
        if actual is None or any(factor is None for factor in factors):
            continue
        expected = Decimal(1)
        for factor in factors:
            expected *= factor
        detail = " × ".join(_fmt(factor) for factor in factors)
        discount = _to_decimal(_get(scope, rule.discount_percent)) if rule.discount_percent else None
        if discount:
            expected *= (Decimal(100) - discount) / Decimal(100)
            detail += f" − {_fmt(discount)} %"
        expected = expected.quantize(CENT, rounding=ROUND_HALF_UP)
        if abs(expected - actual) > tolerance:
            warnings.append(_mismatch(rule, f"{prefix}{rule.result}", expected, actual, f"Calcul : {detail}."))
    return warnings


def _rule_sum_array_equals(rule: RuleDefinition, data: dict[str, Any], tolerance: Decimal) -> list[Warning]:
    items = _get(data, rule.array) or []
    actual = _to_decimal(_get(data, rule.total))
    values = [_to_decimal(item.get(rule.item_field)) for item in items if isinstance(item, dict)]
    if actual is None or not values or any(value is None for value in values):
        return []
    expected = sum(values, Decimal(0))
    if abs(expected - actual) <= tolerance:
        return []
    detail = " + ".join(_fmt(value) for value in values)
    return [_mismatch(rule, rule.total, expected, actual, f"Calcul : {detail}.")]


def _rule_sum_fields_equals(rule: RuleDefinition, data: dict[str, Any], tolerance: Decimal) -> list[Warning]:
    warnings: list[Warning] = []
    for scope, prefix in _scopes(rule, data):
        terms = [_to_decimal(_get(scope, name)) for name in rule.terms]
        actual = _to_decimal(_get(scope, rule.total))
        if actual is None or any(term is None for term in terms):
            continue
        expected = sum(terms, Decimal(0))
        if abs(expected - actual) > tolerance:
            detail = " + ".join(_fmt(term) for term in terms)
            warnings.append(_mismatch(rule, f"{prefix}{rule.total}", expected, actual, f"Calcul : {detail}."))
    return warnings


def _rule_percentage_equals(rule: RuleDefinition, data: dict[str, Any], tolerance: Decimal) -> list[Warning]:
    warnings: list[Warning] = []
    for scope, prefix in _scopes(rule, data):
        base = _to_decimal(_get(scope, rule.base))
        rate = _to_decimal(_get(scope, rule.rate))
        actual = _to_decimal(_get(scope, rule.result))
        if base is None or rate is None or actual is None:
            continue
        expected = (base * rate / Decimal(100)).quantize(CENT, rounding=ROUND_HALF_UP)
        if abs(expected - actual) > tolerance:
            detail = f"Calcul : {_fmt(base)} × {_fmt(rate)} %."
            warnings.append(_mismatch(rule, f"{prefix}{rule.result}", expected, actual, detail))
    return warnings


def _comparable(value: Any) -> Decimal | date | None:
    number = _to_decimal(value)
    if number is not None:
        return number
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None
    return None


def _rule_less_or_equal(rule: RuleDefinition, data: dict[str, Any], tolerance: Decimal) -> list[Warning]:
    warnings: list[Warning] = []
    for scope, prefix in _scopes(rule, data):
        left_raw, right_raw = _get(scope, rule.left), _get(scope, rule.right)
        left, right = _comparable(left_raw), _comparable(right_raw)
        if left is None or right is None or type(left) is not type(right):
            continue
        exceeded = left - right > tolerance if isinstance(left, Decimal) else left > right
        if exceeded:
            warnings.append(
                make_warning(
                    rule.code, rule.severity, f"{prefix}{rule.right}",
                    f"{rule.label} : {left_raw} > {right_raw}.",
                    expected=f"≥ {left_raw}", actual=right_raw,
                )
            )
    return warnings


_RULE_HANDLERS = {
    "product_equals": _rule_product_equals,
    "sum_array_equals": _rule_sum_array_equals,
    "sum_fields_equals": _rule_sum_fields_equals,
    "percentage_equals": _rule_percentage_equals,
    "less_or_equal": _rule_less_or_equal,
}


def run_rules(definition: SchemaDefinition, data: dict[str, Any], default_tolerance: Decimal) -> list[Warning]:
    warnings: list[Warning] = []
    for rule in definition.validation_rules:
        if rule.tolerance is not None:
            tolerance = rule.tolerance
        elif rule.type == "less_or_equal":
            tolerance = Decimal(0)
        else:
            tolerance = default_tolerance
        warnings.extend(_RULE_HANDLERS[rule.type](rule, data, tolerance))
    return warnings


# --------------------------------------------------------------------------
# Ancrage dans le texte source (détection des valeurs inventées)
# --------------------------------------------------------------------------


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", strip_accents(text).lower())


def _number_candidates(value: int | float) -> set[str]:
    number = Decimal(str(value))
    candidates: set[str] = set()
    plain = f"{number.normalize():f}"
    two_decimals = f"{number:.2f}"
    for text in (plain, two_decimals):
        candidates.add(text)
        candidates.add(text.replace(".", ","))
        integer_part, _, fraction = text.partition(".")
        grouped = f"{int(integer_part):,}"
        if "," in grouped:
            candidates.add(grouped.replace(",", ".") + ("," + fraction if fraction else ""))
            candidates.add(grouped + ("." + fraction if fraction else ""))
    return candidates


def _date_candidates(value: str) -> set[str]:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return {_squash(value)}
    day, month, year = parsed.day, parsed.month, parsed.year
    candidates = {parsed.isoformat(), f"{day}{_FRENCH_MONTH_NAMES[month - 1]}{year}"}
    if day == 1:
        candidates.add(f"1er{_FRENCH_MONTH_NAMES[month - 1]}{year}")
    for separator in ("/", "-", "."):
        candidates.add(f"{day:02d}{separator}{month:02d}{separator}{year}")
        candidates.add(f"{day}{separator}{month}{separator}{year}")
        candidates.add(f"{day:02d}{separator}{month:02d}{separator}{year % 100:02d}")
    return candidates


def _grounding(
    fields: list[FieldDefinition], data: dict[str, Any], prefix: str, source: str, missing: list[Warning]
) -> tuple[int, int]:
    """Retourne (valeurs vérifiées, valeurs retrouvées) et alimente `missing`."""
    checked = found = 0
    for field in fields:
        path = f"{prefix}{field.name}"
        value = data.get(field.name)
        if field.type == "object":
            sub_checked, sub_found = _grounding(field.fields, value or {}, f"{path}.", source, missing)
        elif field.type == "array":
            sub_checked = sub_found = 0
            for index, item in enumerate(value or []):
                item_checked, item_found = _grounding(field.fields, item, f"{path}[{index}].", source, missing)
                sub_checked += item_checked
                sub_found += item_found
        else:
            if _is_blank(value) or field.type in ("boolean", "text"):
                continue
            if field.type in ("number", "integer"):
                candidates = _number_candidates(value)
            elif field.type == "date":
                candidates = _date_candidates(value)
            else:
                candidates = {_squash(str(value))}
            sub_checked = 1
            sub_found = 1 if any(candidate and candidate in source for candidate in candidates) else 0
            if not sub_found:
                missing.append(
                    make_warning(
                        CODE_NOT_IN_SOURCE, "info", path,
                        f"{field.label} : valeur introuvable telle quelle dans le texte du document, à contrôler",
                        actual=value,
                    )
                )
        checked += sub_checked
        found += sub_found
    return checked, found


# --------------------------------------------------------------------------
# Complétude et score de confiance
# --------------------------------------------------------------------------


def _completeness(fields: list[FieldDefinition], data: dict[str, Any]) -> tuple[int, int]:
    """Retourne (champs obligatoires attendus, champs obligatoires renseignés)."""
    expected = filled = 0
    for field in fields:
        value = data.get(field.name)
        if field.type == "object":
            if field.required or not _object_is_empty(value):
                sub_expected, sub_filled = _completeness(field.fields, value or {})
                expected += sub_expected
                filled += sub_filled
        elif field.type == "array":
            items = value or []
            if field.required or field.min_items:
                expected += 1
                filled += 1 if items else 0
            for item in items:
                sub_expected, sub_filled = _completeness(field.fields, item)
                expected += sub_expected
                filled += sub_filled
        elif field.required:
            expected += 1
            filled += 0 if _is_blank(value) else 1
    return expected, filled


def count_blocking(warnings: list[Warning]) -> int:
    return sum(1 for warning in warnings if warning["severity"] in BLOCKING_SEVERITIES)


def compute_confidence(
    completeness: float, grounding: float, warnings: list[Warning], ocr_confidence: float | None
) -> float:
    """Score de confiance heuristique et explicable, entre 0 et 1.

    Base : moyenne de la complétude des champs obligatoires et de l'ancrage des
    valeurs dans le texte source. Chaque incohérence (`warning`) multiplie le
    score par 0,85 et chaque erreur (`error`) par 0,70 ; le tout est pondéré par
    la qualité de l'OCR lorsque le document est scanné.
    """
    errors = sum(1 for warning in warnings if warning["severity"] == "error")
    alerts = sum(1 for warning in warnings if warning["severity"] == "warning")
    score = (0.5 * completeness + 0.5 * grounding) * (0.85**alerts) * (0.70**errors)
    if ocr_confidence is not None:
        score *= min(1.0, max(0.5, ocr_confidence))
    return round(min(1.0, max(0.0, score)), 3)


def evaluate_document(
    definition: SchemaDefinition,
    raw_data: Any,
    source_text: str | None,
    default_tolerance: Decimal,
    ocr_confidence: float | None = None,
) -> tuple[dict[str, Any], list[Warning], float]:
    """Normalise, vérifie et note un document.

    Retourne (données normalisées, alertes, score de confiance). Utilisé aussi
    bien après l'extraction LLM qu'après chaque correction humaine.
    """
    data, warnings = normalize_document(definition, raw_data)
    _check_fields(definition.fields, data, "", warnings)
    warnings.extend(run_rules(definition, data, default_tolerance))

    grounding_ratio = 1.0
    source = _squash(source_text or "")
    if len(source) >= 20:
        missing: list[Warning] = []
        checked, found = _grounding(definition.fields, data, "", source, missing)
        if checked:
            grounding_ratio = found / checked
        flagged = {warning["field"] for warning in warnings}
        warnings.extend(warning for warning in missing if warning["field"] not in flagged)

    expected, filled = _completeness(definition.fields, data)
    completeness_ratio = filled / expected if expected else 1.0
    score = compute_confidence(completeness_ratio, grounding_ratio, warnings, ocr_confidence)

    order = {"error": 0, "warning": 1, "info": 2}
    warnings.sort(key=lambda warning: order[warning["severity"]])
    return data, warnings, score

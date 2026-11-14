"""Tests unitaires du moteur déterministe : normalisation, formats, règles de cohérence."""

from __future__ import annotations

import copy
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import is_local_endpoint
from app.normalization import parse_date, parse_number
from app.schema_registry import (
    SchemaDefinition,
    build_json_schema,
    build_pydantic_model,
    load_schema_files,
)
from app.validation import evaluate_document
from sample_documents import CMR_LLM_ANSWER, INVOICE_LLM_ANSWER

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "backend" / "schemas"
TOLERANCE = Decimal("0.02")


@pytest.fixture(scope="module")
def schemas() -> dict[str, SchemaDefinition]:
    return {definition.code: definition for definition in load_schema_files(SCHEMAS_DIR)}


def _codes(warnings: list[dict], severity: str | None = None) -> list[str]:
    return [w["code"] for w in warnings if severity is None or w["severity"] == severity]


def _evaluate(definition: SchemaDefinition, data: dict) -> tuple[dict, list[dict], float]:
    return evaluate_document(definition, data, None, TOLERANCE)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 234,56 €", "1234.56"),
        ("1.234,56", "1234.56"),
        ("1,234.56", "1234.56"),
        ("1 000,00", "1000.00"),
        ("20 %", "20"),
        ("-12,5", "-12.5"),
        (42, "42"),
        (0.1, "0.1"),
    ],
)
def test_parse_number_handles_french_and_english_formats(raw, expected: str) -> None:
    assert parse_number(raw) == Decimal(expected)


@pytest.mark.parametrize("raw", ["abc", "", True])
def test_parse_number_rejects_garbage(raw) -> None:
    with pytest.raises(ValueError):
        parse_number(raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("14/03/2025", "2025-03-14"),
        ("2025-03-14", "2025-03-14"),
        ("14.03.25", "2025-03-14"),
        ("1er mars 2025", "2025-03-01"),
        ("14 février 2025", "2025-02-14"),
    ],
)
def test_parse_date_normalises_to_iso(raw: str, expected: str) -> None:
    assert parse_date(raw) == expected


def test_parse_date_rejects_impossible_date() -> None:
    with pytest.raises(ValueError):
        parse_date("31/02/2025")


def test_json_schema_is_strict_and_model_accepts_normalised_data(schemas) -> None:
    for code, answer in (("facture_fournisseur", INVOICE_LLM_ANSWER), ("transport_cmr", CMR_LLM_ANSWER)):
        definition = schemas[code]
        json_schema = build_json_schema(definition)
        assert json_schema["additionalProperties"] is False
        assert set(json_schema["required"]) == set(json_schema["properties"])
        data, warnings, _ = _evaluate(definition, answer)
        build_pydantic_model(definition).model_validate(data)
        assert _codes(warnings, "error") == []
    lines = build_json_schema(schemas["facture_fournisseur"])["properties"]["lignes"]
    assert lines["type"] == "array" and lines["items"]["additionalProperties"] is False


def test_valid_documents_have_no_blocking_warning(schemas) -> None:
    _, cmr_warnings, cmr_score = _evaluate(schemas["transport_cmr"], CMR_LLM_ANSWER)
    assert cmr_warnings == [] and cmr_score == 1.0
    fixed = copy.deepcopy(INVOICE_LLM_ANSWER)
    fixed.update(montant_tva=200.0, total_ttc=1200.0)
    _, invoice_warnings, invoice_score = _evaluate(schemas["facture_fournisseur"], fixed)
    assert invoice_warnings == [] and invoice_score == 1.0


def test_vat_error_flags_the_vat_amount(schemas) -> None:
    _, warnings, score = _evaluate(schemas["facture_fournisseur"], INVOICE_LLM_ANSWER)
    assert _codes(warnings) == ["tva_incoherente"]
    assert warnings[0]["field"] == "montant_tva" and warnings[0]["expected"] == 200.0
    assert score < 1.0


def test_line_and_total_errors_are_located_precisely(schemas) -> None:
    data = copy.deepcopy(INVOICE_LLM_ANSWER)
    data.update(montant_tva=200.0, total_ttc=1200.0)
    data["lignes"][1]["montant_ht"] = 310.0  # 5 × 60,00 = 300,00
    _, warnings, _ = _evaluate(schemas["facture_fournisseur"], data)
    by_code = {w["code"]: w for w in warnings}
    assert set(by_code) == {"calcul_ligne_incoherent", "total_ht_incoherent"}
    assert by_code["calcul_ligne_incoherent"]["field"] == "lignes[1].montant_ht"
    assert by_code["calcul_ligne_incoherent"]["expected"] == 300.0
    assert by_code["total_ht_incoherent"]["field"] == "total_ht"
    assert by_code["total_ht_incoherent"]["expected"] == 1010.0


def test_line_discount_is_taken_into_account(schemas) -> None:
    data = copy.deepcopy(INVOICE_LLM_ANSWER)
    data["lignes"][0].update(remise_pct=10, montant_ht=450.0)  # 40 × 12,50 − 10 %
    data.update(total_ht=950.0, montant_tva=190.0, total_ttc=1140.0)
    _, warnings, _ = _evaluate(schemas["facture_fournisseur"], data)
    assert warnings == []


def test_rounding_within_tolerance_is_accepted(schemas) -> None:
    data = copy.deepcopy(INVOICE_LLM_ANSWER)
    data.update(montant_tva=200.01, total_ttc=1200.01)
    _, warnings, _ = _evaluate(schemas["facture_fournisseur"], data)
    assert warnings == []


def test_missing_required_fields_and_invalid_identifiers(schemas) -> None:
    data = copy.deepcopy(INVOICE_LLM_ANSWER)
    data.update(montant_tva=200.0, total_ttc=1200.0, numero_facture=None, date_facture="31/02/2025")
    data["emetteur"]["siret"] = "44328719800017"  # clé de Luhn fausse
    data["emetteur"]["tva_intracom"] = "FR99443287198"  # clé TVA fausse
    data["iban"] = "FR7630006000011234567890188"
    data["lignes"] = []
    normalised, warnings, _ = _evaluate(schemas["facture_fournisseur"], data)
    located = {(w["field"], w["code"]) for w in warnings}
    assert located == {
        ("numero_facture", "champ_obligatoire_manquant"),
        ("date_facture", "format_invalide"),
        ("emetteur.siret", "format_invalide"),
        ("emetteur.tva_intracom", "format_invalide"),
        ("iban", "format_invalide"),
        ("lignes", "champ_obligatoire_manquant"),
    }
    assert all(w["severity"] == "error" for w in warnings)
    assert normalised["date_facture"] == "31/02/2025", "la saisie illisible est conservée pour correction"


def test_unusual_vat_rate_and_date_order(schemas) -> None:
    data = copy.deepcopy(INVOICE_LLM_ANSWER)
    data.update(taux_tva=19.6, montant_tva=196.0, total_ttc=1196.0, date_echeance="2025-03-01")
    _, warnings, _ = _evaluate(schemas["facture_fournisseur"], data)
    assert sorted(_codes(warnings)) == ["dates_incoherentes", "valeur_inattendue"]


def test_cmr_weight_and_parcel_checks(schemas) -> None:
    data = copy.deepcopy(CMR_LLM_ANSWER)
    data["poids_brut_total_kg"] = 650.0
    data["nombre_colis_total"] = 7
    data["marchandises"][1]["poids_net_kg"] = 40.0  # > 35,5 brut ; la somme des nets devient 580
    data["date_livraison"] = "2025-03-10"
    _, warnings, _ = _evaluate(schemas["transport_cmr"], data)
    located = {(w["field"], w["code"]) for w in warnings}
    assert located == {
        ("poids_brut_total_kg", "poids_brut_incoherent"),
        ("nombre_colis_total", "nombre_colis_incoherent"),
        ("marchandises[1].poids_brut_kg", "poids_net_superieur_brut"),
        ("poids_net_total_kg", "poids_net_incoherent"),
        ("date_livraison", "dates_incoherentes"),
    }


def test_values_absent_from_source_text_are_flagged_as_info(schemas) -> None:
    source = "Facture FA-2025-0042 du 14/03/2025 Papeterie Durand SAS total 1 000,00 TVA 200,00 TTC 1 200,00"
    data = {
        "numero_facture": "FA-2025-0042", "date_facture": "2025-03-14",
        "total_ht": 1000.0, "montant_tva": 200.0, "total_ttc": 1200.0,
        "emetteur": {"nom": "Papeterie Dupont"},
    }
    _, warnings, _ = evaluate_document(schemas["facture_fournisseur"], data, source, TOLERANCE)
    info = [w for w in warnings if w["severity"] == "info"]
    assert [(w["field"], w["code"]) for w in info] == [("emetteur.nom", "valeur_non_retrouvee")]


def test_empty_rows_and_unknown_keys_are_dropped(schemas) -> None:
    data = copy.deepcopy(CMR_LLM_ANSWER)
    data["champ_invente"] = "x"
    data["marchandises"].append({"reference_colis": "  ", "poids_brut_kg": None})
    normalised, warnings, _ = _evaluate(schemas["transport_cmr"], data)
    assert "champ_invente" not in normalised
    assert len(normalised["marchandises"]) == 3 and warnings == []


def test_schema_definition_rejects_rule_on_unknown_field() -> None:
    with pytest.raises(ValueError, match="champ inconnu"):
        SchemaDefinition.model_validate(
            {
                "code": "essai", "label": "Essai",
                "fields": [{"name": "total", "label": "Total", "type": "number"}],
                "validation_rules": [
                    {"type": "sum_fields_equals", "code": "x", "label": "x", "terms": ["absent"], "total": "total"}
                ],
            }
        )


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("http://localhost:11434", True),
        ("http://127.0.0.1:11434", True),
        ("http://ollama:11434", True),
        ("http://host.docker.internal:11434", True),
        ("http://192.168.1.20:11434", True),
        ("http://gpu.atelier.lan:11434", True),
        ("https://api.openai.com/v1", False),
        ("http://8.8.8.8:11434", False),
    ],
)
def test_only_local_llm_endpoints_are_accepted(url: str, local: bool) -> None:
    assert is_local_endpoint(url) is local

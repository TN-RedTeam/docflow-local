"""Test d'intégration du pipeline complet de DocFlow Local.

Deux vrais fichiers PDF sont générés à la volée avec reportlab :

1. un bon de livraison / CMR transport valide ;
2. une facture fournisseur dont la TVA imprimée est fausse (210,00 € au lieu de 200,00 €).

Chacun traverse toute la chaîne — import HTTP, extraction du texte du PDF,
appel du client Ollama (transport HTTP simulé, ou vrai serveur si
DOCFLOW_LIVE_OLLAMA=1), normalisation, contrôles déterministes, persistance,
validation humaine, export — et le test prouve que l'anomalie est détectée.
"""

from __future__ import annotations

import csv
import io
import json
import os
import shutil

import pytest
from sqlalchemy.exc import DBAPIError

from app.ingestion import extract_text
from app.models import ProcessedDocument
from sample_documents import SUPPLIER_SIRET, build_scanned_pdf


def _upload(client, schema_code: str, *files: tuple[str, bytes]):
    return client.post(
        "/api/documents/upload",
        params={"wait": "true"},
        data={"schema_code": schema_code},
        files=[("files", (name, content, "application/pdf")) for name, content in files],
    )


def _blocking(document: dict) -> list[dict]:
    return [warning for warning in document["validation_warnings"] if warning["severity"] != "info"]


# --------------------------------------------------------------------------
# Ingestion
# --------------------------------------------------------------------------


def test_generated_pdfs_are_real_pdfs_with_native_text(cmr_pdf: bytes, invoice_pdf: bytes) -> None:
    assert cmr_pdf.startswith(b"%PDF-") and invoice_pdf.startswith(b"%PDF-")

    cmr = extract_text(cmr_pdf)
    assert cmr.method == "native" and cmr.page_count == 1 and cmr.ocr_confidence is None
    assert "CMR-2025-001874" in cmr.text and "611,5" in cmr.text and "PAL-0002" in cmr.text

    invoice = extract_text(invoice_pdf)
    assert invoice.method == "native"
    assert "FA-2025-0042" in invoice.text and "210,00" in invoice.text and "443 287 198 00016" in invoice.text
    assert "[Tableaux détectés" in invoice.text, "les tableaux pdfplumber doivent compléter le texte"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="Tesseract n'est pas installé")
def test_scanned_pdf_falls_back_to_ocr(invoice_pdf: bytes) -> None:
    scanned = build_scanned_pdf(invoice_pdf)
    result = extract_text(scanned, ocr_languages="eng")
    assert result.method == "ocr"
    assert result.ocr_confidence is not None and result.ocr_confidence > 0.5
    assert "FA-2025-0042" in result.text
    assert "210,00" in result.text


# --------------------------------------------------------------------------
# Pipeline complet
# --------------------------------------------------------------------------


def test_valid_delivery_note_passes_all_checks(client, ollama, cmr_pdf: bytes) -> None:
    response = _upload(client, "transport_cmr", ("bon_livraison.pdf", cmr_pdf))
    assert response.status_code == 202, response.text
    summary = response.json()["documents"][0]
    assert summary["status"] == "pending_review"
    assert summary["warning_count"] == 0

    document = client.get(f"/api/documents/{summary['id']}").json()
    assert document["validation_warnings"] == [], document["validation_warnings"]
    assert document["confidence_score"] >= 0.95
    assert document["extraction_method"] == "native"

    data = document["extracted_data"]
    assert data["numero_cmr"] == "CMR-2025-001874"
    assert data["expediteur"]["nom"] == "Papeterie Durand SAS"
    assert data["destinataire"]["ville"] == "Nantes"
    assert data["transporteur"]["siret"] == "82019457900018", "le SIRET doit être normalisé sans espaces"
    assert [item["reference_colis"] for item in data["marchandises"]] == ["PAL-0001", "PAL-0002", "PAL-0003"]
    assert data["poids_brut_total_kg"] == 611.5 and data["poids_net_total_kg"] == 570.0
    assert data["reserves"] is None

    # Le texte réellement extrait du PDF a bien été transmis au LLM, sous contrainte de schéma strict.
    request = ollama.requests[-1]
    assert request["response_format"]["type"] == "json_schema"
    assert request["response_format"]["json_schema"]["strict"] is True
    assert request["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert request["temperature"] == 0
    prompt = ollama.last_prompt("transport_cmr")
    assert "CMR-2025-001874" in prompt and "611,5" in prompt


def test_invoice_vat_error_is_detected(client, ollama, invoice_pdf: bytes) -> None:
    response = _upload(client, "facture_fournisseur", ("facture_erreur_tva.pdf", invoice_pdf))
    assert response.status_code == 202, response.text
    summary = response.json()["documents"][0]
    assert summary["status"] == "pending_review"
    assert summary["warning_count"] == 1

    document = client.get(f"/api/documents/{summary['id']}").json()
    warnings = _blocking(document)
    assert [warning["code"] for warning in warnings] == ["tva_incoherente"]

    vat_warning = warnings[0]
    assert vat_warning["field"] == "montant_tva"
    assert vat_warning["severity"] == "warning"
    assert vat_warning["expected"] == 200.0
    assert vat_warning["actual"] == 210.0
    assert "200,00" in vat_warning["message"] and "210,00" in vat_warning["message"]

    # Les autres contrôles passent : lignes, somme HT, identifiants, et TTC = HT + TVA imprimée.
    data = document["extracted_data"]
    assert data["emetteur"]["siret"] == SUPPLIER_SIRET
    assert data["total_ht"] == 1000.0 and data["total_ttc"] == 1210.0
    assert len(data["lignes"]) == 3
    assert 0.5 < document["confidence_score"] < 0.95

    assert "210,00" in ollama.last_prompt("facture_fournisseur")

    events = [log["event_type"] for log in client.get(f"/api/documents/{summary['id']}/audit").json()]
    assert events == ["uploaded", "text_extracted", "llm_extracted", "checks_completed"]


def test_batch_upload_lists_and_filters(client, cmr_pdf: bytes, invoice_pdf: bytes) -> None:
    response = _upload(client, "transport_cmr", ("bl_1.pdf", cmr_pdf), ("pas_un_pdf.pdf", b"bonjour"))
    assert response.status_code == 202
    body = response.json()
    assert len(body["documents"]) == 1
    assert body["rejected"] == [{"filename": "pas_un_pdf.pdf", "reason": "Le fichier n'est pas un PDF"}]
    _upload(client, "facture_fournisseur", ("facture.pdf", invoice_pdf))

    assert client.get("/api/documents").json()["total"] == 2
    with_warnings = client.get("/api/documents", params={"has_warnings": "true"}).json()
    assert [item["original_filename"] for item in with_warnings["items"]] == ["facture.pdf"]
    by_schema = client.get("/api/documents", params={"schema": "transport_cmr", "status": "pending_review"}).json()
    assert [item["original_filename"] for item in by_schema["items"]] == ["bl_1.pdf"]
    assert client.get("/api/documents", params={"q": "FACT"}).json()["total"] == 1
    assert client.get("/api/documents/stats").json() == {
        "processing": 0, "pending_review": 2, "validated": 0, "with_warnings": 1,
    }

    # Réimporter le même fichier déclenche l'alerte de doublon.
    duplicate = _upload(client, "transport_cmr", ("bl_1_copie.pdf", cmr_pdf)).json()["documents"][0]
    codes = [w["code"] for w in client.get(f"/api/documents/{duplicate['id']}").json()["validation_warnings"]]
    assert codes == ["doublon_possible"]

    assert _upload(client, "schema_inconnu", ("x.pdf", cmr_pdf)).status_code == 404
    assert _upload(client, "transport_cmr", ("x.pdf", b"bonjour")).status_code == 400


def test_human_review_validation_lock_and_export(client, settings, invoice_pdf: bytes, cmr_pdf: bytes) -> None:
    invoice_id = _upload(client, "facture_fournisseur", ("facture.pdf", invoice_pdf)).json()["documents"][0]["id"]
    cmr_id = _upload(client, "transport_cmr", ("bl.pdf", cmr_pdf)).json()["documents"][0]["id"]
    data = client.get(f"/api/documents/{invoice_id}").json()["extracted_data"]

    # 1. Valider sans corriger est refusé tant que l'alerte de TVA subsiste.
    refused = client.patch(f"/api/documents/{invoice_id}/validate", json={"data": data, "actor": "claire"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["validation_warnings"][0]["code"] == "tva_incoherente"

    # 2. Corriger uniquement la TVA déplace l'écart sur le TTC : le moteur recalcule tout.
    data["montant_tva"] = 200.0
    partial = client.patch(f"/api/documents/{invoice_id}", json={"data": data, "actor": "claire"}).json()
    assert [w["code"] for w in _blocking(partial)] == ["ttc_incoherent"]
    assert _blocking(partial)[0]["field"] == "total_ttc" and _blocking(partial)[0]["expected"] == 1200.0

    # 3. Correction complète (saisie « à la française » acceptée) puis validation.
    data["total_ttc"] = "1 200,00"
    validated = client.patch(f"/api/documents/{invoice_id}/validate", json={"data": data, "actor": "claire"})
    assert validated.status_code == 200, validated.text
    body = validated.json()
    assert body["status"] == "validated" and body["validated_by"] == "claire" and body["validated_at"]
    assert body["extracted_data"]["total_ttc"] == 1200.0
    assert _blocking(body) == []

    # 4. La validation a déclenché le dépôt d'export pour l'ERP.
    dropped = settings.export_dir / "facture_fournisseur" / f"{invoice_id}.json"
    assert dropped.is_file()
    assert json.loads(dropped.read_text(encoding="utf-8"))["donnees"]["montant_tva"] == 200.0

    # 5. L'enregistrement est verrouillé : plus de modification, de retraitement ni de suppression.
    assert client.patch(f"/api/documents/{invoice_id}/validate", json={"data": data}).status_code == 409
    assert client.patch(f"/api/documents/{invoice_id}", json={"data": data}).status_code == 409
    assert client.post(f"/api/documents/{invoice_id}/reprocess").status_code == 409
    assert client.delete(f"/api/documents/{invoice_id}").status_code == 409

    # 6. Export JSON : seuls les documents validés sortent par défaut.
    exported = client.get("/api/documents/export", params={"format": "json"})
    assert exported.status_code == 200
    payload = exported.json()
    assert payload["nombre_documents"] == 1
    assert payload["documents"][0]["document_id"] == invoice_id
    assert payload["documents"][0]["donnees"]["numero_facture"] == "FA-2025-0042"

    # 7. Export CSV : une ligne par article, en-tête répété, prêt pour l'import comptable.
    csv_response = client.get("/api/documents/export", params={"format": "csv", "schema": "facture_fournisseur"})
    assert csv_response.status_code == 200
    assert csv_response.headers["content-type"].startswith("text/csv")
    assert "attachment" in csv_response.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig")), delimiter=";"))
    assert len(rows) == 3
    assert {row["numero_facture"] for row in rows} == {"FA-2025-0042"}
    assert {row["emetteur.siret"] for row in rows} == {SUPPLIER_SIRET}
    assert [row["lignes.reference"] for row in rows] == ["REF-A4", "REF-TN", "REF-CL"]
    assert [row["lignes.montant_ht"] for row in rows] == ["500", "300", "200"]
    assert rows[0]["montant_tva"] == "200" and rows[0]["total_ttc"] == "1200"

    # 8. Le CMR, sans alerte, se valide directement ; un CSV multi-types sans schéma est refusé.
    assert client.patch(f"/api/documents/{cmr_id}/validate", json={"actor": "claire"}).status_code == 200
    assert client.get("/api/documents/export", params={"format": "csv"}).status_code == 400
    cmr_csv = client.get("/api/documents/export", params={"format": "csv", "schema": "transport_cmr"})
    cmr_rows = list(csv.DictReader(io.StringIO(cmr_csv.content.decode("utf-8-sig")), delimiter=";"))
    assert [row["marchandises.poids_brut_kg"] for row in cmr_rows] == ["480", "35.5", "96"]

    events = [log["event_type"] for log in client.get(f"/api/documents/{invoice_id}/audit").json()]
    assert events[-4:] == ["validation_refused", "human_corrected", "validated", "exported"]


def test_forced_validation_keeps_warning_trace(client, invoice_pdf: bytes) -> None:
    document_id = _upload(client, "facture_fournisseur", ("facture.pdf", invoice_pdf)).json()["documents"][0]["id"]
    forced = client.patch(f"/api/documents/{document_id}/validate", json={"actor": "paul", "force": True})
    assert forced.status_code == 200
    assert forced.json()["status"] == "validated"
    assert [w["code"] for w in _blocking(forced.json())] == ["tva_incoherente"]
    audit = client.get(f"/api/documents/{document_id}/audit").json()
    assert audit[-2]["event_type"] == "validated" and audit[-2]["details"]["forced"] is True


def test_validated_document_is_locked_at_database_level(client, settings, cmr_pdf: bytes) -> None:
    """Sur PostgreSQL, le trigger refuse toute modification hors API d'un document validé."""
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("Verrou par trigger : test exécuté uniquement sur PostgreSQL (TEST_DATABASE_URL)")
    document_id = _upload(client, "transport_cmr", ("bl.pdf", cmr_pdf)).json()["documents"][0]["id"]
    assert client.patch(f"/api/documents/{document_id}/validate", json={}).status_code == 200

    with client.app.state.session_factory() as session:
        document = session.get(ProcessedDocument, document_id)
        document.extracted_data = {**document.extracted_data, "numero_cmr": "FALSIFIE"}
        with pytest.raises(DBAPIError, match="verrouillé"):
            session.commit()


def test_ollama_unavailable_keeps_document_for_manual_entry(client, ollama, invoice_pdf: bytes) -> None:
    ollama.down = True
    summary = _upload(client, "facture_fournisseur", ("facture.pdf", invoice_pdf)).json()["documents"][0]
    assert summary["status"] == "pending_review"
    document = client.get(f"/api/documents/{summary['id']}").json()
    assert document["confidence_score"] == 0
    assert document["validation_warnings"][0]["code"] == "extraction_echouee"
    assert "injoignable" in document["validation_warnings"][0]["message"]
    assert client.get("/api/health").json()["status"] == "degraded"

    # Ollama revient : la relance réussit et retrouve l'anomalie de TVA.
    ollama.down = False
    retried = client.post(f"/api/documents/{summary['id']}/reprocess", params={"wait": "true"})
    assert retried.status_code == 202
    assert [w["code"] for w in _blocking(retried.json())] == ["tva_incoherente"]
    assert client.get("/api/health").json()["status"] == "ok"


def test_schemas_endpoint_exposes_both_preconfigured_models(client) -> None:
    schemas = client.get("/api/schemas").json()
    assert {schema["code"] for schema in schemas} == {"transport_cmr", "facture_fournisseur"}
    invoice = next(schema for schema in schemas if schema["code"] == "facture_fournisseur")
    assert {rule["code"] for rule in invoice["definition"]["validation_rules"]} >= {"tva_incoherente", "ttc_incoherent"}


# --------------------------------------------------------------------------
# Optionnel : vrai serveur Ollama local
# --------------------------------------------------------------------------


@pytest.mark.skipif(os.environ.get("DOCFLOW_LIVE_OLLAMA") != "1", reason="Définir DOCFLOW_LIVE_OLLAMA=1 pour ce test")
def test_live_ollama_detects_vat_error(settings, invoice_pdf: bytes) -> None:
    """Même scénario avec un vrai modèle local (OLLAMA_BASE_URL / OLLAMA_MODEL)."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    live_settings = settings.model_copy(
        update={
            "ollama_base_url": os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            "ollama_model": os.environ.get("OLLAMA_MODEL", "qwen2.5:7b"),
        }
    )
    with TestClient(create_app(live_settings)) as live_client:
        summary = _upload(live_client, "facture_fournisseur", ("facture.pdf", invoice_pdf)).json()["documents"][0]
        document = live_client.get(f"/api/documents/{summary['id']}").json()
    codes = [warning["code"] for warning in _blocking(document)]
    assert "extraction_echouee" not in codes, document["validation_warnings"]
    assert "tva_incoherente" in codes, document["validation_warnings"]

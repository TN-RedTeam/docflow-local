"""Jeu de test livré (jeu-de-test/) : DocFlow lève-t-il exactement les alertes prévues pour chaque document ?

Le faux modèle du jeu de test joue le rôle d'un modèle qui lit parfaitement : il renvoie la vérité de
chaque document. Ce test vérifie donc tout le reste de la chaîne sur les vrais PDF du jeu : ingestion
(texte natif, OCR, texte caché), contrôles de cohérence, détection de doublon.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.llm_client import OllamaClient
from app.main import create_app

JEU = Path(__file__).resolve().parent.parent / "jeu-de-test"
OCR = shutil.which("tesseract") is not None


def _module(nom: str):
    specification = importlib.util.spec_from_file_location(nom, JEU / f"{nom}.py")
    module = importlib.util.module_from_spec(specification)
    sys.modules[nom] = module
    specification.loader.exec_module(module)
    return module


generer = _module("generer")
faux_modele = _module("faux_modele")
VERITES = faux_modele.charger_verites(JEU / "verite")
CAS = [(nom, *contenu) for nom, contenu in generer.DOCUMENTS.items() if contenu[2] != "copie"]


@pytest.fixture
def client_jeu(settings):
    """Application DocFlow branchée sur le faux modèle du jeu de test."""
    def serveur(requete: httpx.Request) -> httpx.Response:
        if requete.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b"}]})
        statut, corps = faux_modele.reponse(VERITES, json.loads(requete.content))
        return httpx.Response(statut, json=corps)

    llm = OllamaClient(settings.ollama_base_url, settings.ollama_model, transport=httpx.MockTransport(serveur), max_retries=0)
    with TestClient(create_app(settings, llm_client=llm)) as client:
        client.post("/api/connexion", json={"username": "admin", "password": "docflow"})
        yield client
    llm.close()


def deposer(client: TestClient, nom: str, schema: str) -> dict:
    reponse = client.post(
        "/api/documents/upload?wait=true", data={"schema_code": schema},
        files=[("files", (Path(nom).name, (JEU / nom).read_bytes(), "application/pdf"))],
    )
    assert reponse.status_code == 202, reponse.text
    return client.get(f"/api/documents/{reponse.json()['documents'][0]['id']}").json()


def bloquantes(document: dict) -> list[str]:
    return sorted(a["code"] for a in document["validation_warnings"] if a["severity"] in ("error", "warning"))


@pytest.mark.parametrize("nom, schema, verite, variante, attendu, _quoi", CAS, ids=[c[0] for c in CAS])
def test_chaque_document_leve_les_alertes_prevues(client_jeu, nom, schema, verite, variante, attendu, _quoi) -> None:
    if variante == "scan" and not OCR:
        pytest.skip("Tesseract n'est pas installé : le document scanné ne peut pas être lu")
    document = deposer(client_jeu, nom, schema)
    assert bloquantes(document) == sorted(attendu), nom
    if variante == "natif":
        # Chaque valeur de la vérité est imprimée dans le PDF : rien n'est « introuvable dans le texte ».
        assert not [a for a in document["validation_warnings"] if a["code"] == "valeur_non_retrouvee"], nom
    if variante == "cache":
        alerte = next(a for a in document["validation_warnings"] if a["code"] == "texte_cache")
        assert "obsolète" in alerte["actual"]
        assert document["extracted_data"]["iban"] == verite["iban"]          # l'IBAN imprimé, pas celui du piège


def test_le_doublon_est_detecte_apres_l_original(client_jeu) -> None:
    original = deposer(client_jeu, "factures/F01-facture-conforme.pdf", "facture_fournisseur")
    assert bloquantes(original) == []
    copie = deposer(client_jeu, "factures/F10-doublon-de-F01.pdf", "facture_fournisseur")
    assert bloquantes(copie) == ["doublon_possible"]


def test_un_document_hors_jeu_est_refuse_par_le_faux_modele(client_jeu, invoice_pdf: bytes) -> None:
    reponse = client_jeu.post("/api/documents/upload?wait=true", data={"schema_code": "facture_fournisseur"},
                              files=[("files", ("autre.pdf", invoice_pdf, "application/pdf"))])
    document = client_jeu.get(f"/api/documents/{reponse.json()['documents'][0]['id']}").json()
    # L'échec de lecture est signalé ; les champs obligatoires restent vides, à saisir à la main.
    assert "extraction_echouee" in bloquantes(document)
    assert "faux modèle" in next(a["message"] for a in document["validation_warnings"] if a["code"] == "extraction_echouee")


def test_le_generateur_reproduit_le_jeu_livre(tmp_path: Path) -> None:
    produits = {p.relative_to(tmp_path).as_posix() for p in generer.generer(tmp_path)}
    livres = {p.relative_to(JEU).as_posix() for p in JEU.rglob("*") if p.suffix in (".pdf", ".json")}
    assert produits == livres
    for chemin in produits:
        if chemin.endswith(".json"):
            assert (tmp_path / chemin).read_text(encoding="utf-8") == (JEU / chemin).read_text(encoding="utf-8"), chemin

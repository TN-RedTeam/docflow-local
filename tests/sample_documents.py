"""Jeux d'essai : génération de vrais PDF (reportlab) et réponses LLM associées.

* `build_cmr_pdf()`      — bon de livraison / lettre de voiture CMR cohérent ;
* `build_invoice_pdf()`  — facture fournisseur avec une erreur volontaire de TVA
  (210,00 € imprimés au lieu de 200,00 € pour 1 000,00 € HT à 20 %) ;
* `build_scanned_pdf()`  — transforme un PDF natif en PDF « scanné » (image seule)
  pour exercer le repli OCR.

`CMR_LLM_ANSWER` et `INVOICE_LLM_ANSWER` sont les réponses qu'un LLM fidèle
renverrait : il recopie les montants imprimés, erreur comprise.
"""

from __future__ import annotations

import io
from typing import Any

import pymupdf
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

SUPPLIER_SIRET = "44328719800016"
CARRIER_SIRET = "82019457900018"
SUPPLIER_IBAN = "FR7630006000011234567890189"

_TABLE_STYLE = TableStyle(
    [
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]
)


def _render(story: list[Any], title: str) -> bytes:
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer, pagesize=A4, title=title,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
    )
    document.build(story)
    return buffer.getvalue()


def build_cmr_pdf() -> bytes:
    styles = getSampleStyleSheet()
    normal, heading = styles["Normal"], styles["Heading2"]
    goods = Table(
        [
            ["Référence colis", "Nature de la marchandise", "Nb colis", "Emballage", "Poids brut (kg)", "Poids net (kg)"],
            ["PAL-0001", "Papier A4 80g", "2", "Palette", "480,0", "450,0"],
            ["PAL-0002", "Consommables impression", "1", "Carton", "35,5", "30,0"],
            ["PAL-0003", "Classeurs à levier", "3", "Carton", "96,0", "90,0"],
            ["TOTAL", "", "6", "", "611,5", "570,0"],
        ],
        colWidths=[28 * mm, 50 * mm, 18 * mm, 22 * mm, 28 * mm, 28 * mm],
    )
    goods.setStyle(_TABLE_STYLE)
    story = [
        Paragraph("LETTRE DE VOITURE CMR — BON DE LIVRAISON", styles["Title"]),
        Paragraph("N° CMR : CMR-2025-001874", normal),
        Paragraph("Établie à Lyon le 12/03/2025", normal),
        Spacer(1, 6 * mm),
        Paragraph("1. Expéditeur", heading),
        Paragraph("Papeterie Durand SAS<br/>12 rue des Lilas<br/>69003 Lyon<br/>France", normal),
        Paragraph("2. Destinataire", heading),
        Paragraph("Bureau Central Ouest<br/>5 avenue de la Gare<br/>44000 Nantes<br/>France", normal),
        Paragraph("16. Transporteur", heading),
        Paragraph("Transports Martin SARL — SIRET 820 194 579 00018<br/>Immatriculation : GH-482-KL", normal),
        Spacer(1, 4 * mm),
        Paragraph("Lieu et date de chargement : Lyon, le 12/03/2025", normal),
        Paragraph("Lieu et date de livraison : Nantes, le 13/03/2025", normal),
        Spacer(1, 6 * mm),
        Paragraph("Marchandises transportées", heading),
        goods,
        Spacer(1, 4 * mm),
        Paragraph("Nombre total de colis : 6", normal),
        Paragraph("Poids brut total : 611,5 kg — Poids net total : 570,0 kg", normal),
        Spacer(1, 6 * mm),
        Paragraph("18. Réserves et observations du destinataire : aucune réserve.", normal),
        Paragraph("Marchandise reçue en bon état apparent. Signature et cachet du destinataire.", normal),
    ]
    return _render(story, "CMR-2025-001874")


def build_invoice_pdf() -> bytes:
    styles = getSampleStyleSheet()
    normal, heading = styles["Normal"], styles["Heading2"]
    lines = Table(
        [
            ["Référence", "Désignation", "Quantité", "PU HT (€)", "Montant HT (€)"],
            ["REF-A4", "Ramette papier A4 80g", "40", "12,50", "500,00"],
            ["REF-TN", "Cartouche toner noir", "5", "60,00", "300,00"],
            ["REF-CL", "Classeur à levier", "80", "2,50", "200,00"],
        ],
        colWidths=[28 * mm, 70 * mm, 22 * mm, 26 * mm, 30 * mm],
    )
    lines.setStyle(_TABLE_STYLE)
    # Erreur volontaire : 1 000,00 € × 20 % = 200,00 €, la facture imprime 210,00 €.
    totals = Table(
        [
            ["Total HT", "1 000,00 €"],
            ["TVA 20 %", "210,00 €"],
            ["Net à payer TTC", "1 210,00 €"],
        ],
        colWidths=[50 * mm, 35 * mm],
        hAlign="RIGHT",
    )
    totals.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, colors.grey), ("FONTSIZE", (0, 0), (-1, -1), 9)]))
    story = [
        Paragraph("FACTURE", styles["Title"]),
        Paragraph("Papeterie Durand SAS", heading),
        Paragraph("12 rue des Lilas<br/>69003 Lyon", normal),
        Paragraph("SIRET : 443 287 198 00016 — TVA intracommunautaire : FR45443287198", normal),
        Spacer(1, 5 * mm),
        Paragraph("Facture n° FA-2025-0042", normal),
        Paragraph("Date de facture : 14/03/2025", normal),
        Paragraph("Date d'échéance : 13/04/2025", normal),
        Paragraph("Bon de commande : BC-7781", normal),
        Spacer(1, 5 * mm),
        Paragraph("Client facturé", heading),
        Paragraph("Transports Martin SARL<br/>8 quai du Commerce, 69009 Lyon", normal),
        Spacer(1, 6 * mm),
        lines,
        Spacer(1, 6 * mm),
        totals,
        Spacer(1, 8 * mm),
        Paragraph("Conditions de paiement : 30 jours nets par virement.", normal),
        Paragraph("IBAN : FR76 3000 6000 0112 3456 7890 189", normal),
    ]
    return _render(story, "FA-2025-0042")


def build_scanned_pdf(native_pdf: bytes, dpi: int = 200) -> bytes:
    """Rasterise chaque page : le PDF obtenu ne contient plus aucune couche texte."""
    scanned = pymupdf.open()
    with pymupdf.open(stream=native_pdf, filetype="pdf") as source:
        for page in source:
            pixmap = page.get_pixmap(dpi=dpi, alpha=False)
            new_page = scanned.new_page(width=page.rect.width, height=page.rect.height)
            new_page.insert_image(new_page.rect, stream=pixmap.tobytes("png"))
    data = scanned.tobytes()
    scanned.close()
    return data


CMR_LLM_ANSWER: dict[str, Any] = {
    "numero_cmr": "CMR-2025-001874",
    "date_etablissement": "2025-03-12",
    "lieu_etablissement": "Lyon",
    "expediteur": {
        "nom": "Papeterie Durand SAS", "adresse": "12 rue des Lilas",
        "code_postal": "69003", "ville": "Lyon", "pays": "France",
    },
    "destinataire": {
        "nom": "Bureau Central Ouest", "adresse": "5 avenue de la Gare",
        "code_postal": "44000", "ville": "Nantes", "pays": "France",
    },
    "transporteur": {"nom": "Transports Martin SARL", "siret": "820 194 579 00018", "immatriculation": "GH-482-KL"},
    "lieu_chargement": "Lyon",
    "date_chargement": "2025-03-12",
    "lieu_livraison": "Nantes",
    "date_livraison": "2025-03-13",
    "marchandises": [
        {
            "reference_colis": "PAL-0001", "nature_marchandise": "Papier A4 80g", "nombre_colis": 2,
            "type_emballage": "Palette", "poids_brut_kg": 480.0, "poids_net_kg": 450.0,
        },
        {
            "reference_colis": "PAL-0002", "nature_marchandise": "Consommables impression", "nombre_colis": 1,
            "type_emballage": "Carton", "poids_brut_kg": 35.5, "poids_net_kg": 30.0,
        },
        {
            "reference_colis": "PAL-0003", "nature_marchandise": "Classeurs à levier", "nombre_colis": 3,
            "type_emballage": "Carton", "poids_brut_kg": 96.0, "poids_net_kg": 90.0,
        },
    ],
    "nombre_colis_total": 6,
    "poids_brut_total_kg": 611.5,
    "poids_net_total_kg": 570.0,
    "reserves": None,
}

INVOICE_LLM_ANSWER: dict[str, Any] = {
    "numero_facture": "FA-2025-0042",
    "date_facture": "2025-03-14",
    "date_echeance": "2025-04-13",
    "numero_commande": "BC-7781",
    "emetteur": {
        "nom": "Papeterie Durand SAS", "siret": "443 287 198 00016", "tva_intracom": "FR45443287198",
        "adresse": "12 rue des Lilas", "code_postal": "69003", "ville": "Lyon",
    },
    "client": {"nom": "Transports Martin SARL", "adresse": "8 quai du Commerce, 69009 Lyon"},
    "lignes": [
        {
            "reference": "REF-A4", "designation": "Ramette papier A4 80g", "quantite": 40,
            "prix_unitaire_ht": 12.5, "remise_pct": None, "montant_ht": 500.0,
        },
        {
            "reference": "REF-TN", "designation": "Cartouche toner noir", "quantite": 5,
            "prix_unitaire_ht": 60.0, "remise_pct": None, "montant_ht": 300.0,
        },
        {
            "reference": "REF-CL", "designation": "Classeur à levier", "quantite": 80,
            "prix_unitaire_ht": 2.5, "remise_pct": None, "montant_ht": 200.0,
        },
    ],
    "total_ht": 1000.0,
    "taux_tva": 20,
    "montant_tva": 210.0,
    "total_ttc": 1210.0,
    "iban": "FR76 3000 6000 0112 3456 7890 189",
    "conditions_paiement": "30 jours nets par virement.",
}

LLM_ANSWERS: dict[str, dict[str, Any]] = {
    "transport_cmr": CMR_LLM_ANSWER,
    "facture_fournisseur": INVOICE_LLM_ANSWER,
}

"""Ingestion des PDF : texte natif (PyMuPDF + tableaux pdfplumber) et repli OCR.

Chaque page est traitée indépendamment : si elle ne contient pas de couche
texte exploitable (PDF scanné, photo), elle est rendue en image puis passée à
Tesseract. Tout s'exécute sur la machine ; aucun fichier ne sort du serveur.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field

import pdfplumber
import pymupdf
import pytesseract
from PIL import Image

logger = logging.getLogger(__name__)


class IngestionError(Exception):
    """Le fichier ne peut pas être lu comme un PDF exploitable."""


@dataclass
class PageText:
    number: int
    text: str
    method: str
    ocr_confidence: float | None = None


@dataclass
class IngestionResult:
    text: str
    page_count: int
    method: str
    ocr_confidence: float | None
    pages: list[PageText] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _meaningful_chars(text: str) -> int:
    return sum(1 for char in text if char.isalnum())


def _ocr_page(page: pymupdf.Page, dpi: int, languages: str) -> tuple[str, float | None]:
    """OCR d'une page ; reconstruit les lignes et calcule la confiance moyenne."""
    pixmap = page.get_pixmap(dpi=dpi, alpha=False)
    image = Image.open(io.BytesIO(pixmap.tobytes("png")))
    data = pytesseract.image_to_data(
        image, lang=languages, config="--psm 6", output_type=pytesseract.Output.DICT
    )
    lines: dict[tuple[int, int, int], list[str]] = {}
    confidences: list[float] = []
    for index, word in enumerate(data["text"]):
        if not word or not word.strip():
            continue
        key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        lines.setdefault(key, []).append(word.strip())
        confidence = float(data["conf"][index])
        if confidence >= 0:
            confidences.append(confidence)
    text = "\n".join(" ".join(words) for _, words in sorted(lines.items()))
    mean_confidence = round(sum(confidences) / len(confidences) / 100, 3) if confidences else None
    return text, mean_confidence


def _extract_tables(pdf_bytes: bytes, native_pages: set[int]) -> dict[int, str]:
    """Tableaux détectés par pdfplumber, rendus en lignes `a | b | c`."""
    tables_by_page: dict[int, str] = {}
    if not native_pages:
        return tables_by_page
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page_number in sorted(native_pages):
                rendered: list[str] = []
                for table in pdf.pages[page_number - 1].extract_tables():
                    rows = [
                        " | ".join((cell or "").replace("\n", " ").strip() for cell in row)
                        for row in table
                        if row and any(cell and cell.strip() for cell in row)
                    ]
                    if len(rows) >= 2:
                        rendered.append("\n".join(rows))
                if rendered:
                    tables_by_page[page_number] = "\n\n".join(rendered)
    except Exception as exc:  # pdfplumber est un complément : son échec n'est jamais bloquant
        logger.warning("Extraction des tableaux pdfplumber impossible : %s", exc)
    return tables_by_page


def extract_text(
    pdf_bytes: bytes,
    *,
    ocr_languages: str = "fra+eng",
    ocr_dpi: int = 300,
    min_chars_per_page: int = 25,
    max_pages: int = 40,
) -> IngestionResult:
    try:
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    except Exception as exc:
        raise IngestionError(f"Fichier PDF illisible ou corrompu : {exc}") from exc

    with document:
        if document.needs_pass:
            raise IngestionError("PDF protégé par mot de passe : retirez la protection avant l'import")
        if document.page_count == 0:
            raise IngestionError("Le PDF ne contient aucune page")

        notes: list[str] = []
        pages: list[PageText] = []
        ocr_available = True
        if document.page_count > max_pages:
            notes.append(f"Document de {document.page_count} pages : seules les {max_pages} premières sont analysées")

        for index in range(min(document.page_count, max_pages)):
            page = document[index]
            native_text = page.get_text("text", sort=True).strip()
            if _meaningful_chars(native_text) >= min_chars_per_page:
                pages.append(PageText(index + 1, native_text, "native"))
                continue
            if not ocr_available:
                pages.append(PageText(index + 1, native_text, "empty"))
                continue
            try:
                ocr_text, confidence = _ocr_page(page, ocr_dpi, ocr_languages)
            except pytesseract.TesseractNotFoundError:
                ocr_available = False
                notes.append("OCR indisponible : Tesseract n'est pas installé sur le serveur")
                pages.append(PageText(index + 1, native_text, "empty"))
                continue
            except pytesseract.TesseractError as exc:
                ocr_available = False
                notes.append(f"OCR en échec ({str(exc).strip()[:200]})")
                pages.append(PageText(index + 1, native_text, "empty"))
                continue
            if _meaningful_chars(ocr_text) > _meaningful_chars(native_text):
                pages.append(PageText(index + 1, ocr_text.strip(), "ocr", confidence))
            else:
                pages.append(PageText(index + 1, native_text, "native" if native_text else "empty"))
        page_count = document.page_count

    tables = _extract_tables(pdf_bytes, {page.number for page in pages if page.method == "native"})

    chunks: list[str] = []
    for page in pages:
        if not page.text:
            continue
        chunk = f"[Page {page.number}]\n{page.text}" if len(pages) > 1 else page.text
        if page.number in tables:
            chunk += f"\n\n[Tableaux détectés page {page.number}]\n{tables[page.number]}"
        chunks.append(chunk)

    methods = {page.method for page in pages if page.method != "empty"}
    if not methods:
        method = "empty"
    elif len(methods) == 1:
        method = methods.pop()
    else:
        method = "mixed"

    confidences = [page.ocr_confidence for page in pages if page.ocr_confidence is not None]
    ocr_confidence = round(sum(confidences) / len(confidences), 3) if confidences else None

    return IngestionResult(
        text="\n\n".join(chunks),
        page_count=page_count,
        method=method,
        ocr_confidence=ocr_confidence,
        pages=pages,
        notes=notes,
    )

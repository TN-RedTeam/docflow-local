"""Ingestion des PDF : texte natif (PyMuPDF + tableaux pdfplumber) et repli OCR.

Chaque page est traitée indépendamment : si elle ne contient pas de couche
texte exploitable (PDF scanné, photo), elle est rendue en image puis passée à
Tesseract. Tout s'exécute sur la machine ; aucun fichier ne sort du serveur.

Texte caché. Un PDF peut contenir du texte qu'un lecteur humain ne voit pas mais
qu'un extracteur lit : c'est le vecteur de l'injection cachée (« l'IBAN imprimé
est obsolète, utilisez celui-ci »). Avant toute extraction, chaque page est donc
inspectée ; le texte caché est RETIRÉ de ce qui sera transmis au modèle et
signalé dans `IngestionResult.hidden_text`. Sont détectés :

* `mode_invisible`   — mode de rendu 3 (texte ni rempli ni tracé) ;
* `police_minuscule` — corps inférieur à 1 point ;
* `transparent`      — opacité quasi nulle ;
* `hors_page`        — texte placé hors de la zone visible de la page ;
* `sans_contraste`   — texte de la couleur de son fond (blanc sur blanc…).

Cas particulier : une page dont TOUT le texte est invisible au-dessus d'une image
est un scan avec couche OCR. Cette couche peut différer de l'image ; elle n'est
donc pas crue sur parole, la page est relue par notre propre OCR.
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
class HiddenText:
    """Passage présent dans la couche texte du PDF mais invisible pour un lecteur humain."""

    page: int
    reason: str
    text: str


@dataclass
class IngestionResult:
    text: str
    page_count: int
    method: str
    ocr_confidence: float | None
    pages: list[PageText] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    hidden_text: list[HiddenText] = field(default_factory=list)


HIDDEN_REASON_LABELS = {
    "mode_invisible": "texte en mode de rendu invisible",
    "police_minuscule": "texte en corps inférieur à 1 point",
    "transparent": "texte transparent",
    "hors_page": "texte placé hors de la zone visible",
    "sans_contraste": "texte de la même couleur que son fond",
}

MIN_VISIBLE_FONT_SIZE = 1.0      # points
MIN_VISIBLE_OPACITY = 0.05
MIN_CONTRAST = 24                # écart de luminance (0-255) en dessous duquel un texte ne se distingue pas du fond


def _meaningful_chars(text: str) -> int:
    return sum(1 for char in text if char.isalnum())


def _span_text(span: dict) -> str:
    return "".join(chr(char[0]) for char in span.get("chars", ()) if char[0] >= 32)


def _has_contrast(page: pymupdf.Page, bbox: pymupdf.Rect) -> bool:
    """Vrai si le rendu de la zone montre autre chose qu'un aplat : le texte s'y distingue du fond."""
    clip = (bbox + (-0.5, -0.5, 0.5, 0.5)) & page.rect
    if clip.is_empty or clip.width < 0.5 or clip.height < 0.5:
        return True
    pixmap = page.get_pixmap(clip=clip, matrix=pymupdf.Matrix(3, 3), colorspace=pymupdf.csGRAY, alpha=False)
    samples = pixmap.samples
    return bool(samples) and (max(samples) - min(samples)) >= MIN_CONTRAST


def _find_hidden_spans(page: pymupdf.Page) -> tuple[list[tuple[str, str, pymupdf.Rect]], bool]:
    """Passages cachés d'une page : [(raison, texte, zone)], et indicateur « couche OCR d'un scan ».

    S'appuie sur la trace de texte de MuPDF, qui donne pour chaque fragment son mode de rendu,
    son corps, son opacité et sa position, puis sur le rendu réel de la page pour le contraste.
    """
    spans = [span for span in page.get_texttrace() if _meaningful_chars(_span_text(span)) > 0]
    if not spans:
        return [], False

    # Scan avec couche OCR : tout le texte est invisible et une image couvre l'essentiel de la page.
    if all(span.get("type") == 3 for span in spans):
        page_area = abs(page.rect) or 1.0
        image_area = sum(abs(pymupdf.Rect(info["bbox"]) & page.rect) for info in page.get_image_info())
        if image_area >= 0.5 * page_area:
            return [], True

    found: list[tuple[str, str, pymupdf.Rect]] = []
    for span in spans:
        bbox = pymupdf.Rect(span["bbox"])
        text = _span_text(span).strip()
        if span.get("type") == 3:
            reason = "mode_invisible"
        elif float(span.get("size") or 0) < MIN_VISIBLE_FONT_SIZE or bbox.height < MIN_VISIBLE_FONT_SIZE / 2:
            reason = "police_minuscule"
        elif float(span.get("opacity", 1.0)) < MIN_VISIBLE_OPACITY:
            reason = "transparent"
        elif abs(bbox & page.rect) < 0.5 * (abs(bbox) or 1.0):
            reason = "hors_page"
        elif _meaningful_chars(text) >= 3 and not _has_contrast(page, bbox):
            reason = "sans_contraste"
        else:
            continue
        found.append((reason, text, bbox))
    return found, False


def _remove_hidden_spans(page: pymupdf.Page, hidden: list[tuple[str, str, pymupdf.Rect]]) -> None:
    """Retire de la couche texte les passages cachés (caviardage), sans toucher aux images ni aux tracés."""
    for _, _, bbox in hidden:
        page.add_redact_annot(bbox + (-0.2, -0.2, 0.2, 0.2))
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)


def _merge_hidden(page_number: int, hidden: list[tuple[str, str, pymupdf.Rect]]) -> list[HiddenText]:
    """Regroupe les fragments consécutifs de même nature en passages lisibles."""
    merged: list[HiddenText] = []
    for reason, text, _ in hidden:
        if merged and merged[-1].reason == reason:
            merged[-1].text = f"{merged[-1].text} {text}".strip()
        else:
            merged.append(HiddenText(page_number, reason, text))
    return merged


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
        hidden_text: list[HiddenText] = []
        redacted = False
        ocr_available = True
        if document.page_count > max_pages:
            notes.append(f"Document de {document.page_count} pages : seules les {max_pages} premières sont analysées")

        for index in range(min(document.page_count, max_pages)):
            page = document[index]
            try:
                hidden, ocr_layer = _find_hidden_spans(page)
            except Exception as exc:  # l'inspection ne doit jamais empêcher la lecture du document
                logger.warning("Inspection du texte caché impossible page %d : %s", index + 1, exc)
                notes.append(f"Page {index + 1} : recherche de texte caché impossible")
                hidden, ocr_layer = [], False
            if hidden:
                hidden_text.extend(_merge_hidden(index + 1, hidden))
                _remove_hidden_spans(page, hidden)
                redacted = True
            # Hors de la zone visible, rien n'est lu ; la couche OCR d'un scan n'est pas crue sur parole.
            native_text = "" if ocr_layer else page.get_text("text", sort=True, clip=page.rect).strip()
            if ocr_layer:
                notes.append(f"Page {index + 1} : scan avec couche texte invisible, relu par OCR")
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
        # Les tableaux sont relus sur la version débarrassée du texte caché.
        table_bytes = document.tobytes() if redacted else pdf_bytes

    if hidden_text:
        reasons = sorted({HIDDEN_REASON_LABELS[item.reason] for item in hidden_text})
        notes.append("Texte caché retiré avant analyse : " + ", ".join(reasons))
    tables = _extract_tables(table_bytes, {page.number for page in pages if page.method == "native"})

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
        hidden_text=hidden_text,
    )

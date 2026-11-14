"""Pipeline complet : PDF → texte → extraction LLM → vérification → base."""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .ingestion import IngestionError, extract_text
from .llm_client import ExtractionError, OllamaClient
from .models import STATUS_PENDING_REVIEW, STATUS_PROCESSING, ExtractionAuditLog, ProcessedDocument
from .normalization import Warning, make_warning
from .schema_registry import definition_of
from .validation import (
    CODE_DUPLICATE,
    CODE_EXTRACTION_FAILED,
    CODE_UNREADABLE,
    count_blocking,
    evaluate_document,
)

logger = logging.getLogger(__name__)

# Alertes qui ne dépendent pas des données saisies et survivent à une correction humaine.
PERSISTENT_CODES = (CODE_DUPLICATE,)
MIN_TEXT_CHARS = 30


def add_audit(
    session: Session, document_id: uuid.UUID | None, event_type: str, actor: str = "system", **details: Any
) -> None:
    session.add(ExtractionAuditLog(document_id=document_id, event_type=event_type, actor=actor, details=details))


def _duplicate_warning(session: Session, document: ProcessedDocument) -> Warning | None:
    twin = session.scalars(
        select(ProcessedDocument)
        .where(
            ProcessedDocument.file_sha256 == document.file_sha256,
            ProcessedDocument.id != document.id,
            ProcessedDocument.created_at <= document.created_at,
        )
        .order_by(ProcessedDocument.created_at)
        .limit(1)
    ).first()
    if twin is None:
        return None
    return make_warning(
        CODE_DUPLICATE, "warning", None,
        f"Fichier identique déjà importé le {twin.created_at:%d/%m/%Y} sous le nom « {twin.original_filename} »",
        actual=str(twin.id),
    )


def apply_evaluation(
    document: ProcessedDocument, raw_data: Any, settings: Settings, extra_warnings: list[Warning]
) -> None:
    """Recalcule données normalisées, alertes et score, puis les pose sur le document."""
    ocr_confidence = float(document.ocr_confidence) if document.ocr_confidence is not None else None
    data, warnings, score = evaluate_document(
        definition_of(document.schema), raw_data, document.raw_text, settings.amount_tolerance, ocr_confidence
    )
    warnings = [*extra_warnings, *warnings]
    document.extracted_data = data
    document.validation_warnings = warnings
    document.warning_count = count_blocking(warnings)
    document.confidence_score = Decimal(str(score))


def _fail(document: ProcessedDocument, settings: Settings, code: str, message: str, extra: list[Warning]) -> None:
    apply_evaluation(document, {}, settings, [make_warning(code, "error", None, message), *extra])
    document.confidence_score = Decimal("0")


def process_document(
    session_factory: sessionmaker[Session], llm: OllamaClient, settings: Settings, document_id: uuid.UUID
) -> None:
    """Traite un document en attente. Ne lève jamais : tout échec devient une alerte visible."""
    with session_factory() as session:
        document = session.get(ProcessedDocument, document_id)
        if document is None or document.status != STATUS_PROCESSING:
            return
        try:
            _run(session, document, llm, settings)
        except Exception as exc:  # garde-fou : un document ne doit jamais rester bloqué en traitement
            logger.exception("Traitement du document %s en échec", document_id)
            session.rollback()
            document = session.get(ProcessedDocument, document_id)
            if document is None:
                return
            _fail(document, settings, CODE_EXTRACTION_FAILED, f"Erreur interne pendant le traitement : {exc}", [])
            add_audit(session, document.id, "processing_failed", error=str(exc))
        document.status = STATUS_PENDING_REVIEW
        session.commit()


def _run(session: Session, document: ProcessedDocument, llm: OllamaClient, settings: Settings) -> None:
    extra: list[Warning] = []
    duplicate = _duplicate_warning(session, document)
    if duplicate:
        extra.append(duplicate)

    try:
        ingestion = extract_text(
            document.source_file,
            ocr_languages=settings.ocr_languages,
            ocr_dpi=settings.ocr_dpi,
            min_chars_per_page=settings.ocr_min_chars_per_page,
            max_pages=settings.max_pages,
        )
    except IngestionError as exc:
        _fail(document, settings, CODE_UNREADABLE, str(exc), extra)
        add_audit(session, document.id, "ingestion_failed", error=str(exc))
        return

    document.raw_text = ingestion.text
    document.page_count = ingestion.page_count
    document.extraction_method = ingestion.method
    document.ocr_confidence = Decimal(str(ingestion.ocr_confidence)) if ingestion.ocr_confidence is not None else None
    add_audit(
        session, document.id, "text_extracted",
        method=ingestion.method, pages=ingestion.page_count, characters=len(ingestion.text),
        ocr_confidence=ingestion.ocr_confidence, notes=ingestion.notes,
    )

    if len(ingestion.text.strip()) < MIN_TEXT_CHARS:
        reason = " ; ".join(ingestion.notes) or "aucun texte détecté, même après OCR"
        _fail(document, settings, CODE_UNREADABLE, f"Document illisible : {reason}. Saisie manuelle nécessaire.", extra)
        return

    definition = definition_of(document.schema)
    try:
        result = llm.extract(definition, ingestion.text)
    except ExtractionError as exc:
        _fail(document, settings, CODE_EXTRACTION_FAILED, f"{exc} Saisie manuelle ou relance possible.", extra)
        add_audit(session, document.id, "llm_failed", model=llm.model, error=str(exc))
        return

    document.llm_model = result.model
    document.llm_raw_data = result.data
    add_audit(
        session, document.id, "llm_extracted",
        model=result.model, duration_ms=result.duration_ms, attempts=result.attempts,
        truncated_input=result.truncated_input, usage=result.usage,
    )
    if result.truncated_input:
        extra.append(
            make_warning(
                "document_tronque", "warning", None,
                f"Document trop long : seuls les {settings.llm_max_input_chars} premiers caractères ont été analysés",
            )
        )

    apply_evaluation(document, result.data, settings, extra)
    add_audit(
        session, document.id, "checks_completed",
        confidence_score=float(document.confidence_score or 0),
        blocking_warnings=document.warning_count,
        codes=sorted({warning["code"] for warning in document.validation_warnings}),
    )

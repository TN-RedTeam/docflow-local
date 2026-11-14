"""Endpoints documents : import, liste, révision, validation, export."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from concurrent.futures import Future
from datetime import date, datetime, time, timezone
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, Response, UploadFile, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session, undefer

from ..api_models import (
    AuditLogOut,
    CorrectionPayload,
    DocumentDetail,
    DocumentList,
    DocumentSummary,
    RejectedFile,
    StatsOut,
    UploadResponse,
    ValidationPayload,
)
from ..config import Settings
from ..exporter import export_csv, export_json, schema_for_csv, write_export_file
from ..models import (
    STATUS_PENDING_REVIEW,
    STATUS_PROCESSING,
    STATUS_VALIDATED,
    DocumentSchema,
    ExtractionAuditLog,
    ProcessedDocument,
)
from ..pipeline import PERSISTENT_CODES, add_audit, apply_evaluation, process_document
from ..schema_registry import definition_of

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/documents", tags=["documents"])

StatusFilter = Literal["processing", "pending_review", "validated"]


def get_session(request: Request):
    with request.app.state.session_factory() as session:
        yield session


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _load(session: Session, document_id: uuid.UUID, *, with_text: bool = False) -> ProcessedDocument:
    options = [undefer(ProcessedDocument.raw_text)] if with_text else []
    document = session.get(ProcessedDocument, document_id, options=options)
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document introuvable")
    return document


def _require_editable(document: ProcessedDocument) -> None:
    if document.status == STATUS_VALIDATED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Document déjà validé : l'enregistrement est verrouillé")
    if document.status == STATUS_PROCESSING:
        raise HTTPException(status.HTTP_409_CONFLICT, "Document en cours de traitement : réessayez dans un instant")


def _submit(request: Request, document_id: uuid.UUID) -> Future:
    state = request.app.state
    return state.executor.submit(process_document, state.session_factory, state.llm, state.settings, document_id)


def _apply_filters(
    query,
    *,
    schema: str | None,
    status_filter: str | None,
    q: str | None,
    has_warnings: bool | None,
    date_from: date | None,
    date_to: date | None,
):
    if schema:
        query = query.where(ProcessedDocument.schema.has(DocumentSchema.code == schema))
    if status_filter:
        query = query.where(ProcessedDocument.status == status_filter)
    if q:
        query = query.where(ProcessedDocument.original_filename.ilike(f"%{q.strip()}%"))
    if has_warnings is True:
        query = query.where(ProcessedDocument.warning_count > 0)
    elif has_warnings is False:
        query = query.where(ProcessedDocument.warning_count == 0)
    if date_from:
        query = query.where(ProcessedDocument.created_at >= datetime.combine(date_from, time.min, timezone.utc))
    if date_to:
        query = query.where(ProcessedDocument.created_at <= datetime.combine(date_to, time.max, timezone.utc))
    return query


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


@router.post("/upload", response_model=UploadResponse, status_code=status.HTTP_202_ACCEPTED)
async def upload_documents(
    request: Request,
    session: SessionDep,
    settings: SettingsDep,
    schema_code: Annotated[str, Form(description="Code du schéma : transport_cmr, facture_fournisseur, ...")],
    files: Annotated[list[UploadFile], File(description="Un ou plusieurs fichiers PDF")],
    wait: Annotated[bool, Query(description="Attendre la fin de l'extraction avant de répondre")] = False,
) -> UploadResponse:
    """Importe un PDF ou un lot de PDF. L'extraction s'exécute en tâche de fond (ou en ligne si `wait=true`)."""
    schema_row = session.scalar(
        select(DocumentSchema).where(DocumentSchema.code == schema_code, DocumentSchema.is_active.is_(True))
    )
    if schema_row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Schéma « {schema_code} » inconnu")
    if len(files) > settings.max_batch_files:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            f"Lot limité à {settings.max_batch_files} fichiers par envoi",
        )

    accepted: list[ProcessedDocument] = []
    rejected: list[RejectedFile] = []
    for upload in files:
        filename = (upload.filename or "document.pdf").replace("\\", "/").rsplit("/", 1)[-1][:512] or "document.pdf"
        content = await upload.read(settings.max_upload_bytes + 1)
        if len(content) > settings.max_upload_bytes:
            rejected.append(RejectedFile(filename=filename, reason=f"Fichier supérieur à {settings.max_upload_mb} Mo"))
            continue
        if b"%PDF-" not in content[:1024]:
            rejected.append(RejectedFile(filename=filename, reason="Le fichier n'est pas un PDF"))
            continue
        document = ProcessedDocument(
            id=uuid.uuid4(),
            schema_id=schema_row.id,
            original_filename=filename,
            content_type="application/pdf",
            file_size=len(content),
            file_sha256=hashlib.sha256(content).hexdigest(),
            source_file=content,
            status=STATUS_PROCESSING,
            extracted_data={},
            validation_warnings=[],
        )
        session.add(document)
        session.flush()
        add_audit(session, document.id, "uploaded", filename=filename, size=len(content), schema=schema_code)
        accepted.append(document)

    if not accepted:
        session.rollback()
        reasons = " ; ".join(f"{item.filename} : {item.reason}" for item in rejected) or "aucun fichier reçu"
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Aucun fichier exploitable ({reasons})")

    session.commit()
    futures = [_submit(request, document.id) for document in accepted]
    if wait:
        await asyncio.gather(*(asyncio.wrap_future(future) for future in futures))
        session.expire_all()
    documents = [DocumentSummary.model_validate(session.get(ProcessedDocument, doc.id)) for doc in accepted]
    return UploadResponse(documents=documents, rejected=rejected)


# --------------------------------------------------------------------------
# Liste, statistiques, export (déclarés avant les routes paramétrées par id)
# --------------------------------------------------------------------------


@router.get("", response_model=DocumentList)
def list_documents(
    session: SessionDep,
    schema: Annotated[str | None, Query(description="Code du schéma")] = None,
    status_filter: Annotated[StatusFilter | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query(description="Recherche sur le nom de fichier")] = None,
    has_warnings: Annotated[bool | None, Query(description="Avec ou sans alerte de cohérence")] = None,
    date_from: Annotated[date | None, Query(description="Importé à partir du (AAAA-MM-JJ)")] = None,
    date_to: Annotated[date | None, Query(description="Importé jusqu'au (AAAA-MM-JJ)")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DocumentList:
    filters = dict(
        schema=schema, status_filter=status_filter, q=q,
        has_warnings=has_warnings, date_from=date_from, date_to=date_to,
    )
    total = session.scalar(_apply_filters(select(func.count(ProcessedDocument.id)), **filters)) or 0
    rows = session.scalars(
        _apply_filters(select(ProcessedDocument), **filters)
        .order_by(ProcessedDocument.created_at.desc(), ProcessedDocument.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return DocumentList(
        items=[DocumentSummary.model_validate(row) for row in rows], total=total, limit=limit, offset=offset
    )


@router.get("/stats", response_model=StatsOut)
def document_stats(session: SessionDep) -> StatsOut:
    counts = dict(
        session.execute(select(ProcessedDocument.status, func.count()).group_by(ProcessedDocument.status)).all()
    )
    with_warnings = session.scalar(
        select(func.count(ProcessedDocument.id)).where(
            ProcessedDocument.status == STATUS_PENDING_REVIEW, ProcessedDocument.warning_count > 0
        )
    )
    return StatsOut(
        processing=counts.get(STATUS_PROCESSING, 0),
        pending_review=counts.get(STATUS_PENDING_REVIEW, 0),
        validated=counts.get(STATUS_VALIDATED, 0),
        with_warnings=with_warnings or 0,
    )


@router.get("/export")
def export_documents(
    session: SessionDep,
    export_format: Annotated[Literal["csv", "json"], Query(alias="format")] = "json",
    schema: Annotated[str | None, Query(description="Code du schéma (obligatoire en CSV si plusieurs types)")] = None,
    status_filter: Annotated[StatusFilter, Query(alias="status")] = "validated",
    ids: Annotated[str | None, Query(description="Identifiants séparés par des virgules")] = None,
    only_new: Annotated[bool, Query(description="Uniquement les documents jamais exportés")] = False,
    date_from: Annotated[date | None, Query()] = None,
    date_to: Annotated[date | None, Query()] = None,
) -> Response:
    """Export normalisé, par défaut des seuls documents validés, pour injection ERP / comptabilité."""
    query = _apply_filters(
        select(ProcessedDocument),
        schema=schema, status_filter=status_filter, q=None, has_warnings=None, date_from=date_from, date_to=date_to,
    )
    if ids:
        try:
            wanted = [uuid.UUID(part.strip()) for part in ids.split(",") if part.strip()]
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Paramètre ids invalide") from exc
        query = query.where(ProcessedDocument.id.in_(wanted))
    if only_new:
        query = query.where(ProcessedDocument.exported_at.is_(None))
    documents = list(session.scalars(query.order_by(ProcessedDocument.created_at, ProcessedDocument.id)).all())

    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    if export_format == "csv":
        definition = schema_for_csv(documents)
        if definition is None and documents:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "L'export CSV porte sur un seul type de document : précisez le paramètre schema",
            )
        if definition is None:
            schema_row = session.scalar(select(DocumentSchema).where(DocumentSchema.code == schema)) if schema else None
            if schema_row is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Aucun document à exporter")
            definition = definition_of(schema_row)
        body = export_csv(definition, documents)
        media_type = "text/csv; charset=utf-8"
        filename = f"docflow_{definition.code}_{stamp}.csv"
    else:
        body = export_json(documents, now)
        media_type = "application/json"
        filename = f"docflow_{schema or 'documents'}_{stamp}.json"

    for document in documents:
        if document.status == STATUS_VALIDATED:
            document.exported_at = now
    if documents:
        add_audit(
            session, None, "exported",
            format=export_format, count=len(documents), document_ids=[str(doc.id) for doc in documents],
        )
    session.commit()
    return Response(
        content=body,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Document-Count": str(len(documents)),
        },
    )


# --------------------------------------------------------------------------
# Document unitaire
# --------------------------------------------------------------------------


@router.get("/{document_id}", response_model=DocumentDetail)
def get_document(document_id: uuid.UUID, session: SessionDep) -> DocumentDetail:
    return DocumentDetail.model_validate(_load(session, document_id))


@router.get("/{document_id}/file")
def get_document_file(document_id: uuid.UUID, session: SessionDep) -> Response:
    """Fichier PDF source, affiché tel quel dans la visionneuse de révision."""
    document = session.get(ProcessedDocument, document_id, options=[undefer(ProcessedDocument.source_file)])
    if document is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Document introuvable")
    return Response(
        content=document.source_file,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(document.original_filename)}",
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{document_id}/audit", response_model=list[AuditLogOut])
def get_document_audit(document_id: uuid.UUID, session: SessionDep) -> list[AuditLogOut]:
    _load(session, document_id)
    rows = session.scalars(
        select(ExtractionAuditLog)
        .where(ExtractionAuditLog.document_id == document_id)
        .order_by(ExtractionAuditLog.created_at, ExtractionAuditLog.id)
    ).all()
    return [AuditLogOut.model_validate(row) for row in rows]


def _reevaluate(document: ProcessedDocument, payload: CorrectionPayload, settings: Settings) -> bool:
    """Applique la correction humaine et relance tous les contrôles. Retourne True si les données ont changé."""
    before = document.extracted_data
    raw = payload.data if payload.data is not None else before
    persistent = [warning for warning in document.validation_warnings if warning.get("code") in PERSISTENT_CODES]
    try:
        apply_evaluation(document, raw, settings, persistent)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Données incompatibles avec le schéma : {exc}") from exc
    return document.extracted_data != before


@router.patch("/{document_id}", response_model=DocumentDetail)
def save_correction(
    document_id: uuid.UUID, payload: CorrectionPayload, session: SessionDep, settings: SettingsDep
) -> DocumentDetail:
    """Enregistre une correction intermédiaire et recalcule les alertes, sans verrouiller."""
    document = _load(session, document_id, with_text=True)
    _require_editable(document)
    changed = _reevaluate(document, payload, settings)
    if changed:
        add_audit(
            session, document.id, "human_corrected", actor=payload.actor,
            blocking_warnings=document.warning_count,
        )
    session.commit()
    return DocumentDetail.model_validate(document)


@router.patch("/{document_id}/validate", response_model=DocumentDetail)
def validate_document(
    document_id: uuid.UUID, payload: ValidationPayload, session: SessionDep, settings: SettingsDep
) -> DocumentDetail:
    """Validation finale après contrôle humain : verrouille l'enregistrement et déclenche l'export.

    Les contrôles sont rejoués sur les données corrigées. S'il reste des alertes,
    la validation est refusée (409) sauf si l'opérateur confirme avec `force=true`.
    """
    document = _load(session, document_id, with_text=True)
    _require_editable(document)
    changed = _reevaluate(document, payload, settings)

    if document.warning_count and not payload.force:
        remaining = [w for w in document.validation_warnings if w["severity"] != "info"]
        add_audit(
            session, document.id, "validation_refused", actor=payload.actor,
            changed=changed, blocking_warnings=document.warning_count,
        )
        session.commit()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail={
                "message": f"{len(remaining)} alerte(s) non résolue(s) : corrigez-les ou confirmez la validation forcée",
                "validation_warnings": remaining,
            },
        )

    document.status = STATUS_VALIDATED
    document.validated_at = datetime.now(timezone.utc)
    document.validated_by = payload.actor
    add_audit(
        session, document.id, "validated", actor=payload.actor,
        changed=changed, forced=bool(document.warning_count), remaining_warnings=document.warning_count,
    )
    session.flush()

    if settings.export_dir is not None:
        try:
            path = write_export_file(settings.export_dir, document)
        except OSError as exc:
            logger.error("Dépôt d'export impossible pour %s : %s", document.id, exc)
            add_audit(session, document.id, "export_failed", error=str(exc))
        else:
            document.exported_at = datetime.now(timezone.utc)
            add_audit(session, document.id, "exported", format="json", path=str(path))
    session.commit()
    return DocumentDetail.model_validate(document)


@router.post("/{document_id}/reprocess", response_model=DocumentDetail, status_code=status.HTTP_202_ACCEPTED)
def reprocess_document(
    document_id: uuid.UUID,
    request: Request,
    session: SessionDep,
    wait: Annotated[bool, Query()] = False,
) -> DocumentDetail:
    """Relance l'extraction complète (par exemple après le redémarrage d'Ollama)."""
    document = _load(session, document_id)
    _require_editable(document)
    document.status = STATUS_PROCESSING
    add_audit(session, document.id, "reprocess_requested")
    session.commit()
    future = _submit(request, document.id)
    if wait:
        future.result()
    session.expire_all()
    return DocumentDetail.model_validate(_load(session, document_id))


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_document(document_id: uuid.UUID, session: SessionDep) -> Response:
    """Supprime un document non validé (erreur d'import, doublon)."""
    document = _load(session, document_id)
    if document.status == STATUS_VALIDATED:
        raise HTTPException(status.HTTP_409_CONFLICT, "Document validé : suppression interdite")
    session.delete(document)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


"""Modèles d'entrée/sortie de l'API REST."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SchemaOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    code: str
    label: str
    description: str | None
    version: int
    definition: dict[str, Any]


class DocumentSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    schema_code: str
    schema_label: str
    original_filename: str
    file_size: int
    page_count: int | None
    extraction_method: str | None
    status: str
    confidence_score: float | None
    warning_count: int
    created_at: datetime
    updated_at: datetime
    validated_at: datetime | None
    validated_by: str | None
    exported_at: datetime | None


class DocumentDetail(DocumentSummary):
    ocr_confidence: float | None
    llm_model: str | None
    extracted_data: dict[str, Any]
    validation_warnings: list[dict[str, Any]]


class DocumentList(BaseModel):
    items: list[DocumentSummary]
    total: int
    limit: int
    offset: int


class RejectedFile(BaseModel):
    filename: str
    reason: str


class UploadResponse(BaseModel):
    documents: list[DocumentSummary]
    rejected: list[RejectedFile]


class CorrectionPayload(BaseModel):
    """Données corrigées par l'opérateur. Si `data` est omis, les données en base sont conservées."""

    data: dict[str, Any] | None = None
    actor: str = Field(default="operateur", min_length=1, max_length=128)


class ValidationPayload(CorrectionPayload):
    """`force=true` valide malgré des alertes restantes (décision humaine tracée dans l'audit)."""

    force: bool = False


class AuditLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    event_type: str
    actor: str
    details: dict[str, Any]
    created_at: datetime


class StatsOut(BaseModel):
    processing: int
    pending_review: int
    validated: int
    with_warnings: int

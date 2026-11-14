"""Modèles ORM. Le schéma de référence PostgreSQL est `db/init.sql`."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, deferred, mapped_column, relationship

from .database import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")
BigIntPk = BigInteger().with_variant(Integer(), "sqlite")

STATUS_PROCESSING = "processing"
STATUS_PENDING_REVIEW = "pending_review"
STATUS_VALIDATED = "validated"
ALL_STATUSES = (STATUS_PROCESSING, STATUS_PENDING_REVIEW, STATUS_VALIDATED)


class DocumentSchema(Base):
    __tablename__ = "document_schemas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    definition: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class ProcessedDocument(Base):
    __tablename__ = "processed_documents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    schema_id: Mapped[int] = mapped_column(ForeignKey("document_schemas.id"), nullable=False, index=True)

    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), nullable=False, default="application/pdf")
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    file_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_file: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))

    page_count: Mapped[int | None] = mapped_column(Integer)
    extraction_method: Mapped[str | None] = mapped_column(String(16))
    ocr_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    raw_text: Mapped[str | None] = deferred(mapped_column(Text))
    llm_model: Mapped[str | None] = mapped_column(String(128))
    llm_raw_data: Mapped[dict[str, Any] | None] = deferred(mapped_column(JsonType))

    extracted_data: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))
    validation_warnings: Mapped[list[dict[str, Any]]] = mapped_column(JsonType, nullable=False, default=list)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default=STATUS_PROCESSING, index=True)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    validated_by: Mapped[str | None] = mapped_column(String(128))
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    schema: Mapped[DocumentSchema] = relationship(lazy="joined")
    audit_logs: Mapped[list["ExtractionAuditLog"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

    @property
    def schema_code(self) -> str:
        return self.schema.code

    @property
    def schema_label(self) -> str:
        return self.schema.label


class ExtractionAuditLog(Base):
    __tablename__ = "extraction_audit_logs"

    id: Mapped[int] = mapped_column(BigIntPk, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("processed_documents.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="system")
    details: Mapped[dict[str, Any]] = mapped_column(JsonType, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    document: Mapped[ProcessedDocument | None] = relationship(back_populates="audit_logs")

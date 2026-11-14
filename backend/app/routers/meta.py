"""Endpoints transverses : santé du service et catalogue des schémas."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select, text

from .. import __version__
from ..api_models import SchemaOut
from ..models import DocumentSchema
from .documents import SessionDep

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health")
def health(request: Request, session: SessionDep) -> dict[str, Any]:
    """État de la base et d'Ollama. L'API reste utilisable (saisie manuelle) si Ollama est arrêté."""
    try:
        session.execute(text("SELECT 1"))
        database = {"reachable": True}
    except Exception as exc:
        database = {"reachable": False, "detail": str(exc)}
    ollama = request.app.state.llm.health()
    healthy = database["reachable"] and ollama["reachable"] and ollama["model_available"]
    return {
        "status": "ok" if healthy else "degraded",
        "version": __version__,
        "database": database,
        "ollama": ollama,
    }


@router.get("/schemas", response_model=list[SchemaOut])
def list_schemas(session: SessionDep) -> list[SchemaOut]:
    rows = session.scalars(
        select(DocumentSchema).where(DocumentSchema.is_active.is_(True)).order_by(DocumentSchema.label)
    ).all()
    return [SchemaOut.model_validate(row) for row in rows]


@router.get("/schemas/{code}", response_model=SchemaOut)
def get_schema(code: str, session: SessionDep) -> SchemaOut:
    row = session.scalar(select(DocumentSchema).where(DocumentSchema.code == code))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Schéma « {code} » inconnu")
    return SchemaOut.model_validate(row)

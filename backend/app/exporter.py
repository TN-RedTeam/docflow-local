"""Export normalisé des documents (CSV à plat ou JSON) pour ERP et logiciels comptables."""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ProcessedDocument
from .schema_registry import FieldDefinition, SchemaDefinition, definition_of, find_field

logger = logging.getLogger(__name__)

META_COLUMNS = [
    "document_id", "fichier_source", "type_document", "statut",
    "date_validation", "valide_par", "score_confiance",
]


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def document_to_export(document: ProcessedDocument) -> dict[str, Any]:
    return {
        "document_id": str(document.id),
        "fichier_source": document.original_filename,
        "type_document": document.schema_code,
        "version_schema": document.schema.version,
        "statut": document.status,
        "date_import": _iso(document.created_at),
        "date_validation": _iso(document.validated_at),
        "valide_par": document.validated_by,
        "score_confiance": float(document.confidence_score) if document.confidence_score is not None else None,
        "alertes_restantes": [w for w in document.validation_warnings if w.get("severity") != "info"],
        "donnees": document.extracted_data,
    }


def export_json(documents: list[ProcessedDocument], generated_at: datetime) -> bytes:
    payload = {
        "genere_le": generated_at.isoformat(),
        "nombre_documents": len(documents),
        "documents": [document_to_export(document) for document in documents],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


def _header_columns(fields: list[FieldDefinition], prefix: str, line_array: str | None) -> list[tuple[str, str]]:
    """Colonnes d'en-tête : (nom de colonne, chemin). Le tableau de lignes est traité à part."""
    columns: list[tuple[str, str]] = []
    for field in fields:
        path = f"{prefix}{field.name}"
        if field.type == "object":
            columns.extend(_header_columns(field.fields, f"{path}.", line_array))
        elif path != line_array:
            columns.append((path, path))
    return columns


def _lookup(data: Any, path: str) -> Any:
    current = data
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def export_csv(definition: SchemaDefinition, documents: list[ProcessedDocument], delimiter: str = ";") -> bytes:
    """CSV à plat : une ligne par article/colis, les champs d'en-tête étant répétés.

    Encodage UTF-8 avec BOM et séparateur `;` (ouverture directe dans Excel FR),
    point décimal, dates ISO.
    """
    line_array = definition.export.line_array
    header_columns = _header_columns(definition.fields, "", line_array)
    line_fields: list[FieldDefinition] = []
    if line_array:
        line_fields = find_field(definition.fields, line_array).fields

    columns = [*META_COLUMNS, *(name for name, _ in header_columns)]
    if line_array:
        columns.append(f"{line_array}.numero_ligne")
        columns.extend(f"{line_array}.{field.name}" for field in line_fields)

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter=delimiter, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(columns)
    for document in documents:
        data = document.extracted_data or {}
        base = [
            str(document.id),
            document.original_filename,
            document.schema_code,
            document.status,
            _iso(document.validated_at) or "",
            document.validated_by or "",
            _cell(float(document.confidence_score)) if document.confidence_score is not None else "",
        ]
        base.extend(_cell(_lookup(data, path)) for _, path in header_columns)
        if not line_array:
            writer.writerow(base)
            continue
        items = _lookup(data, line_array) or []
        if not items:
            writer.writerow([*base, "", *("" for _ in line_fields)])
        for number, item in enumerate(items, start=1):
            writer.writerow([*base, str(number), *(_cell(item.get(field.name)) for field in line_fields)])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def write_export_file(export_dir: Path, document: ProcessedDocument) -> Path:
    """Dépose le JSON d'un document validé dans le dossier d'échange surveillé par l'ERP.

    L'écriture est atomique (fichier temporaire puis renommage) pour qu'un
    import ERP ne lise jamais un fichier partiel.
    """
    target_dir = export_dir / document.schema_code
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{document.id}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(document_to_export(document), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return target


def schema_for_csv(documents: list[ProcessedDocument]) -> SchemaDefinition | None:
    """Schéma commun aux documents, ou None s'ils relèvent de plusieurs types."""
    codes = {document.schema_code for document in documents}
    if len(codes) != 1:
        return None
    return definition_of(documents[0].schema)

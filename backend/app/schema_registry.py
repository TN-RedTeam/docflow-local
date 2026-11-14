"""Schémas documentaires dynamiques pilotés par configuration JSON.

Un fichier `schemas/<code>.json` décrit les champs à extraire et les règles de
cohérence à vérifier. À partir de cette seule définition sont générés :

* le JSON Schema strict transmis au LLM (`response_format`),
* un modèle Pydantic dynamique qui garantit la forme des données stockées,
* le formulaire de révision du frontend,
* les colonnes de l'export.

Ajouter un type de document revient donc à déposer un nouveau fichier JSON.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DocumentSchema

logger = logging.getLogger(__name__)

FieldType = Literal["string", "text", "number", "integer", "date", "boolean", "object", "array"]
FieldFormat = Literal["siret", "siren", "tva_intracom", "iban", "code_postal_fr"]
RuleType = Literal[
    "product_equals", "sum_array_equals", "sum_fields_equals", "percentage_equals", "less_or_equal"
]

SCALAR_TYPES = ("string", "text", "number", "integer", "date", "boolean")

_REQUIRED_RULE_PARAMS: dict[str, tuple[str, ...]] = {
    "product_equals": ("factors", "result"),
    "sum_array_equals": ("array", "item_field", "total"),
    "sum_fields_equals": ("terms", "total"),
    "percentage_equals": ("base", "rate", "result"),
    "less_or_equal": ("left", "right"),
}


class FieldDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    type: FieldType = "string"
    required: bool = False
    description: str | None = None
    format: FieldFormat | None = None
    unit: str | None = None
    allowed_values: list[float | str] | None = None
    min_items: int = Field(default=0, ge=0)
    fields: list["FieldDefinition"] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_structure(self) -> "FieldDefinition":
        if self.type in ("object", "array") and not self.fields:
            raise ValueError(f"Le champ '{self.name}' de type {self.type} doit déclarer des sous-champs")
        if self.type in SCALAR_TYPES and self.fields:
            raise ValueError(f"Le champ scalaire '{self.name}' ne peut pas avoir de sous-champs")
        if self.type == "array" and any(child.type in ("object", "array") for child in self.fields):
            raise ValueError(f"Les éléments du tableau '{self.name}' doivent être des champs scalaires")
        names = [child.name for child in self.fields]
        if len(names) != len(set(names)):
            raise ValueError(f"Sous-champs en double dans '{self.name}'")
        return self


class RuleDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: RuleType
    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    severity: Literal["error", "warning"] = "warning"
    tolerance: Decimal | None = Field(default=None, ge=0)

    array: str | None = None
    factors: list[str] = Field(default_factory=list)
    discount_percent: str | None = None
    result: str | None = None
    item_field: str | None = None
    terms: list[str] = Field(default_factory=list)
    total: str | None = None
    base: str | None = None
    rate: str | None = None
    left: str | None = None
    right: str | None = None

    @model_validator(mode="after")
    def _check_params(self) -> "RuleDefinition":
        missing = [name for name in _REQUIRED_RULE_PARAMS[self.type] if not getattr(self, name)]
        if missing:
            raise ValueError(f"Règle '{self.code}' ({self.type}) : paramètres manquants {missing}")
        return self


class ExportOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_array: str | None = None


class SchemaDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    label: str
    description: str | None = None
    version: int = Field(default=1, ge=1)
    document_hint: str | None = None
    fields: list[FieldDefinition] = Field(min_length=1)
    validation_rules: list[RuleDefinition] = Field(default_factory=list)
    export: ExportOptions = Field(default_factory=ExportOptions)

    @model_validator(mode="after")
    def _check_references(self) -> "SchemaDefinition":
        names = [field.name for field in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("Champs racine en double dans le schéma")
        for rule in self.validation_rules:
            scope = self.fields
            if rule.array:
                array_field = find_field(self.fields, rule.array)
                if array_field is None or array_field.type != "array":
                    raise ValueError(f"Règle '{rule.code}' : '{rule.array}' n'est pas un tableau du schéma")
                scope = array_field.fields
            item_paths = [rule.item_field] if rule.type == "sum_array_equals" else []
            if rule.type == "sum_array_equals":
                root_paths, scoped_paths = [rule.total], item_paths
            else:
                root_paths = []
                scoped_paths = [
                    *rule.factors, *rule.terms,
                    rule.discount_percent, rule.result, rule.total, rule.base, rule.rate, rule.left, rule.right,
                ]
            for path in root_paths:
                if path and find_field(self.fields, path) is None:
                    raise ValueError(f"Règle '{rule.code}' : champ inconnu '{path}'")
            for path in scoped_paths:
                if path and find_field(scope, path) is None:
                    raise ValueError(f"Règle '{rule.code}' : champ inconnu '{path}'")
        if self.export.line_array:
            line_field = find_field(self.fields, self.export.line_array)
            if line_field is None or line_field.type != "array":
                raise ValueError(f"export.line_array '{self.export.line_array}' n'est pas un tableau du schéma")
        return self


def find_field(fields: list[FieldDefinition], path: str) -> FieldDefinition | None:
    """Résout un chemin pointé (`emetteur.siret`) dans une liste de champs."""
    current = fields
    found: FieldDefinition | None = None
    for part in path.split("."):
        found = next((field for field in current if field.name == part), None)
        if found is None:
            return None
        current = found.fields
    return found


# --------------------------------------------------------------------------
# JSON Schema strict pour le LLM
# --------------------------------------------------------------------------

_JSON_TYPES = {
    "string": "string",
    "text": "string",
    "date": "string",
    "number": "number",
    "integer": "integer",
    "boolean": "boolean",
}


def _describe(field: FieldDefinition) -> str:
    parts = [field.label]
    if field.description:
        parts.append(field.description)
    if field.type == "date":
        parts.append("Format AAAA-MM-JJ")
    if field.unit:
        parts.append(f"Valeur numérique en {field.unit}, sans l'unité")
    return ". ".join(parts)


def _object_schema(fields: list[FieldDefinition]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {field.name: _field_schema(field) for field in fields},
        "required": [field.name for field in fields],
        "additionalProperties": False,
    }


def _field_schema(field: FieldDefinition) -> dict[str, Any]:
    if field.type == "object":
        return {**_object_schema(field.fields), "description": _describe(field)}
    if field.type == "array":
        return {"type": "array", "description": _describe(field), "items": _object_schema(field.fields)}
    return {"type": [_JSON_TYPES[field.type], "null"], "description": _describe(field)}


def build_json_schema(definition: SchemaDefinition) -> dict[str, Any]:
    """JSON Schema « strict » : toutes les clés requises, valeurs absentes à null."""
    return _object_schema(definition.fields)


# --------------------------------------------------------------------------
# Modèle Pydantic dynamique
# --------------------------------------------------------------------------

_PYTHON_TYPES: dict[str, Any] = {
    "string": str,
    "text": str,
    "date": str,
    "number": float,
    "integer": int,
    "boolean": bool,
}


def _build_model(name: str, fields: list[FieldDefinition]) -> type[BaseModel]:
    attributes: dict[str, Any] = {}
    for field in fields:
        child_name = f"{name}_{field.name}"
        if field.type == "object":
            attributes[field.name] = (_build_model(child_name, field.fields), ...)
        elif field.type == "array":
            attributes[field.name] = (list[_build_model(child_name, field.fields)], Field(default_factory=list))
        else:
            attributes[field.name] = (Optional[_PYTHON_TYPES[field.type]], None)
    return create_model(name, __config__=ConfigDict(extra="forbid", strict=True), **attributes)


_MODEL_CACHE: dict[tuple[str, int, str], type[BaseModel]] = {}


def build_pydantic_model(definition: SchemaDefinition) -> type[BaseModel]:
    """Modèle Pydantic strict décrivant la forme exacte des données d'un document."""
    key = (definition.code, definition.version, definition.model_dump_json())
    if key not in _MODEL_CACHE:
        _MODEL_CACHE[key] = _build_model(f"Doc_{definition.code}", definition.fields)
    return _MODEL_CACHE[key]


# --------------------------------------------------------------------------
# Chargement depuis le disque et synchronisation en base
# --------------------------------------------------------------------------


def load_schema_files(schemas_dir: Path) -> list[SchemaDefinition]:
    definitions: list[SchemaDefinition] = []
    for path in sorted(schemas_dir.glob("*.json")):
        try:
            definition = SchemaDefinition.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            raise ValueError(f"Schéma invalide dans {path.name} : {exc}") from exc
        if definition.code != path.stem:
            raise ValueError(f"{path.name} : le code '{definition.code}' doit correspondre au nom du fichier")
        definitions.append(definition)
    return definitions


def sync_schemas(session: Session, schemas_dir: Path) -> int:
    """Insère ou met à jour en base les schémas présents sur le disque."""
    definitions = load_schema_files(schemas_dir)
    for definition in definitions:
        payload = definition.model_dump(mode="json")
        row = session.scalar(select(DocumentSchema).where(DocumentSchema.code == definition.code))
        if row is None:
            session.add(
                DocumentSchema(
                    code=definition.code,
                    label=definition.label,
                    description=definition.description,
                    version=definition.version,
                    definition=payload,
                )
            )
        elif row.definition != payload:
            row.label = definition.label
            row.description = definition.description
            row.version = definition.version
            row.definition = payload
    session.commit()
    logger.info("%d schéma(s) documentaire(s) synchronisé(s)", len(definitions))
    return len(definitions)


def definition_of(row: DocumentSchema) -> SchemaDefinition:
    return SchemaDefinition.model_validate(row.definition)

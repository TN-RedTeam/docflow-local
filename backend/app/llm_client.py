"""Client d'extraction structurée vers une instance Ollama locale.

Utilise l'endpoint compatible OpenAI `/v1/chat/completions` avec
`response_format` de type `json_schema` strict : la sortie du modèle est
contrainte par grammaire à respecter le schéma du document.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .schema_registry import SchemaDefinition, build_json_schema

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """Tu es un moteur d'extraction de données pour des documents d'entreprise français.
Tu reçois le texte brut d'un document (issu d'un PDF ou d'un OCR) et tu remplis le schéma JSON imposé.

Règles impératives :
1. Recopie uniquement des informations présentes dans le document. N'invente rien, ne déduis rien.
2. Si une information est absente ou illisible, mets null.
3. Ne recalcule et ne corrige jamais un montant, un poids ou une quantité : recopie la valeur imprimée, même si elle paraît fausse. Un contrôle arithmétique est effectué après toi.
4. Nombres : point comme séparateur décimal, sans séparateur de milliers, sans devise ni unité (1 234,50 € devient 1234.5).
5. Dates : format AAAA-MM-JJ.
6. Identifiants (SIRET, TVA, IBAN, numéros de pièce) : recopie-les caractère par caractère.
7. Une ligne de tableau du document correspond à un élément de tableau JSON, dans le même ordre.
Réponds uniquement avec l'objet JSON."""


class ExtractionError(Exception):
    """L'extraction structurée par le LLM local a échoué."""


@dataclass
class LLMResult:
    data: dict[str, Any]
    model: str
    duration_ms: int
    attempts: int
    truncated_input: bool
    usage: dict[str, Any]


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL)
    if fenced:
        text = fenced.group(1)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        parsed = json.loads(text[start : end + 1])
    if not isinstance(parsed, dict):
        raise ValueError("la réponse n'est pas un objet JSON")
    return parsed


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_seconds: float = 300.0,
        max_retries: int = 2,
        max_input_chars: int = 24000,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.max_retries = max_retries
        self.max_input_chars = max_input_chars
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout_seconds, connect=10.0),
            transport=transport,
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def build_payload(self, definition: SchemaDefinition, document_text: str) -> dict[str, Any]:
        hint = f"\n{definition.document_hint}" if definition.document_hint else ""
        user_prompt = (
            f"Type de document : {definition.label}.{hint}\n\n"
            f"--- DÉBUT DU DOCUMENT ---\n{document_text}\n--- FIN DU DOCUMENT ---"
        )
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "seed": 0,
            "stream": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": definition.code,
                    "strict": True,
                    "schema": build_json_schema(definition),
                },
            },
        }

    def extract(self, definition: SchemaDefinition, document_text: str) -> LLMResult:
        truncated = len(document_text) > self.max_input_chars
        payload = self.build_payload(definition, document_text[: self.max_input_chars])
        started = time.monotonic()
        last_error = "aucune tentative"

        for attempt in range(1, self.max_retries + 2):
            try:
                response = self._client.post("/v1/chat/completions", json=payload)
            except httpx.ConnectError as exc:
                raise ExtractionError(
                    f"Ollama est injoignable à l'adresse {self.base_url}. "
                    "Vérifiez que le service est démarré (`ollama serve`)."
                ) from exc
            except httpx.TimeoutException:
                last_error = "délai d'attente dépassé"
                logger.warning("Ollama : tentative %d expirée", attempt)
                continue
            except httpx.HTTPError as exc:
                last_error = f"erreur réseau ({exc})"
                logger.warning("Ollama : tentative %d en échec : %s", attempt, exc)
                continue

            if response.status_code == 404:
                raise ExtractionError(
                    f"Le modèle « {self.model} » est introuvable sur Ollama. "
                    f"Installez-le avec : ollama pull {self.model}"
                )
            if response.status_code >= 500:
                last_error = f"erreur serveur HTTP {response.status_code}"
                continue
            if response.status_code >= 400:
                raise ExtractionError(f"Ollama a refusé la requête (HTTP {response.status_code}) : {response.text[:300]}")

            try:
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                data = _parse_json_object(content)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                last_error = f"réponse non exploitable ({exc})"
                logger.warning("Ollama : tentative %d, JSON invalide : %s", attempt, exc)
                continue

            return LLMResult(
                data=data,
                model=str(body.get("model") or self.model),
                duration_ms=int((time.monotonic() - started) * 1000),
                attempts=attempt,
                truncated_input=truncated,
                usage=body.get("usage") or {},
            )

        raise ExtractionError(f"Extraction LLM en échec après {self.max_retries + 1} tentative(s) : {last_error}")

    def health(self) -> dict[str, Any]:
        """État d'Ollama et disponibilité du modèle configuré."""
        try:
            response = self._client.get("/api/tags", timeout=5.0)
            response.raise_for_status()
            names = [str(item.get("name", "")) for item in response.json().get("models", [])]
        except (httpx.HTTPError, ValueError) as exc:
            return {"reachable": False, "model": self.model, "model_available": False, "detail": str(exc)}
        wanted = self.model if ":" in self.model else f"{self.model}:latest"
        available = any(name == wanted or name == self.model for name in names)
        return {"reachable": True, "model": self.model, "model_available": available, "installed_models": names}

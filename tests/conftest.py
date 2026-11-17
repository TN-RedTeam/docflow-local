"""Fixtures partagées : application de test, base isolée, Ollama simulé."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
for path in (ROOT / "backend", ROOT / "tests"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.config import Settings  # noqa: E402
from app.llm_client import OllamaClient  # noqa: E402
from app.main import create_app  # noqa: E402
from fake_ollama import canned_completion  # noqa: E402
from sample_documents import build_cmr_pdf, build_invoice_pdf  # noqa: E402


class OllamaRecorder:
    """Transport httpx simulant Ollama : enregistre chaque requête reçue."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connexion refusée", request=request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:7b"}]})
        assert request.url.path == "/v1/chat/completions"
        payload = json.loads(request.content)
        self.requests.append(payload)
        status, body = canned_completion(payload)
        return httpx.Response(status, json=body)

    def last_prompt(self, schema_code: str) -> str:
        for payload in reversed(self.requests):
            if payload["response_format"]["json_schema"]["name"] == schema_code:
                return payload["messages"][-1]["content"]
        raise AssertionError(f"aucune requête LLM pour le schéma {schema_code}")


@pytest.fixture(scope="session")
def cmr_pdf() -> bytes:
    return build_cmr_pdf()


@pytest.fixture(scope="session")
def invoice_pdf() -> bytes:
    return build_invoice_pdf()


@pytest.fixture
def ollama() -> OllamaRecorder:
    return OllamaRecorder()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Base SQLite jetable par défaut ; PostgreSQL si TEST_DATABASE_URL est défini."""
    database_url = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'docflow_test.db'}"
    return Settings(
        _env_file=None,
        database_url=database_url,
        ollama_base_url="http://localhost:11434",
        ollama_model="qwen2.5:7b",
        export_dir=tmp_path / "exports",
    )


@pytest.fixture
def client(settings: Settings, ollama: OllamaRecorder):
    llm = OllamaClient(
        settings.ollama_base_url, settings.ollama_model, transport=httpx.MockTransport(ollama), max_retries=1
    )
    app = create_app(settings, llm_client=llm)
    with TestClient(app) as test_client:
        if settings.database_url.startswith("postgresql"):
            with app.state.engine.begin() as connection:
                connection.execute(text("TRUNCATE auth_sessions, users, extraction_audit_logs, processed_documents"))
        resp = test_client.post("/api/connexion", json={"username": "admin", "password": "docflow"})
        assert resp.status_code == 200, f"Auth setup failed: {resp.text}"
        yield test_client
    llm.close()

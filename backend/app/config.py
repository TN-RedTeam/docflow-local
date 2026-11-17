"""Configuration de l'application, pilotée par variables d'environnement."""

from __future__ import annotations

import ipaddress
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent

_LOCAL_HOSTNAMES = {"localhost", "host.docker.internal"}
_LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".localdomain")


def is_local_endpoint(url: str) -> bool:
    """Indique si l'URL pointe vers la machine ou le réseau privé de l'entreprise.

    Sont acceptés : boucle locale, plages IP privées, noms d'hôte sans point
    (services Docker, machines du LAN) et suffixes de domaines internes.
    """
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in _LOCAL_HOSTNAMES or "." not in host and ":" not in host:
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return host.endswith(_LOCAL_SUFFIXES)
    return address.is_loopback or address.is_private or address.is_link_local


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://docflow:docflow@localhost:5432/docflow"

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b"
    ollama_timeout_seconds: float = 300.0
    ollama_max_retries: int = 2
    allow_remote_llm: bool = False
    llm_max_input_chars: int = 24000

    ocr_languages: str = "fra+eng"
    ocr_dpi: int = 300
    ocr_min_chars_per_page: int = 25
    max_pages: int = 40

    max_upload_mb: int = 25
    max_batch_files: int = 50
    worker_concurrency: int = 1

    schemas_dir: Path = BACKEND_DIR / "schemas"
    init_sql_path: Path = BACKEND_DIR / "db" / "init.sql"
    export_dir: Path | None = None

    amount_tolerance: Decimal = Decimal("0.02")
    cors_origins: str = "http://localhost:5173,http://localhost:8080"

    admin_user: str = "admin"
    admin_pass: str = "docflow"
    session_lifetime_hours: int = 24

    @field_validator("export_dir", mode="before")
    @classmethod
    def _empty_export_dir_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    def assert_local_llm(self) -> None:
        """Refuse de démarrer si le LLM configuré n'est pas sur le réseau local."""
        if self.allow_remote_llm or is_local_endpoint(self.ollama_base_url):
            return
        raise RuntimeError(
            f"OLLAMA_BASE_URL ({self.ollama_base_url}) ne pointe pas vers une adresse locale ou privée. "
            "DocFlow Local n'envoie jamais de document hors du réseau de l'entreprise. "
            "Si cette adresse est bien interne, définissez ALLOW_REMOTE_LLM=true."
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()

"""Moteur SQLAlchemy, fabrique de sessions et initialisation du schéma."""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Base déclarative commune à tous les modèles."""


def build_engine(database_url: str) -> Engine:
    if database_url.startswith("sqlite"):
        engine = create_engine(database_url, connect_args={"check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine
    return create_engine(database_url, pool_pre_ping=True, pool_size=5, max_overflow=10)


def build_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


def init_database(engine: Engine, init_sql_path: Path) -> None:
    """Crée les tables.

    Sur PostgreSQL, `init.sql` (idempotent) fait foi : il porte les contraintes
    et les triggers de verrouillage. Sur les autres moteurs (SQLite pour les
    tests), les tables sont créées à partir des modèles ORM.
    """
    from . import models  # noqa: F401  (enregistre les modèles sur Base.metadata)

    if engine.dialect.name == "postgresql" and init_sql_path.is_file():
        sql = init_sql_path.read_text(encoding="utf-8")
        # Exécution directe par le pilote, sans paramètres : les « % » du script
        # (RAISE EXCEPTION '... %') ne doivent pas être interprétés comme des marqueurs.
        raw_connection = engine.raw_connection()
        try:
            raw_connection.driver_connection.execute(sql)
            raw_connection.commit()
        finally:
            raw_connection.close()
        logger.info("Schéma PostgreSQL appliqué depuis %s", init_sql_path)
        return
    Base.metadata.create_all(engine)
    logger.info("Schéma créé depuis les modèles ORM (dialecte %s)", engine.dialect.name)

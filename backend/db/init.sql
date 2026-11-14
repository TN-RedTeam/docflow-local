-- DocFlow Local — schéma PostgreSQL 16.
-- Script idempotent : exécuté par l'image postgres au premier démarrage
-- (docker-entrypoint-initdb.d) et rejoué sans risque par le backend à chaque lancement.
-- Les schémas documentaires (transport_cmr, facture_fournisseur, ...) sont synchronisés
-- dans `document_schemas` par le backend à partir des fichiers backend/schemas/*.json.

CREATE TABLE IF NOT EXISTS document_schemas (
    id          SERIAL PRIMARY KEY,
    code        VARCHAR(64)  NOT NULL UNIQUE,
    label       VARCHAR(255) NOT NULL,
    description TEXT,
    version     INTEGER      NOT NULL DEFAULT 1,
    definition  JSONB        NOT NULL,
    is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS processed_documents (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    schema_id           INTEGER      NOT NULL REFERENCES document_schemas (id),

    original_filename   VARCHAR(512) NOT NULL,
    content_type        VARCHAR(128) NOT NULL DEFAULT 'application/pdf',
    file_size           INTEGER      NOT NULL CHECK (file_size >= 0),
    file_sha256         VARCHAR(64)  NOT NULL,
    source_file         BYTEA        NOT NULL,

    page_count          INTEGER,
    extraction_method   VARCHAR(16)  CHECK (extraction_method IN ('native', 'ocr', 'mixed', 'empty')),
    ocr_confidence      NUMERIC(4, 3) CHECK (ocr_confidence BETWEEN 0 AND 1),
    raw_text            TEXT,
    llm_model           VARCHAR(128),
    llm_raw_data        JSONB,

    extracted_data      JSONB        NOT NULL DEFAULT '{}'::jsonb,
    confidence_score    NUMERIC(4, 3) CHECK (confidence_score BETWEEN 0 AND 1),
    validation_warnings JSONB        NOT NULL DEFAULT '[]'::jsonb,
    warning_count       INTEGER      NOT NULL DEFAULT 0,

    status              VARCHAR(32)  NOT NULL DEFAULT 'processing'
                        CHECK (status IN ('processing', 'pending_review', 'validated')),
    validated_at        TIMESTAMPTZ,
    validated_by        VARCHAR(128),
    exported_at         TIMESTAMPTZ,

    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT validated_requires_timestamp
        CHECK (status <> 'validated' OR validated_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_processed_documents_schema_id  ON processed_documents (schema_id);
CREATE INDEX IF NOT EXISTS ix_processed_documents_status     ON processed_documents (status);
CREATE INDEX IF NOT EXISTS ix_processed_documents_created_at ON processed_documents (created_at DESC);
CREATE INDEX IF NOT EXISTS ix_processed_documents_sha256     ON processed_documents (file_sha256);
CREATE INDEX IF NOT EXISTS ix_processed_documents_data_gin   ON processed_documents USING GIN (extracted_data);

CREATE TABLE IF NOT EXISTS extraction_audit_logs (
    id          BIGSERIAL PRIMARY KEY,
    document_id UUID REFERENCES processed_documents (id) ON DELETE CASCADE,
    event_type  VARCHAR(64)  NOT NULL,
    actor       VARCHAR(128) NOT NULL DEFAULT 'system',
    details     JSONB        NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_extraction_audit_logs_document_id ON extraction_audit_logs (document_id);
CREATE INDEX IF NOT EXISTS ix_extraction_audit_logs_created_at  ON extraction_audit_logs (created_at DESC);

-- Horodatage automatique des modifications.
CREATE OR REPLACE FUNCTION docflow_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_document_schemas_touch ON document_schemas;
CREATE TRIGGER trg_document_schemas_touch
    BEFORE UPDATE ON document_schemas
    FOR EACH ROW EXECUTE FUNCTION docflow_touch_updated_at();

DROP TRIGGER IF EXISTS trg_processed_documents_touch ON processed_documents;
CREATE TRIGGER trg_processed_documents_touch
    BEFORE UPDATE ON processed_documents
    FOR EACH ROW EXECUTE FUNCTION docflow_touch_updated_at();

-- Verrou en base : un document validé est immuable. Seul le marquage d'export
-- (exported_at) reste modifiable ; toute autre modification ou suppression est refusée,
-- même si elle ne passe pas par l'API.
CREATE OR REPLACE FUNCTION docflow_lock_validated_document() RETURNS trigger AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status = 'validated' THEN
            RAISE EXCEPTION 'Document % validé : suppression interdite', OLD.id
                USING ERRCODE = 'check_violation';
        END IF;
        RETURN OLD;
    END IF;

    IF OLD.status = 'validated' AND (
           NEW.status              IS DISTINCT FROM OLD.status
        OR NEW.schema_id           IS DISTINCT FROM OLD.schema_id
        OR NEW.extracted_data      IS DISTINCT FROM OLD.extracted_data
        OR NEW.validation_warnings IS DISTINCT FROM OLD.validation_warnings
        OR NEW.confidence_score    IS DISTINCT FROM OLD.confidence_score
        OR NEW.source_file         IS DISTINCT FROM OLD.source_file
        OR NEW.file_sha256         IS DISTINCT FROM OLD.file_sha256
        OR NEW.validated_at        IS DISTINCT FROM OLD.validated_at
        OR NEW.validated_by        IS DISTINCT FROM OLD.validated_by
    ) THEN
        RAISE EXCEPTION 'Document % validé : enregistrement verrouillé', OLD.id
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_processed_documents_lock ON processed_documents;
CREATE TRIGGER trg_processed_documents_lock
    BEFORE UPDATE OR DELETE ON processed_documents
    FOR EACH ROW EXECUTE FUNCTION docflow_lock_validated_document();

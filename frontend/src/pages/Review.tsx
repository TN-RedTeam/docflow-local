import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  ApiError,
  deleteDocument,
  documentFileUrl,
  downloadExport,
  fetchDocument,
  fetchDocuments,
  reprocessDocument,
  saveCorrection,
  validateDocument,
} from "../api";
import DynamicForm from "../components/DynamicForm";
import { Banner, ConfidenceBar, Spinner, StatusBadge } from "../components/ui";
import { EXTRACTION_LABELS, blockingWarnings, formatDateTime, indexWarnings } from "../lib/format";
import { navigateToDashboard, navigateToDocument } from "../lib/hooks";
import { fieldDomId, setAt } from "../lib/paths";
import type { DocumentData, DocumentDetail, ExportFormat, SchemaOut, ValidationWarning } from "../types";

const AUTOSAVE_DELAY_MS = 900;
const POLL_INTERVAL_MS = 2000;

interface ReviewProps {
  documentId: string;
  schemas: SchemaOut[];
  operator: string;
  exportFormat: ExportFormat;
  onExportFormatChange: (format: ExportFormat) => void;
}

const SEVERITY_DOT: Record<string, string> = {
  error: "bg-red-500",
  warning: "bg-amber-500",
  info: "bg-sky-500",
};

function scrollToField(path: string | null) {
  if (!path) return;
  const element = document.getElementById(fieldDomId(path));
  if (!element) return;
  element.scrollIntoView({ behavior: "smooth", block: "center" });
  if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement) {
    element.focus({ preventScroll: true });
  }
}

function WarningList({ warnings }: { warnings: ValidationWarning[] }) {
  return (
    <ul className="space-y-1">
      {warnings.map((warning, index) => (
        <li key={`${warning.code}-${warning.field}-${index}`} className="flex items-start gap-2 text-sm">
          <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${SEVERITY_DOT[warning.severity]}`} aria-hidden="true" />
          {warning.field ? (
            <button type="button" className="text-left text-slate-800 underline-offset-2 hover:underline" onClick={() => scrollToField(warning.field)}>
              {warning.message}
            </button>
          ) : (
            <span className="text-slate-800">{warning.message}</span>
          )}
        </li>
      ))}
    </ul>
  );
}

export default function Review({ documentId, schemas, operator, exportFormat, onExportFormatChange }: ReviewProps) {
  const [document, setDocument] = useState<DocumentDetail | null>(null);
  const [draft, setDraft] = useState<DocumentData>({});
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirmWarnings, setConfirmWarnings] = useState<ValidationWarning[] | null>(null);
  const draftVersion = useRef(0);

  const accept = useCallback((detail: DocumentDetail, replaceDraft: boolean) => {
    setDocument(detail);
    if (replaceDraft) {
      setDraft(detail.extracted_data);
      setDirty(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    setDocument(null);
    setError(null);
    setNotice(null);
    setConfirmWarnings(null);
    fetchDocument(documentId)
      .then((detail) => !cancelled && accept(detail, true))
      .catch((cause) => !cancelled && setError(cause instanceof ApiError ? cause.message : "Chargement impossible"));
    return () => {
      cancelled = true;
    };
  }, [documentId, accept]);

  // Document encore en extraction : on attend la fin du traitement.
  const status = document?.status;
  useEffect(() => {
    if (status !== "processing") return;
    const timer = setInterval(() => {
      fetchDocument(documentId)
        .then((detail) => detail.status !== "processing" && accept(detail, true))
        .catch(() => undefined);
    }, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [status, documentId, accept]);

  // Recontrôle automatique : chaque correction est renvoyée au moteur de vérification,
  // les bordures d'alerte reflètent donc toujours l'état réel des données saisies.
  useEffect(() => {
    if (!dirty || status !== "pending_review") return;
    const version = draftVersion.current;
    const timer = setTimeout(async () => {
      setSaving(true);
      try {
        const detail = await saveCorrection(documentId, draft, operator);
        const unchangedSince = version === draftVersion.current;
        accept(detail, unchangedSince);
        setError(null);
      } catch (cause) {
        setError(cause instanceof ApiError ? cause.message : "Enregistrement impossible");
      } finally {
        setSaving(false);
      }
    }, AUTOSAVE_DELAY_MS);
    return () => clearTimeout(timer);
  }, [dirty, draft, status, documentId, operator, accept]);

  const schema = useMemo(
    () => schemas.find((candidate) => candidate.code === document?.schema_code) ?? null,
    [schemas, document?.schema_code],
  );
  const warningIndex = useMemo(() => indexWarnings(document?.validation_warnings ?? []), [document?.validation_warnings]);
  const blocking = useMemo(() => blockingWarnings(document?.validation_warnings ?? []), [document?.validation_warnings]);
  const infoCount = (document?.validation_warnings.length ?? 0) - blocking.length;
  const readOnly = status !== "pending_review" || busy;

  const handleChange = (path: string, value: unknown) => {
    draftVersion.current += 1;
    setDraft((previous) => setAt(previous, path, value));
    setDirty(true);
  };

  const runValidation = async (force: boolean) => {
    setBusy(true);
    setError(null);
    setConfirmWarnings(null);
    try {
      const detail = await validateDocument(documentId, draft, operator, force);
      accept(detail, true);
      try {
        await downloadExport({ format: exportFormat, ids: [detail.id], schema: detail.schema_code });
        setNotice(`Document validé et verrouillé. Export ${exportFormat.toUpperCase()} téléchargé.`);
      } catch (cause) {
        const reason = cause instanceof ApiError ? cause.message : "erreur inconnue";
        setNotice(null);
        setError(`Document validé, mais le téléchargement de l'export a échoué : ${reason}`);
      }
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409 && cause.warnings.length > 0) {
        setConfirmWarnings(cause.warnings);
        fetchDocument(documentId).then((detail) => accept(detail, true)).catch(() => undefined);
      } else {
        setError(cause instanceof ApiError ? cause.message : "Validation impossible");
      }
    } finally {
      setBusy(false);
    }
  };

  const handleReprocess = async () => {
    if (!window.confirm("Relancer l'extraction automatique ? Les corrections saisies seront remplacées.")) return;
    setBusy(true);
    setError(null);
    try {
      accept(await reprocessDocument(documentId), true);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Relance impossible");
    } finally {
      setBusy(false);
    }
  };

  const handleDelete = async () => {
    if (!window.confirm("Supprimer définitivement ce document et ses données extraites ?")) return;
    setBusy(true);
    try {
      await deleteDocument(documentId);
      navigateToDashboard();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Suppression impossible");
      setBusy(false);
    }
  };

  const goToNext = async () => {
    try {
      const next = await fetchDocuments({ schema: "", status: "pending_review", q: "", hasWarnings: false }, 5, 0);
      const candidate = next.items.find((item) => item.id !== documentId);
      if (candidate) navigateToDocument(candidate.id);
      else navigateToDashboard();
    } catch {
      navigateToDashboard();
    }
  };

  if (!document) {
    return (
      <main className="mx-auto w-full max-w-3xl space-y-3 px-4 py-8">
        {error ? <Banner tone="error">{error}</Banner> : <p className="flex items-center gap-2 text-sm text-slate-600"><Spinner /> Chargement du document…</p>}
        <button type="button" className="btn-secondary" onClick={navigateToDashboard}>← Retour au tableau de bord</button>
      </main>
    );
  }

  return (
    <main className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 border-b border-slate-200 bg-white px-4 py-2.5">
        <button type="button" className="btn-secondary px-2.5 py-1" onClick={navigateToDashboard}>← Documents</button>
        <div className="min-w-0 flex-1">
          <h1 className="truncate text-sm font-semibold" title={document.original_filename}>{document.original_filename}</h1>
          <p className="text-xs text-slate-500">
            {document.schema_label}
            {document.extraction_method ? ` · ${EXTRACTION_LABELS[document.extraction_method]}` : ""}
            {document.ocr_confidence !== null ? ` (qualité OCR ${Math.round(document.ocr_confidence * 100)} %)` : ""}
            {document.llm_model ? ` · ${document.llm_model}` : ""}
            {` · importé le ${formatDateTime(document.created_at)}`}
          </p>
        </div>
        <ConfidenceBar score={document.confidence_score} />
        <StatusBadge status={document.status} />
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 lg:grid-cols-2">
        <section className="flex min-h-[60vh] flex-col border-b border-slate-200 bg-slate-200 lg:min-h-0 lg:border-b-0 lg:border-r" aria-label="Document source">
          <iframe
            key={document.id}
            title={`Document source : ${document.original_filename}`}
            src={`${documentFileUrl(document.id)}#view=FitH`}
            className="min-h-0 w-full flex-1 border-0 bg-white"
          />
          <p className="bg-slate-100 px-3 py-1.5 text-xs text-slate-600">
            Le PDF ne s'affiche pas ?{" "}
            <a className="font-medium text-teal-800 underline" href={documentFileUrl(document.id)} target="_blank" rel="noreferrer">
              L'ouvrir dans un nouvel onglet
            </a>
          </p>
        </section>

        <section className="flex min-h-0 flex-col bg-white" aria-label="Données extraites">
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-4 py-4">
            {error && <Banner tone="error" onClose={() => setError(null)}>{error}</Banner>}
            {notice && <Banner tone="success" onClose={() => setNotice(null)}>{notice}</Banner>}

            {status === "processing" && (
              <Banner tone="info">
                <span className="flex items-center gap-2"><Spinner /> Extraction en cours par le modèle local…</span>
              </Banner>
            )}

            {status === "validated" && (
              <Banner tone="success">
                Validé le {formatDateTime(document.validated_at)} par {document.validated_by}. L'enregistrement est verrouillé
                {document.exported_at ? ` · exporté le ${formatDateTime(document.exported_at)}` : ""}.
              </Banner>
            )}

            {status !== "processing" && blocking.length > 0 && (
              <div className="rounded-md border border-amber-300 bg-amber-50 px-3 py-2.5">
                <h2 className="mb-1.5 text-sm font-semibold text-amber-900">
                  {blocking.length} alerte(s) {status === "validated" ? "acceptée(s) à la validation" : "à traiter"}
                </h2>
                <WarningList warnings={blocking} />
              </div>
            )}
            {status === "pending_review" && blocking.length === 0 && (
              <Banner tone="success">Tous les contrôles de cohérence sont passés. Relisez puis validez.</Banner>
            )}
            {status !== "processing" && infoCount > 0 && (
              <p className="text-xs text-sky-800">
                {infoCount} valeur(s) à contrôler à l'œil (bordure bleue pointillée) : introuvables telles quelles dans le texte du document.
              </p>
            )}

            {schema ? (
              status !== "processing" && (
                <DynamicForm
                  fields={schema.definition.fields}
                  data={draft}
                  warnings={warningIndex}
                  readOnly={readOnly}
                  onChange={handleChange}
                />
              )
            ) : (
              <Banner tone="error">Schéma « {document.schema_code} » introuvable.</Banner>
            )}
          </div>

          <div className="flex flex-wrap items-center gap-2 border-t border-slate-200 bg-slate-50 px-4 py-3">
            {status === "pending_review" && (
              <>
                <button type="button" className="btn-danger" disabled={busy} onClick={handleDelete}>Supprimer</button>
                <button type="button" className="btn-secondary" disabled={busy} onClick={handleReprocess}>Relancer l'extraction</button>
                <span className="ml-auto flex items-center gap-1.5 text-xs text-slate-500" aria-live="polite">
                  {saving ? (<><Spinner className="h-3 w-3" /> Recontrôle…</>) : dirty ? "Modifications en attente" : "Contrôles à jour"}
                </span>
                <label className="flex items-center gap-1.5 text-xs text-slate-600">
                  Export
                  <select
                    className="input w-20 py-1"
                    value={exportFormat}
                    onChange={(event) => onExportFormatChange(event.target.value as ExportFormat)}
                  >
                    <option value="csv">CSV</option>
                    <option value="json">JSON</option>
                  </select>
                </label>
                <button type="button" className="btn-primary" disabled={busy || saving} onClick={() => runValidation(false)}>
                  {busy ? <Spinner /> : null} Valider et exporter
                </button>
              </>
            )}
            {status === "validated" && (
              <>
                <button
                  type="button"
                  className="btn-secondary"
                  onClick={() =>
                    downloadExport({ format: exportFormat, ids: [document.id], schema: document.schema_code }).catch((cause) =>
                      setError(cause instanceof ApiError ? cause.message : "Export impossible"),
                    )
                  }
                >
                  Retélécharger l'export {exportFormat.toUpperCase()}
                </button>
                <button type="button" className="btn-primary ml-auto" onClick={goToNext}>Document suivant à réviser →</button>
              </>
            )}
          </div>
        </section>
      </div>

      {confirmWarnings && (
        <div className="fixed inset-0 z-20 flex items-center justify-center bg-slate-900/50 p-4" role="dialog" aria-modal="true" aria-labelledby="titre-confirmation">
          <div className="card max-h-[85vh] w-full max-w-lg overflow-y-auto p-5">
            <h2 id="titre-confirmation" className="text-base font-semibold">
              {confirmWarnings.length} alerte(s) non résolue(s)
            </h2>
            <p className="mt-1 text-sm text-slate-600">
              Corrigez les champs signalés, ou confirmez la validation en l'état : votre décision sera tracée dans le journal d'audit.
            </p>
            <div className="mt-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2.5">
              <WarningList warnings={confirmWarnings} />
            </div>
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" className="btn-primary" onClick={() => setConfirmWarnings(null)}>Revenir corriger</button>
              <button type="button" className="btn-secondary" onClick={() => runValidation(true)}>Valider malgré les alertes</button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

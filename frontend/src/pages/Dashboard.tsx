import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, downloadExport, fetchDocuments, fetchStats, uploadDocuments } from "../api";
import Dropzone from "../components/Dropzone";
import { Banner, ConfidenceBar, StatusBadge } from "../components/ui";
import { EXTRACTION_LABELS, formatBytes, formatDateTime } from "../lib/format";
import { navigateToDocument } from "../lib/hooks";
import type { DocumentFilters, DocumentList, ExportFormat, SchemaOut, Stats, UploadResponse } from "../types";

const PAGE_SIZE = 25;
const POLL_INTERVAL_MS = 2500;

interface DashboardProps {
  schemas: SchemaOut[];
}

function StatTile({ label, value, tone }: { label: string; value: number | null; tone?: "amber" | "teal" }) {
  const color = tone === "amber" && value ? "text-amber-700" : tone === "teal" ? "text-teal-700" : "text-slate-900";
  return (
    <div className="card px-4 py-3">
      <p className="text-xs font-medium uppercase tracking-wide text-slate-500">{label}</p>
      <p className={`mt-1 text-2xl font-semibold tabular-nums ${color}`}>{value ?? "—"}</p>
    </div>
  );
}

export default function Dashboard({ schemas }: DashboardProps) {
  const [schemaCode, setSchemaCode] = useState<string>("");
  const [filters, setFilters] = useState<DocumentFilters>({ schema: "", status: "", q: "", hasWarnings: false });
  const [page, setPage] = useState(0);
  const [list, setList] = useState<DocumentList | null>(null);
  const [stats, setStats] = useState<Stats | null>(null);
  const [uploadLabel, setUploadLabel] = useState<string | null>(null);
  const [uploadResult, setUploadResult] = useState<UploadResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const requestId = useRef(0);

  useEffect(() => {
    if (!schemaCode && schemas.length > 0) setSchemaCode(schemas[0].code);
  }, [schemas, schemaCode]);

  const refresh = useCallback(async () => {
    const current = ++requestId.current;
    try {
      const [documents, counters] = await Promise.all([
        fetchDocuments(filters, PAGE_SIZE, page * PAGE_SIZE),
        fetchStats(),
      ]);
      if (current !== requestId.current) return;
      setList(documents);
      setStats(counters);
      setError(null);
    } catch (cause) {
      if (current !== requestId.current) return;
      setError(cause instanceof ApiError ? cause.message : "Chargement impossible");
    }
  }, [filters, page]);

  useEffect(() => {
    const timer = setTimeout(refresh, filters.q ? 250 : 0);
    return () => clearTimeout(timer);
  }, [refresh, filters.q]);

  // Tant que des documents sont en cours d'extraction, la liste se rafraîchit seule.
  const processing = stats?.processing ?? 0;
  useEffect(() => {
    if (processing === 0) return;
    const timer = setInterval(refresh, POLL_INTERVAL_MS);
    return () => clearInterval(timer);
  }, [processing, refresh]);

  const updateFilters = (patch: Partial<DocumentFilters>) => {
    setPage(0);
    setFilters((previous) => ({ ...previous, ...patch }));
  };

  const handleFiles = async (files: File[]) => {
    if (!schemaCode) return;
    setUploadResult(null);
    setError(null);
    setUploadLabel(`Envoi de ${files.length} fichier(s)…`);
    try {
      const result = await uploadDocuments(schemaCode, files, (sent, total) =>
        setUploadLabel(`Envoi ${sent} / ${total}…`),
      );
      setUploadResult(result);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Envoi impossible");
    } finally {
      setUploadLabel(null);
      refresh();
    }
  };

  const handleExport = async (format: ExportFormat) => {
    setError(null);
    setNotice(null);
    try {
      const count = await downloadExport({ format, schema: filters.schema || undefined });
      setNotice(
        count > 0
          ? `${count} document(s) validé(s) exporté(s) au format ${format.toUpperCase()}.`
          : "Aucun document validé à exporter pour ce filtre.",
      );
      refresh();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : "Export impossible");
    }
  };

  const total = list?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));

  return (
    <main className="mx-auto w-full max-w-7xl space-y-5 px-4 py-5">
      <section className="grid grid-cols-2 gap-3 lg:grid-cols-4" aria-label="Indicateurs">
        <StatTile label="À réviser" value={stats?.pending_review ?? null} />
        <StatTile label="Dont avec alertes" value={stats?.with_warnings ?? null} tone="amber" />
        <StatTile label="Validés" value={stats?.validated ?? null} tone="teal" />
        <StatTile label="En traitement" value={stats?.processing ?? null} />
      </section>

      {error && <Banner tone="error" onClose={() => setError(null)}>{error}</Banner>}
      {notice && <Banner tone="success" onClose={() => setNotice(null)}>{notice}</Banner>}

      <section className="card p-4" aria-labelledby="titre-import">
        <h2 id="titre-import" className="text-base font-semibold">Importer des documents</h2>
        <div className="mt-3 grid gap-4 lg:grid-cols-[minmax(0,22rem)_1fr]">
          <fieldset>
            <legend className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">1. Type de document</legend>
            <div className="space-y-2">
              {schemas.map((schema) => {
                const selected = schema.code === schemaCode;
                return (
                  <label
                    key={schema.code}
                    className={`flex cursor-pointer gap-3 rounded-md border px-3 py-2.5 transition-colors ${
                      selected ? "border-teal-700 bg-teal-50 ring-1 ring-teal-700" : "border-slate-200 hover:border-slate-400"
                    }`}
                  >
                    <input
                      type="radio"
                      name="schema"
                      className="mt-1 accent-teal-700"
                      checked={selected}
                      onChange={() => setSchemaCode(schema.code)}
                    />
                    <span>
                      <span className="block text-sm font-medium">{schema.label}</span>
                      <span className="block text-xs text-slate-600">{schema.description}</span>
                    </span>
                  </label>
                );
              })}
              {schemas.length === 0 && <p className="text-sm text-slate-500">Aucun schéma disponible.</p>}
            </div>
          </fieldset>
          <div>
            <p className="mb-2 text-xs font-medium uppercase tracking-wide text-slate-500">2. Fichiers PDF</p>
            <Dropzone disabled={!schemaCode || uploadLabel !== null} busyLabel={uploadLabel} onFiles={handleFiles} />
            {uploadResult && (
              <div className="mt-2 space-y-1 text-sm">
                {uploadResult.documents.length > 0 && (
                  <p className="text-teal-800">
                    {uploadResult.documents.length} document(s) reçu(s) : l'extraction s'exécute en tâche de fond.
                  </p>
                )}
                {uploadResult.rejected.map((item, index) => (
                  <p key={`${item.filename}-${index}`} className="text-red-700">
                    {item.filename} — {item.reason}
                  </p>
                ))}
              </div>
            )}
          </div>
        </div>
      </section>

      <section className="card" aria-labelledby="titre-documents">
        <div className="flex flex-wrap items-end gap-3 border-b border-slate-200 p-4">
          <h2 id="titre-documents" className="mr-auto text-base font-semibold">
            Documents <span className="text-sm font-normal text-slate-500">({total})</span>
          </h2>
          <label className="text-xs font-medium text-slate-600">
            Recherche
            <input
              className="input mt-1 w-44"
              placeholder="Nom de fichier"
              value={filters.q}
              onChange={(event) => updateFilters({ q: event.target.value })}
            />
          </label>
          <label className="text-xs font-medium text-slate-600">
            Type
            <select className="input mt-1 w-44" value={filters.schema} onChange={(event) => updateFilters({ schema: event.target.value })}>
              <option value="">Tous les types</option>
              {schemas.map((schema) => (
                <option key={schema.code} value={schema.code}>{schema.label}</option>
              ))}
            </select>
          </label>
          <label className="text-xs font-medium text-slate-600">
            Statut
            <select
              className="input mt-1 w-36"
              value={filters.status}
              onChange={(event) => updateFilters({ status: event.target.value as DocumentFilters["status"] })}
            >
              <option value="">Tous</option>
              <option value="pending_review">À réviser</option>
              <option value="validated">Validé</option>
              <option value="processing">En traitement</option>
            </select>
          </label>
          <label className="flex items-center gap-2 pb-2 text-sm text-slate-700">
            <input
              type="checkbox"
              className="h-4 w-4 accent-teal-700"
              checked={filters.hasWarnings}
              onChange={(event) => updateFilters({ hasWarnings: event.target.checked })}
            />
            Avec alertes
          </label>
          <div className="flex gap-2">
            <button
              type="button"
              className="btn-secondary"
              disabled={!filters.schema}
              title={filters.schema ? "Documents validés du type sélectionné" : "Choisissez un type de document pour l'export CSV"}
              onClick={() => handleExport("csv")}
            >
              Exporter CSV
            </button>
            <button type="button" className="btn-secondary" title="Documents validés" onClick={() => handleExport("json")}>
              Exporter JSON
            </button>
          </div>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full min-w-[760px] text-sm">
            <thead>
              <tr className="border-b border-slate-200 bg-slate-50 text-left text-xs font-medium uppercase tracking-wide text-slate-500">
                <th className="px-4 py-2">Fichier</th>
                <th className="px-3 py-2">Type</th>
                <th className="px-3 py-2">Statut</th>
                <th className="px-3 py-2">Confiance</th>
                <th className="px-3 py-2">Alertes</th>
                <th className="px-3 py-2">Importé le</th>
                <th className="px-4 py-2 text-right">Action</th>
              </tr>
            </thead>
            <tbody>
              {list?.items.map((document) => (
                <tr key={document.id} className="border-b border-slate-100 last:border-0 hover:bg-slate-50">
                  <td className="max-w-[18rem] px-4 py-2.5">
                    <p className="truncate font-medium" title={document.original_filename}>{document.original_filename}</p>
                    <p className="text-xs text-slate-500">
                      {formatBytes(document.file_size)}
                      {document.page_count ? ` · ${document.page_count} p.` : ""}
                      {document.extraction_method ? ` · ${EXTRACTION_LABELS[document.extraction_method]}` : ""}
                    </p>
                  </td>
                  <td className="px-3 py-2.5 text-slate-700">{document.schema_label}</td>
                  <td className="px-3 py-2.5"><StatusBadge status={document.status} /></td>
                  <td className="px-3 py-2.5">
                    {document.status === "processing" ? <span className="text-slate-400">—</span> : <ConfidenceBar score={document.confidence_score} />}
                  </td>
                  <td className="px-3 py-2.5">
                    {document.warning_count > 0 ? (
                      <span className="inline-flex items-center gap-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold text-amber-900">
                        ⚠ {document.warning_count}
                      </span>
                    ) : document.status === "processing" ? (
                      <span className="text-slate-400">—</span>
                    ) : (
                      <span className="text-xs text-teal-700">Aucune</span>
                    )}
                  </td>
                  <td className="whitespace-nowrap px-3 py-2.5 text-slate-600">{formatDateTime(document.created_at)}</td>
                  <td className="px-4 py-2.5 text-right">
                    <button
                      type="button"
                      className={document.status === "pending_review" ? "btn-primary px-2.5 py-1" : "btn-secondary px-2.5 py-1"}
                      disabled={document.status === "processing"}
                      onClick={() => navigateToDocument(document.id)}
                    >
                      {document.status === "validated" ? "Consulter" : "Réviser"}
                    </button>
                  </td>
                </tr>
              ))}
              {list && list.items.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-4 py-10 text-center text-sm text-slate-500">
                    Aucun document. Importez un premier lot de PDF ci-dessus.
                  </td>
                </tr>
              )}
              {!list && !error && (
                <tr>
                  <td colSpan={7} className="px-4 py-10 text-center text-sm text-slate-500">Chargement…</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>

        {pageCount > 1 && (
          <div className="flex items-center justify-end gap-3 border-t border-slate-200 px-4 py-2.5 text-sm">
            <span className="text-slate-600">Page {page + 1} / {pageCount}</span>
            <button type="button" className="btn-secondary px-2.5 py-1" disabled={page === 0} onClick={() => setPage(page - 1)}>
              Précédent
            </button>
            <button type="button" className="btn-secondary px-2.5 py-1" disabled={page + 1 >= pageCount} onClick={() => setPage(page + 1)}>
              Suivant
            </button>
          </div>
        )}
      </section>
    </main>
  );
}

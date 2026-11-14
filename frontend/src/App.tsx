import { useEffect, useState } from "react";

import { ApiError, fetchHealth, fetchSchemas } from "./api";
import { Banner } from "./components/ui";
import { navigateToDashboard, useHashRoute, useStoredState } from "./lib/hooks";
import Dashboard from "./pages/Dashboard";
import Review from "./pages/Review";
import type { ExportFormat, Health, SchemaOut } from "./types";

const HEALTH_INTERVAL_MS = 30_000;

function HealthIndicator({ health, unreachable }: { health: Health | null; unreachable: boolean }) {
  let color = "bg-slate-300";
  let label = "Vérification…";
  if (unreachable) {
    color = "bg-red-500";
    label = "Backend injoignable";
  } else if (health) {
    if (!health.database.reachable) {
      color = "bg-red-500";
      label = "Base de données injoignable";
    } else if (!health.ollama.reachable) {
      color = "bg-red-500";
      label = "Ollama arrêté — saisie manuelle uniquement";
    } else if (!health.ollama.model_available) {
      color = "bg-amber-500";
      label = `Modèle ${health.ollama.model} non installé`;
    } else {
      color = "bg-teal-500";
      label = `Local · ${health.ollama.model}`;
    }
  }
  return (
    <span className="flex items-center gap-2 text-xs text-slate-300" title="État de l'inférence locale">
      <span className={`h-2 w-2 rounded-full ${color}`} aria-hidden="true" />
      {label}
    </span>
  );
}

export default function App() {
  const route = useHashRoute();
  const [schemas, setSchemas] = useState<SchemaOut[]>([]);
  const [schemaError, setSchemaError] = useState<string | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [unreachable, setUnreachable] = useState(false);
  const [operator, setOperator] = useStoredState("docflow.operateur", "");
  const [exportFormat, setExportFormat] = useStoredState("docflow.format_export", "csv");

  useEffect(() => {
    fetchSchemas()
      .then((list) => {
        setSchemas(list);
        setSchemaError(null);
      })
      .catch((cause) => setSchemaError(cause instanceof ApiError ? cause.message : "Chargement des schémas impossible"));
  }, []);

  useEffect(() => {
    const check = () =>
      fetchHealth()
        .then((result) => {
          setHealth(result);
          setUnreachable(false);
        })
        .catch(() => setUnreachable(true));
    check();
    const timer = setInterval(check, HEALTH_INTERVAL_MS);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="flex h-full flex-col">
      <header className="flex flex-wrap items-center gap-x-5 gap-y-2 bg-slate-900 px-4 py-2.5 text-white">
        <button type="button" className="flex items-center gap-2 rounded text-left" onClick={navigateToDashboard}>
          <span className="flex h-7 w-7 items-center justify-center rounded-md bg-teal-600 text-sm font-bold">D</span>
          <span>
            <span className="block text-sm font-semibold leading-tight">DocFlow Local</span>
            <span className="block text-[11px] leading-tight text-slate-400">Vos documents ne quittent pas ce réseau</span>
          </span>
        </button>
        <div className="ml-auto flex flex-wrap items-center gap-x-5 gap-y-2">
          <HealthIndicator health={health} unreachable={unreachable} />
          <label className="flex items-center gap-2 text-xs text-slate-300">
            Opérateur
            <input
              className="w-36 rounded border border-slate-600 bg-slate-800 px-2 py-1 text-sm text-white placeholder:text-slate-500"
              placeholder="Votre nom"
              value={operator}
              maxLength={128}
              onChange={(event) => setOperator(event.target.value)}
            />
          </label>
        </div>
      </header>

      {schemaError && (
        <div className="px-4 pt-4">
          <Banner tone="error">{schemaError}</Banner>
        </div>
      )}

      {route.page === "review" ? (
        <Review
          documentId={route.documentId}
          schemas={schemas}
          operator={operator.trim() || "operateur"}
          exportFormat={exportFormat === "json" ? "json" : "csv"}
          onExportFormatChange={(format: ExportFormat) => setExportFormat(format)}
        />
      ) : (
        <div className="min-h-0 flex-1 overflow-y-auto">
          <Dashboard schemas={schemas} />
        </div>
      )}
    </div>
  );
}

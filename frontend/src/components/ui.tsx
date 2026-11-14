import type { ReactNode } from "react";

import { STATUS_LABELS, formatPercent } from "../lib/format";
import type { DocumentStatus } from "../types";

const STATUS_STYLES: Record<DocumentStatus, string> = {
  processing: "bg-slate-100 text-slate-700 ring-slate-300",
  pending_review: "bg-amber-50 text-amber-800 ring-amber-300",
  validated: "bg-teal-50 text-teal-800 ring-teal-300",
};

export function StatusBadge({ status }: { status: DocumentStatus }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_STYLES[status]}`}
    >
      {status === "processing" && <Spinner className="h-3 w-3" />}
      {STATUS_LABELS[status]}
    </span>
  );
}

export function Spinner({ className = "h-4 w-4" }: { className?: string }) {
  return (
    <svg className={`animate-spin ${className}`} viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" opacity="0.25" />
      <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
    </svg>
  );
}

export function ConfidenceBar({ score }: { score: number | null }) {
  if (score === null) {
    return <span className="text-slate-400">—</span>;
  }
  const color = score >= 0.9 ? "bg-teal-600" : score >= 0.7 ? "bg-amber-500" : "bg-red-500";
  return (
    <div className="flex items-center gap-2" title="Score de confiance : complétude, ancrage dans le texte, cohérence">
      <div className="h-1.5 w-16 overflow-hidden rounded-full bg-slate-200">
        <div className={`h-full ${color}`} style={{ width: `${Math.round(score * 100)}%` }} />
      </div>
      <span className="tabular-nums text-slate-700">{formatPercent(score)}</span>
    </div>
  );
}

export function Banner({
  tone,
  children,
  onClose,
}: {
  tone: "error" | "success" | "info";
  children: ReactNode;
  onClose?: () => void;
}) {
  const styles = {
    error: "border-red-200 bg-red-50 text-red-800",
    success: "border-teal-200 bg-teal-50 text-teal-900",
    info: "border-slate-200 bg-white text-slate-700",
  }[tone];
  return (
    <div role={tone === "error" ? "alert" : "status"} className={`flex items-start gap-3 rounded-md border px-3 py-2 text-sm ${styles}`}>
      <div className="min-w-0 flex-1">{children}</div>
      {onClose && (
        <button type="button" onClick={onClose} className="shrink-0 rounded px-1 text-current opacity-60 hover:opacity-100" aria-label="Fermer">
          ✕
        </button>
      )}
    </div>
  );
}

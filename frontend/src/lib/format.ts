import type { DocumentStatus, Severity, ValidationWarning } from "../types";

const dateTimeFormat = new Intl.DateTimeFormat("fr-FR", { dateStyle: "short", timeStyle: "short" });
const numberFormat = new Intl.NumberFormat("fr-FR", { maximumFractionDigits: 6 });

export function formatDateTime(iso: string | null): string {
  return iso ? dateTimeFormat.format(new Date(iso)) : "—";
}

export function formatBytes(size: number): string {
  if (size < 1024) return `${size} o`;
  if (size < 1024 * 1024) return `${Math.round(size / 1024)} Ko`;
  return `${(size / 1024 / 1024).toFixed(1).replace(".", ",")} Mo`;
}

export function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return numberFormat.format(value);
  return String(value);
}

export function formatPercent(score: number | null): string {
  return score === null ? "—" : `${Math.round(score * 100)} %`;
}

export const STATUS_LABELS: Record<DocumentStatus, string> = {
  processing: "En traitement",
  pending_review: "À réviser",
  validated: "Validé",
};

export const EXTRACTION_LABELS: Record<string, string> = {
  native: "Texte natif",
  ocr: "OCR",
  mixed: "Natif + OCR",
  empty: "Illisible",
};

const SEVERITY_RANK: Record<Severity, number> = { error: 0, warning: 1, info: 2 };

/** Regroupe les alertes par chemin de champ, la plus grave en premier. */
export function indexWarnings(warnings: ValidationWarning[]): Map<string, ValidationWarning[]> {
  const index = new Map<string, ValidationWarning[]>();
  for (const warning of warnings) {
    if (!warning.field) continue;
    const list = index.get(warning.field) ?? [];
    list.push(warning);
    list.sort((a, b) => SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]);
    index.set(warning.field, list);
  }
  return index;
}

export function blockingWarnings(warnings: ValidationWarning[]): ValidationWarning[] {
  return warnings.filter((warning) => warning.severity !== "info");
}

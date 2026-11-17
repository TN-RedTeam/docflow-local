import type {
  DocumentData,
  DocumentDetail,
  DocumentFilters,
  DocumentList,
  ExportFormat,
  Health,
  SchemaOut,
  Stats,
  UploadResponse,
  ValidationWarning,
} from "./types";

const API_BASE = "/api";
const UPLOAD_CHUNK_SIZE = 5;

export class ApiError extends Error {
  status: number;
  warnings: ValidationWarning[];

  constructor(status: number, message: string, warnings: ValidationWarning[] = []) {
    super(message);
    this.status = status;
    this.warnings = warnings;
  }
}

async function toApiError(response: Response): Promise<ApiError> {
  let message = `Erreur ${response.status}`;
  let warnings: ValidationWarning[] = [];
  try {
    const body = await response.json();
    const detail = body?.detail;
    if (typeof detail === "string") {
      message = detail;
    } else if (Array.isArray(detail)) {
      message = detail.map((item: { msg?: string }) => item.msg ?? "").join(" ; ");
    } else if (detail && typeof detail === "object") {
      message = detail.message ?? message;
      warnings = detail.validation_warnings ?? [];
    }
  } catch {
    message = response.statusText || message;
  }
  return new ApiError(response.status, message, warnings);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { credentials: "include", ...init });
  } catch {
    throw new ApiError(0, "Le serveur DocFlow est injoignable. Vérifiez que le backend est démarré.");
  }
  if (response.status === 401) {
    window.dispatchEvent(new CustomEvent("docflow:unauthorized"));
    throw new ApiError(401, "Session expirée");
  }
  if (!response.ok) {
    throw await toApiError(response);
  }
  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export interface AuthUser {
  id: number;
  username: string;
  is_admin: boolean;
}

export async function login(username: string, password: string): Promise<AuthUser> {
  const res = await request<{ user: AuthUser }>("/connexion", jsonInit("POST", { username, password }));
  return res.user;
}

export async function logout(): Promise<void> {
  await request<unknown>("/deconnexion", { method: "POST" });
}

export async function fetchMe(): Promise<AuthUser> {
  const res = await request<{ user: AuthUser }>("/moi");
  return res.user;
}

function jsonInit(method: string, body: unknown): RequestInit {
  return { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
}

export function fetchHealth(): Promise<Health> {
  return request<Health>("/health");
}

export function fetchSchemas(): Promise<SchemaOut[]> {
  return request<SchemaOut[]>("/schemas");
}

export function fetchStats(): Promise<Stats> {
  return request<Stats>("/documents/stats");
}

export function fetchDocuments(filters: DocumentFilters, limit: number, offset: number): Promise<DocumentList> {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (filters.schema) params.set("schema", filters.schema);
  if (filters.status) params.set("status", filters.status);
  if (filters.q.trim()) params.set("q", filters.q.trim());
  if (filters.hasWarnings) params.set("has_warnings", "true");
  return request<DocumentList>(`/documents?${params.toString()}`);
}

export function fetchDocument(id: string): Promise<DocumentDetail> {
  return request<DocumentDetail>(`/documents/${id}`);
}

export function documentFileUrl(id: string): string {
  return `${API_BASE}/documents/${id}/file`;
}

/** Envoie les fichiers par paquets pour garder des requêtes de taille raisonnable. */
export async function uploadDocuments(
  schemaCode: string,
  files: File[],
  onProgress: (sent: number, total: number) => void,
): Promise<UploadResponse> {
  const result: UploadResponse = { documents: [], rejected: [] };
  for (let start = 0; start < files.length; start += UPLOAD_CHUNK_SIZE) {
    const chunk = files.slice(start, start + UPLOAD_CHUNK_SIZE);
    const form = new FormData();
    form.append("schema_code", schemaCode);
    chunk.forEach((file) => form.append("files", file, file.name));
    try {
      const part = await request<UploadResponse>("/documents/upload", { method: "POST", body: form });
      result.documents.push(...part.documents);
      result.rejected.push(...part.rejected);
    } catch (error) {
      const reason = error instanceof ApiError ? error.message : "Envoi impossible";
      if (error instanceof ApiError && error.status === 0) throw error;
      chunk.forEach((file) => result.rejected.push({ filename: file.name, reason }));
    }
    onProgress(Math.min(start + chunk.length, files.length), files.length);
  }
  return result;
}

export function saveCorrection(id: string, data: DocumentData, actor: string): Promise<DocumentDetail> {
  return request<DocumentDetail>(`/documents/${id}`, jsonInit("PATCH", { data, actor }));
}

export function validateDocument(
  id: string,
  data: DocumentData,
  actor: string,
  force: boolean,
): Promise<DocumentDetail> {
  return request<DocumentDetail>(`/documents/${id}/validate`, jsonInit("PATCH", { data, actor, force }));
}

export function reprocessDocument(id: string): Promise<DocumentDetail> {
  return request<DocumentDetail>(`/documents/${id}/reprocess`, { method: "POST" });
}

export function deleteDocument(id: string): Promise<void> {
  return request<void>(`/documents/${id}`, { method: "DELETE" });
}

export interface ExportOptions {
  format: ExportFormat;
  schema?: string;
  ids?: string[];
}

/** Télécharge l'export normalisé et retourne le nombre de documents exportés. */
export async function downloadExport(options: ExportOptions): Promise<number> {
  const params = new URLSearchParams({ format: options.format });
  if (options.schema) params.set("schema", options.schema);
  if (options.ids?.length) params.set("ids", options.ids.join(","));
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/documents/export?${params.toString()}`, { credentials: "include" });
  } catch {
    throw new ApiError(0, "Le serveur DocFlow est injoignable.");
  }
  if (!response.ok) {
    throw await toApiError(response);
  }
  const disposition = response.headers.get("Content-Disposition") ?? "";
  const filename = /filename="([^"]+)"/.exec(disposition)?.[1] ?? `docflow_export.${options.format}`;
  const url = URL.createObjectURL(await response.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
  return Number(response.headers.get("X-Document-Count") ?? "0");
}

export type FieldType = "string" | "text" | "number" | "integer" | "date" | "boolean" | "object" | "array";

export interface FieldDef {
  name: string;
  label: string;
  type: FieldType;
  required: boolean;
  description: string | null;
  format: string | null;
  unit: string | null;
  allowed_values: (number | string)[] | null;
  min_items: number;
  fields: FieldDef[];
}

export interface RuleDef {
  type: string;
  code: string;
  label: string;
}

export interface SchemaDefinition {
  code: string;
  label: string;
  description: string | null;
  version: number;
  fields: FieldDef[];
  validation_rules: RuleDef[];
}

export interface SchemaOut {
  code: string;
  label: string;
  description: string | null;
  version: number;
  definition: SchemaDefinition;
}

export type Severity = "error" | "warning" | "info";

export interface ValidationWarning {
  code: string;
  severity: Severity;
  field: string | null;
  message: string;
  expected: unknown;
  actual: unknown;
}

export type DocumentStatus = "processing" | "pending_review" | "validated";

export interface DocumentSummary {
  id: string;
  schema_code: string;
  schema_label: string;
  original_filename: string;
  file_size: number;
  page_count: number | null;
  extraction_method: "native" | "ocr" | "mixed" | "empty" | null;
  status: DocumentStatus;
  confidence_score: number | null;
  warning_count: number;
  created_at: string;
  updated_at: string;
  validated_at: string | null;
  validated_by: string | null;
  exported_at: string | null;
}

export type DocumentData = Record<string, unknown>;

export interface DocumentDetail extends DocumentSummary {
  ocr_confidence: number | null;
  llm_model: string | null;
  extracted_data: DocumentData;
  validation_warnings: ValidationWarning[];
}

export interface DocumentList {
  items: DocumentSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface UploadResponse {
  documents: DocumentSummary[];
  rejected: { filename: string; reason: string }[];
}

export interface Stats {
  processing: number;
  pending_review: number;
  validated: number;
  with_warnings: number;
}

export interface Health {
  status: "ok" | "degraded";
  version: string;
  database: { reachable: boolean };
  ollama: { reachable: boolean; model: string; model_available: boolean };
}

export interface DocumentFilters {
  schema: string;
  status: DocumentStatus | "";
  q: string;
  hasWarnings: boolean;
}

export type ExportFormat = "csv" | "json";

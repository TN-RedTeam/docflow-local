import { emptyRow, fieldDomId, getAt } from "../lib/paths";
import { formatValue } from "../lib/format";
import type { DocumentData, FieldDef, ValidationWarning } from "../types";

interface DynamicFormProps {
  fields: FieldDef[];
  data: DocumentData;
  warnings: Map<string, ValidationWarning[]>;
  readOnly: boolean;
  onChange: (path: string, value: unknown) => void;
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

const SEVERITY_INPUT: Record<string, string> = {
  error: "input-error",
  warning: "input-warning",
  info: "input-info",
};

const SEVERITY_TEXT: Record<string, string> = {
  error: "text-red-700",
  warning: "text-amber-800",
  info: "text-sky-800",
};

function isBlank(value: unknown): boolean {
  return value === null || value === undefined || value === "";
}

function WarningMessages({
  path,
  warnings,
  readOnly,
  onChange,
}: {
  path: string;
  warnings: ValidationWarning[];
  readOnly: boolean;
  onChange: (path: string, value: unknown) => void;
}) {
  if (warnings.length === 0) return null;
  return (
    <ul className="mt-1 space-y-1">
      {warnings.map((warning, index) => (
        <li key={`${warning.code}-${index}`} className={`text-xs leading-snug ${SEVERITY_TEXT[warning.severity]}`}>
          {warning.message}
          {!readOnly && warning.severity === "warning" && typeof warning.expected === "number" && (
            <button
              type="button"
              className="ml-1.5 rounded border border-amber-400 bg-white px-1.5 py-0.5 font-medium text-amber-900 hover:bg-amber-100"
              onClick={() => onChange(path, warning.expected)}
            >
              Appliquer {formatValue(warning.expected)}
            </button>
          )}
        </li>
      ))}
    </ul>
  );
}

function ScalarInput({
  field,
  path,
  value,
  severity,
  readOnly,
  compact,
  onChange,
}: {
  field: FieldDef;
  path: string;
  value: unknown;
  severity: string | undefined;
  readOnly: boolean;
  compact: boolean;
  onChange: (path: string, value: unknown) => void;
}) {
  const className = `input ${severity ? SEVERITY_INPUT[severity] : ""}`;
  const common = {
    id: fieldDomId(path),
    disabled: readOnly,
    "aria-invalid": severity === "error" || severity === "warning" ? true : undefined,
    "aria-label": compact ? field.label : undefined,
  };
  const emit = (text: string) => onChange(path, text === "" ? null : text);

  if (field.type === "boolean") {
    return (
      <select
        {...common}
        className={className}
        value={value === true ? "true" : value === false ? "false" : ""}
        onChange={(event) => onChange(path, event.target.value === "" ? null : event.target.value === "true")}
      >
        <option value="">—</option>
        <option value="true">Oui</option>
        <option value="false">Non</option>
      </select>
    );
  }

  if (field.type === "text") {
    return (
      <textarea
        {...common}
        className={className}
        rows={2}
        value={isBlank(value) ? "" : String(value)}
        onChange={(event) => emit(event.target.value)}
      />
    );
  }

  if (field.type === "date" && (isBlank(value) || ISO_DATE.test(String(value)))) {
    return (
      <input
        {...common}
        type="date"
        className={className}
        value={isBlank(value) ? "" : String(value)}
        onChange={(event) => emit(event.target.value)}
      />
    );
  }

  const numeric = field.type === "number" || field.type === "integer";
  return (
    <div className="relative">
      <input
        {...common}
        type="text"
        inputMode={numeric ? "decimal" : undefined}
        className={`${className} ${numeric ? "text-right tabular-nums" : ""} ${field.unit && !compact ? "pr-9" : ""}`}
        value={isBlank(value) ? "" : String(value)}
        onChange={(event) => emit(event.target.value)}
      />
      {field.unit && !compact && (
        <span className="pointer-events-none absolute inset-y-0 right-2.5 flex items-center text-xs text-slate-500">
          {field.unit}
        </span>
      )}
    </div>
  );
}

function ScalarField(props: {
  field: FieldDef;
  path: string;
  data: DocumentData;
  warnings: Map<string, ValidationWarning[]>;
  readOnly: boolean;
  onChange: (path: string, value: unknown) => void;
}) {
  const { field, path, data, warnings, readOnly, onChange } = props;
  const own = warnings.get(path) ?? [];
  return (
    <div className={field.type === "text" ? "sm:col-span-2" : ""}>
      <label htmlFor={fieldDomId(path)} className="mb-1 block text-xs font-medium text-slate-600">
        {field.label}
        {field.required && <span className="ml-0.5 text-red-600" title="Champ obligatoire">*</span>}
      </label>
      <ScalarInput
        field={field}
        path={path}
        value={getAt(data, path)}
        severity={own[0]?.severity}
        readOnly={readOnly}
        compact={false}
        onChange={onChange}
      />
      <WarningMessages path={path} warnings={own} readOnly={readOnly} onChange={onChange} />
    </div>
  );
}

function ArrayField(props: {
  field: FieldDef;
  path: string;
  data: DocumentData;
  warnings: Map<string, ValidationWarning[]>;
  readOnly: boolean;
  onChange: (path: string, value: unknown) => void;
}) {
  const { field, path, data, warnings, readOnly, onChange } = props;
  const rawRows = getAt(data, path);
  const rows = Array.isArray(rawRows) ? (rawRows as Record<string, unknown>[]) : [];
  const own = warnings.get(path) ?? [];

  const cellWarnings: { path: string; label: string; list: ValidationWarning[] }[] = [];
  rows.forEach((_, rowIndex) =>
    field.fields.forEach((column) => {
      const cellPath = `${path}[${rowIndex}].${column.name}`;
      const list = warnings.get(cellPath);
      if (list) cellWarnings.push({ path: cellPath, label: `Ligne ${rowIndex + 1}`, list });
    }),
  );

  return (
    <section id={fieldDomId(path)} className="sm:col-span-2">
      <div className="mb-1.5 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-800">
          {field.label}
          {field.required && <span className="ml-0.5 text-red-600">*</span>}
          <span className="ml-2 text-xs font-normal text-slate-500">{rows.length} ligne(s)</span>
        </h3>
        {!readOnly && (
          <button type="button" className="btn-secondary px-2 py-1 text-xs" onClick={() => onChange(path, [...rows, emptyRow(field.fields)])}>
            + Ajouter une ligne
          </button>
        )}
      </div>
      <div className={`overflow-x-auto rounded-md border ${own.length ? "border-2 border-red-500" : "border-slate-200"}`}>
        <table className="w-full min-w-[640px] border-collapse text-sm">
          <thead>
            <tr className="bg-slate-50 text-left text-xs font-medium text-slate-600">
              <th className="w-8 px-2 py-1.5 text-center">#</th>
              {field.fields.map((column) => (
                <th key={column.name} className="px-1.5 py-1.5">
                  {column.label}
                  {column.unit && <span className="font-normal text-slate-400"> ({column.unit})</span>}
                  {column.required && <span className="ml-0.5 text-red-600">*</span>}
                </th>
              ))}
              {!readOnly && <th className="w-8" />}
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 && (
              <tr>
                <td colSpan={field.fields.length + 2} className="px-3 py-4 text-center text-xs text-slate-500">
                  Aucune ligne extraite.
                </td>
              </tr>
            )}
            {rows.map((_, rowIndex) => (
              <tr key={rowIndex} className="border-t border-slate-100 align-top">
                <td className="px-2 py-2 text-center text-xs tabular-nums text-slate-500">{rowIndex + 1}</td>
                {field.fields.map((column) => {
                  const cellPath = `${path}[${rowIndex}].${column.name}`;
                  return (
                    <td key={column.name} className={`px-1 py-1 ${column.type === "number" || column.type === "integer" ? "w-24" : "min-w-[9rem]"}`}>
                      <ScalarInput
                        field={column}
                        path={cellPath}
                        value={getAt(data, cellPath)}
                        severity={warnings.get(cellPath)?.[0]?.severity}
                        readOnly={readOnly}
                        compact
                        onChange={onChange}
                      />
                    </td>
                  );
                })}
                {!readOnly && (
                  <td className="px-1 py-1 text-center">
                    <button
                      type="button"
                      className="rounded px-1.5 py-1 text-slate-400 hover:bg-red-50 hover:text-red-700"
                      aria-label={`Supprimer la ligne ${rowIndex + 1}`}
                      onClick={() => onChange(path, rows.filter((__, index) => index !== rowIndex))}
                    >
                      ✕
                    </button>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <WarningMessages path={path} warnings={own} readOnly={readOnly} onChange={onChange} />
      {cellWarnings.map((entry) => (
        <div key={entry.path} className="mt-1 flex gap-1.5 text-xs">
          <span className="shrink-0 font-medium text-slate-600">{entry.label} :</span>
          <div className="[&>ul]:mt-0">
            <WarningMessages path={entry.path} warnings={entry.list} readOnly={readOnly} onChange={onChange} />
          </div>
        </div>
      ))}
    </section>
  );
}

function FieldGroup(props: DynamicFormProps & { prefix: string }) {
  const { fields, prefix, ...rest } = props;
  return (
    <div className="grid grid-cols-1 gap-x-4 gap-y-3 sm:grid-cols-2">
      {fields.map((field) => {
        const path = `${prefix}${field.name}`;
        if (field.type === "object") {
          return (
            <fieldset key={path} className="rounded-md border border-slate-200 px-3 pb-3 pt-1 sm:col-span-2">
              <legend className="px-1 text-sm font-semibold text-slate-800">
                {field.label}
                {field.required && <span className="ml-0.5 text-red-600">*</span>}
              </legend>
              <FieldGroup {...rest} fields={field.fields} prefix={`${path}.`} />
            </fieldset>
          );
        }
        if (field.type === "array") {
          return <ArrayField key={path} field={field} path={path} {...rest} />;
        }
        return <ScalarField key={path} field={field} path={path} {...rest} />;
      })}
    </div>
  );
}

/** Formulaire généré à partir du schéma : objets en groupes, tableaux en grilles éditables. */
export default function DynamicForm(props: DynamicFormProps) {
  return <FieldGroup {...props} prefix="" />;
}

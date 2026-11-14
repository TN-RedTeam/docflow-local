import { useRef, useState } from "react";
import type { DragEvent } from "react";

import { Spinner } from "./ui";

interface DropzoneProps {
  disabled: boolean;
  busyLabel: string | null;
  onFiles: (files: File[]) => void;
}

function isPdf(file: File): boolean {
  return file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
}

/** Zone de dépôt : glisser-déposer d'un lot de PDF ou sélection classique. */
export default function Dropzone({ disabled, busyLabel, onFiles }: DropzoneProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const [ignored, setIgnored] = useState(0);

  const submit = (list: FileList | null) => {
    if (!list || disabled) return;
    const all = Array.from(list);
    const pdfs = all.filter(isPdf);
    setIgnored(all.length - pdfs.length);
    if (pdfs.length > 0) onFiles(pdfs);
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    submit(event.dataTransfer.files);
  };

  return (
    <div>
      <div
        role="button"
        tabIndex={disabled ? -1 : 0}
        aria-disabled={disabled}
        onClick={() => !disabled && inputRef.current?.click()}
        onKeyDown={(event) => {
          if ((event.key === "Enter" || event.key === " ") && !disabled) {
            event.preventDefault();
            inputRef.current?.click();
          }
        }}
        onDragOver={(event) => {
          event.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={`flex min-h-[9.5rem] flex-col items-center justify-center rounded-lg border-2 border-dashed px-4 py-6 text-center transition-colors ${
          disabled
            ? "cursor-not-allowed border-slate-200 bg-slate-50 text-slate-400"
            : dragging
              ? "cursor-copy border-teal-600 bg-teal-50 text-teal-900"
              : "cursor-pointer border-slate-300 bg-slate-50 text-slate-600 hover:border-teal-600 hover:bg-white"
        }`}
      >
        {busyLabel ? (
          <div className="flex items-center gap-2 text-sm font-medium text-slate-700">
            <Spinner /> {busyLabel}
          </div>
        ) : (
          <>
            <svg className="mb-2 h-8 w-8" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" aria-hidden="true">
              <path d="M12 16V4m0 0-4 4m4-4 4 4M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <p className="text-sm font-medium">Glissez-déposez vos PDF ici</p>
            <p className="mt-1 text-xs text-slate-500">ou cliquez pour les sélectionner — un fichier ou tout un lot, natifs ou scannés</p>
          </>
        )}
        <input
          ref={inputRef}
          type="file"
          accept="application/pdf,.pdf"
          multiple
          className="hidden"
          onChange={(event) => {
            submit(event.target.files);
            event.target.value = "";
          }}
        />
      </div>
      {ignored > 0 && (
        <p className="mt-2 text-xs text-amber-700">{ignored} fichier(s) ignoré(s) : seuls les PDF sont acceptés.</p>
      )}
    </div>
  );
}

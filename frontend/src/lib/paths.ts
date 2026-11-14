import type { DocumentData, FieldDef } from "../types";

type Token = string | number;

/** Découpe `lignes[2].montant_ht` en ["lignes", 2, "montant_ht"]. */
export function parsePath(path: string): Token[] {
  const tokens: Token[] = [];
  for (const part of path.split(".")) {
    const match = /^([^[\]]+)((?:\[\d+\])*)$/.exec(part);
    if (!match) {
      tokens.push(part);
      continue;
    }
    tokens.push(match[1]);
    for (const index of match[2].matchAll(/\[(\d+)\]/g)) {
      tokens.push(Number(index[1]));
    }
  }
  return tokens;
}

export function getAt(data: unknown, path: string): unknown {
  let current: unknown = data;
  for (const token of parsePath(path)) {
    if (current === null || current === undefined) return undefined;
    current = (current as Record<Token, unknown>)[token];
  }
  return current;
}

function setTokens(current: unknown, tokens: Token[], value: unknown): unknown {
  if (tokens.length === 0) return value;
  const [head, ...rest] = tokens;
  if (typeof head === "number") {
    const copy = Array.isArray(current) ? [...current] : [];
    copy[head] = setTokens(copy[head], rest, value);
    return copy;
  }
  const source = current && typeof current === "object" && !Array.isArray(current) ? current : {};
  return { ...(source as object), [head]: setTokens((source as Record<string, unknown>)[head], rest, value) };
}

/** Retourne une copie de `data` où la valeur située à `path` est remplacée. */
export function setAt(data: DocumentData, path: string, value: unknown): DocumentData {
  return setTokens(data, parsePath(path), value) as DocumentData;
}

/** Ligne vide conforme aux sous-champs d'un tableau. */
export function emptyRow(fields: FieldDef[]): Record<string, unknown> {
  return Object.fromEntries(fields.map((field) => [field.name, null]));
}

/** Identifiant DOM stable d'un champ, utilisé pour faire défiler jusqu'à une alerte. */
export function fieldDomId(path: string): string {
  return `champ-${path.replace(/[^a-zA-Z0-9_]/g, "-")}`;
}

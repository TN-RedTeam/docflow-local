import { useCallback, useEffect, useState } from "react";

export type Route = { page: "dashboard" } | { page: "review"; documentId: string };

function parseHash(hash: string): Route {
  const match = /^#\/documents\/([0-9a-fA-F-]{36})$/.exec(hash);
  return match ? { page: "review", documentId: match[1] } : { page: "dashboard" };
}

/** Routage minimal par fragment d'URL : `#/` et `#/documents/<id>`. */
export function useHashRoute(): Route {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  return route;
}

export function navigateToDashboard(): void {
  window.location.hash = "#/";
}

export function navigateToDocument(id: string): void {
  window.location.hash = `#/documents/${id}`;
}

/** État persistant dans le navigateur (nom de l'opérateur, format d'export préféré). */
export function useStoredState(key: string, initial: string): [string, (value: string) => void] {
  const [value, setValue] = useState<string>(() => {
    try {
      return window.localStorage.getItem(key) ?? initial;
    } catch {
      return initial;
    }
  });
  const update = useCallback(
    (next: string) => {
      setValue(next);
      try {
        window.localStorage.setItem(key, next);
      } catch {
        // Stockage indisponible (navigation privée) : la valeur reste en mémoire.
      }
    },
    [key],
  );
  return [value, update];
}

#!/usr/bin/env python3
"""faux_modele.py — faux Ollama qui « lit » parfaitement les documents du jeu de test.

Pour tester DocFlow Local chez vous sans carte graphique ni modèle installé :

    python3 jeu-de-test/faux_modele.py            (port 11434, celui d'Ollama)

Il répond aux deux routes qu'utilise DocFlow (`GET /api/tags`, `POST /v1/chat/completions`). Pour chaque
document reçu, il cherche dans le texte le numéro d'une facture ou d'une CMR du jeu de test, et renvoie la
vérité correspondante (dossier verite/). Un document qui n'appartient pas au jeu est refusé : DocFlow
l'affiche alors en « extraction échouée », avec saisie manuelle possible.

Ce n'est PAS un modèle de langage : il montre ce que DocFlow fait d'une extraction parfaite (contrôles,
alertes, validation, export). Pour tester la lecture elle-même, utilisez un vrai modèle (Ollama).
Bibliothèque standard uniquement.
"""
from __future__ import annotations

import argparse
import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ICI = Path(__file__).resolve().parent
MODELE = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")


def _compact(texte: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", texte.upper())


def charger_verites(dossier: Path) -> dict[str, list[tuple[str, dict]]]:
    """schéma -> [(numéro compacté, données)], le plus long numéro d'abord."""
    verites: dict[str, list[tuple[str, dict]]] = {}
    for fichier in sorted(dossier.glob("*.json")):
        contenu = json.loads(fichier.read_text(encoding="utf-8"))
        donnees = contenu["donnees"]
        numero = donnees.get("numero_facture") or donnees.get("numero_cmr")
        verites.setdefault(contenu["schema"], []).append((_compact(numero), donnees))
    for liste in verites.values():
        liste.sort(key=lambda element: -len(element[0]))
    return verites


def reponse(verites: dict, requete: dict) -> tuple[int, dict]:
    schema = (((requete.get("response_format") or {}).get("json_schema")) or {}).get("name", "")
    texte = _compact("\n".join(m.get("content", "") for m in requete.get("messages", []) if m.get("role") == "user"))
    for numero, donnees in verites.get(schema, []):
        if numero and numero in texte:
            return 200, {
                "id": "chatcmpl-faux", "object": "chat.completion", "model": requete.get("model") or MODELE,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(donnees, ensure_ascii=False)},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
    return 400, {"error": {"message": "Document inconnu du faux modèle : il ne lit que les documents du jeu de test."}}


def creer_gestionnaire(verites: dict):
    class Gestionnaire(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:
            return

        def _json(self, statut: int, objet: dict) -> None:
            corps = json.dumps(objet, ensure_ascii=False).encode("utf-8")
            self.send_response(statut)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(corps)))
            self.end_headers()
            self.wfile.write(corps)

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/api/tags":
                self._json(200, {"models": [{"name": MODELE if ":" in MODELE else f"{MODELE}:latest"}]})
            elif self.path == "/api/version":
                self._json(200, {"version": "0.0.0-faux-modele"})
            else:
                self._json(404, {"error": "route inconnue"})

        def do_POST(self) -> None:  # noqa: N802
            if self.path != "/v1/chat/completions":
                self._json(404, {"error": "route inconnue"})
                return
            requete = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            self._json(*reponse(verites, requete))

    return Gestionnaire


def main() -> None:
    parseur = argparse.ArgumentParser(description="Faux Ollama pour le jeu de test de DocFlow Local.")
    parseur.add_argument("--port", type=int, default=11434)
    parseur.add_argument("--hote", default="0.0.0.0", help="0.0.0.0 pour être joignable depuis les conteneurs Docker")
    args = parseur.parse_args()
    verites = charger_verites(ICI / "verite")
    serveur = ThreadingHTTPServer((args.hote, args.port), creer_gestionnaire(verites))
    print(f"Faux modèle « {MODELE} » en écoute sur le port {args.port} "
          f"({sum(len(v) for v in verites.values())} documents connus). Ctrl+C pour arrêter.", flush=True)
    try:
        serveur.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()

"""Faux serveur Ollama pour les tests et les démonstrations sans GPU.

Il expose les deux routes utilisées par DocFlow Local (`/api/tags` et
`/v1/chat/completions`) et renvoie, selon le schéma demandé, la réponse
préparée pour les PDF d'exemple de `sample_documents.py`.

Démonstration :
    python tests/fake_ollama.py --port 11434
    python tests/fake_ollama.py --write-samples ./exemples   # génère les 2 PDF d'essai

Ce serveur ne lit pas réellement les documents : il ne sert qu'à faire tourner
l'application de bout en bout avec les fichiers d'exemple.
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sample_documents import LLM_ANSWERS, build_cmr_pdf, build_invoice_pdf  # noqa: E402

FAKE_MODEL = "docflow-fake:latest"


def canned_completion(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """Réponse `/v1/chat/completions` pour une requête donnée : (code HTTP, corps JSON)."""
    response_format = payload.get("response_format") or {}
    json_schema = response_format.get("json_schema") or {}
    if response_format.get("type") != "json_schema" or json_schema.get("strict") is not True:
        return 400, {"error": {"message": "response_format json_schema strict attendu"}}
    answer = LLM_ANSWERS.get(json_schema.get("name", ""))
    if answer is None:
        return 400, {"error": {"message": f"schéma inconnu du faux serveur : {json_schema.get('name')}"}}
    content = json.dumps(answer, ensure_ascii=False)
    return 200, {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": payload.get("model") or FAKE_MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


class _Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802 (nom imposé par http.server)
        if self.path == "/api/tags":
            names = sorted({FAKE_MODEL, *self.server.model_names})
            self._send(200, {"models": [{"name": name} for name in names]})
        else:
            self._send(404, {"error": "route inconnue"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/v1/chat/completions":
            self._send(404, {"error": "route inconnue"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "JSON invalide"}})
            return
        self._send(*canned_completion(payload))

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        sys.stderr.write("fake-ollama: " + format % args + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Faux serveur Ollama pour DocFlow Local")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11434)
    parser.add_argument("--model", action="append", default=[], help="Nom de modèle à déclarer comme installé")
    parser.add_argument("--write-samples", metavar="DOSSIER", help="Écrit les deux PDF d'exemple puis quitte")
    arguments = parser.parse_args()

    if arguments.write_samples:
        target = Path(arguments.write_samples)
        target.mkdir(parents=True, exist_ok=True)
        (target / "bon_livraison_cmr_valide.pdf").write_bytes(build_cmr_pdf())
        (target / "facture_fournisseur_erreur_tva.pdf").write_bytes(build_invoice_pdf())
        print(f"PDF d'exemple écrits dans {target.resolve()}")
        return

    server = ThreadingHTTPServer((arguments.host, arguments.port), _Handler)
    server.model_names = arguments.model or ["qwen2.5:7b", "llama3.2:latest"]
    print(f"Faux Ollama à l'écoute sur http://{arguments.host}:{arguments.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == "__main__":
    main()

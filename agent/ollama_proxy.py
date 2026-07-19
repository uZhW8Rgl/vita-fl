"""Bearer-authenticated reverse proxy for a cross-CVM Ollama deployment."""

from __future__ import annotations

import json
import os
import secrets
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class OllamaProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _write(self, status: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        expected = os.environ.get("OLLAMA_PROXY_TOKEN", "")
        supplied = self.headers.get("Authorization", "")
        return bool(expected) and secrets.compare_digest(supplied, f"Bearer {expected}")

    def _proxy(self) -> None:
        if self.path == "/healthz":
            self._health()
            return
        if not self.path.startswith("/api/"):
            self._write(404, b'{"error":"not found"}')
            return
        if not self._authorized():
            self._write(401, b'{"error":"unauthorized"}')
            return
        length = int(self.headers.get("Content-Length", "0"))
        if length > MAX_REQUEST_BYTES:
            self._write(413, b'{"error":"request too large"}')
            return
        body = self.rfile.read(length) if length else None
        upstream = os.environ.get("OLLAMA_UPSTREAM", "http://ollama:11434").rstrip("/")
        headers = {"Accept": self.headers.get("Accept", "application/json")}
        if body is not None:
            headers["Content-Type"] = self.headers.get("Content-Type", "application/json")
        request = urllib.request.Request(
            f"{upstream}{self.path}",
            data=body,
            headers=headers,
            method=self.command,
        )
        try:
            with urllib.request.urlopen(request, timeout=660) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    self._write(502, b'{"error":"upstream response too large"}')
                    return
                self._write(response.status, payload, response.headers.get("Content-Type", "application/json"))
        except urllib.error.HTTPError as exc:
            self._write(exc.code, exc.read(MAX_RESPONSE_BYTES), exc.headers.get("Content-Type", "application/json"))
        except urllib.error.URLError as exc:
            self._write(502, json.dumps({"error": f"Ollama upstream unavailable: {exc}"}).encode("utf-8"))

    def _health(self) -> None:
        upstream = os.environ.get("OLLAMA_UPSTREAM", "http://ollama:11434").rstrip("/")
        model = os.environ.get("OLLAMA_MODEL", "qwen3:1.7b")
        try:
            with urllib.request.urlopen(f"{upstream}/api/tags", timeout=3) as response:
                payload = json.loads(response.read(1_048_577).decode("utf-8"))
            names = {
                str(item.get("name") or item.get("model"))
                for item in payload.get("models", [])
                if isinstance(item, dict)
            }
            if model not in names:
                raise RuntimeError("configured model is not ready")
        except Exception as exc:
            self._write(503, json.dumps({"status": "starting", "detail": str(exc)}).encode("utf-8"))
            return
        self._write(200, json.dumps({"status": "ok", "model": model}).encode("utf-8"))

    do_GET = _proxy
    do_POST = _proxy
    do_DELETE = _proxy

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    token = os.environ.get("OLLAMA_PROXY_TOKEN", "")
    if len(token) < 24:
        raise RuntimeError("OLLAMA_PROXY_TOKEN must contain at least 24 characters")
    port = int(os.environ.get("OLLAMA_PROXY_PORT", "11434"))
    ThreadingHTTPServer(("0.0.0.0", port), OllamaProxyHandler).serve_forever()


if __name__ == "__main__":
    main()

"""Small readiness client for the separately deployed Ollama service."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def check_ollama_readiness(
    base_url: str | None = None,
    model: str | None = None,
    timeout: float = 3.0,
) -> dict[str, str]:
    service_url = (base_url or os.environ.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434")).rstrip("/")
    expected_model = model or os.environ.get("OLLAMA_MODEL", "qwen3:0.6b")
    api_token = os.environ.get("OLLAMA_API_TOKEN", "")
    headers = {"Authorization": f"Bearer {api_token}"} if api_token else {}
    request = urllib.request.Request(f"{service_url}/api/tags", headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(1_048_577).decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Ollama is not reachable at {service_url}: {exc}") from exc
    models = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(models, list):
        raise RuntimeError("Ollama /api/tags response does not contain a model list.")
    installed = {
        str(item.get("name") or item.get("model"))
        for item in models
        if isinstance(item, dict) and (item.get("name") or item.get("model"))
    }
    if expected_model not in installed:
        raise RuntimeError(f"Ollama model {expected_model!r} has not finished loading.")
    return {"status": "ok", "model": expected_model}

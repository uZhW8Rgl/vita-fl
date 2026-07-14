from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from agent.ollama_client import check_ollama_readiness


class _Response:
    def __init__(self, payload: dict[str, object]):
        self._body = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


class OllamaReadinessTests(unittest.TestCase):
    @patch("agent.ollama_client.urllib.request.urlopen")
    def test_expected_model_is_ready(self, urlopen) -> None:
        urlopen.return_value = _Response({"models": [{"name": "qwen3:0.6b"}]})

        with patch.dict("os.environ", {"OLLAMA_API_TOKEN": "test-token"}):
            result = check_ollama_readiness("https://ollama.example", "qwen3:0.6b")

        self.assertEqual(result, {"status": "ok", "model": "qwen3:0.6b"})
        self.assertEqual(urlopen.call_args.args[0].get_header("Authorization"), "Bearer test-token")

    @patch("agent.ollama_client.urllib.request.urlopen")
    def test_missing_model_is_not_ready(self, urlopen) -> None:
        urlopen.return_value = _Response({"models": []})

        with self.assertRaisesRegex(RuntimeError, "has not finished loading"):
            check_ollama_readiness("https://ollama.example", "qwen3:0.6b")

    @patch("agent.ollama_client.urllib.request.urlopen")
    def test_unreachable_ollama_is_not_ready(self, urlopen) -> None:
        urlopen.side_effect = urllib.error.URLError("offline")

        with self.assertRaisesRegex(RuntimeError, "not reachable"):
            check_ollama_readiness("https://ollama.example", "qwen3:0.6b")


if __name__ == "__main__":
    unittest.main()

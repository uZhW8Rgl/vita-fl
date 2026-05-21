#!/usr/bin/env python3
"""Small HTTP wrapper around the local zk_inference pipeline."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from zk_inference.service_tools import prepare_mnist_sample, prove_single_image


HOST = os.environ.get("ZK_INFERENCE_HOST", "0.0.0.0")
PORT = int(os.environ.get("ZK_INFERENCE_PORT", "8090"))


class Handler(BaseHTTPRequestHandler):
    server_version = "zk-inference-http/1.0"

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        self._send_json(HTTPStatus.OK, {"ok": True})

    def do_POST(self) -> None:
        if self.path not in {"/prove-single-image", "/prepare-mnist-sample"}:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if self.path == "/prepare-mnist-sample":
                result = prepare_mnist_sample(
                    index=payload.get("index"),
                    images=payload.get("images", "data/mnist/data/t10k-images.idx3-ubyte"),
                    labels=payload.get("labels", "data/mnist/data/t10k-labels.idx1-ubyte"),
                    out_dir=payload.get("out_dir", "zk_inference/single_query"),
                    input_json=payload.get("input_json", "zk_inference/out/input.json"),
                    metadata_out=payload.get("metadata_out", "zk_inference/single_query/selection.json"),
                    seed=payload.get("seed"),
                )
            else:
                result = prove_single_image(
                    model_path=payload["model_path"],
                    signature_path=payload["signature_path"],
                    index=payload.get("index"),
                    workdir=payload.get("workdir", "zk_inference/out"),
                    query_dir=payload.get("query_dir", "zk_inference/single_query"),
                    skip_calibration=bool(payload.get("skip_calibration", True)),
                )
            status = HTTPStatus.OK if result.get("ok", False) else HTTPStatus.BAD_GATEWAY
            self._send_json(status, result)
        except KeyError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"missing field: {exc.args[0]}"})
        except Exception as exc:  # pragma: no cover - runtime safety
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"zk_inference HTTP service listening on {HOST}:{PORT}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

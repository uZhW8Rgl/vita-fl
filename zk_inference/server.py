#!/usr/bin/env python3
"""Small HTTP wrapper around the local zk_inference pipeline."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from tee_inference.service.model_source import provision_latest_model
from zk_inference.job_runtime import ZkJobError, ZkJobRuntime
from zk_inference.service_tools import (
    create_single_image_query,
    export_model,
    run_ezkl,
)


HOST = os.environ.get("ZK_INFERENCE_HOST", "0.0.0.0")
PORT = int(os.environ.get("ZK_INFERENCE_PORT", "8090"))
RUNTIME = ZkJobRuntime(
    Path(os.environ.get("ZK_RUNTIME_DIR", "/work/zk-inference")),
    provision_latest_model,
    export_model,
    create_single_image_query,
    run_ezkl,
)


class Handler(BaseHTTPRequestHandler):
    server_version = "zk-inference-http/1.0"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, RUNTIME.health())
            return
        if self.path.startswith("/v1/jobs/"):
            try:
                self._send_json(HTTPStatus.OK, {"ok": True, **RUNTIME.job_metadata(self.path.rsplit("/", 1)[-1])})
            except ZkJobError as exc:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)})
            return
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return

    def do_POST(self) -> None:
        if self.path not in {
            "/export-model",
            "/create-query",
            "/run-ezkl",
            "/v1/models/fetch",
            "/v1/jobs",
        } and not (self.path.startswith("/v1/jobs/") and self.path.endswith("/run-and-verify")):
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            if self.path == "/v1/models/fetch":
                result = RUNTIME.fetch_model()
            elif self.path == "/v1/jobs":
                result = RUNTIME.create_job(payload.get("index"))
            elif self.path.startswith("/v1/jobs/") and self.path.endswith("/run-and-verify"):
                result = RUNTIME.run_and_verify(self.path.split("/")[3])
            elif self.path == "/export-model":
                result = export_model(
                    model_path=payload["model_path"],
                    out_dir=payload.get("out_dir", "zk_inference/out"),
                )
            elif self.path == "/create-query":
                result = create_single_image_query(
                    index=payload.get("index"),
                    images=payload.get("images"),
                    labels=payload.get("labels"),
                    out_dir=payload.get("out_dir", "zk_inference/single_query"),
                    input_json=payload.get("input_json", "zk_inference/out/input.json"),
                )
            elif self.path == "/run-ezkl":
                result = run_ezkl(
                    workdir=payload.get("workdir", "zk_inference/out"),
                    model=payload.get("model", "model_logits.onnx"),
                    data=payload.get("data", "input.json"),
                    skip_calibration=bool(payload.get("skip_calibration", True)),
                )
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
                return
            status = HTTPStatus.OK if result.get("ok", True) else HTTPStatus.BAD_GATEWAY
            self._send_json(status, {"ok": status == HTTPStatus.OK, **result})
        except KeyError as exc:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": f"missing field: {exc.args[0]}"})
        except ZkJobError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
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

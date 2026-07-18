#!/usr/bin/env python3
"""Small HTTP wrapper around the local zk_inference pipeline."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agent_receipts.environment import RECEIPT_HEADER, bearer_token, receipt_header, receiver_from_environment
from agent_receipts.receiver_log import (
    SCITT_BUNDLE_URL_HEADER,
    SCITT_TRANSACTION_HEADER,
    ReceiverTransparencyPublisher,
)
from agent_receipts.sello_v1 import ReceiptVerificationError, verify_authorization_token
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
RECEIPT_RECEIVER = receiver_from_environment("zk-inference")
RECEIPT_PUBLISHER = ReceiverTransparencyPublisher("zk-inference") if RECEIPT_RECEIVER is not None else None

PUBLIC_ACTIONS = {
    "/v1/models/fetch": "fetch_latest_verified_zk_model_bundle",
    "/v1/jobs": "generate_random_zk_chestmnist_image",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "zk-inference-http/1.0"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, RUNTIME.health())
            return
        if self.path.startswith("/v1/sello/receipts/"):
            try:
                bundle = RECEIPT_PUBLISHER.read(self.path.rsplit("/", 1)[-1]) if RECEIPT_PUBLISHER else None
                if bundle is None:
                    raise FileNotFoundError
                self._send_bytes(HTTPStatus.OK, bundle, "application/cbor")
            except (FileNotFoundError, OSError):
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "receipt publication not found"})
            return
        if self.path.startswith("/v1/jobs/") and self.path.endswith("/transparency-bundle"):
            try:
                bundle = RUNTIME.transparency_bundle(self.path.split("/")[3])
                self._send_bytes(HTTPStatus.OK, bundle, "application/cbor")
            except ZkJobError as exc:
                self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
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
        self._receipt_context = None
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
            raw_input = self.rfile.read(length)
            payload = json.loads(raw_input.decode("utf-8") or "{}")
            action = PUBLIC_ACTIONS.get(self.path)
            if self.path.startswith("/v1/jobs/") and self.path.endswith("/run-and-verify"):
                action = "generate_and_verify_zk_inference_proof"
                raw_input = json.dumps(
                    {"job_id": self.path.split("/")[3]}, sort_keys=True, separators=(",", ":")
                ).encode()
            if action is not None and RECEIPT_RECEIVER is not None:
                token = bearer_token(self.headers)
                verify_authorization_token(token, RECEIPT_RECEIVER.token_issuer_key)
                self._receipt_context = (token, action, raw_input)
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
        except ReceiptVerificationError as exc:
            self._receipt_context = None
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": str(exc)})
        except ZkJobError as exc:
            self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
        except Exception as exc:  # pragma: no cover - runtime safety
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self._send_bytes(status, body, "application/json")

    def _send_bytes(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        receipt_headers: dict[str, str] = {}
        context = getattr(self, "_receipt_context", None)
        if context is not None and RECEIPT_RECEIVER is not None:
            try:
                token, action, action_input = context
                claims = verify_authorization_token(token, RECEIPT_RECEIVER.token_issuer_key)
                receipt = RECEIPT_RECEIVER.issue(
                    token,
                    action_type=action,
                    action_input=action_input,
                    action_output=body,
                    result_status="success" if int(status) < 400 else "error",
                    service_defined_fields={"http-status": int(status)},
                )
                publication_id, registration = RECEIPT_PUBLISHER.publish(receipt, claims["sello_logs"][0])
                receipt_headers[RECEIPT_HEADER] = receipt_header(receipt)
                receipt_headers[SCITT_BUNDLE_URL_HEADER] = f"/v1/sello/receipts/{publication_id}"
                receipt_headers[SCITT_TRANSACTION_HEADER] = str(registration["transaction_id"])
            except Exception as exc:
                status = HTTPStatus.SERVICE_UNAVAILABLE
                content_type = "application/json"
                body = json.dumps(
                    {"ok": False, "error": "receiver receipt could not be committed to SCITT", "detail": str(exc)},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                receipt_headers = {}
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in receipt_headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"zk_inference HTTP service listening on {HOST}:{PORT}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

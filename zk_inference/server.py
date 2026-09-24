#!/usr/bin/env python3
"""Small HTTP wrapper around the local zk_inference pipeline."""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from agent_receipts.environment import RECEIPT_HEADER, bearer_token, receipt_header, receiver_from_environment
from agent_receipts.receiver_log import (
    SCITT_BUNDLE_URL_HEADER,
    SCITT_TRANSACTION_HEADER,
    ReceiverTransparencyPublisher,
)
from agent_receipts.sello_v1 import ReceiptVerificationError, authorization_origin, verify_authorization_token
from transport_security.peer import authenticated_peer
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
RECEIPT_PUBLISHER = ReceiverTransparencyPublisher("zk-inference")
RECEIVER_ORIGIN = authorization_origin(os.environ.get("ZK_INFERENCE_ORIGIN", ""))
JOB_OWNERS: dict[str, str] = {}

PUBLIC_ACTIONS = {
    "/v1/models/fetch": "fetch_latest_verified_zk_model_bundle",
    "/v1/jobs": "generate_random_zk_chestmnist_image",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "zk-inference-http/1.0"

    def _authorize(self, scope: str) -> tuple[str, dict[str, Any]]:
        try:
            peer = authenticated_peer(self)
        except ValueError as exc:
            raise ReceiptVerificationError(str(exc)) from exc
        token = bearer_token(self.headers)
        claims = verify_authorization_token(
            token,
            RECEIPT_RECEIVER.token_issuer_key,
            expected_audience=RECEIVER_ORIGIN,
            required_scope=scope,
            expected_subject=peer.subject,
            cert_thumbprint=peer.fingerprint,
        )
        return token, claims

    @staticmethod
    def _assert_job_owner(job_id: str, subject: str) -> None:
        if not subject or JOB_OWNERS.get(job_id) != subject:
            raise ReceiptVerificationError("ZK inference job is not owned by this agent")

    def do_GET(self) -> None:
        self._receipt_context = None
        if self.path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True})
            return
        try:
            scope = (
                "receipts:read"
                if self.path.startswith("/v1/sello/receipts/") or self.path.endswith("/transparency-bundle")
                else "jobs:read"
            )
            _token, claims = self._authorize(scope)
            if self.path.startswith("/v1/jobs/"):
                self._assert_job_owner(self.path.split("/")[3], claims["sub"])
        except ReceiptVerificationError as exc:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": str(exc)})
            return
        if self.path.startswith("/v1/sello/receipts/"):
            try:
                bundle = RECEIPT_PUBLISHER.read(self.path.rsplit("/", 1)[-1])
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
            "/v1/models/fetch",
            "/v1/jobs",
        } and not (self.path.startswith("/v1/jobs/") and self.path.endswith("/run-and-verify")):
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
            return
        try:
            action = PUBLIC_ACTIONS.get(self.path)
            if self.path.startswith("/v1/jobs/") and self.path.endswith("/run-and-verify"):
                action = "generate_and_verify_zk_inference_proof"
            token, claims = self._authorize(action)
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 <= length <= 16_384:
                raise ValueError("request body is too large")
            raw_input = self.rfile.read(length)
            payload = json.loads(raw_input.decode("utf-8") or "{}")
            if action == "generate_and_verify_zk_inference_proof":
                raw_input = json.dumps(
                    {"job_id": self.path.split("/")[3]}, sort_keys=True, separators=(",", ":")
                ).encode()
                self._assert_job_owner(self.path.split("/")[3], claims["sub"])
            self._receipt_context = (token, action, raw_input)
            if self.path == "/v1/models/fetch":
                result = RUNTIME.fetch_model()
            elif self.path == "/v1/jobs":
                result = RUNTIME.create_job(payload.get("index"))
                JOB_OWNERS[str(result["job_id"])] = claims["sub"]
            elif self.path.startswith("/v1/jobs/") and self.path.endswith("/run-and-verify"):
                result = RUNTIME.run_and_verify(self.path.split("/")[3])
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
        if context is not None:
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
    # The inactive standalone ZK deployment has no same-container mTLS proxy or
    # private Unix socket yet. Never expose the forwarded-peer-header handler
    # on TCP: copied public certificates would otherwise impersonate a peer.
    raise RuntimeError(
        "Standalone ZK HTTP transport is disabled until its authenticated mTLS proxy "
        "and private Unix socket are configured"
    )


if __name__ == "__main__":
    raise SystemExit(main())

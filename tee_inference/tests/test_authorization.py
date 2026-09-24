from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import httpx
import numpy as np

from agent_receipts.environment import RECEIPT_HEADER, decode_receipt_header
from agent_receipts.sello_v1 import SelloOwner
from tee_inference.service.app import create_job_app
from tee_inference.service.jobs import TeeJobStore
from tee_inference.tests.sello_fixtures import AuthenticatedServiceTestCase
from transport_security.peer import certificate_thumbprint


class ProtectedInferenceTests(AuthenticatedServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.loads = 0
        self.inferences = 0
        self.model_artifact_hash = bytes.fromhex("31" * 32)
        self.model_manifest_hash = bytes.fromhex("32" * 32)
        dataset = self.directory / "test-data.npz"
        np.savez(dataset, images=np.zeros((1, 28, 28), dtype=np.uint8), labels=np.zeros((1, 14), dtype=np.uint8))
        self.store = TeeJobStore(self.directory / "jobs", dataset)
        self.app = create_job_app(self.load_model, self.store)

    def load_model(self):
        self.loads += 1
        return self, None

    def infer(self, request: bytes) -> bytes:
        self.inferences += 1
        return b"inference-response"

    def call(self, method: str, route: str, *, headers=None, **kwargs) -> httpx.Response:
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=self.origin) as client:
                return await client.request(method, route, headers=headers, **kwargs)

        return asyncio.run(run())

    def test_every_operation_requires_token_and_mtls_before_work(self) -> None:
        routes = [
            ("POST", "/v1/models/fetch"),
            ("POST", "/v1/jobs"),
            ("POST", "/v1/jobs/" + "a" * 32 + "/run"),
            ("GET", "/v1/jobs/" + "a" * 32),
            ("GET", "/v1/sello/receipts/" + "a" * 64),
        ]
        headers = self.authorization_headers()
        missing_token = {k: v for k, v in headers.items() if k != "Authorization"}
        missing_peer = {"Authorization": headers["Authorization"]}
        for method, route in routes:
            for absent in ({}, missing_token, missing_peer):
                with self.subTest(route=route, headers=list(absent)):
                    self.assertEqual(self.call(method, route, headers=absent).status_code, 401)
        self.assertEqual((self.loads, self.inferences), (0, 0))
        self.publish.assert_not_called()

    def test_wrong_token_binding_scope_and_expiry_rejected_before_model_load(self) -> None:
        base = {
            "audience": self.origin,
            "cert_thumbprint": certificate_thumbprint(self.certificate),
            "scopes": self.scopes,
        }
        bad_tokens = {
            "audience": self.owner.token(**{**base, "audience": "https://another-worker.example"}),
            "scope": self.owner.token(**{**base, "scopes": ["jobs:read"]}),
            "certificate": self.owner.token(
                **{**base, "cert_thumbprint": certificate_thumbprint(self.agent_certificate(self.subject))}
            ),
            "expired": self.owner.token(**base, now=int(time.time()) - 1000),
            "subject": SelloOwner(
                self.issuer, bytes.fromhex("23" * 32), {}, subject="another-agent", log_urls=["https://scitt.example"]
            ).token(**base),
        }
        good_token = self.owner.token(**base)
        header, claims, signature = good_token.split(".")
        bad_tokens["signature"] = ".".join((header, claims, ("A" if signature[0] != "A" else "B") + signature[1:]))
        for reason, token in bad_tokens.items():
            with self.subTest(reason=reason):
                self.assertEqual(
                    self.call(
                        "POST", "/v1/models/fetch", headers=self.authorization_headers(token=token), json={}
                    ).status_code,
                    401,
                )
        self.assertEqual((self.loads, self.inferences), (0, 0))
        self.publish.assert_not_called()

    def test_owner_bound_job_flow_emits_verifiable_encrypted_receipt(self) -> None:
        headers = self.authorization_headers()
        self.assertEqual(self.call("POST", "/v1/models/fetch", headers=headers, json={}).status_code, 200)
        created = self.call("POST", "/v1/jobs", headers=headers, json={})
        job_id = created.json()["job_id"]
        metadata = self.call("GET", f"/v1/jobs/{job_id}", headers=headers)
        self.assertEqual(metadata.status_code, 200)
        response = self.call("POST", f"/v1/jobs/{job_id}/run", headers=headers)
        self.assertEqual(response.status_code, 200)
        verified = self.owner.verify(
            decode_receipt_header(response.headers[RECEIPT_HEADER]),
            headers["Authorization"].split(" ", 1)[1],
            expected_service="tee-inference",
            expected_action="run_and_verify_tee_inference",
            action_input=(f'{{"job_id":"{job_id}"}}').encode(),
            action_output=response.content,
        )
        self.assertEqual(verified.body["result-status"], "success")
        self.assertEqual(self.inferences, 1)
        self.assertEqual(self.publish.call_count, 3)
        self.assertNotIn(RECEIPT_HEADER, metadata.headers)
        self.assertEqual((self.directory / "jobs" / job_id / "evidence.cbor").read_bytes(), response.content)

    def test_another_authenticated_agent_cannot_read_or_run_job(self) -> None:
        headers = self.authorization_headers()
        self.call("POST", "/v1/models/fetch", headers=headers, json={})
        job_id = self.call("POST", "/v1/jobs", headers=headers, json={}).json()["job_id"]
        other_cert = self.agent_certificate("second-agent")
        other_owner = SelloOwner(
            self.issuer, bytes.fromhex("24" * 32), {}, subject="second-agent", log_urls=["https://scitt.example"]
        )
        other = self.authorization_headers(
            token=other_owner.token(
                audience=self.origin, cert_thumbprint=certificate_thumbprint(other_cert), scopes=self.scopes
            ),
            certificate=other_cert,
        )
        self.assertEqual(self.call("GET", f"/v1/jobs/{job_id}", headers=other).status_code, 403)
        self.assertEqual(self.call("POST", f"/v1/jobs/{job_id}/run", headers=other).status_code, 403)
        self.assertEqual(self.inferences, 0)

    def test_receipt_download_is_authenticated_without_recursive_receipt(self) -> None:
        with patch("tee_inference.service.app.ReceiverTransparencyPublisher.read", return_value=b"publication") as read:
            path = "/v1/sello/receipts/" + "a" * 64
            self.assertEqual(self.call("GET", path).status_code, 401)
            read.assert_not_called()
            result = self.call("GET", path, headers=self.authorization_headers())
        self.assertEqual(result.content, b"publication")
        self.assertNotIn(RECEIPT_HEADER, result.headers)
        self.publish.assert_not_called()

    def test_publication_failure_cannot_return_success_without_receipt(self) -> None:
        self.publish.side_effect = RuntimeError("log unavailable")
        response = self.call("POST", "/v1/models/fetch", headers=self.authorization_headers(), json={})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(RECEIPT_HEADER, response.headers)
        self.assertEqual(self.loads, 1)  # Publication failure is not an operation rollback.

    def test_revoked_client_certificate_cannot_use_its_unexpired_token(self) -> None:
        headers = self.authorization_headers()
        self.write_crl(self.certificate)
        self.assertEqual(self.call("POST", "/v1/models/fetch", headers=headers, json={}).status_code, 401)
        self.assertEqual((self.loads, self.inferences), (0, 0))

    def test_health_is_minimal_and_legacy_routes_are_absent(self) -> None:
        self.assertEqual(self.call("GET", "/healthz").json(), {"status": "ok"})
        for path in ("/v1/prepare", "/v1/infer"):
            self.assertEqual(self.call("POST", path).status_code, 404)
        self.assertEqual((self.loads, self.inferences), (0, 0))

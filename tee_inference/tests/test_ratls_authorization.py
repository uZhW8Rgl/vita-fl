"""RA-TLS API authorization with real PoP signatures and a local fake session.

These tests isolate HTTP authorization; they do not simulate DCAP or a TLS
handshake. Session attestation and actual TLS have separate transport tests.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace
from unittest.mock import patch

import httpx
import numpy as np
from nacl.signing import SigningKey

from agent_receipts.environment import RECEIPT_HEADER, decode_receipt_header
from agent_receipts.sello_v1 import SelloOwner
from tee_inference.protocol.v1 import decode_request
from tee_inference.service.app import create_job_app
from tee_inference.service.jobs import TeeJobStore
from tee_inference.tests.sello_fixtures import AuthenticatedServiceTestCase
from transport_security.attestation import SessionBinding
from transport_security.pop import PoPIdentity, proof_headers, public_key_thumbprint


class RatlsAuthorizationTests(AuthenticatedServiceTestCase):
    def setUp(self) -> None:
        super().setUp()
        seed = bytes.fromhex("33" * 32)
        public_key = bytes(SigningKey(seed).verify_key)
        self.agent = PoPIdentity(self.subject, seed, public_key_thumbprint(public_key))
        self.session = SessionBinding(
            "a" * 64,
            {"tls_spki_sha256": b"t" * 32, "challenge": b"c" * 32},
            b"session-verified-by-the-transport-tests",
        )
        patch.dict(
            os.environ,
            {"TEE_TRANSPORT_MODE": "ratls", "AGENT_POP_REGISTRY": json.dumps({self.subject: public_key.hex()})},
        ).start()
        patch("tee_inference.service.ratls.RatlsSessionManager", return_value=self).start()
        self.loads = 0
        self.inferences = 0
        self.requests = []
        self.model_artifact_hash = b"m" * 32
        self.model_manifest_hash = b"h" * 32
        dataset = self.directory / "test-data.npz"
        np.savez(dataset, images=np.zeros((1, 28, 28), dtype=np.uint8), labels=np.zeros((1, 14), dtype=np.uint8))
        self.store = TeeJobStore(self.directory / "jobs", dataset)
        self.app = create_job_app(self.load_model, self.store)

    def require_session(self, session_id):
        if session_id != self.session.session_id:
            raise ValueError("unknown or expired session")
        return self.session

    def load_model(self):
        self.loads += 1
        return self, self

    def infer(self, request):
        self.inferences += 1
        self.requests.append(request)
        return b"numeric-inference-response"

    def emit(self, request, response, *, session):
        assert session is self.session
        return b"test-air-evidence"

    def call(self, method, path, *, body=b"", headers=None):
        async def request():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=self.origin) as client:
                return await client.request(method, path, content=body, headers=headers)

        return asyncio.run(request())

    def pop_headers(self, method, path, body=b"", *, token=None, agent=None):
        agent = agent or self.agent
        token = token or self.owner.token(audience=self.origin, pop_thumbprint=agent.thumbprint, scopes=self.scopes)
        return {
            "Authorization": "Bearer " + token,
            "X-Vita-Attestation-Session": self.session.session_id,
            "X-Vita-Tls-Spki": self.session.claims["tls_spki_sha256"].hex(),
            **proof_headers(
                agent,
                method=method,
                url=self.origin + path,
                body=body,
                token=token,
                session_id=self.session.session_id,
            ),
        }

    def authorized_call(self, method, path, body=b"{}"):
        headers = self.pop_headers(method, path, body)
        return self.call(method, path, body=body, headers=headers), headers

    def test_protected_routes_reject_unsigned_calls_and_spoofed_proxy_headers_before_work(self):
        routes = [
            ("POST", "/v1/models/fetch"),
            ("POST", "/v1/jobs"),
            ("POST", "/v1/jobs/" + "a" * 32 + "/run"),
            ("GET", "/v1/jobs/" + "a" * 32),
            ("GET", "/v1/sello/receipts/" + "a" * 64),
        ]
        for method, path in routes:
            valid = self.pop_headers(method, path)
            for headers in (
                {},
                {key: value for key, value in valid.items() if key != "X-Vita-PoP"},
                {key: value for key, value in valid.items() if key != "Authorization"},
                {**self.authorization_headers(), "X-Vita-Tls-Spki": self.session.claims["tls_spki_sha256"].hex()},
            ):
                with self.subTest(path=path, headers=list(headers)):
                    self.assertEqual(self.call(method, path, headers=headers).status_code, 401)
        self.assertEqual((self.loads, self.inferences), (0, 0))
        self.publish.assert_not_called()

    def test_forged_subject_and_wrong_pop_key_do_not_authorize_model_load(self):
        path = "/v1/models/fetch"
        attacker_seed = b"x" * 32
        attacker = PoPIdentity(
            self.subject, attacker_seed, public_key_thumbprint(bytes(SigningKey(attacker_seed).verify_key))
        )
        forged_owner = SelloOwner(
            self.issuer, b"o" * 32, {}, subject="another-agent", log_urls=["https://scitt.example"]
        )
        forged_token = forged_owner.token(
            audience=self.origin, scopes=self.scopes, pop_thumbprint=self.agent.thumbprint
        )
        cases = [
            self.pop_headers("POST", path, token=forged_token),
            self.pop_headers("POST", path, agent=attacker),
            self.pop_headers("POST", path, agent=replace(self.agent, subject="another-agent")),
        ]
        for headers in cases:
            self.assertEqual(self.call("POST", path, headers=headers).status_code, 401)
        self.assertEqual(self.loads, 0)
        self.publish.assert_not_called()

    def test_mutated_body_session_and_tls_header_are_rejected_before_work(self):
        path = "/v1/models/fetch"
        headers = self.pop_headers("POST", path, b"{}")
        cases = [
            (headers, b'{"changed":true}'),
            ({**headers, "X-Vita-Attestation-Session": "b" * 64}, b"{}"),
            ({**headers, "X-Vita-Tls-Spki": "ff" * 32}, b"{}"),
        ]
        for changed, body in cases:
            self.assertEqual(self.call("POST", path, body=body, headers=changed).status_code, 401)
        self.assertEqual(self.loads, 0)

    def test_same_proof_cannot_execute_twice(self):
        path = "/v1/models/fetch"
        headers = self.pop_headers("POST", path, b"{}")
        self.assertEqual(self.call("POST", path, body=b"{}", headers=headers).status_code, 200)
        self.assertEqual(self.call("POST", path, body=b"{}", headers=headers).status_code, 401)
        self.assertEqual(self.loads, 1)
        self.assertEqual(self.publish.call_count, 1)

    def test_registry_revocation_rejects_unexpired_token(self):
        path = "/v1/models/fetch"
        headers = self.pop_headers("POST", path)
        os.environ["AGENT_POP_REGISTRY"] = json.dumps({"other-agent": bytes(SigningKey.generate().verify_key).hex()})
        self.assertEqual(self.call("POST", path, headers=headers).status_code, 401)
        self.assertEqual(self.loads, 0)

    def test_allowed_operations_emit_sello_receipts_bound_to_session_and_tls_key(self):
        prepared, _ = self.authorized_call("POST", "/v1/models/fetch")
        self.assertEqual(prepared.status_code, 200)
        created, _ = self.authorized_call("POST", "/v1/jobs")
        self.assertEqual(created.status_code, 200)
        job_id = created.json()["job_id"]
        response, headers = self.authorized_call("POST", f"/v1/jobs/{job_id}/run", b"")
        self.assertEqual(response.status_code, 200)
        token = headers["Authorization"].removeprefix("Bearer ")
        receipt = self.owner.verify(
            decode_receipt_header(response.headers[RECEIPT_HEADER]),
            token,
            expected_service="tee-inference",
            expected_action="run_and_verify_tee_inference",
            action_input=json.dumps({"job_id": job_id}, sort_keys=True, separators=(",", ":")).encode(),
            action_output=response.content,
        )
        fields = receipt.body["service-defined-fields"]
        self.assertEqual(fields["attested-session-id"], self.session.session_id)
        self.assertEqual(fields["tls-spki-sha256"], self.session.claims["tls_spki_sha256"].hex())
        self.assertEqual(decode_request(self.requests[0])[5], self.session.claims["challenge"])
        self.assertEqual(self.inferences, 1)
        self.assertEqual(self.publish.call_count, 3)

    def test_distinct_registered_agent_cannot_access_another_subjects_job(self):
        self.authorized_call("POST", "/v1/models/fetch")
        created, _ = self.authorized_call("POST", "/v1/jobs")
        job_id = created.json()["job_id"]
        other_seed = b"y" * 32
        other_key = bytes(SigningKey(other_seed).verify_key)
        other = PoPIdentity("other-agent", other_seed, public_key_thumbprint(other_key))
        os.environ["AGENT_POP_REGISTRY"] = json.dumps(
            {self.subject: self.agent.public_key.hex(), other.subject: other.public_key.hex()}
        )
        other_owner = SelloOwner(
            self.issuer, b"z" * 32, {}, subject=other.subject, log_urls=["https://scitt.example"]
        )
        for method, path in [("GET", f"/v1/jobs/{job_id}"), ("POST", f"/v1/jobs/{job_id}/run")]:
            token = other_owner.token(audience=self.origin, scopes=self.scopes, pop_thumbprint=other.thumbprint)
            headers = self.pop_headers(method, path, token=token, agent=other)
            self.assertEqual(self.call(method, path, headers=headers).status_code, 403)
        self.assertEqual(self.inferences, 0)

    def test_log_failure_withholds_response_without_claiming_operation_rollback(self):
        self.publish.side_effect = RuntimeError("log unavailable")
        response, _ = self.authorized_call("POST", "/v1/models/fetch")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(RECEIPT_HEADER, response.headers)
        self.assertEqual(self.loads, 1)

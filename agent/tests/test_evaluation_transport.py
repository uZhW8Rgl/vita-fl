"""Probe requests reach the real receiver authorization with real PoP/Sello keys.

Transport attestation/SCITT are fixture boundaries, not cloud/TLS observations.
"""

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent import evaluation_transport as probe
from agent_receipts.environment import RECEIPT_HEADER, decode_receipt_header
from tee_inference.tests import test_ratls_authorization as authorization


@pytest.fixture
def receiver(monkeypatch):
    sut = authorization.RatlsAuthorizationTests()
    sut.setUp()
    monkeypatch.setenv("AGENT_EVALUATION_ENABLED", "true")
    sut.authorized_call("POST", "/v1/models/fetch")
    with (
        patch("agent.sello_client._registered_tee_receiver", return_value=(sut.origin, None)),
        patch("agent_receipts.environment.owner_from_environment", return_value=sut.owner),
    ):

        def begin(action, service, body, *, receiver_base_url):
            assert body == b'{"index":null}'
            return SimpleNamespace(
                receiver_origin=sut.origin,
                client_identity=sut.agent,
                token=sut.owner.token(audience=sut.origin, scopes=[action], pop_thumbprint=sut.agent.thumbprint),
            )

        def complete(call, headers, raw, status, **kwargs):
            assert status == 200
            sut.owner.verify(
                decode_receipt_header(headers[RECEIPT_HEADER]),
                call.token,
                expected_service="tee-inference",
                expected_action="generate_random_tee_chestmnist_image",
                action_input=b'{"index":null}',
                action_output=raw,
            )

        def send(call, session, peer, method, path, body, headers):
            headers = {**headers, "X-Vita-Tls-Spki": peer}
            response = sut.call(method, path, body=body, headers=headers)
            return response.status_code, response.headers, response.content

        monkeypatch.setattr("agent.sello_client.begin_receiver_call", begin)
        monkeypatch.setattr("agent.sello_client.complete_receiver_call", complete)
        monkeypatch.setattr(probe, "establish", lambda _: (sut.session, "74" * 32))
        monkeypatch.setattr(probe, "send", send)
        yield sut
    sut.doCleanups()


@pytest.mark.parametrize(
    "case,expected",
    [
        ("valid_request", "ACCEPTED"),
        ("same_proof_twice", "REPLAY_REJECTED"),
        ("concurrent_same_proof", "EXACTLY_ONE_ACCEPTED"),
        *(
            (name, "AUTH_REJECTED")
            for name in (
                "changed_body",
                "changed_method",
                "changed_url",
                "changed_token",
                "changed_session",
                "expired_proof",
                "expired_token",
            )
        ),
    ],
)
def test_live_probe_mutations_are_rejected_by_real_receiver_authorization(receiver, case, expected):
    code, facts = probe.run_case(case, "prepared", {})
    assert code == expected
    assert facts["side_effect_count"] == int(case in {"valid_request", "same_proof_twice", "concurrent_same_proof"})
    if case == "concurrent_same_proof":
        assert facts["accepted_count"] == 1
    assert receiver.inferences == 0


def test_unknown_failure_is_not_classified_as_auth_rejection(receiver, monkeypatch):
    monkeypatch.setattr(probe, "send", lambda *a: (503, {}, b'{"error":"log unavailable"}'))
    with pytest.raises(ValueError, match="unexpected HTTP outcome"):
        probe.run_case("changed_body", "prepared", {})


def test_expired_session_requires_real_receiver_rejection(receiver, monkeypatch):
    receiver.session.claims["expires_at"] = int(time.time()) - 1
    monkeypatch.setattr(receiver, "require_session", lambda _: (_ for _ in ()).throw(ValueError("expired")))
    code, facts = probe.run_case("expired_session", "prepared", {})
    assert code == "AUTH_REJECTED" and facts["side_effect_count"] == 0

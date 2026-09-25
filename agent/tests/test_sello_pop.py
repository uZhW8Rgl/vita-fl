"""Sello must bind tokens to the selected transport identity without downgrade."""

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import cbor2
import pytest
from nacl.signing import SigningKey

from agent.sello_client import ReceiverCall, begin_receiver_call, complete_receiver_call, validate_receiver_security
from agent_receipts.environment import RECEIPT_HEADER, receipt_header
from agent_receipts.owner_audit import CONTEXT_CONTENT_TYPE, open_audit_context
from agent_receipts.sello_v1 import (
    ReceiptVerificationError,
    SelloOwner,
    SelloReceiver,
    b64url_encode,
    verify_authorization_token,
)
from transport_security.attestation import VerifiedSession
from transport_security.client import ClientIdentity
from transport_security.pop import (
    PoPIdentity,
    ReplayCache,
    capture_pop_identity,
    proof_headers,
    verify_pop_proof,
)

NOW = 1_800_000_000
ORIGIN = "https://worker.example"


@pytest.fixture
def owner():
    return SelloOwner(
        SigningKey(bytes.fromhex("11" * 32)),
        bytes.fromhex("22" * 32),
        {},
        subject="agent-a",
        log_urls=["https://scitt.example"],
    )


@pytest.fixture
def identity(monkeypatch):
    monkeypatch.setenv("AGENT_POP_SIGNING_SEED", "33" * 32)
    monkeypatch.setenv("SELLO_OWNER_SUBJECT", "agent-a")
    return capture_pop_identity()


def token(owner, identity):
    return owner.token(audience=ORIGIN, scopes=["run", "receipts:read"], pop_thumbprint=identity.thumbprint, now=NOW)


def test_pop_bound_token_and_receipt_round_trip(owner, identity):
    authorization = token(owner, identity)
    claims = verify_authorization_token(
        authorization,
        owner.token_issuer_public_key,
        expected_audience=ORIGIN,
        required_scope="run",
        expected_subject=identity.subject,
        expected_pop_thumbprint=identity.thumbprint,
        now=NOW,
    )
    assert claims["cnf"] == {"jkt": identity.thumbprint}
    receiver = SelloReceiver("tee-inference", SigningKey.generate(), owner.token_issuer_public_key)
    receipt = receiver.issue(
        authorization,
        action_type="run",
        action_input=b"input",
        action_output=b"output",
        result_status="success",
        now=NOW,
    )
    verified = owner.verify(
        receipt,
        authorization,
        expected_service="tee-inference",
        expected_action="run",
        action_input=b"input",
        action_output=b"output",
        trusted_service_key=receiver.public_key,
    )
    assert verified.body["result-status"] == "success"


def test_binding_profiles_are_exclusive_at_issuance_and_verification(owner, identity):
    with pytest.raises(ReceiptVerificationError, match="exactly one"):
        owner.token(audience=ORIGIN, scopes=["run"], now=NOW)
    with pytest.raises(ReceiptVerificationError, match="exactly one"):
        owner.token(
            audience=ORIGIN,
            scopes=["run"],
            cert_thumbprint=identity.thumbprint,
            pop_thumbprint=identity.thumbprint,
            now=NOW,
        )
    pop_token = token(owner, identity)
    cert_token = owner.token(audience=ORIGIN, scopes=["run"], cert_thumbprint=identity.thumbprint, now=NOW)
    for authorization, checks in (
        (pop_token, {"cert_thumbprint": identity.thumbprint}),
        (cert_token, {"expected_pop_thumbprint": identity.thumbprint}),
        (pop_token, {"cert_thumbprint": identity.thumbprint, "expected_pop_thumbprint": identity.thumbprint}),
        (pop_token, {"expected_pop_thumbprint": b64url_encode(b"x" * 32)}),
    ):
        with pytest.raises(ReceiptVerificationError):
            verify_authorization_token(authorization, owner.token_issuer_public_key, now=NOW, **checks)


def test_attacker_with_own_registered_key_cannot_use_stolen_victim_token(owner, identity, monkeypatch):
    victim_token = token(owner, identity)
    monkeypatch.setenv("AGENT_POP_SIGNING_SEED", "44" * 32)
    monkeypatch.setenv("SELLO_OWNER_SUBJECT", "agent-b")
    attacker = capture_pop_identity()
    request = {"method": "POST", "url": ORIGIN + "/run", "body": b"", "token": victim_token, "session_id": "1" * 64}
    proof = proof_headers(attacker, **request, now=NOW)
    peer = verify_pop_proof(
        proof,
        **request,
        registry={identity.subject: identity.public_key, attacker.subject: attacker.public_key},
        replay_cache=ReplayCache(),
        now=NOW,
    )
    with pytest.raises(ReceiptVerificationError):
        verify_authorization_token(
            victim_token,
            owner.token_issuer_public_key,
            expected_subject=peer.subject,
            expected_pop_thumbprint=peer.thumbprint,
            now=NOW,
        )
    # Even the locally held issuer seed cannot authorize the attacker's key to
    # masquerade as agent-a: the receiver's independent registry remains binding.
    forged_identity_token = owner.token(audience=ORIGIN, scopes=["run"], pop_thumbprint=attacker.thumbprint, now=NOW)
    with pytest.raises(ReceiptVerificationError, match="subject"):
        verify_authorization_token(
            forged_identity_token,
            owner.token_issuer_public_key,
            expected_subject=peer.subject,
            expected_pop_thumbprint=peer.thumbprint,
            now=NOW,
        )


def configure_owner(monkeypatch, owner):
    monkeypatch.setenv("SELLO_TOKEN_ISSUER_SIGNING_SEED", bytes(owner.token_signing_key).hex())
    monkeypatch.setenv("SELLO_OWNER_HPKE_PRIVATE_KEY", owner.hpke_private_key.hex())
    monkeypatch.setenv("SELLO_SERVICE_REGISTRY", json.dumps({"zk-inference": "55" * 32}))
    monkeypatch.setenv("SELLO_LOG_URLS", "https://scitt.example")


def test_ratls_tee_call_uses_pop_without_client_certificate_and_pins_receiver(owner, identity, monkeypatch):
    configure_owner(monkeypatch, owner)
    monkeypatch.setenv("TEE_TRANSPORT_MODE", "ratls")
    admitted_key = b"a" * 32
    with (
        patch("agent.sello_client._registered_tee_receiver", return_value=(ORIGIN, admitted_key)),
        patch("agent.sello_client.capture_client_identity", side_effect=AssertionError("must not load TLS client key")),
    ):
        call = begin_receiver_call("run", "tee-inference", b"input", receiver_base_url=ORIGIN)
    assert isinstance(call.client_identity, PoPIdentity)
    assert call.client_identity.receiver_origin == ORIGIN
    assert call.client_identity.trusted_service_key == admitted_key
    claims = verify_authorization_token(
        call.token, owner.token_issuer_public_key, expected_pop_thumbprint=identity.thumbprint
    )
    assert claims["scope"] == ["receipts:read", "run"]
    assert call.headers == {"Authorization": "Bearer " + call.token}
    monkeypatch.setenv("AGENT_POP_SIGNING_SEED", "66" * 32)
    assert call.client_identity.thumbprint == identity.thumbprint


def test_ratls_mode_does_not_change_zk_client_authentication(owner, identity, monkeypatch):
    configure_owner(monkeypatch, owner)
    monkeypatch.setenv("TEE_TRANSPORT_MODE", "ratls")
    cert_identity = ClientIdentity(Path("/old-generation/cert"), Path("/old-generation/key"), identity.thumbprint)
    with patch("agent.sello_client.capture_client_identity", return_value=cert_identity):
        call = begin_receiver_call("run", "zk-inference", b"input", receiver_base_url=ORIGIN)
    assert call.client_identity is cert_identity
    assert verify_authorization_token(call.token, owner.token_issuer_public_key)["cnf"] == {
        "x5t#S256": identity.thumbprint
    }


def test_ratls_startup_requires_pop_key_instead_of_mtls_credentials(owner, identity, monkeypatch):
    configure_owner(monkeypatch, owner)
    monkeypatch.setenv("TEE_TRANSPORT_MODE", "ratls")
    with (
        patch("agent.sello_client.validate_client_configuration", side_effect=AssertionError("not mTLS")),
        patch("agent.sello_client.validate_attestation_configuration") as policy_check,
    ):
        validate_receiver_security()
    policy_check.assert_called_once_with()
    monkeypatch.delenv("AGENT_POP_SIGNING_SEED")
    with pytest.raises(ValueError):
        validate_receiver_security()


@pytest.mark.parametrize(
    "mismatch",
    [
        None,
        "missing_session",
        "origin",
        "sello_key",
        "session_id",
        "tls_key",
        "missing_fields",
        "registration_failure",
        "registered_digest",
        "registered_log",
        "context_registration_failure",
        "context_registered_digest",
        "context_registered_log",
    ],
)
def test_pop_receipt_is_bound_to_the_original_verified_session(owner, identity, monkeypatch, mismatch):
    configure_owner(monkeypatch, owner)
    signing_key = SigningKey.generate()
    receiver = SelloReceiver("tee-inference", signing_key, owner.token_issuer_public_key)
    authorization = token(owner, identity)
    session = VerifiedSession(
        hashlib.sha256(b"verified-by-transport").hexdigest(),
        {"origin": ORIGIN, "sello_public_key": receiver.public_key, "tls_spki_sha256": b"t" * 32},
        b"verified-by-transport",
        {"verified": True},
    )
    fields = {"attested-session-id": session.session_id, "tls-spki-sha256": (b"t" * 32).hex()}
    if mismatch == "origin":
        session.claims["origin"] = "https://other.example"
    elif mismatch == "sello_key":
        session.claims["sello_public_key"] = b"x" * 32
    elif mismatch == "session_id":
        fields["attested-session-id"] = "b" * 64
    elif mismatch == "tls_key":
        fields["tls-spki-sha256"] = (b"x" * 32).hex()
    elif mismatch == "missing_fields":
        fields = {}
    receipt = receiver.issue(
        authorization,
        action_type="run",
        action_input=b"input",
        action_output=b"output",
        result_status="success",
        service_defined_fields=fields,
        now=NOW,
    )
    call = ReceiverCall("run", "tee-inference", authorization, b"input", identity, receiver.public_key, ORIGIN)
    registration = {
        "service_url": "https://wrong.example" if mismatch == "registered_log" else "https://scitt.example",
        "transaction_id": "session-1.3",
        "evidence_sha256": "f" * 64 if mismatch == "registered_digest" else session.session_id,
        "_signed_statement": b"agent-signed-session-statement",
        "_transparent_statement": b"session-inclusion-evidence",
    }

    def register_evidence(raw, **options):
        if options["content_type"] != CONTEXT_CONTENT_TYPE:
            if mismatch == "registration_failure":
                raise RuntimeError("session registration unavailable")
            return registration
        if mismatch == "context_registration_failure":
            raise RuntimeError("owner context registration unavailable")
        return {
            **registration,
            "transaction_id": "context-1.4",
            "service_url": "https://wrong.example" if mismatch == "context_registered_log" else "https://scitt.example",
            "evidence_sha256": "f" * 64 if mismatch == "context_registered_digest" else hashlib.sha256(raw).hexdigest(),
            "content_type": CONTEXT_CONTENT_TYPE,
        }

    with (
        patch(
            "agent.sello_client.verify_publication_bundle",
            return_value={"service_url": "https://scitt.example", "transaction_id": "1.2"},
        ) as verify_log,
        patch(
            "agent.sello_client.register_verified_evidence",
            side_effect=register_evidence,
        ) as register_session,
        patch(
            "agent.transparency_index.record_transparency_entry",
            side_effect=[
                {"record_id": "session-record"},
                {"record_id": "context-record"},
                {"record_id": "tool-record"},
            ],
        ) as record_entry,
    ):
        options = {
            "receiver_base_url": ORIGIN,
            "publication_bundle": b"verified-publication",
            "verified_session": None if mismatch == "missing_session" else session,
        }
        if mismatch is None:
            result = complete_receiver_call(call, {RECEIPT_HEADER: receipt_header(receipt)}, b"output", 200, **options)
            assert result["transaction_id"] == "1.2"
            audit = result["attestation_session"]
            assert audit["session_id"] == session.session_id
            assert audit["transparency_record_id"] == "session-record"
            assert audit["transaction_id"] == "session-1.3"
            assert audit["phala_signed"] is False
            assert audit["verifier_assessment"] == dict(session.quote_verification)
            assert Path(audit["evidence_path"]).read_bytes() == session.evidence
            assert json.loads(Path(audit["assessment_path"]).read_text())["session_id"] == session.session_id
            publication = cbor2.loads(Path(audit["publication_bundle_path"]).read_bytes())
            assert publication[1] == registration["_signed_statement"]
            assert publication[2] == registration["_transparent_statement"]
            assert register_session.call_count == 2
            register_session.assert_any_call(
                session.evidence,
                url="https://scitt.example",
                transparent_statement_path=str(
                    Path(audit["evidence_path"]).with_name("attestation-session-transparent-statement.cose")
                ),
                content_type="application/vnd.master-thesis.ratls-session+cbor",
                identity=owner.subject,
            )
            assert record_entry.call_args_list[0].kwargs["evidence_type"] == "attestation-session"
            context_registration = register_session.call_args_list[1]
            assert context_registration.kwargs["content_type"] == CONTEXT_CONTENT_TYPE
            opened = open_audit_context(
                context_registration.args[0], owner.hpke_private_key, hashlib.sha256(receipt).hexdigest()
            )
            assert opened == {
                "authorization_token": authorization,
                "action_input": b"input",
                "action_output": b"output",
            }
            context_audit = result["owner_audit"]
            assert context_audit["receipt_sha256"] == hashlib.sha256(receipt).hexdigest()
            assert context_audit["evidence_sha256"] == hashlib.sha256(context_registration.args[0]).hexdigest()
            assert context_audit["transparency_record_id"] == "context-record"
            assert context_audit["transaction_id"] == "context-1.4"
            assert record_entry.call_args_list[1].kwargs["evidence_type"] == "sello-verification-context"
            assert record_entry.call_args_list[2].kwargs["verification"]["attestation_session"] == audit
            assert record_entry.call_args_list[2].kwargs["verification"]["owner_audit"] == context_audit
            assert authorization not in json.dumps(result)
        elif mismatch.startswith("context_"):
            with pytest.raises(RuntimeError, match="context"):
                complete_receiver_call(call, {RECEIPT_HEADER: receipt_header(receipt)}, b"output", 200, **options)
            assert register_session.call_count == 2
            assert record_entry.call_count == 1
            assert record_entry.call_args.kwargs["evidence_type"] == "attestation-session"
        else:
            with pytest.raises(RuntimeError, match="session"):
                complete_receiver_call(call, {RECEIPT_HEADER: receipt_header(receipt)}, b"output", 200, **options)
            record_entry.assert_not_called()
            if mismatch in {"registration_failure", "registered_digest", "registered_log"}:
                register_session.assert_called_once()
                verify_log.assert_called_once()
            else:
                register_session.assert_not_called()
                verify_log.assert_not_called()


def test_ratls_startup_rejects_missing_platform_policy(owner, identity, monkeypatch):
    configure_owner(monkeypatch, owner)
    monkeypatch.setenv("TEE_TRANSPORT_MODE", "ratls")
    monkeypatch.delenv("RATLS_ALLOWED_PLATFORM_MEASUREMENTS", raising=False)
    with pytest.raises(ValueError, match="RATLS_ALLOWED_PLATFORM_MEASUREMENTS"):
        validate_receiver_security()


def test_mtls_receipt_keeps_existing_log_flow_without_session_registration(owner, identity, monkeypatch):
    configure_owner(monkeypatch, owner)
    receiver = SelloReceiver("tee-inference", SigningKey.generate(), owner.token_issuer_public_key)
    authorization = owner.token(audience=ORIGIN, scopes=["run"], cert_thumbprint=identity.thumbprint, now=NOW)
    receipt = receiver.issue(
        authorization,
        action_type="run",
        action_input=b"input",
        action_output=b"output",
        result_status="success",
        now=NOW,
    )
    cert_identity = ClientIdentity(Path("/cert"), Path("/key"), identity.thumbprint)
    call = ReceiverCall("run", "tee-inference", authorization, b"input", cert_identity, receiver.public_key, ORIGIN)
    with (
        patch(
            "agent.sello_client.verify_publication_bundle",
            return_value={"service_url": "https://scitt.example", "transaction_id": "legacy-1.1"},
        ),
        patch("agent.sello_client.register_verified_evidence") as register_session,
        patch(
            "agent.transparency_index.record_transparency_entry", return_value={"record_id": "legacy-record"}
        ) as record,
    ):
        result = complete_receiver_call(
            call,
            {RECEIPT_HEADER: receipt_header(receipt)},
            b"output",
            200,
            receiver_base_url=ORIGIN,
            publication_bundle=b"legacy-publication",
        )
    assert result["transaction_id"] == "legacy-1.1"
    assert "attestation_session" not in result
    assert "owner_audit" not in result
    register_session.assert_not_called()
    record.assert_called_once()

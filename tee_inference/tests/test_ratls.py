"""RA-TLS session lifetime, key rotation and cross-layer AIR binding tests."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import cbor2
import pytest
from nacl.signing import SigningKey

from agent import tee_inference_client as client
from agent.tests.test_tee_inference_client import CONTRACT_POLICY_ENV, IMAGE_DIGEST, valid_fixture
from tee_inference.air.v1 import ENCLAVE_MEASUREMENTS
from tee_inference.protocol.v1 import encode_deterministic
from tee_inference.service.attestation import AirEvidenceEmitter
from tee_inference.service.ratls import RatlsSessionManager
from transport_security import attestation as a
from transport_security.tests.test_attestation import ApiResponse, api_value, certificate, quote_for


class FakeDstack:
    def __init__(self, *, base_quote=None, event_log="[]", app_compose="{}"):
        self.base_quote = quote_for(bytes(64)) if base_quote is None else base_quote
        self.event_log = event_log
        self.app_compose = app_compose
        self.calls = []

    def call(self, path, payload):
        self.calls.append(path)
        if path == "/GetKey":
            assert payload == {"path": "master-thesis/air-v1", "purpose": "air-ed25519-signing"}
            return {"key": "2a" * 32}
        if path == "/GetQuote":
            quote = bytearray(self.base_quote)
            quote[568:632] = bytes.fromhex(payload["report_data"])
            return {"quote": bytes(quote).hex(), "event_log": self.event_log}
        if path == "/Info":
            return {
                "tcb_info": {
                    **{name: value.hex() for name, value in a.parse_tdx_quote(self.base_quote).items()},
                    "app_compose": self.app_compose,
                }
            }
        raise AssertionError(path)


@pytest.fixture
def manager(tmp_path):
    der, pem = certificate()
    cert_path = tmp_path / "tls.crt"
    cert_path.write_bytes(pem)
    clock = SimpleNamespace(now=1000)
    dstack = FakeDstack()
    store = RatlsSessionManager(
        b"s" * 32,
        "https://tee.example",
        client=dstack,
        certificate_path=cert_path,
        clock=lambda: clock.now,
        max_sessions=2,
    )
    return SimpleNamespace(store=store, clock=clock, certificate_path=cert_path, der=der, dstack=dstack)


def test_session_available_before_model_load_and_binds_real_certificate(manager):
    evidence = manager.store.create_session(b"c" * 32)
    session = manager.store.require_session(hashlib.sha256(evidence).hexdigest())
    assert session.evidence == evidence
    assert session.claims["tls_spki_sha256"] == a.tls_spki_sha256(manager.der)
    assert session.claims["air_public_key"] == bytes(SigningKey(bytes.fromhex("2a" * 32)).verify_key)
    assert manager.dstack.calls == ["/GetKey", "/GetQuote", "/Info"]


def test_session_preserves_dstack_quote_with_70_bytes_of_zero_padding(manager):
    manager.dstack.base_quote += bytes(70)
    evidence = manager.store.create_session(b"c" * 32)
    session = manager.store.require_session(hashlib.sha256(evidence).hexdigest())
    value = a.decode_session_evidence(session.evidence)
    expected_quote = bytearray(manager.dstack.base_quote)
    expected_quote[568:632] = a.session_report_data(dict(session.claims))
    assert value["quote"] == bytes(expected_quote)
    assert value["quote"][-70:] == bytes(70)
    assert len(value["quote"]) == 636 + int.from_bytes(value["quote"][632:636], "little") + 70


@pytest.mark.parametrize("fault", ["truncated", "nonzero-padding", "empty-signature"])
def test_session_rejects_invalid_dstack_quote_without_caching(manager, monkeypatch, fault):
    original_call = manager.dstack.call

    def malformed_quote(path, payload):
        response = original_call(path, payload)
        if path == "/GetQuote":
            quote = bytes.fromhex(response["quote"])
            if fault == "truncated":
                quote = quote[:-1]
            elif fault == "nonzero-padding":
                quote += bytes(69) + b"\x01"
            else:
                quote = quote[:632] + bytes(4) + quote[636:]
            response["quote"] = quote.hex()
        return response

    monkeypatch.setattr(manager.dstack, "call", malformed_quote)
    with pytest.raises(a.AttestationVerificationError, match="invalid session evidence"):
        manager.store.create_session(b"c" * 32)
    assert not manager.store._sessions


def test_capacity_preserves_active_sessions_and_rejects_before_quote(manager):
    sessions = []
    for index in range(2):
        evidence = manager.store.create_session(bytes([index]) * 32)
        sessions.append(hashlib.sha256(evidence).hexdigest())
    calls_before = list(manager.dstack.calls)
    with pytest.raises(a.AttestationVerificationError, match="capacity"):
        manager.store.create_session(b"x" * 32)
    assert manager.dstack.calls == calls_before
    for session_id in sessions:
        assert manager.store.require_session(session_id).session_id == session_id
    assert len(manager.store._sessions) == 2

    manager.clock.now = 1300
    with pytest.raises(a.AttestationVerificationError, match="expired"):
        manager.store.require_session(sessions[0])
    assert not manager.store._sessions
    replacement = manager.store.create_session(b"x" * 32)
    assert manager.store.require_session(hashlib.sha256(replacement).hexdigest())


def test_concurrent_quotes_cannot_evict_the_first_admitted_session(manager, monkeypatch):
    manager.store.max_sessions = 1
    barrier = threading.Barrier(2)
    original_call = manager.dstack.call

    def synchronized_quote(path, payload):
        if path == "/GetQuote":
            barrier.wait(timeout=5)
        return original_call(path, payload)

    monkeypatch.setattr(manager.dstack, "call", synchronized_quote)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(manager.store.create_session, bytes([index]) * 32) for index in range(2)]
        accepted = []
        rejected = []
        for future in futures:
            try:
                accepted.append(future.result(timeout=10))
            except a.AttestationVerificationError as exc:
                rejected.append(str(exc))
    assert len(accepted) == 1
    assert rejected == ["RA-TLS session capacity is exhausted"]
    session_id = hashlib.sha256(accepted[0]).hexdigest()
    assert manager.store.require_session(session_id).evidence == accepted[0]
    assert len(manager.store._sessions) == 1


def test_server_rejects_session_after_tls_key_rotation(manager):
    evidence = manager.store.create_session(b"c" * 32)
    _, new_pem = certificate()
    manager.certificate_path.write_bytes(new_pem)
    with pytest.raises(a.AttestationVerificationError, match="previous TLS key"):
        manager.store.require_session(hashlib.sha256(evidence).hexdigest())


@pytest.mark.parametrize("challenge", [b"", b"x" * 31, b"x" * 33, "x" * 32])
def test_server_rejects_malformed_challenges(manager, challenge):
    with pytest.raises(a.AttestationVerificationError, match="challenge"):
        manager.store.create_session(challenge)
    assert manager.dstack.calls == ["/GetKey"]


def test_server_rejects_wrong_connection_key(manager):
    evidence = manager.store.create_session(b"c" * 32)
    with pytest.raises(a.AttestationVerificationError, match="TLS connection"):
        manager.store.require_session(hashlib.sha256(evidence).hexdigest(), tls_spki=b"x" * 32)


@pytest.fixture
def air_bundle(tmp_path, monkeypatch):
    for name, value in CONTRACT_POLICY_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("TEE_TRANSPORT_MODE", "ratls")
    original_request, original_bundle = valid_fixture()
    old = cbor2.loads(original_bundle)
    base_quote = bytearray(old[6])
    base_quote[2:4] = (2).to_bytes(2, "little")
    base_quote[4:8] = (0x81).to_bytes(4, "little")
    base_quote[632:636] = (len(base_quote) - 636).to_bytes(4, "little")
    dstack = FakeDstack(base_quote=bytes(base_quote), event_log=old[7], app_compose=old[8])
    measurements = a.parse_tdx_quote(bytes(base_quote))
    monkeypatch.setenv(
        "RATLS_ALLOWED_PLATFORM_MEASUREMENTS",
        json.dumps([{name: measurements[name].hex() for name in ("mrtd", "rtmr0", "rtmr1", "rtmr2")}]),
    )
    der, pem = certificate()
    cert_path = tmp_path / "tls.crt"
    cert_path.write_bytes(pem)
    manager = RatlsSessionManager(b"s" * 32, "https://tee.example", client=dstack, certificate_path=cert_path)
    evidence = manager.create_session(b"c" * 32)
    session = manager.require_session(hashlib.sha256(evidence).hexdigest())

    verified_quotes = {}

    def api_open(request, timeout):
        if request.get_method() == "GET":
            checksum = request.full_url.rsplit("/", 1)[-1]
            return ApiResponse(verified_quotes[checksum], "application/octet-stream")
        quote = bytes.fromhex(json.loads(request.data)["hex"])
        value = api_value(quote)
        verified_quotes[value["checksum"]] = quote
        return ApiResponse(value)

    monkeypatch.setattr(a.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=api_open))

    def deployment(event_log, compose, quote):
        events, _ = client._verify_rtmr3(event_log, quote)
        client._verify_app_compose(compose, events, IMAGE_DIGEST, *client._required_contract_policy())

    verified_session = a.verify_session_evidence(
        evidence,
        b"c" * 32,
        der,
        "https://tee.example",
        b"s" * 32,
        deployment_validator=deployment,
    )
    emitter = AirEvidenceEmitter.__new__(AirEvidenceEmitter)
    emitter.manifest_bytes = old[9]
    emitter.manifest = cbor2.loads(old[9])
    emitter.manifest_hash = hashlib.sha256(old[9]).digest()
    emitter.signing_key = SigningKey(bytes.fromhex("2a" * 32))
    emitter.sequence = 0
    emitter.client = dstack
    request = cbor2.loads(original_request)
    request[5] = session.claims["challenge"]
    request = encode_deterministic(request)
    response = cbor2.loads(old[3])
    response[3] = hashlib.sha256(request).digest()
    response = encode_deterministic(response)
    bundle = emitter.emit(request, response, session=session)
    return SimpleNamespace(
        request=request,
        response=response,
        bundle=bundle,
        verified_session=verified_session,
        session=session,
        emitter=emitter,
        original_bundle=original_bundle,
        manager=manager,
    )


def verify_air(fixture, bundle=None, session=None):
    return client.verify_tee_inference_bundle(
        fixture.bundle if bundle is None else bundle,
        fixture.request,
        IMAGE_DIGEST,
        verified_session=fixture.verified_session if session is None else session,
    )


def test_air_v2_requires_session_challenge_quote_verification_and_existing_policy(air_bundle):
    result = verify_air(air_bundle)
    assert result["dcap_collateral_verified"] is True
    assert result["quote_verification"]["provider"] == "phala"
    assert result["ratls_session_id"] == air_bundle.verified_session.session_id
    assert result["rtmr3_event_count"] == 3


def test_air_v1_downgrade_and_missing_verified_session_rejected(air_bundle):
    with pytest.raises(client.TeeInferenceVerificationError, match="AIR v2"):
        verify_air(air_bundle, bundle=air_bundle.original_bundle)
    with pytest.raises(client.TeeInferenceVerificationError, match="verified TLS session"):
        client.verify_tee_inference_bundle(air_bundle.bundle, air_bundle.request, IMAGE_DIGEST)


def test_session_substitution_rejected_even_for_same_software_and_air_key(air_bundle):
    other_evidence = air_bundle.manager.create_session(b"d" * 32)
    bundle = cbor2.loads(air_bundle.bundle)
    bundle[11] = other_evidence
    bundle[12] = hashlib.sha256(other_evidence).digest()
    with pytest.raises(client.TeeInferenceVerificationError, match="another RA-TLS session"):
        verify_air(air_bundle, encode_deterministic(bundle))


def test_emitter_rejects_old_nonce_and_missing_session(air_bundle):
    request = cbor2.loads(air_bundle.request)
    request[5] = b"old!" * 8
    with pytest.raises(RuntimeError, match="nonce"):
        air_bundle.emitter.emit(encode_deterministic(request), air_bundle.response, session=air_bundle.session)
    with pytest.raises(RuntimeError, match="admitted session"):
        air_bundle.emitter.emit(air_bundle.request, air_bundle.response)


def test_failed_phala_verification_prevents_air_acceptance(air_bundle, monkeypatch):
    monkeypatch.setattr(
        a.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(
            open=lambda *args, **kwargs: ApiResponse({"success": False}),
        ),
    )
    with pytest.raises(client.TeeInferenceVerificationError, match="Phala rejected"):
        verify_air(air_bundle)


def test_air_signed_measurements_must_match_quote(air_bundle):
    bundle = cbor2.loads(air_bundle.bundle)
    tagged = cbor2.loads(bundle[4])
    protected, unprotected, payload, signature = tagged.value
    claims = cbor2.loads(payload)
    claims[ENCLAVE_MEASUREMENTS]["pcr0"] = b"x" * 48
    payload = encode_deterministic(claims)
    signature = air_bundle.emitter.signing_key.sign(
        encode_deterministic(["Signature1", protected, b"", payload])
    ).signature
    bundle[4] = cbor2.dumps(cbor2.CBORTag(18, [protected, unprotected, payload, signature]), canonical=True)
    with pytest.raises(client.TeeInferenceVerificationError, match="signed measurements"):
        verify_air(air_bundle, encode_deterministic(bundle))


def test_old_v1_reportdata_is_rejected_in_v2(air_bundle):
    bundle = cbor2.loads(air_bundle.bundle)
    legacy_data = hashlib.sha256(b"MasterThesis.AIR.key.v1" + bundle[5] + hashlib.sha256(bundle[9]).digest()).digest()
    legacy_data += hashlib.sha256(air_bundle.request).digest()
    quote = bytearray(bundle[6])
    quote[568:632] = legacy_data
    bundle[6] = bytes(quote)
    bundle[10] = legacy_data
    with pytest.raises(client.TeeInferenceVerificationError, match="REPORTDATA"):
        verify_air(air_bundle, encode_deterministic(bundle))


def test_accepted_long_running_inference_can_finish_after_admission_expiry(air_bundle, monkeypatch):
    # Session age is checked at admission, not after a potentially long native forward pass.
    monkeypatch.setattr("tee_inference.service.attestation.time.time", lambda: time.monotonic() + 9999999999)
    bundle = air_bundle.emitter.emit(air_bundle.request, air_bundle.response, session=air_bundle.session)
    assert cbor2.loads(bundle)[12] == bytes.fromhex(air_bundle.session.session_id)

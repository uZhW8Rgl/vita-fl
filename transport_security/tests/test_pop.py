"""Security boundaries for the custom VITA-FL registered-agent PoP profile."""

import base64
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace

import pytest
from nacl.signing import SigningKey

from transport_security.pop import (
    PROOF_HEADER,
    PoPAuthenticationError,
    PoPIdentity,
    ReplayCache,
    capture_pop_identity,
    load_agent_registry,
    proof_headers,
    public_key_thumbprint,
    public_request_url,
    registered_agents,
    tee_transport_mode,
    verify_pop_proof,
)

NOW = 1_800_000_000
SESSION = "verified-session-" + "1" * 32
REQUEST = {
    "method": "POST",
    "url": "https://worker.example/v1/jobs/run?mode=one&index=2",
    "body": b'{"job_id":"0123"}',
    "token": "issuer.signed.token",
    "session_id": SESSION,
}


def identity(subject="agent-a", byte=1):
    seed = bytes([byte]) * 32
    return PoPIdentity(subject, seed, public_key_thumbprint(bytes(SigningKey(seed).verify_key)))


def verify(headers, *, agent=None, cache=None, now=NOW, **overrides):
    agent = agent or identity()
    return verify_pop_proof(
        headers,
        **{**REQUEST, **overrides},
        registry={agent.subject: agent.public_key},
        replay_cache=cache or ReplayCache(),
        now=now,
    )


def test_valid_proof_authenticates_the_operator_registered_key():
    agent = identity()
    headers = proof_headers(agent, **REQUEST, now=NOW)
    peer = verify(headers)
    assert peer.subject == agent.subject
    assert peer.thumbprint == agent.thumbprint
    assert peer.fingerprint == agent.thumbprint


@pytest.mark.parametrize(
    "changed",
    [
        {"method": "GET"},
        {"url": "https://other.example/v1/jobs/run?mode=one&index=2"},
        {"url": "https://worker.example/v1/jobs/other?mode=one&index=2"},
        {"url": "https://worker.example/v1/jobs/run?mode=one&index=3"},
        {"url": "https://worker.example/v1/jobs/run?index=2&mode=one"},
        {"body": b'{"job_id": "0123"}'},
        {"token": "another.signed.token"},
        {"session_id": "verified-session-" + "2" * 32},
    ],
)
def test_each_request_component_and_session_are_bound(changed):
    headers = proof_headers(identity(), **REQUEST, now=NOW)
    with pytest.raises(PoPAuthenticationError, match="binding"):
        verify(headers, **changed)


def test_stolen_token_without_registered_agent_private_key_is_insufficient():
    attacker = identity("agent-a", 2)
    headers = proof_headers(attacker, **REQUEST, now=NOW)
    with pytest.raises(PoPAuthenticationError, match="registered key"):
        verify(headers)


def test_agent_key_cannot_claim_another_subject():
    agent = identity()
    impersonator = replace(agent, subject="agent-b")
    headers = proof_headers(impersonator, **REQUEST, now=NOW)
    with pytest.raises(PoPAuthenticationError, match="not registered"):
        verify(headers)


@pytest.mark.parametrize("proof_time", [NOW - 61, NOW + 6])
def test_expired_and_future_proofs_are_rejected(proof_time):
    headers = proof_headers(identity(), **REQUEST, now=proof_time)
    with pytest.raises(PoPAuthenticationError, match="validity window"):
        verify(headers)


def test_replay_is_consumed_once_even_under_concurrent_requests():
    headers = proof_headers(identity(), **REQUEST, now=NOW)
    cache = ReplayCache()

    def check(_):
        try:
            verify(headers, cache=cache)
            return True
        except PoPAuthenticationError:
            return False

    with ThreadPoolExecutor(max_workers=8) as executor:
        assert sum(executor.map(check, range(20))) == 1


def test_cache_exhaustion_does_not_evict_still_valid_replay_records():
    agent = identity()
    first = proof_headers(agent, **REQUEST, now=NOW)
    second = proof_headers(agent, **REQUEST, now=NOW)
    cache = ReplayCache(capacity=1)
    verify(first, cache=cache)
    with pytest.raises(PoPAuthenticationError, match="full"):
        verify(second, cache=cache)
    with pytest.raises(PoPAuthenticationError, match="replayed"):
        verify(first, cache=cache)
    fresh = proof_headers(agent, **REQUEST, now=NOW + 61)
    verify(fresh, cache=cache, now=NOW + 61)


def test_failed_binding_does_not_consume_an_otherwise_valid_proof():
    headers = proof_headers(identity(), **REQUEST, now=NOW)
    cache = ReplayCache()
    with pytest.raises(PoPAuthenticationError):
        verify(headers, cache=cache, body=b"changed")
    verify(headers, cache=cache)


def test_proof_signature_and_profile_cannot_be_modified():
    headers = proof_headers(identity(), **REQUEST, now=NOW)
    first, second, signature = headers[PROOF_HEADER].split(".")
    changed_signature = base64.urlsafe_b64encode(b"x" * 64).rstrip(b"=").decode()
    with pytest.raises(PoPAuthenticationError, match="signature"):
        verify({PROOF_HEADER: f"{first}.{second}.{changed_signature}"})
    for raw in ("", "a.b", "x" * 4097, f"{first}=.{second}.{signature}"):
        with pytest.raises(PoPAuthenticationError):
            verify({PROOF_HEADER: raw})
    with pytest.raises(PoPAuthenticationError, match="exactly one"):
        verify({**headers, PROOF_HEADER.lower(): headers[PROOF_HEADER]})


def test_registry_is_required_and_operator_revocation_is_applied(monkeypatch):
    agent = identity()
    headers = proof_headers(agent, **REQUEST, now=NOW)
    monkeypatch.setenv("AGENT_POP_REGISTRY", json.dumps({agent.subject: agent.public_key.hex()}))
    assert load_agent_registry() == {agent.subject: agent.public_key}
    assert verify_pop_proof(headers, **REQUEST, replay_cache=ReplayCache(), now=NOW).subject == agent.subject
    monkeypatch.setenv("AGENT_POP_REGISTRY", json.dumps({"agent-b": identity("agent-b", 2).public_key.hex()}))
    with pytest.raises(PoPAuthenticationError, match="not registered"):
        verify_pop_proof(headers, **REQUEST, replay_cache=ReplayCache(), now=NOW)
    monkeypatch.delenv("AGENT_POP_REGISTRY")
    with pytest.raises(PoPAuthenticationError, match="required"):
        load_agent_registry()


@pytest.mark.parametrize("value", ["{}", "[]", '{"agent-a":"bad"}', '{"agent-a":"x","agent-a":"y"}'])
def test_invalid_registry_is_rejected_at_startup(monkeypatch, value):
    monkeypatch.setenv("AGENT_POP_REGISTRY", value)
    with pytest.raises(PoPAuthenticationError):
        load_agent_registry()


def test_one_registered_key_cannot_stand_for_two_subjects():
    key = identity().public_key
    with pytest.raises(PoPAuthenticationError, match="exactly one"):
        registered_agents({"agent-a": key, "agent-b": key})


def test_key_snapshot_survives_environment_rotation_and_hides_seed_in_repr(monkeypatch):
    original = identity()
    monkeypatch.setenv("SELLO_OWNER_SUBJECT", original.subject)
    monkeypatch.setenv("AGENT_POP_SIGNING_SEED", original.signing_seed.hex())
    snapshot = capture_pop_identity()
    monkeypatch.setenv("AGENT_POP_SIGNING_SEED", identity(byte=2).signing_seed.hex())
    assert snapshot.thumbprint != capture_pop_identity().thumbprint
    verify(proof_headers(snapshot, **REQUEST, now=NOW))
    assert "signing_seed" not in repr(snapshot)
    with pytest.raises(FrozenInstanceError):
        snapshot.subject = "changed"


@pytest.mark.parametrize("encoding", ["hex", "base64", "base64url"])
def test_base64_seed_and_default_subject(monkeypatch, encoding):
    seed = bytes([251]) * 32
    value = {
        "hex": seed.hex(),
        "base64": base64.b64encode(seed).decode(),
        "base64url": base64.urlsafe_b64encode(seed).rstrip(b"=").decode(),
    }[encoding]
    monkeypatch.setenv("AGENT_POP_SIGNING_SEED", value)
    monkeypatch.delenv("SELLO_OWNER_SUBJECT", raising=False)
    assert capture_pop_identity().subject == "master-thesis-agent"
    assert capture_pop_identity().signing_seed == seed


def test_transport_mode_is_explicit_and_fail_closed(monkeypatch):
    monkeypatch.delenv("TEE_TRANSPORT_MODE", raising=False)
    assert tee_transport_mode() == "mtls"
    monkeypatch.setenv("TEE_TRANSPORT_MODE", "RaTLS")
    assert tee_transport_mode() == "ratls"
    for bad in ("", "insecure", "disabled", "typo"):
        monkeypatch.setenv("TEE_TRANSPORT_MODE", bad)
        with pytest.raises(PoPAuthenticationError):
            tee_transport_mode()


def test_url_keeps_raw_query_and_rejects_ambiguous_destinations():
    assert public_request_url("https://WORKER.example/a%2fb?x=1&x=2") == "https://worker.example/a%2fb?x=1&x=2"
    assert public_request_url("https://worker.example/a?") == "https://worker.example/a?"
    for bad in ("http://worker.example/", "https://user@worker.example/", "https://worker.example/#", "https://a/\n"):
        with pytest.raises(PoPAuthenticationError):
            public_request_url(bad)

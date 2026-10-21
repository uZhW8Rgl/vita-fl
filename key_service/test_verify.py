"""Synthetic quotes exercise binding rejection; they do not attest real hardware."""

import hashlib
import json

import pytest

from key_service.verify import deployment_bindings, report_data, verify
from transport_security.attestation import TDX_REPORTDATA_OFFSET


def fixture():
    app_id = "11" * 20
    manifest = json.dumps(
        {"docker_compose_file": "services:\n  worker:\n    image: example/worker@sha256:" + "22" * 32 + "\n"}
    )
    claims = {
        "version": 1,
        "chainId": "31337",
        "participant": "0x" + "33" * 20,
        "appId": "0x" + app_id,
        "registry": "0x" + "44" * 20,
        "policy": "0x" + "55" * 20,
        "serviceId": "66" * 32,
        "challenge": "77" * 32,
        "recipientPublicKey": "88" * 32,
        "composeHash": hashlib.sha256(manifest.encode()).hexdigest(),
    }
    events = [
        {"imr": 3, "event_type": 0x08000001, "event": name, "event_payload": payload}
        for name, payload in [("app-id", app_id), ("compose-hash", claims["composeHash"])]
    ]
    state = bytes(48)
    for event in events:
        digest = hashlib.sha384(
            (0x08000001).to_bytes(4, "little")
            + b":"
            + event["event"].encode()
            + b":"
            + bytes.fromhex(event["event_payload"])
        ).digest()
        state = hashlib.sha384(state + digest).digest()
    quote = bytearray(637)
    quote[:2] = (4).to_bytes(2, "little")
    quote[2:4] = (2).to_bytes(2, "little")
    quote[4:8] = (0x81).to_bytes(4, "little")
    quote[520:568] = state
    quote[TDX_REPORTDATA_OFFSET : TDX_REPORTDATA_OFFSET + 64] = report_data(claims)
    quote[632:636] = (1).to_bytes(4, "little")
    quote[-1] = 1
    return {
        "claims": claims,
        "evidence": {"quote": quote.hex(), "eventLog": json.dumps(events), "appCompose": manifest},
    }


def test_authenticated_exact_context():
    value = fixture()

    def authenticated(quote, expected):
        assert quote[TDX_REPORTDATA_OFFSET : TDX_REPORTDATA_OFFSET + 64] == expected
        return {"verified": True}

    assert verify(value, appraise=authenticated)["composeHash"] == value["claims"]["composeHash"]


@pytest.mark.parametrize(
    "field", ["challenge", "participant", "recipientPublicKey", "policy", "serviceId", "appId", "registry"]
)
def test_quote_cannot_be_reused_for_other_release(field):
    value = fixture()
    value["claims"][field] = ("0x" if value["claims"][field].startswith("0x") else "") + "ab" * (
        20 if value["claims"][field].startswith("0x") else 32
    )
    with pytest.raises(ValueError, match="REPORTDATA"):
        verify(value, appraise=lambda *_: {"verified": True})


def test_fabricated_quote_never_passes_without_authentication(monkeypatch):
    monkeypatch.delenv("RATLS_ALLOWED_PLATFORM_MEASUREMENTS", raising=False)
    with pytest.raises(ValueError, match="authentication"):
        verify(fixture(), appraise=lambda *_: {"verified": False})
    # The actual production path has no synthetic verifier mode; missing configured
    # trust references reject this fixture before contacting an online provider.
    with pytest.raises(ValueError):
        verify(fixture())


def test_changed_manifest_and_replayed_events_are_rejected():
    value = fixture()
    value["evidence"]["appCompose"] += " "
    with pytest.raises(ValueError, match="Compose"):
        verify(value, appraise=lambda *_: {"verified": True})
    value = fixture()
    events = json.loads(value["evidence"]["eventLog"])
    events[0]["event_payload"] = "99" * 20
    value["evidence"]["eventLog"] = json.dumps(events)
    with pytest.raises(ValueError, match="RTMR3"):
        verify(value, appraise=lambda *_: {"verified": True})


def test_unknown_claims_and_debug_guest_fail_closed():
    value = fixture()
    value["claims"]["oldMeasurement"] = "00" * 32
    with pytest.raises(ValueError):
        verify(value)
    value = fixture()
    quote = bytearray.fromhex(value["evidence"]["quote"])
    quote[168] = 1
    with pytest.raises(ValueError, match="debug"):
        deployment_bindings(bytes(quote), value["evidence"]["eventLog"], value["evidence"]["appCompose"])


def test_measured_app_id_must_match_signed_release():
    value = fixture()
    value["claims"]["appId"] = "0x" + "99" * 20
    quote = bytearray.fromhex(value["evidence"]["quote"])
    quote[TDX_REPORTDATA_OFFSET : TDX_REPORTDATA_OFFSET + 64] = report_data(value["claims"])
    value["evidence"]["quote"] = quote.hex()
    with pytest.raises(ValueError, match="deployment mismatch"):
        verify(value, appraise=lambda *_: {"verified": True})

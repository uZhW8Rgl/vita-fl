"""Resolve the worker's public origin without trusting an HTTP Host header."""

import json
import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from cryptography import x509
from nacl.signing import SigningKey

from pki import ratls_runtime

APP_ID = "a1" * 20
GATEWAY = "dstack-prod.example.test"


@pytest.fixture(autouse=True)
def isolated_origin(monkeypatch):
    for name in ("TEE_INFERENCE_ORIGIN", "DSTACK_GATEWAY_DOMAIN", "KUBO_API"):
        monkeypatch.delenv(name, raising=False)


def client(app_id=APP_ID):
    return SimpleNamespace(call=Mock(return_value={"app_id": app_id}))


def test_explicit_origin_is_preserved_without_dstack_lookup(monkeypatch):
    monkeypatch.setenv("TEE_INFERENCE_ORIGIN", "https://reserved.example.test:9443")
    monkeypatch.setenv("DSTACK_GATEWAY_DOMAIN", "invalid/domain")
    dstack = client()
    assert ratls_runtime.resolve_origin(dstack_client=dstack) == "https://reserved.example.test:9443"
    dstack.call.assert_not_called()


def test_explicit_origin_is_canonical_for_node_and_session_claims(monkeypatch):
    monkeypatch.setenv("TEE_INFERENCE_ORIGIN", "https://RESERVED.example.test:443/")
    assert ratls_runtime.resolve_origin(dstack_client=client()) == "https://reserved.example.test"


@pytest.mark.parametrize("app_id", [APP_ID, APP_ID.upper(), "app_" + APP_ID])
def test_local_app_id_and_measured_gateway_produce_tls_passthrough_origin(monkeypatch, app_id):
    monkeypatch.setenv("DSTACK_GATEWAY_DOMAIN", ".DSTACK-PROD.example.test")
    dstack = client(app_id)
    assert ratls_runtime.resolve_origin(dstack_client=dstack) == f"https://{APP_ID}-8443s.{GATEWAY}"
    dstack.call.assert_called_once_with("/Info", {})


def test_gateway_suffix_can_be_derived_from_measured_kubo_api(monkeypatch):
    monkeypatch.setenv("KUBO_API", "https://runtime-5001s.dstack-prod.phala.network/")
    assert ratls_runtime.resolve_origin(dstack_client=client()) == f"https://{APP_ID}-8443s.dstack-prod.phala.network"


@pytest.mark.parametrize(
    "origin",
    [
        "http://reserved.example.test",
        "https://user@reserved.example.test",
        "https://reserved.example.test/path",
        "https://reserved.example.test?",
        "https://reserved.example.test#",
        "https://reserved.example.test:0",
        "https://reserved.example.test:65536",
        "https://bad..example.test",
        "https://reserved.example.test\n",
        "   ",
    ],
)
def test_invalid_explicit_origin_never_falls_back_to_dstack(monkeypatch, origin):
    monkeypatch.setenv("TEE_INFERENCE_ORIGIN", origin)
    monkeypatch.setenv("DSTACK_GATEWAY_DOMAIN", GATEWAY)
    dstack = client()
    with pytest.raises(ValueError, match="TEE_INFERENCE_ORIGIN"):
        ratls_runtime.resolve_origin(dstack_client=dstack)
    dstack.call.assert_not_called()


@pytest.mark.parametrize("app_id", [None, 7, "", "0x" + APP_ID, "a" * 39, "g" * 40, APP_ID + ".attacker.test"])
def test_invalid_local_app_id_is_rejected(monkeypatch, app_id):
    monkeypatch.setenv("DSTACK_GATEWAY_DOMAIN", GATEWAY)
    with pytest.raises(ValueError, match="app_id"):
        ratls_runtime.resolve_origin(dstack_client=client(app_id))


@pytest.mark.parametrize("info", [None, [], {}, {"appId": APP_ID}])
def test_missing_info_app_id_is_rejected(monkeypatch, info):
    monkeypatch.setenv("DSTACK_GATEWAY_DOMAIN", GATEWAY)
    with pytest.raises(ValueError, match="app_id"):
        ratls_runtime.resolve_origin(dstack_client=SimpleNamespace(call=Mock(return_value=info)))


@pytest.mark.parametrize(
    "gateway", [" ", "https://gateway.test", "gateway.test:443", "bad..test", "-bad.test", "test", "a/evil.test"]
)
def test_invalid_explicit_gateway_does_not_fall_back_to_kubo(monkeypatch, gateway):
    monkeypatch.setenv("DSTACK_GATEWAY_DOMAIN", gateway)
    monkeypatch.setenv("KUBO_API", "https://runtime-5001s.dstack-prod.phala.network")
    dstack = client()
    with pytest.raises(ValueError, match="DSTACK_GATEWAY_DOMAIN"):
        ratls_runtime.resolve_origin(dstack_client=dstack)
    dstack.call.assert_not_called()


@pytest.mark.parametrize(
    "kubo",
    [
        "",
        "http://runtime-5001s.dstack-prod.phala.network",
        "https://user@runtime-5001s.dstack-prod.phala.network",
        "https://runtime-5001s.dstack-prod.phala.network/api/v0",
        "https://runtime-5001s.attacker.test",
        "https://runtime-5001s.dstack-prod.phala.network:8443",
        "https://runtime.dstack-prod.phala.network",
        "https://runtime-0s.dstack-prod.phala.network",
        "https://runtime-99999s.dstack-prod.phala.network",
    ],
)
def test_missing_or_invalid_gateway_source_is_rejected(monkeypatch, kubo):
    monkeypatch.setenv("KUBO_API", kubo)
    with pytest.raises(ValueError, match="KUBO_API"):
        ratls_runtime.resolve_origin(dstack_client=client())


def test_local_dstack_failure_does_not_fall_back_to_untrusted_app_id(monkeypatch):
    monkeypatch.setenv("DSTACK_GATEWAY_DOMAIN", GATEWAY)
    monkeypatch.setenv("APP_ID", APP_ID)
    dstack = SimpleNamespace(call=Mock(side_effect=RuntimeError("dstack unavailable")))
    with pytest.raises(RuntimeError, match="dstack unavailable"):
        ratls_runtime.resolve_origin(dstack_client=dstack)


def test_origin_cli_prints_only_the_public_origin(monkeypatch, capsys):
    monkeypatch.setenv("TEE_INFERENCE_ORIGIN", "https://reserved.example.test")
    monkeypatch.setattr("sys.argv", ["pki.ratls_runtime", "origin"])
    assert ratls_runtime.main() == 0
    result = capsys.readouterr()
    assert result.out == "https://reserved.example.test\n"
    assert result.err == ""


def test_prepare_exports_resolved_origin_for_its_inference_child(tmp_path, monkeypatch):
    origin = f"https://{APP_ID}-8443s.{GATEWAY}"
    monkeypatch.setattr(ratls_runtime, "resolve_origin", lambda: origin)
    monkeypatch.setenv("AGENT_POP_REGISTRY", json.dumps({"agent": bytes(SigningKey.generate().verify_key).hex()}))
    monkeypatch.setenv("RATLS_STATE_DIR", str(tmp_path))
    previous_umask = os.umask(0o077)
    try:
        ratls_runtime.prepare()
    finally:
        os.umask(previous_umask)
    assert os.environ["TEE_INFERENCE_ORIGIN"] == origin
    certificate = x509.load_pem_x509_certificate((tmp_path / "tls.crt").read_bytes())
    assert certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
        x509.DNSName
    ) == [f"{APP_ID}-8443s.{GATEWAY}"]

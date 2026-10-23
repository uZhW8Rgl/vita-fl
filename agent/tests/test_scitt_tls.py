"""Real local TLS handshakes and policy tests for the SCITT transport."""

from __future__ import annotations

import http.server
import ipaddress
import ssl
import threading
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import cbor2
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from agent_receipts import scitt
from agent_receipts import scitt_tls as tls


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in ("SCITT_CA_CERT", "SCITT_CA_URL", "SCITT_GATEWAY_CA_BOOTSTRAP", "SCITT_DEVELOPMENT", "SELLO_LOG_URLS"):
        monkeypatch.delenv(name, raising=False)


def certificate(tmp_path, *, hostname="127.0.0.1", expired=False):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test log")])
    now = datetime.now(timezone.utc)
    san = x509.IPAddress(ipaddress.ip_address(hostname)) if hostname == "127.0.0.1" else x509.DNSName(hostname)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=2))
        .not_valid_after(now - timedelta(days=1) if expired else now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([san]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    return cert_path, key_path


@pytest.mark.parametrize("fault", [None, "hostname", "expired", "untrusted"])
def test_real_tls_accepts_only_valid_trusted_peer(tmp_path, monkeypatch, fault):
    cert, key = certificate(
        tmp_path, hostname="wrong.example" if fault == "hostname" else "127.0.0.1", expired=fault == "expired"
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"authenticated log")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if fault != "untrusted":
        monkeypatch.setenv("SCITT_CA_CERT", str(cert))
    client = tls.create_client(f"https://127.0.0.1:{server.server_port}")
    try:
        if fault:
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                client.session.get("/")
        else:
            assert client.session.get("/").text == "authenticated log"
    finally:
        client.session.close()
        server.shutdown()
        server.server_close()
        thread.join()


def test_development_cannot_disable_validation(monkeypatch):
    with pytest.raises(ValueError, match="Unverified SCITT TLS"):
        tls.create_client("https://log.example", development=True)
    monkeypatch.setenv("SCITT_DEVELOPMENT", "1")
    with pytest.raises(ValueError, match="Unverified SCITT TLS"):
        tls.create_client("https://log.example")


def test_bootstrap_uses_same_configured_runtime_and_web_pki(tmp_path, monkeypatch):
    cert, _ = certificate(tmp_path)
    origin = "https://0123456789012345678901234567890123456789-8000s.dstack-test.phala.network"
    monkeypatch.setenv("SCITT_URL", origin)
    monkeypatch.setenv("SCITT_GATEWAY_CA_BOOTSTRAP", "1")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, content=cert.read_bytes())

    original = httpx.Client

    def client(**options):
        assert options["verify"] is True
        assert options["trust_env"] is False
        assert options["follow_redirects"] is False
        return original(**options, transport=httpx.MockTransport(respond))

    with patch.object(tls.httpx, "Client", client):
        context = tls.tls_context(origin)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert str(requests[0].url) == origin.replace("-8000s.", "-8002.") + "/service-cert.pem"
    with pytest.raises(ValueError, match="not configured"):
        tls.tls_context(origin.replace("012345", "abcdef"))


@pytest.mark.parametrize(
    "status,body", [(302, b""), (200, b"invalid certificate"), (200, b"x" * (tls.MAX_CERTIFICATE_BYTES + 1))]
)
def test_bootstrap_rejects_redirect_invalid_or_oversized_certificate(monkeypatch, status, body):
    origin = "https://0123456789012345678901234567890123456789-8000s.dstack-test.phala.network"
    monkeypatch.setenv("SCITT_URL", origin)
    monkeypatch.setenv("SCITT_GATEWAY_CA_BOOTSTRAP", "1")
    original = httpx.Client
    with patch.object(
        tls.httpx,
        "Client",
        lambda **kw: original(
            **kw,
            transport=httpx.MockTransport(
                lambda request: httpx.Response(status, content=body, headers={"Location": "https://attacker.example"})
            ),
        ),
    ):
        with pytest.raises((ValueError, ssl.SSLError)):
            tls.tls_context(origin)


def test_sdk_redirect_cannot_send_request_to_another_origin():
    client = tls.create_client("https://log.example")
    try:
        with pytest.raises(ValueError, match="redirect changed"):
            client.session.get("http://log.example/entries")
        with pytest.raises(ValueError, match="redirect changed"):
            client.session.get("https://attacker.example/entries")
    finally:
        client.session.close()


def test_receipt_cannot_select_log_or_tls_policy(monkeypatch):
    monkeypatch.setenv("SCITT_URL", "https://trusted.example")
    bundle = cbor2.dumps(
        {1: b"signed", 2: b"receipt", 3: {"service_url": "https://attacker.example", "development_tls": True}},
        canonical=True,
    )
    with (
        patch("pyscitt.crypto.parse_cose_sign1", return_value=({}, b"evidence")),
        patch.object(scitt, "create_client") as create,
    ):
        with pytest.raises(scitt.ScittRegistrationError, match="unconfigured"):
            scitt.verify_publication_bundle(b"evidence", bundle)
    create.assert_not_called()


def test_old_development_metadata_does_not_disable_tls(monkeypatch):
    monkeypatch.setenv("SCITT_URL", "https://trusted.example")
    bundle = cbor2.dumps(
        {1: b"signed", 2: b"receipt", 3: {"service_url": "https://trusted.example", "development_tls": True}},
        canonical=True,
    )
    with (
        patch("pyscitt.crypto.parse_cose_sign1", return_value=({}, b"evidence")),
        patch.object(scitt, "create_client", side_effect=ValueError("stop after policy")) as create,
    ):
        with pytest.raises(scitt.ScittRegistrationError, match="stop after policy"):
            scitt.verify_publication_bundle(b"evidence", bundle)
    create.assert_called_once_with("https://trusted.example")

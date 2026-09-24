import json
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from pki.runtime import _install, validate_crl
from transport_security.client import cert_thumbprint, open_receiver, validate_client_configuration
from transport_security.peer import PeerAuthenticationError, authenticated_peer, certificate_thumbprint


def pem(certificate):
    return certificate.public_bytes(serialization.Encoding.PEM)


def key_pem(key):
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )


def issue(name, issuer=None, *, ca=False, uris=(), dns=(), expired=False, client=True):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer[0].subject if issuer else subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(hours=2))
        .not_valid_after(now + timedelta(hours=-1 if expired else 1))
        .add_extension(
            x509.BasicConstraints(ca=ca, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=ca,
                crl_sign=ca,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
    )
    if not ca:
        builder = builder.add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier(uri) for uri in uris] + [x509.DNSName(value) for value in dns]
            ),
            critical=False,
        ).add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH if client else ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
    return builder.sign(issuer[1] if issuer else key, hashes.SHA256()), key


def crl_for(issuer, *, revoked=(), number=1, expired=False, signing_key=None):
    now = datetime.now(timezone.utc)
    builder = (
        x509.CertificateRevocationListBuilder()
        .issuer_name(issuer[0].subject)
        .last_update(now - timedelta(minutes=2))
        .next_update(now + timedelta(minutes=-1 if expired else 10))
        .add_extension(x509.CRLNumber(number), critical=False)
    )
    for serial in revoked:
        builder = builder.add_revoked_certificate(
            x509.RevokedCertificateBuilder().serial_number(serial).revocation_date(now - timedelta(minutes=1)).build()
        )
    return builder.sign(signing_key or issuer[1], hashes.SHA256()).public_bytes(serialization.Encoding.PEM)


def request_for(cert, *, verified="SUCCESS"):
    return SimpleNamespace(headers={"x-vita-mtls-verify": verified, "x-vita-client-cert": quote(pem(cert).decode())})


def test_peer_identity_is_certificate_uri_and_sha256_thumbprint(tmp_path, monkeypatch):
    issuer = issue("CA", ca=True)
    cert, _ = issue("untrusted-common-name", issuer, uris=["urn:vita-fl:agent:agent-42"])
    (tmp_path / "issuer.pem").write_bytes(pem(issuer[0]))
    (tmp_path / "leaf.pem").write_bytes(pem(cert))
    (tmp_path / "crl.pem").write_bytes(crl_for(issuer))
    monkeypatch.setenv("TLS_CERT_PATH", str(tmp_path / "leaf.pem"))
    monkeypatch.setenv("TLS_CLIENT_CA_CERT_PATH", str(tmp_path / "issuer.pem"))
    monkeypatch.setenv("TLS_CLIENT_CRL_PATH", str(tmp_path / "crl.pem"))
    peer = authenticated_peer(request_for(cert))
    assert peer.subject == "agent-42"
    assert peer.fingerprint == certificate_thumbprint(cert)
    (tmp_path / "crl.pem").write_bytes(crl_for(issuer, revoked=[cert.serial_number]))
    with pytest.raises(PeerAuthenticationError):
        authenticated_peer(request_for(cert))


@pytest.mark.parametrize(
    "options",
    [
        {"uris": ["urn:vita-fl:inference:worker-0"]},
        {"uris": ["urn:vita-fl:agent:one", "urn:vita-fl:agent:two"]},
        {"uris": ["urn:vita-fl:agent:one"], "expired": True},
        {"uris": ["urn:vita-fl:agent:one"], "client": False},
        {"ca": True},
    ],
)
def test_peer_rejects_invalid_role_lifetime_and_purpose(options):
    cert, _ = issue("leaf", **options)
    with pytest.raises(PeerAuthenticationError):
        authenticated_peer(request_for(cert))


def test_peer_requires_success_from_private_proxy():
    cert, _ = issue("leaf", uris=["urn:vita-fl:agent:one"])
    with pytest.raises(PeerAuthenticationError):
        authenticated_peer(request_for(cert, verified="NONE"))


def test_crl_rejects_stale_forged_and_rollback():
    issuer = issue("CA", ca=True)
    valid = crl_for(issuer, number=5)
    assert validate_crl(valid, pem(issuer[0])) is not None
    for bad in (
        crl_for(issuer, expired=True),
        crl_for(issuer, signing_key=ec.generate_private_key(ec.SECP256R1())),
        crl_for(issuer, number=4),
    ):
        with pytest.raises(ValueError):
            validate_crl(bad, pem(issuer[0]), previous=valid)


def test_atomic_install_rejects_wrong_key_and_wrong_identity(tmp_path, monkeypatch):
    issuer = issue("CA", ca=True)
    cert, key = issue("agent-42", issuer, uris=["urn:vita-fl:agent:agent-42"])
    (tmp_path / "root_ca.crt").write_bytes(pem(issuer[0]))
    monkeypatch.setenv("PKI_SUBJECT", "agent-42")
    _install(tmp_path, pem(cert), key_pem(key), "agent")
    before = (tmp_path / "current").readlink()
    with pytest.raises(ValueError):
        _install(tmp_path, pem(cert), key_pem(ec.generate_private_key(ec.SECP256R1())), "agent")
    monkeypatch.setenv("PKI_SUBJECT", "different-agent")
    with pytest.raises(ValueError):
        _install(tmp_path, pem(cert), key_pem(key), "agent")
    assert (tmp_path / "current").readlink() == before
    assert (tmp_path / "current/tls.key").stat().st_mode & 0o077 == 0


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "https://example.org/")
            self.end_headers()
            return
        self.send_response(200)
        client_certificate = x509.load_der_x509_certificate(self.connection.getpeercert(binary_form=True))
        self.send_header("X-Test-Client-Certificate", certificate_thumbprint(client_certificate))
        self.end_headers()
        self.wfile.write(b"mutually authenticated")

    def log_message(self, *_args):
        pass


@pytest.fixture
def tls_server(tmp_path, monkeypatch):
    root = issue("CA", ca=True)
    server = issue("localhost", root, dns=["localhost"], client=False)
    client = issue("agent-42", root, uris=["urn:vita-fl:agent:agent-42"])
    for name, contents in {
        "root.pem": pem(root[0]),
        "server.pem": pem(server[0]),
        "server.key": key_pem(server[1]),
        "client.pem": pem(client[0]),
        "client.key": key_pem(client[1]),
        "crl.pem": crl_for(root),
    }.items():
        path = tmp_path / name
        path.write_bytes(contents)
        path.chmod(0o600)
    for name, path in {
        "TLS_CERT_PATH": "client.pem",
        "TLS_KEY_PATH": "client.key",
        "TLS_CA_CERT_PATH": "root.pem",
        "TLS_CRL_PATH": "crl.pem",
    }.items():
        monkeypatch.setenv(name, str(tmp_path / path))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(tmp_path / "server.pem"), str(tmp_path / "server.key"))
    context.load_verify_locations(str(tmp_path / "root.pem"))
    context.verify_mode = ssl.CERT_REQUIRED
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield tmp_path, root, server, client, f"https://localhost:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()
    thread.join()


def test_real_mtls_and_thumbprint(tls_server):
    _, _, _, client, url = tls_server
    validate_client_configuration()
    with open_receiver(url, timeout=3) as response:
        assert response.read() == b"mutually authenticated"
    assert cert_thumbprint() == certificate_thumbprint(client[0])


def test_rejects_plaintext_redirect_and_revoked_server(tls_server):
    path, root, server, _, url = tls_server
    with pytest.raises(ValueError):
        open_receiver(url.replace("https:", "http:"))
    with pytest.raises(ValueError):
        open_receiver(url + "/redirect")
    (path / "crl.pem").write_bytes(crl_for(root, revoked=[server[0].serial_number]))
    with pytest.raises(Exception, match="revoked"):
        open_receiver(url, timeout=3)


def test_rejects_missing_ca_and_publicly_readable_private_key(tls_server, monkeypatch):
    path, *_ = tls_server
    monkeypatch.delenv("TLS_CA_CERT_PATH")
    with pytest.raises(ValueError, match="TLS_CA_CERT_PATH"):
        validate_client_configuration()
    monkeypatch.setenv("TLS_CA_CERT_PATH", str(path / "root.pem"))
    (path / "client.key").chmod(0o644)
    with pytest.raises(ValueError, match="group or others"):
        validate_client_configuration()


def test_call_keeps_certificate_binding_across_renewals_and_receipt_download(tls_server, monkeypatch):
    """Real TLS sees the original leaf despite two atomic generation switches."""
    import urllib.request

    from nacl.signing import SigningKey

    from agent.sello_client import begin_receiver_call, complete_receiver_call
    from agent_receipts.environment import RECEIPT_HEADER, receipt_header
    from agent_receipts.receiver_log import SCITT_BUNDLE_URL_HEADER
    from agent_receipts.sello_v1 import SelloReceiver, b64url_encode, verify_authorization_token

    path, root, server, client, url = tls_server
    (path / "root_ca.crt").write_bytes(pem(root[0]))
    monkeypatch.setenv("PKI_SUBJECT", "agent-42")
    _install(path, pem(client[0]), key_pem(client[1]), "agent")
    monkeypatch.setenv("TLS_CERT_PATH", str(path / "current/tls.crt"))
    monkeypatch.setenv("TLS_KEY_PATH", str(path / "current/tls.key"))
    issuer, receiver_key = SigningKey.generate(), SigningKey.generate()
    for name, value in {
        "SELLO_TOKEN_ISSUER_SIGNING_SEED": b64url_encode(bytes(issuer)),
        "SELLO_OWNER_HPKE_PRIVATE_KEY": b64url_encode(bytes.fromhex("43" * 32)),
        "SELLO_OWNER_SUBJECT": "agent-42",
        "SELLO_SERVICE_REGISTRY": json.dumps({"zk-inference": b64url_encode(bytes(receiver_key.verify_key))}),
        "SELLO_LOG_URLS": "https://scitt.example",
    }.items():
        monkeypatch.setenv(name, value)
    call = begin_receiver_call("test-action", "zk-inference", b"", receiver_base_url=url)
    original = certificate_thumbprint(client[0])

    renewed = issue("agent-42", root, uris=["urn:vita-fl:agent:agent-42"])
    _install(path, pem(renewed[0]), key_pem(renewed[1]), "agent")
    assert cert_thumbprint() != original
    request = urllib.request.Request(url, headers=call.headers)
    with open_receiver(request, timeout=3, identity=call.client_identity) as response:
        observed = response.headers["X-Test-Client-Certificate"]
        body = response.read()
    assert observed == original
    verify_authorization_token(call.token, issuer.verify_key, cert_thumbprint=observed)

    second_renewal = issue("agent-42", root, uris=["urn:vita-fl:agent:agent-42"])
    _install(path, pem(second_renewal[0]), key_pem(second_renewal[1]), "agent")
    receiver = SelloReceiver("zk-inference", receiver_key, issuer.verify_key)
    receipt = receiver.issue(
        call.token, action_type=call.action, action_input=b"", action_output=body, result_status="success"
    )
    observed_downloads = []

    def download(request, timeout, *, identity):
        response = open_receiver(request, timeout=timeout, identity=identity)
        observed_downloads.append(response.headers["X-Test-Client-Certificate"])
        verify_authorization_token(
            request.get_header("Authorization").removeprefix("Bearer "),
            issuer.verify_key,
            cert_thumbprint=observed_downloads[-1],
        )
        return response

    monkeypatch.setattr("agent.sello_client.open_receiver", download)
    monkeypatch.setattr(
        "agent.sello_client.verify_publication_bundle",
        lambda *_: {
            "service_url": "https://scitt.example",
            "transaction_id": "test-transaction",
        },
    )
    monkeypatch.setattr("agent.transparency_index.record_transparency_entry", lambda **_: {"record_id": "test"})
    result = complete_receiver_call(
        call,
        {RECEIPT_HEADER: receipt_header(receipt), SCITT_BUNDLE_URL_HEADER: "/v1/sello/receipts/test"},
        body,
        200,
        receiver_base_url=url,
    )
    assert result["transaction_id"] == "test-transaction"
    assert observed_downloads == [original]

    # Identity is fixed; revocation policy remains fresh for every HTTP call.
    (path / "crl.pem").write_bytes(crl_for(root, revoked=[server[0].serial_number]))
    with pytest.raises(Exception, match="revoked"):
        open_receiver(request, timeout=3, identity=call.client_identity)

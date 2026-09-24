"""Real TLS sockets: no credentials or inputs before key-bound attestation."""

from __future__ import annotations

import hashlib
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from nacl.signing import SigningKey

from pki import ratls_runtime
from transport_security import attestation, ratls_client
from transport_security.client import open_receiver
from transport_security.pop import PoPIdentity, ReplayCache, public_key_thumbprint, verify_pop_proof


@pytest.fixture
def ratls_server(tmp_path, monkeypatch):
    seed = bytes(range(32))
    pub = bytes(SigningKey(seed).verify_key)
    registry = {"agent-test": pub.hex()}
    monkeypatch.setenv("AGENT_POP_REGISTRY", json.dumps(registry))
    monkeypatch.setenv("TEE_INFERENCE_ORIGIN", "https://localhost")
    monkeypatch.setenv("RATLS_STATE_DIR", str(tmp_path))
    old_umask = __import__("os").umask(0o077)
    try:
        ratls_runtime.prepare()
    finally:
        __import__("os").umask(old_umask)
    cert = x509.load_pem_x509_certificate((tmp_path / "tls.crt").read_bytes())
    spki = attestation.tls_spki_sha256(cert.public_bytes(Encoding.DER))
    state = {"events": [], "fault": None, "verified": False, "spki": spki}
    replay = ReplayCache()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            state["events"].append((self.path, dict(self.headers), body, state["verified"]))
            if self.path == "/v1/attestation":
                now = int(time.time())
                claims = {
                    "version": 1,
                    "challenge": body,
                    "tls_spki_sha256": spki,
                    "air_public_key": b"a" * 32,
                    "sello_public_key": b"s" * 32,
                    "origin": state["origin"],
                    "issued_at": now,
                    "expires_at": now + 300,
                }
                if state["fault"] == "key":
                    claims["tls_spki_sha256"] = b"x" * 32
                if state["fault"] == "nonce":
                    claims["challenge"] = b"x" * 32
                if state["fault"] == "expired":
                    claims.update(issued_at=now - 600, expires_at=now - 300)
                evidence = attestation.encode_session_evidence(claims, b"test quote", "[]", "{}")
                state["session_id"] = hashlib.sha256(evidence).hexdigest()
                self.send_response(200)
                self.send_header("Content-Type", "application/cbor")
                self.send_header("Content-Length", str(len(evidence)))
                if state["fault"] == "close":
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                if state["fault"] == "slow":
                    for byte in evidence:
                        try:
                            self.wfile.write(bytes([byte]))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError, ssl.SSLError):
                            break
                        time.sleep(0.02)
                else:
                    self.wfile.write(evidence)
                return
            peer = verify_pop_proof(
                self.headers,
                method="POST",
                url=state["origin"] + self.path,
                body=body,
                token=self.headers["Authorization"][7:],
                session_id=state["session_id"],
                registry=registry,
                replay_cache=replay,
            )
            state["subject"] = peer.subject
            self.send_response(403 if state["fault"] == "denied" else 200)
            self.send_header("Content-Length", "2")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(b"{}")
            self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.load_cert_chain(str(tmp_path / "tls.crt"), str(tmp_path / "tls.key"))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    state["origin"] = f"https://localhost:{server.server_port}"
    identity = PoPIdentity("agent-test", seed, public_key_thumbprint(pub), state["origin"], b"s" * 32)

    def quote_verifier(*_args):
        if state["fault"] == "verifier":
            raise ValueError("Verifier unavailable")
        state["verified"] = True
        return {"provider": "test", "verified": True}

    monkeypatch.setattr(attestation, "verify_quote_with_phala", quote_verifier)
    monkeypatch.setattr(ratls_client, "verify_deployment", lambda *args: None)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, identity
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request_for(identity):
    return urllib.request.Request(
        identity.receiver_origin + "/v1/jobs?index=7",
        data=b"private input",
        headers={"Authorization": "Bearer bound.token.signature"},
        method="POST",
    )


def test_actual_tls_connection_is_attested_before_credentials_and_body(ratls_server):
    state, identity = ratls_server
    with open_receiver(request_for(identity), identity=identity, timeout=3) as response:
        assert response.read() == b"{}"
        assert response.verified_session.session_id == state["session_id"]
    assert len(state["events"]) == 2
    _, first_headers, challenge, _ = state["events"][0]
    assert "Authorization" not in first_headers
    assert "X-Vita-PoP" not in first_headers
    assert len(challenge) == 32 and challenge != b"private input"
    assert state["events"][1][2:] == (b"private input", True)
    assert state["subject"] == "agent-test"


@pytest.mark.parametrize("fault", ["key", "nonce", "expired", "close", "verifier"])
def test_failed_attestation_never_sends_protected_http(ratls_server, fault):
    state, identity = ratls_server
    state["fault"] = fault
    with pytest.raises(ValueError):
        open_receiver(request_for(identity), identity=identity, timeout=3)
    assert len(state["events"]) == 1
    assert "Authorization" not in state["events"][0][1]


def test_error_response_retains_attested_session_and_owns_socket(ratls_server):
    state, identity = ratls_server
    state["fault"] = "denied"
    with pytest.raises(urllib.error.HTTPError) as error:
        open_receiver(request_for(identity), identity=identity, timeout=3)
    with error.value as response:
        assert response.read() == b"{}"
        assert response.verified_session.session_id == state["session_id"]


def test_origin_change_rejected_before_connect(ratls_server):
    state, identity = ratls_server
    with pytest.raises(ValueError):
        open_receiver(request_for(identity), identity=replace(identity, receiver_origin="https://other.test"))
    assert state["events"] == []


def test_ratls_runtime_rotates_local_key_and_overwrites_proxy_headers(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_POP_REGISTRY", json.dumps({"agent": bytes(SigningKey.generate().verify_key).hex()}))
    monkeypatch.setenv("TEE_INFERENCE_ORIGIN", "https://inference.test")
    monkeypatch.setenv("RATLS_STATE_DIR", str(tmp_path))
    import os

    previous_umask = os.umask(0o077)
    try:
        ratls_runtime.prepare()
        original = (tmp_path / "tls.key").read_bytes()
        ratls_runtime.prepare()
        assert (tmp_path / "tls.key").read_bytes() != original
        assert (tmp_path / "tls.key").stat().st_mode & 0o077 == 0
        output = tmp_path / "nginx.conf"
        ratls_runtime.render_nginx(output)
        config = output.read_text()
        assert "__TLS_" not in config
        assert "ssl_verify_client off;" in config
        assert 'proxy_set_header X-Vita-Client-Cert "";' in config
        assert "proxy_set_header X-Vita-Tls-Spki " in config
        assert "keepalive_timeout 65;" in config
    finally:
        os.umask(previous_umask)


def test_generated_nginx_configuration_is_accepted(tmp_path, monkeypatch):
    import os
    import shutil
    import subprocess

    nginx = shutil.which("nginx")
    if nginx is None:
        pytest.skip("nginx is required for configuration validation")
    monkeypatch.setenv("AGENT_POP_REGISTRY", json.dumps({"agent": bytes(SigningKey.generate().verify_key).hex()}))
    monkeypatch.setenv("TEE_INFERENCE_ORIGIN", "https://inference.test")
    monkeypatch.setenv("RATLS_STATE_DIR", str(tmp_path))
    previous_umask = os.umask(0o077)
    try:
        ratls_runtime.prepare()
        config = tmp_path / "nginx.conf"
        ratls_runtime.render_nginx(config)
        config.write_text(config.read_text().replace("/run/vita-fl", str(tmp_path)))
        (tmp_path / "logs").mkdir()
        result = subprocess.run([nginx, "-p", str(tmp_path), "-t", "-c", str(config)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
    finally:
        os.umask(previous_umask)


@pytest.mark.parametrize("zk_url", ["", "https://disabled-zk.test"])
def test_ratls_agent_with_disabled_zk_needs_no_ca(monkeypatch, zk_url):
    from pki import runtime
    from transport_security import pop

    monkeypatch.setenv("TEE_TRANSPORT_MODE", "ratls")
    monkeypatch.setenv("ZK_INFERENCE_ENABLED", "false")
    monkeypatch.setenv("ZK_INFERENCE_URL", zk_url)
    monkeypatch.setattr(pop, "capture_pop_identity", lambda: object())
    monkeypatch.setattr(runtime, "prepare", lambda role: pytest.fail("Unexpected PKI enrollment"))

    def executed(program, arguments):
        assert program == "python" and arguments == ["python", "agent/run_agent.py"]
        raise SystemExit(0)

    monkeypatch.setattr(runtime.os, "execvp", executed)
    with pytest.raises(SystemExit) as result:
        runtime.supervise("agent", ["python", "agent/run_agent.py"])
    assert result.value.code == 0


def test_slow_evidence_has_absolute_deadline_and_sends_no_credentials(ratls_server, monkeypatch):
    state, identity = ratls_server
    state["fault"] = "slow"
    monkeypatch.setattr(ratls_client, "MAX_BOOTSTRAP_SECONDS", 0.1)
    started = time.monotonic()
    with pytest.raises((TimeoutError, OSError)):
        open_receiver(request_for(identity), identity=identity, timeout=3)
    assert time.monotonic() - started < 1
    assert len(state["events"]) == 1
    assert "Authorization" not in state["events"][0][1]

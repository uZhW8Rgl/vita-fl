"""Exercise the production proxy template against an actual private receiver."""

import json
import os
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

import pytest

from pki.runtime import render_nginx
from transport_security.peer import PeerAuthenticationError, authenticated_peer
from transport_security.tests.test_transport import crl_for, issue, key_pem, pem


@pytest.mark.skipif(not shutil.which("nginx"), reason="requires nginx binary")
def test_nginx_private_socket_overwrites_headers_and_enforces_revocation(tmp_path, monkeypatch):
    root = issue("Root", ca=True)
    issuer = issue("Intermediate", root, ca=True)
    worker = issue("worker-0", issuer, dns=["localhost"], client=False)
    agent = issue("agent-42", issuer, uris=["urn:vita-fl:agent:agent-42"])
    other = issue("agent-other", issuer, uris=["urn:vita-fl:agent:agent-other"])
    wrong_role = issue("worker-1", issuer, uris=["urn:vita-fl:inference:worker-1"])
    rogue_root = issue("Untrusted Root", ca=True)
    rogue = issue("rogue", rogue_root, uris=["urn:vita-fl:agent:rogue"])
    for name, certificate, key in (
        ("worker", *worker),
        ("agent", *agent),
        ("wrong-role", *wrong_role),
        ("rogue", *rogue),
    ):
        chain = pem(certificate) + pem(rogue_root[0] if name == "rogue" else issuer[0])
        (tmp_path / f"{name}.crt").write_bytes(chain)
        (tmp_path / f"{name}.key").write_bytes(key_pem(key))
        (tmp_path / f"{name}.key").chmod(0o600)
    (tmp_path / "root.crt").write_bytes(pem(root[0]))
    crl = tmp_path / "crl.pem"
    crl.write_bytes(crl_for(issuer))
    for name, file in {
        "TLS_CERT_PATH": "worker.crt",
        "TLS_KEY_PATH": "worker.key",
        "TLS_CLIENT_CA_CERT_PATH": "root.crt",
        "TLS_CLIENT_CRL_PATH": "crl.pem",
    }.items():
        monkeypatch.setenv(name, str(tmp_path / file))

    class Receiver(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.calls += 1
            try:
                identity = authenticated_peer(self)
            except PeerAuthenticationError:
                self.send_response(401)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"subject": identity.subject}).encode())

        def log_message(self, *_args):
            pass

    # AF_UNIX paths are limited to roughly 108 bytes, even on deep CI checkouts.
    runtime_directory = tempfile.TemporaryDirectory(prefix="vita-nginx-", dir="/tmp")
    runtime = Path(runtime_directory.name)
    sock = runtime / "inference.sock"
    receiver = socketserver.UnixStreamServer(str(sock), Receiver)
    sock.chmod(0o600)
    receiver.calls = 0
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    configuration = tmp_path / "nginx.conf"
    render_nginx(configuration)
    # Isolate only paths/port; retain production TLS, header, and Unix-socket rules.
    configuration.write_text(
        configuration.read_text()
        .replace("/run/vita-fl", str(runtime))
        .replace("listen 8443 ssl;", f"listen 127.0.0.1:{port} ssl;")
    )
    error_log = (tmp_path / "nginx.log").open("wb")
    nginx = subprocess.Popen(
        ["nginx", "-p", str(runtime), "-c", str(configuration), "-e", str(tmp_path / "early.log"), "-g", "daemon off;"],
        stdout=error_log,
        stderr=error_log,
    )

    def request(client=None, headers=None):
        context = ssl.create_default_context(cafile=str(tmp_path / "root.crt"))
        if client:
            context.load_cert_chain(str(tmp_path / f"{client}.crt"), str(tmp_path / f"{client}.key"))
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context),
        )
        return opener.open(
            urllib.request.Request(f"https://localhost:{port}/", headers=headers or {}),
            timeout=3,
        )

    try:
        for _ in range(100):
            if nginx.poll() is not None:
                pytest.fail("nginx startup failed: " + (tmp_path / "nginx.log").read_text())
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.02)
        assert os.stat(sock).st_mode & 0o777 == 0o600
        assert os.stat(runtime).st_mode & 0o777 == 0o700
        spoof = {
            "X-Vita-Mtls-Verify": "SUCCESS",
            "X-Vita-Client-Cert": urllib.parse.quote(pem(other[0]).decode()),
        }
        # Forged authentication headers cannot bypass mandatory TLS certificates.
        with pytest.raises((urllib.error.URLError, ssl.SSLError)):
            request(headers=spoof)
        assert receiver.calls == 0
        with pytest.raises((urllib.error.URLError, ssl.SSLError)):
            request("rogue", spoof)
        assert receiver.calls == 0
        with request("agent", spoof) as response:
            assert json.load(response) == {"subject": "agent-42"}
        # A chain-valid non-agent certificate is rejected by the private receiver.
        with pytest.raises(urllib.error.HTTPError) as wrong_role_error:
            request("wrong-role", spoof)
        assert wrong_role_error.value.code == 401
        # Per-request CRL validation revokes access without needing a proxy reload.
        crl.write_bytes(crl_for(issuer, revoked=[agent[0].serial_number], number=2))
        with pytest.raises(urllib.error.HTTPError) as revoked:
            request("agent", spoof)
        assert revoked.value.code == 401
    finally:
        nginx.terminate()
        nginx.wait(timeout=10)
        error_log.close()
        receiver.shutdown()
        receiver.server_close()
        thread.join(timeout=5)
        runtime_directory.cleanup()

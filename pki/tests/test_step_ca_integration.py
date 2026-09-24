"""Runs against disposable loopback step-ca when both official binaries exist."""

import argparse
import os
import shutil
import socket
import subprocess
import time

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import ExtendedKeyUsageOID

from pki.operator import initialize, issue_token, revoke
from pki.runtime import _enroll, _refresh_crl, _renew, prepare


@pytest.mark.skipif(
    not shutil.which("step") or not shutil.which("step-ca"), reason="requires step and step-ca binaries"
)
def test_real_ca_one_use_renewal_and_active_revocation(tmp_path, monkeypatch):
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = f"https://localhost:{port}"
    password = tmp_path / "password"
    password.write_text("temporary-integration-test-password")
    password.chmod(0o600)
    offline, online = tmp_path / "offline", tmp_path / "online"
    initialize(
        argparse.Namespace(
            offline=offline,
            online=online,
            password_file=password,
            ca_url=url,
            address=f"127.0.0.1:{port}",
        )
    )
    assert not (online / "secrets/root_ca_key").exists()
    log = (tmp_path / "ca.log").open("wb")
    ca = subprocess.Popen(
        [
            "step-ca",
            str(online / "config/ca.json"),
            "--password-file",
            str(password),
        ],
        stdout=log,
        stderr=log,
    )
    try:
        for _ in range(100):
            if ca.poll() is not None:
                pytest.fail("Disposable step-ca did not start")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.1)
        monkeypatch.setenv("STEPPATH", str(offline))
        root = online / "certs/root_ca.crt"
        token_file = tmp_path / "enrollment.jwt"
        issue_token(
            argparse.Namespace(
                role="agent",
                subject="master-thesis-agent",
                dns=None,
                csr=None,
                password_file=password,
                ca_url=url,
                root=root,
                output=token_file,
            )
        )
        token = token_file.read_text().strip()
        state = tmp_path / "agent"
        monkeypatch.setenv("PKI_STATE_DIR", str(state))
        monkeypatch.setenv("PKI_CA_URL", url)
        monkeypatch.setenv(
            "PKI_ROOT_FINGERPRINT", x509.load_pem_x509_certificate(root.read_bytes()).fingerprint(hashes.SHA256()).hex()
        )
        monkeypatch.setenv("PKI_SUBJECT", "master-thesis-agent")
        monkeypatch.setenv("PKI_ENROLLMENT_TOKEN", token)
        prepare("agent")
        first = x509.load_pem_x509_certificate((state / "current/tls.crt").read_bytes())
        assert set(first.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == {
            ExtendedKeyUsageOID.CLIENT_AUTH,
        }
        key_before = (state / "current/tls.key").read_bytes()
        # Reboot succeeds with no token. The private key stays local and stable.
        prepare("agent")
        assert "PKI_ENROLLMENT_TOKEN" not in os.environ
        replay = tmp_path / "replay"
        replay.mkdir()
        shutil.copy2(root, replay / "root_ca.crt")
        monkeypatch.setenv("PKI_ENROLLMENT_TOKEN", token)
        with pytest.raises(Exception):
            _enroll(replay, "agent")
        _renew(state, "agent")
        renewed = x509.load_pem_x509_certificate((state / "current/tls.crt").read_bytes())
        assert renewed.serial_number != first.serial_number
        assert (state / "current/tls.key").read_bytes() == key_before
        # Exercise the actual operator command; the enrollment/renewal and
        # revocation credentials never carry the leaf private key to the CA.
        revoke(
            argparse.Namespace(
                role="agent",
                serial=str(renewed.serial_number),
                password_file=password,
                ca_url=url,
                root=root,
            )
        )
        with pytest.raises(ValueError, match="revoked"):
            _refresh_crl(state)
        with pytest.raises(RuntimeError):
            _renew(state, "agent")
        # The worker has an explicit DNS identity and needs clientAuth only for
        # certificate-authenticated renewal. Its URI cannot authorize agent use.
        issue_token(
            argparse.Namespace(
                role="worker",
                subject="worker-0",
                dns="localhost",
                csr=None,
                password_file=password,
                ca_url=url,
                root=root,
                output=token_file,
            )
        )
        worker_state = tmp_path / "worker"
        monkeypatch.setenv("PKI_STATE_DIR", str(worker_state))
        monkeypatch.setenv("PKI_SUBJECT", "worker-0")
        monkeypatch.setenv("PKI_DNS_NAME", "localhost")
        monkeypatch.setenv("PKI_ENROLLMENT_TOKEN", token_file.read_text().strip())
        prepare("worker")
        worker = x509.load_pem_x509_certificate((worker_state / "current/tls.crt").read_bytes())
        assert set(worker.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) == {
            ExtendedKeyUsageOID.SERVER_AUTH,
            ExtendedKeyUsageOID.CLIENT_AUTH,
        }
        names = worker.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        assert names.get_values_for_type(x509.DNSName) == ["localhost"]
        assert names.get_values_for_type(x509.UniformResourceIdentifier) == ["urn:vita-fl:inference:worker-0"]
        worker_key = (worker_state / "current/tls.key").read_bytes()
        _renew(worker_state, "worker")
        assert (worker_state / "current/tls.key").read_bytes() == worker_key
    finally:
        ca.terminate()
        ca.wait(timeout=10)
        log.close()

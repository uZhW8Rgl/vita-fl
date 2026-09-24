"""Local-key enrollment, pinned CA, renewal and fail-closed mTLS supervision.

Run with ``python -m pki.runtime run worker|agent -- PROGRAM ...``.
The operator issues a scoped one-use step-ca token; only a CSR leaves this host.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def atomic_write(path: Path, data: bytes, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _run(command):
    # Commands contain filenames/public parameters only. Never include enrollment
    # tokens in argv or propagate process output that could disclose credentials.
    result = subprocess.run(command, capture_output=True, check=False, timeout=60)
    if result.returncode:
        raise RuntimeError(f"{Path(command[0]).name} certificate operation failed")
    return result.stdout


def _identity(role: str):
    subject = os.environ.get("PKI_SUBJECT", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", subject):
        raise ValueError("PKI_SUBJECT must be an explicit agent or worker identifier")
    identity = f"urn:vita-fl:{'agent' if role == 'agent' else 'inference'}:{subject}"
    names = [x509.UniformResourceIdentifier(identity)]
    if role == "worker":
        dns = os.environ.get("PKI_DNS_NAME", "")
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", dns):
            raise ValueError("PKI_DNS_NAME must name the receiver TLS passthrough endpoint")
        names.append(x509.DNSName(dns))
    return subject, names


def _ca_url():
    value = os.environ.get("PKI_CA_URL", "").rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("PKI_CA_URL must be an authenticated HTTPS URL")
    if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("PKI_CA_URL must be an HTTPS origin")
    return value


def _root(state: Path):
    fingerprint = os.environ.get("PKI_ROOT_FINGERPRINT", "").lower().replace(":", "")
    if not re.fullmatch(r"[a-f0-9]{64}", fingerprint):
        raise ValueError("PKI_ROOT_FINGERPRINT must pin a SHA-256 root-certificate fingerprint")
    root = state / "root_ca.crt"
    if not root.exists():
        staged = state / "root_ca.crt.pending"
        _run(["step", "ca", "root", str(staged), "--ca-url", _ca_url(), "--fingerprint", fingerprint, "--force"])
        data = staged.read_bytes()
        staged.unlink()
        certificate = x509.load_pem_x509_certificate(data)
        if certificate.fingerprint(hashes.SHA256()).hex() != fingerprint:
            raise ValueError("CA root fingerprint mismatch")
        atomic_write(root, data, 0o644)
    certificate = x509.load_pem_x509_certificate(root.read_bytes())
    if certificate.fingerprint(hashes.SHA256()).hex() != fingerprint:
        raise ValueError("Persisted CA root does not match configured trust anchor")
    return root


def _ca_request(state: Path, path: str, payload=None, *, authenticated=False):
    context = ssl.create_default_context(cafile=str(state / "root_ca.crt"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if authenticated:
        context.load_cert_chain(str(state / "current/tls.crt"), str(state / "current/tls.key"))
    body = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(
        _ca_url() + path,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    from transport_security.client import _NoRedirect

    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=context),
        _NoRedirect(),
    )
    with opener.open(request, timeout=30) as response:
        return response.read(1024 * 1024)


def _validate_pair(certificate_pem: bytes, key_pem: bytes, root: Path, role: str):
    chain = x509.load_pem_x509_certificates(certificate_pem)
    if not chain:
        raise ValueError("CA returned no certificate")
    leaf = chain[0]
    key = serialization.load_pem_private_key(key_pem, password=None)

    def public_bytes(public_key):
        return public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)

    if public_bytes(leaf.public_key()) != public_bytes(key.public_key()):
        raise ValueError("Issued certificate does not match the locally generated key")
    _, names = _identity(role)
    actual = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if set(actual) != set(names):
        raise ValueError("Issued certificate has unexpected identity or DNS names")
    now = datetime.now(timezone.utc)
    if not leaf.not_valid_before_utc <= now < leaf.not_valid_after_utc:
        raise ValueError("Issued certificate is outside its validity period")
    if leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("CA issued a CA certificate where a leaf was required")
    usages = leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    wanted = ExtendedKeyUsageOID.CLIENT_AUTH if role == "agent" else ExtendedKeyUsageOID.SERVER_AUTH
    if wanted not in usages:
        raise ValueError("Issued certificate lacks its required TLS purpose")
    with tempfile.TemporaryDirectory(prefix="vita-cert-check-", dir=root.parent) as directory:
        path = Path(directory)
        (path / "leaf.pem").write_bytes(leaf.public_bytes(serialization.Encoding.PEM))
        (path / "chain.pem").write_bytes(certificate_pem)
        _run(["openssl", "verify", "-CAfile", str(root), "-untrusted", str(path / "chain.pem"), str(path / "leaf.pem")])
    return leaf


def _install(state: Path, certificate: bytes, key: bytes, role: str):
    _validate_pair(certificate, key, state / "root_ca.crt", role)
    generation = Path(tempfile.mkdtemp(prefix="generation-", dir=state))
    try:
        atomic_write(generation / "tls.crt", certificate, 0o644)
        atomic_write(generation / "tls.key", key, 0o600)
        link = state / ".current-next"
        link.unlink(missing_ok=True)
        link.symlink_to(generation.name, target_is_directory=True)
        os.replace(link, state / "current")
    except BaseException:
        shutil.rmtree(generation)
        raise


def _enroll(state: Path, role: str):
    token = os.environ.pop("PKI_ENROLLMENT_TOKEN", "").strip()
    token_path = os.environ.pop("PKI_ENROLLMENT_TOKEN_FILE", "").strip()
    if token_path:
        if token:
            raise ValueError("Provide one enrollment-token source")
        token = Path(token_path).read_text().strip()
    if not token:
        raise ValueError("A fresh operator-issued one-use enrollment token is required")
    # Keep pending material across interrupted enrollment. The private key is
    # generated locally; neither sign request nor logs contain its bytes.
    pending_key = state / "pending.key"
    if pending_key.exists():
        key = serialization.load_pem_private_key(pending_key.read_bytes(), password=None)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        atomic_write(
            pending_key,
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            ),
        )
    subject, names = _identity(role)
    csr = (
        x509.CertificateSigningRequestBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )
    csr_pem = csr.public_bytes(serialization.Encoding.PEM)
    atomic_write(state / "identity.csr", csr_pem, 0o644)
    result = json.loads(_ca_request(state, "/1.0/sign", {"csr": csr_pem.decode(), "ott": token}))
    chain = result.get("certChain") or [result.get("crt", ""), result.get("ca", "")]
    if not isinstance(chain, list) or not all(isinstance(item, str) for item in chain):
        raise ValueError("Invalid step-ca sign response")
    _install(state, "\n".join(chain).encode(), pending_key.read_bytes(), role)
    pending_key.unlink()


def _renew(state: Path, role: str):
    output = state / "renewed.crt.pending"
    _run(
        [
            "step",
            "ca",
            "renew",
            str(state / "current/tls.crt"),
            str(state / "current/tls.key"),
            "--ca-url",
            _ca_url(),
            "--root",
            str(state / "root_ca.crt"),
            "--out",
            str(output),
            "--force",
        ]
    )
    try:
        _install(state, output.read_bytes(), (state / "current/tls.key").read_bytes(), role)
    finally:
        output.unlink(missing_ok=True)


def validate_crl(raw: bytes, chain_pem: bytes, *, previous: bytes | None = None):
    crl = x509.load_pem_x509_crl(raw) if raw.startswith(b"-----BEGIN") else x509.load_der_x509_crl(raw)
    issuers = [cert for cert in x509.load_pem_x509_certificates(chain_pem) if cert.subject == crl.issuer]
    if len(issuers) != 1 or not crl.is_signature_valid(issuers[0].public_key()):
        raise ValueError("CRL signature is not from the pinned certificate issuer")
    now = datetime.now(timezone.utc)
    if crl.next_update_utc is None or not crl.last_update_utc <= now < crl.next_update_utc:
        raise ValueError("CRL is stale or not yet valid")
    if previous:
        old = x509.load_pem_x509_crl(previous)
        if crl.last_update_utc < old.last_update_utc:
            raise ValueError("CRL rollback detected")
        try:
            old_number = old.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number
            new_number = crl.extensions.get_extension_for_class(x509.CRLNumber).value.crl_number
            if new_number < old_number:
                raise ValueError("CRL sequence rollback detected")
        except x509.ExtensionNotFound:
            pass
    return crl


def _refresh_crl(state: Path):
    raw = _ca_request(state, "/1.0/crl")
    path = state / "crl.pem"
    chain_pem = (state / "current/tls.crt").read_bytes()
    chain_pem += (state / "root_ca.crt").read_bytes()
    crl = validate_crl(raw, chain_pem, previous=path.read_bytes() if path.exists() else None)
    leaf = x509.load_pem_x509_certificate((state / "current/tls.crt").read_bytes())
    if crl.get_revoked_certificate_by_serial_number(leaf.serial_number) is not None:
        raise ValueError("This endpoint certificate has been revoked")
    atomic_write(path, crl.public_bytes(serialization.Encoding.PEM), 0o644)


def prepare(role: str) -> Path:
    if role not in ("agent", "worker"):
        raise ValueError("Unknown PKI role")
    os.umask(0o077)
    _identity(role)
    state = Path(
        os.environ.get("PKI_STATE_DIR", "/var/lib/vita-fl/tls" if role == "worker" else "/var/lib/vita-fl-agent-tls")
    )
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    _root(state)
    if not (state / "current").exists():
        _enroll(state, role)
    else:
        # A valid stored identity survives restart without replaying a spent token.
        _validate_pair(
            (state / "current/tls.crt").read_bytes(),
            (state / "current/tls.key").read_bytes(),
            state / "root_ca.crt",
            role,
        )
    os.environ.pop("PKI_ENROLLMENT_TOKEN", None)
    os.environ.pop("PKI_ENROLLMENT_TOKEN_FILE", None)
    _refresh_crl(state)
    os.environ.update(
        {
            "TLS_CERT_PATH": str(state / "current/tls.crt"),
            "TLS_KEY_PATH": str(state / "current/tls.key"),
            "TLS_CA_CERT_PATH": str(state / "root_ca.crt"),
            "TLS_CRL_PATH": str(state / "crl.pem"),
            "TLS_CLIENT_CA_CERT_PATH": str(state / "root_ca.crt"),
            "TLS_CLIENT_CRL_PATH": str(state / "crl.pem"),
        }
    )
    return state


def render_nginx(output: Path):
    if os.environ.get("TEE_TRANSPORT_MODE", "mtls").lower() == "ratls":
        from pki.ratls_runtime import render_nginx as render_ratls

        return render_ratls(output)
    source = Path(__file__).with_name("nginx-inference.conf.template").read_text()
    for name in ("TLS_CERT_PATH", "TLS_KEY_PATH", "TLS_CLIENT_CA_CERT_PATH", "TLS_CLIENT_CRL_PATH"):
        value = os.environ.get(name, "")
        if not value.startswith("/") or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
            raise ValueError(f"{name} must be a safe absolute file path")
        source = source.replace(f"__{name}__", value)
    atomic_write(output, source.encode())


def supervise(role: str, command: list[str]):
    if not command:
        raise ValueError("A supervised application command is required")
    mode = os.environ.get("TEE_TRANSPORT_MODE", "mtls").strip().lower()
    if mode not in {"mtls", "ratls"}:
        raise ValueError("Unknown TEE_TRANSPORT_MODE")
    if mode == "ratls" and role == "worker":
        from pki.ratls_runtime import supervise as supervise_ratls

        return supervise_ratls(command)
    zk_enabled = os.environ.get("ZK_INFERENCE_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}
    needs_client_pki = zk_enabled and bool(os.environ.get("ZK_INFERENCE_URL", "").strip())
    if mode == "ratls" and role == "agent" and not needs_client_pki:
        from transport_security.pop import capture_pop_identity

        capture_pop_identity()
        os.execvp(command[0], command)
    state = prepare(role)
    children = []
    stopping = False

    def stop(_signal=None, _frame=None):
        nonlocal stopping
        stopping = True
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    configuration = state / "nginx.conf"
    try:
        if role == "worker":
            runtime = Path("/run/vita-fl")
            runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
            runtime.chmod(0o700)
            render_nginx(configuration)
            _run(["nginx", "-t", "-c", str(configuration)])
            children.append(
                subprocess.Popen(["nginx", "-c", str(configuration), "-g", "daemon off;"], start_new_session=True)
            )
        children.append(subprocess.Popen(command, start_new_session=True))
        next_check = time.monotonic() + 30
        while not stopping:
            for child in children:
                if child.poll() is not None:
                    return child.returncode
            if time.monotonic() >= next_check:
                # Any renewal/CRL error closes the receiver rather than silently
                # extending revoked or stale authorization during a CA outage.
                _refresh_crl(state)
                leaf = x509.load_pem_x509_certificate((state / "current/tls.crt").read_bytes())
                lifespan = leaf.not_valid_after_utc - leaf.not_valid_before_utc
                if datetime.now(timezone.utc) >= leaf.not_valid_before_utc + lifespan * (2 / 3):
                    _renew(state, role)
                if role == "worker":
                    children[0].send_signal(signal.SIGHUP)
                next_check = time.monotonic() + 30
            time.sleep(0.25)
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="operation", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("role", choices=("worker", "agent"))
    run.add_argument("command", nargs=argparse.REMAINDER)
    render = subparsers.add_parser("render-nginx")
    render.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.operation == "render-nginx":
        render_nginx(args.output)
        return 0
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    return supervise(args.role, command)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        # Avoid logging an HTTP error body, command, token or key material.
        print(f"Certificate lifecycle stopped: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)

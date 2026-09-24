"""Ephemeral TLS keys authenticated by fresh, channel-bound TDX evidence.

The self-signed certificate carries the TLS key. The public challenge exchange
authenticates it before protected HTTP data; keys are never enrolled with a CA.
"""

from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def prepare() -> Path:
    from pki.runtime import atomic_write
    from transport_security.pop import load_agent_registry

    load_agent_registry()
    origin = os.environ.get("TEE_INFERENCE_ORIGIN", "")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise ValueError("TEE_INFERENCE_ORIGIN must be an HTTPS origin")
    os.umask(0o077)
    state = Path(os.environ.get("RATLS_STATE_DIR", "/run/vita-fl/ratls"))
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    state.chmod(0o700)
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "VITA-FL attested inference")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(parsed.hostname)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(key, hashes.SHA256())
    )
    atomic_write(
        state / "tls.key",
        key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()),
    )
    atomic_write(state / "tls.crt", certificate.public_bytes(serialization.Encoding.PEM), 0o644)
    os.environ.update(TLS_CERT_PATH=str(state / "tls.crt"), TLS_KEY_PATH=str(state / "tls.key"))
    return state


def render_nginx(output: Path) -> None:
    from pki.runtime import atomic_write

    source = Path(__file__).with_name("nginx-ratls.conf.template").read_text()
    for name in ("TLS_CERT_PATH", "TLS_KEY_PATH"):
        value = os.environ.get(name, "")
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
            raise ValueError(f"{name} must be a safe absolute file path")
        source = source.replace(f"__{name}__", value)
    certificate = x509.load_pem_x509_certificate(Path(os.environ["TLS_CERT_PATH"]).read_bytes())
    spki = certificate.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    source = source.replace("__TLS_SPKI_SHA256__", hashlib.sha256(spki).hexdigest())
    atomic_write(output, source.encode())


def supervise(command: list[str]) -> int:
    from pki.runtime import _run

    if not command:
        raise ValueError("A supervised application command is required")
    state = prepare()
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
    try:
        runtime = Path("/run/vita-fl")
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime.chmod(0o700)
        configuration = state / "nginx.conf"
        render_nginx(configuration)
        _run(["nginx", "-t", "-c", str(configuration)])
        children.append(subprocess.Popen(command, start_new_session=True))
        children.append(
            subprocess.Popen(["nginx", "-c", str(configuration), "-g", "daemon off;"], start_new_session=True)
        )
        # Restart the receiver and its session store together before expiry.
        deadline = time.monotonic() + 23 * 3600
        while not stopping:
            for child in children:
                if child.poll() is not None:
                    return child.returncode or 1
            if time.monotonic() >= deadline:
                return 1  # Container restart creates the next key generation.
            time.sleep(0.25)
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        (state / "tls.key").unlink(missing_ok=True)
    return 0

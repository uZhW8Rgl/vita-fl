"""Ephemeral TLS keys authenticated by fresh, channel-bound TDX evidence.

The self-signed certificate carries the TLS key. The public challenge exchange
authenticates it before protected HTTP data; keys are never enrolled with a CA.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def _dns_name(value: str, name: str, *, domain: bool = False) -> str:
    labels = value.split(".")
    if (
        not value
        or len(value) > 253
        or (domain and len(labels) < 2)
        or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in labels)
    ):
        raise ValueError(f"{name} must contain a valid DNS name")
    return value.lower()


def _https_origin(value: str, name: str) -> str:
    if not isinstance(value, str) or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError(f"{name} must be an HTTPS origin")
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or "?" in value
            or "#" in value
            or parsed.path not in ("", "/")
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError()
        hostname = _dns_name(parsed.hostname, name)
    except ValueError as exc:
        raise ValueError(f"{name} must be an HTTPS origin with a valid DNS hostname") from exc
    # Match Node's URL.origin: one public origin is shared by registration,
    # TLS/session claims and request-proof verification.
    authority = hostname if port in (None, 443) else f"{hostname}:{port}"
    return f"https://{authority}"


def resolve_origin(*, dstack_client=None) -> str:
    """Resolve only from explicit measured configuration and local dstack Info."""
    configured = os.environ.get("TEE_INFERENCE_ORIGIN", "")
    if configured:
        return _https_origin(configured, "TEE_INFERENCE_ORIGIN")

    explicit_gateway = os.environ.get("DSTACK_GATEWAY_DOMAIN", "")
    if explicit_gateway:
        gateway = _dns_name(explicit_gateway.strip().removeprefix("."), "DSTACK_GATEWAY_DOMAIN", domain=True)
    else:
        kubo = _https_origin(os.environ.get("KUBO_API", ""), "KUBO_API")
        parsed = urlsplit(kubo)
        host, separator, gateway = parsed.hostname.partition(".")
        service = re.fullmatch(r"[A-Za-z0-9-]+-([0-9]{1,5})s?", host)
        if (
            not separator
            or parsed.port is not None
            or service is None
            or not 1 <= int(service[1]) <= 65535
            or not (gateway == "phala.network" or gateway.endswith(".phala.network"))
        ):
            raise ValueError("KUBO_API must identify an HTTPS Phala gateway endpoint")
        gateway = _dns_name(gateway, "KUBO_API gateway", domain=True)

    if dstack_client is None:
        from tee_inference.service.attestation import DstackClient

        dstack_client = DstackClient()
    info = dstack_client.call("/Info", {})
    app_id = info.get("app_id") if isinstance(info, dict) else None
    if not isinstance(app_id, str) or not re.fullmatch(r"(?:app_)?[0-9a-fA-F]{40}", app_id):
        raise ValueError("dstack /Info must provide a 40-character hexadecimal app_id")
    app_id = app_id.removeprefix("app_").lower()
    return _https_origin(f"https://{app_id}-8443s.{gateway}", "Derived TEE_INFERENCE_ORIGIN")


def prepare() -> Path:
    from pki.runtime import atomic_write
    from transport_security.pop import load_agent_registry

    load_agent_registry()
    origin = resolve_origin()
    os.environ["TEE_INFERENCE_ORIGIN"] = origin
    parsed = urlsplit(origin)
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("origin",))
    parser.parse_args()
    try:
        print(resolve_origin())
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"RA-TLS origin resolution failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

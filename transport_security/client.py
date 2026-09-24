"""HTTPS-only inference requests with pinned CA, client certificate and CRL."""

from __future__ import annotations

import os
import ssl
import stat
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509

from .peer import certificate_thumbprint


def _required_file(name: str, *, private: bool = False) -> Path:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required for authenticated inference transport")
    path = Path(value)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"{name} must point to a regular file")
    if private and metadata.st_mode & 0o077:
        raise ValueError(f"{name} must not be readable by group or others")
    return path


@dataclass(frozen=True)
class ClientIdentity:
    """One immutable certificate generation, shared by token and TLS requests."""

    certificate_path: Path
    key_path: Path
    thumbprint: str


def capture_client_identity() -> ClientIdentity:
    certificate = _required_file("TLS_CERT_PATH")
    configured_key = _required_file("TLS_KEY_PATH", private=True)
    # Resolve the atomic ``current`` symlink exactly once. Renewals retain old
    # generation directories so a request and its receipt download can finish
    # using the certificate to which their authorization token is bound.
    resolved_certificate = certificate.resolve(strict=True)
    key = (
        resolved_certificate.parent / configured_key.name
        if certificate.parent == configured_key.parent
        else configured_key.resolve(strict=True)
    )
    metadata = key.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
        raise ValueError("TLS_KEY_PATH must point to a private regular file")
    leaf = x509.load_pem_x509_certificate(resolved_certificate.read_bytes())
    return ClientIdentity(resolved_certificate, key, certificate_thumbprint(leaf))


def tls_context(identity: ClientIdentity | None = None) -> ssl.SSLContext:
    identity = identity or capture_client_identity()
    authority = _required_file("TLS_CA_CERT_PATH")
    revocations = _required_file("TLS_CRL_PATH")
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=str(authority))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cafile=str(revocations))
    context.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
    context.load_cert_chain(str(identity.certificate_path), str(identity.key_path))
    return context


def validate_client_configuration() -> None:
    tls_context()


def cert_thumbprint() -> str:
    certificate = x509.load_pem_x509_certificate(_required_file("TLS_CERT_PATH").read_bytes())
    return certificate_thumbprint(certificate)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Inference redirects are forbidden; use the attested HTTPS endpoint")


def open_receiver(request, timeout=30, *, identity=None):
    from .pop import PoPIdentity

    if isinstance(identity, PoPIdentity):
        from .ratls_client import open_attested

        return open_attested(request, timeout, identity)
    url = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Inference receiver must use an authenticated HTTPS URL")
    if parsed.fragment:
        raise ValueError("Inference URL must not contain a fragment")
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=tls_context(identity)),
        _NoRedirect(),
    )
    return opener.open(request, timeout=timeout)

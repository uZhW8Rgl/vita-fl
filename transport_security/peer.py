"""Read identity supplied by the mandatory, same-container mTLS proxy.

This module is only safe behind nginx on the private Unix socket. Never bind
the application to TCP: public clients must not be able to supply these headers.
nginx verifies the chain; this module checks role and active leaf revocation on
every request. This avoids an nginx CRL_CHECK_ALL dependency on an offline-root CRL.
"""

import base64
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID


class PeerAuthenticationError(ValueError):
    """No verified, currently valid agent certificate accompanied the request."""


@dataclass(frozen=True)
class AuthenticatedPeer:
    subject: str
    fingerprint: str
    role: str = "agent"


def certificate_thumbprint(certificate: x509.Certificate) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(certificate.public_bytes(Encoding.DER)).digest())
        .decode("ascii")
        .rstrip("=")
    )


def authenticated_peer(request) -> AuthenticatedPeer:
    headers = request.headers
    if headers.get("x-vita-mtls-verify") != "SUCCESS":
        raise PeerAuthenticationError("A verified mTLS agent certificate is required")
    encoded = headers.get("x-vita-client-cert", "")
    if not isinstance(encoded, str) or not encoded or len(encoded) > 16384:
        raise PeerAuthenticationError("Missing or invalid client certificate")
    try:
        pem = unquote(encoded, errors="strict").encode("ascii")
        if pem.count(b"-----BEGIN CERTIFICATE-----") != 1:
            raise ValueError("Expected a single leaf certificate")
        certificate = x509.load_pem_x509_certificate(pem)
        now = datetime.now(timezone.utc)
        if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc:
            raise ValueError("Client certificate is outside its validity period")
        if certificate.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
            raise ValueError("A CA certificate cannot act as an agent")
        usage = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        if ExtendedKeyUsageOID.CLIENT_AUTH not in usage:
            raise ValueError("Certificate is not authorized for TLS client authentication")
        uris = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(
            x509.UniformResourceIdentifier
        )
        identities = [uri for uri in uris if uri.startswith("urn:vita-fl:")]
        if len(identities) != 1:
            raise ValueError("Exactly one VITA-FL role identity is required")
        match = re.fullmatch(r"urn:vita-fl:agent:([A-Za-z0-9][A-Za-z0-9_.-]{0,127})", identities[0])
        if not match:
            raise ValueError("Certificate does not identify an agent")
        from pki.runtime import validate_crl

        crl_path = os.environ.get("TLS_CLIENT_CRL_PATH", "")
        chain_path = os.environ.get("TLS_CERT_PATH", "")
        root_path = os.environ.get("TLS_CLIENT_CA_CERT_PATH", "")
        if not crl_path or not chain_path or not root_path:
            raise ValueError("Receiver CRL trust configuration is required")
        issuer_chain = Path(chain_path).read_bytes() + Path(root_path).read_bytes()
        crl = validate_crl(Path(crl_path).read_bytes(), issuer_chain)
        if certificate.issuer != crl.issuer:
            raise ValueError("The client certificate issuer has no configured revocation list")
        if crl.get_revoked_certificate_by_serial_number(certificate.serial_number) is not None:
            raise ValueError("Agent certificate has been revoked")
    except (ValueError, OSError, UnicodeError, x509.ExtensionNotFound) as exc:
        raise PeerAuthenticationError("Invalid mTLS agent certificate") from exc
    return AuthenticatedPeer(match.group(1), certificate_thumbprint(certificate))

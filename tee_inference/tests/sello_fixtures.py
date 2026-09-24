"""Real cryptographic identities for HTTP authorization tests."""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from nacl.signing import SigningKey

from agent_receipts.sello_v1 import SelloOwner, SelloReceiver
from transport_security.peer import certificate_thumbprint


class AuthenticatedServiceTestCase(unittest.TestCase):
    origin = "https://worker0.example"
    subject = "test-agent"
    scopes = [
        "fetch_latest_verified_tee_model_bundle",
        "generate_random_tee_chestmnist_image",
        "run_and_verify_tee_inference",
        "jobs:read",
        "receipts:read",
    ]

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.ca_key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(timezone.utc)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-only-root")])
        self.ca = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .sign(self.ca_key, hashes.SHA256())
        )
        ca_path = self.directory / "root.pem"
        ca_path.write_bytes(self.ca.public_bytes(serialization.Encoding.PEM))
        self.certificate = self.agent_certificate(self.subject)
        certificate_path = self.directory / "leaf.pem"
        certificate_path.write_bytes(self.certificate.public_bytes(serialization.Encoding.PEM))
        self.crl_path = self.directory / "crl.pem"
        self.write_crl()
        self.issuer = SigningKey(bytes.fromhex("21" * 32))
        self.receiver = SelloReceiver("tee-inference", SigningKey(bytes.fromhex("22" * 32)), self.issuer.verify_key)
        self.owner = SelloOwner(
            self.issuer,
            bytes.fromhex("23" * 32),
            {"tee-inference": self.receiver.public_key},
            subject=self.subject,
            log_urls=["https://scitt.example"],
        )
        self.addCleanup(patch.stopall)
        patch.dict(
            os.environ,
            {
                "TEE_INFERENCE_ORIGIN": self.origin,
                "TLS_CLIENT_CA_CERT_PATH": str(ca_path),
                "TLS_CA_CERT_PATH": str(ca_path),
                "TLS_CERT_PATH": str(certificate_path),
                "TLS_CLIENT_CRL_PATH": str(self.crl_path),
            },
            clear=False,
        ).start()
        patch("tee_inference.service.app.receiver_from_environment", return_value=self.receiver).start()
        self.publish = patch(
            "tee_inference.service.app.ReceiverTransparencyPublisher.publish",
            return_value=("a" * 64, {"transaction_id": "1.1"}),
        ).start()

    def write_crl(self, revoked_certificate=None) -> None:
        now = datetime.now(timezone.utc)
        builder = (
            x509.CertificateRevocationListBuilder()
            .issuer_name(self.ca.subject)
            .last_update(now - timedelta(minutes=1))
            .next_update(now + timedelta(hours=1))
        )
        if revoked_certificate is not None:
            builder = builder.add_revoked_certificate(
                x509.RevokedCertificateBuilder()
                .serial_number(revoked_certificate.serial_number)
                .revocation_date(now - timedelta(minutes=1))
                .build()
            )
        self.crl_path.write_bytes(builder.sign(self.ca_key, hashes.SHA256()).public_bytes(serialization.Encoding.PEM))

    def agent_certificate(self, subject: str) -> x509.Certificate:
        now = datetime.now(timezone.utc)
        key = ec.generate_private_key(ec.SECP256R1())
        return (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, subject)]))
            .issuer_name(self.ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(
                x509.SubjectAlternativeName([x509.UniformResourceIdentifier(f"urn:vita-fl:agent:{subject}")]),
                critical=False,
            )
            .sign(self.ca_key, hashes.SHA256())
        )

    def authorization_headers(self, *, token: str | None = None, certificate=None) -> dict[str, str]:
        certificate = certificate or self.certificate
        if token is None:
            token = self.owner.token(
                audience=self.origin, cert_thumbprint=certificate_thumbprint(certificate), scopes=self.scopes
            )
        return {
            "Authorization": f"Bearer {token}",
            "x-vita-mtls-verify": "SUCCESS",
            "x-vita-client-cert": quote(certificate.public_bytes(serialization.Encoding.PEM).decode(), safe=""),
        }

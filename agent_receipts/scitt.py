"""SCITT registration and verification shared by receipt receivers and owners."""

from __future__ import annotations

import fcntl
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import cbor2

SCITT_CONTENT_TYPE = "application/vnd.master-thesis.tee-inference-evidence+cbor"
SCITT_ZK_CONTENT_TYPE = "application/vnd.master-thesis.zk-inference-proof+cbor"
SCITT_TOOL_RECEIPT_CONTENT_TYPE = "application/vnd.master-thesis.agent-tool-receipt+cbor"
SCITT_EKU = "2.999"


class ScittRegistrationError(RuntimeError):
    """SCITT registration or receipt verification failed."""


def _atomic_write(path: Path, value: bytes, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(value)
    if mode is not None:
        temporary.chmod(mode)
    temporary.replace(path)


def _signer_paths(directory: Path) -> dict[str, Path]:
    return {
        "root_key": directory / "root-key.pem",
        "root_cert": directory / "root-cert.pem",
        "leaf_key": directory / "leaf-key.pem",
        "leaf_cert": directory / "leaf-cert.pem",
    }


def _certificate_is_current(path: Path) -> bool:
    from cryptography import x509

    certificate = x509.load_pem_x509_certificate(path.read_bytes())
    return certificate.not_valid_after_utc > datetime.now(timezone.utc) + timedelta(hours=24)


def _load_or_create_signer(directory: Path, identity: str):
    from pyscitt import crypto

    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / ".identity.lock"
    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        paths = _signer_paths(directory)
        complete = all(path.is_file() for path in paths.values())
        if complete and _certificate_is_current(paths["leaf_cert"]):
            root_cert = paths["root_cert"].read_text(encoding="utf-8")
            leaf_cert = paths["leaf_cert"].read_text(encoding="utf-8")
            leaf_key = paths["leaf_key"].read_text(encoding="utf-8")
        else:
            root_key, _ = crypto.generate_keypair("ec")
            root_cert = crypto.generate_cert(root_key, ca=True, cn=f"{identity}-scitt-root")
            leaf_key, _ = crypto.generate_keypair("ec")
            leaf_cert = crypto.generate_cert(
                leaf_key,
                issuer=(root_cert, root_key),
                cn=identity,
                add_eku=SCITT_EKU,
            )
            _atomic_write(paths["root_key"], root_key.encode(), 0o600)
            _atomic_write(paths["root_cert"], root_cert.encode(), 0o644)
            _atomic_write(paths["leaf_key"], leaf_key.encode(), 0o600)
            _atomic_write(paths["leaf_cert"], leaf_cert.encode(), 0o644)

        fingerprint = crypto.get_cert_fingerprint_b64url(root_cert)
        issuer = f"did:x509:0:sha256:{fingerprint}::eku:{SCITT_EKU}"
        return crypto.Signer(
            leaf_key,
            issuer=issuer,
            algorithm="ES256",
            x5c=[leaf_cert, root_cert],
        )


def _sign_evidence(evidence: bytes, signer: Any, content_type: str) -> bytes:
    from pyscitt import crypto

    return crypto.sign_statement(signer, evidence, content_type=content_type, cwt=True)


def _submit_and_verify(url: str, development: bool, signed_statement: bytes):
    from pyscitt.client import Client
    from pyscitt.verify import StaticTrustStore, verify_transparent_statement

    client = Client(url, development=development)
    try:
        submission = client.submit_signed_statement_and_wait(signed_statement)
        trust_store = StaticTrustStore(cose_keys=client.get_scitt_keys())
        details = verify_transparent_statement(submission.response_bytes, trust_store, signed_statement)
    finally:
        client.session.close()
    if not details:
        raise ScittRegistrationError("transparent statement contains no verified SCITT receipt")
    return submission, details


def register_verified_evidence(
    evidence: bytes,
    *,
    url: str | None = None,
    development: bool | None = None,
    signer_dir: str | None = None,
    transparent_statement_path: str | None = None,
    content_type: str = SCITT_CONTENT_TYPE,
    identity: str = "master-thesis-agent",
) -> dict[str, Any]:
    """Submit evidence and return the bytes needed for independent verification."""

    if not evidence:
        raise ScittRegistrationError("cannot register an empty evidence bundle")
    if not content_type.startswith("application/vnd.master-thesis.") or not content_type.endswith("+cbor"):
        raise ScittRegistrationError("SCITT content type must be a master-thesis CBOR media type")
    service_url = (url or os.environ.get("SCITT_URL", "https://127.0.0.1:8000")).rstrip("/")
    if not service_url.startswith("https://"):
        raise ScittRegistrationError("SCITT_URL must use HTTPS")
    if development is None:
        development = os.environ.get("SCITT_DEVELOPMENT", "1").lower() in {"1", "true", "yes"}
    signer_path = signer_dir or os.environ.get("SCITT_SIGNER_DIR", "/tmp/scitt-signer")
    try:
        signer = _load_or_create_signer(Path(signer_path), identity)
        signed_statement = _sign_evidence(evidence, signer, content_type)
        submission, receipt_details = _submit_and_verify(service_url, development, signed_statement)
    except ScittRegistrationError:
        raise
    except Exception as exc:
        raise ScittRegistrationError(f"SCITT registration failed: {exc}") from exc

    transparent_statement = submission.response_bytes
    if transparent_statement_path:
        _atomic_write(Path(transparent_statement_path), transparent_statement, 0o644)
    return {
        "status": "registered-and-receipt-verified",
        "service_url": service_url,
        "development_tls": development,
        "content_type": content_type,
        "transaction_id": submission.tx,
        "evidence_sha256": hashlib.sha256(evidence).hexdigest(),
        "signed_statement_sha256": hashlib.sha256(signed_statement).hexdigest(),
        "transparent_statement_sha256": hashlib.sha256(transparent_statement).hexdigest(),
        "transparent_statement_bytes": len(transparent_statement),
        "transparent_statement_path": transparent_statement_path or "",
        "receipts": receipt_details,
        "_signed_statement": signed_statement,
        "_transparent_statement": transparent_statement,
    }


def public_registration(registration: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in registration.items() if not key.startswith("_")}


def encode_publication_bundle(registration: dict[str, Any]) -> bytes:
    return cbor2.dumps(
        {
            1: registration["_signed_statement"],
            2: registration["_transparent_statement"],
            3: public_registration(registration),
        },
        canonical=True,
    )


def verify_publication_bundle(evidence: bytes, bundle: bytes) -> dict[str, Any]:
    """Verify that the exact evidence was included before the receiver released output."""

    from pyscitt import crypto
    from pyscitt.client import Client
    from pyscitt.verify import StaticTrustStore, verify_transparent_statement

    try:
        decoded = cbor2.loads(bundle)
        if cbor2.dumps(decoded, canonical=True) != bundle or not isinstance(decoded, dict) or set(decoded) != {1, 2, 3}:
            raise ValueError("invalid canonical publication bundle")
        signed_statement, transparent_statement, metadata = decoded[1], decoded[2], decoded[3]
        if not isinstance(signed_statement, bytes) or not isinstance(transparent_statement, bytes) or not isinstance(metadata, dict):
            raise ValueError("invalid publication bundle fields")
        _headers, signed_payload = crypto.parse_cose_sign1(signed_statement)
        if signed_payload != evidence:
            raise ValueError("SCITT signed statement does not contain the receiver receipt")
        service_url = metadata.get("service_url", "")
        if not isinstance(service_url, str) or not service_url.startswith("https://"):
            raise ValueError("invalid SCITT service URL")
        client = Client(service_url, development=bool(metadata.get("development_tls")))
        try:
            trust_store = StaticTrustStore(cose_keys=client.get_scitt_keys())
            details = verify_transparent_statement(transparent_statement, trust_store, signed_statement)
        finally:
            client.session.close()
        if not details:
            raise ValueError("SCITT transparent statement contains no verified receipt")
        if metadata.get("evidence_sha256") != hashlib.sha256(evidence).hexdigest():
            raise ValueError("SCITT evidence digest mismatch")
        if metadata.get("signed_statement_sha256") != hashlib.sha256(signed_statement).hexdigest():
            raise ValueError("SCITT signed-statement digest mismatch")
        if metadata.get("transparent_statement_sha256") != hashlib.sha256(transparent_statement).hexdigest():
            raise ValueError("SCITT transparent-statement digest mismatch")
        result = dict(metadata)
        result["receipts"] = details
        result["status"] = "registered-and-receipt-verified"
        return result
    except ScittRegistrationError:
        raise
    except Exception as exc:
        raise ScittRegistrationError(f"SCITT publication verification failed: {exc}") from exc

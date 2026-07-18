"""Register verified TEE evidence with SCITT-CCF and verify its receipt."""

from __future__ import annotations

import fcntl
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

SCITT_CONTENT_TYPE = "application/vnd.master-thesis.tee-inference-evidence+cbor"
SCITT_ZK_CONTENT_TYPE = "application/vnd.master-thesis.zk-inference-proof+cbor"
SCITT_EKU = "2.999"
DEFAULT_SCITT_URL = os.environ.get("SCITT_URL", "https://127.0.0.1:8000").rstrip("/")
DEFAULT_SCITT_DEVELOPMENT = os.environ.get("SCITT_DEVELOPMENT", "1").strip().lower() in {
    "1",
    "true",
    "yes",
}
DEFAULT_SCITT_SIGNER_DIR = os.environ.get("SCITT_SIGNER_DIR", "agent/state/scitt")
DEFAULT_SCITT_STATEMENT_PATH = os.environ.get(
    "SCITT_TRANSPARENT_STATEMENT_PATH",
    "tee_inference/out/latest-transparent-statement.cose",
)


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


def _load_or_create_signer(directory: Path):
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
            root_key, _root_public = crypto.generate_keypair("ec")
            root_cert = crypto.generate_cert(
                root_key,
                ca=True,
                cn="master-thesis-scitt-agent-root",
            )
            leaf_key, _leaf_public = crypto.generate_keypair("ec")
            leaf_cert = crypto.generate_cert(
                leaf_key,
                issuer=(root_cert, root_key),
                cn="master-thesis-tee-inference-agent",
                add_eku=SCITT_EKU,
            )
            _atomic_write(paths["root_key"], root_key.encode("utf-8"), 0o600)
            _atomic_write(paths["root_cert"], root_cert.encode("utf-8"), 0o644)
            _atomic_write(paths["leaf_key"], leaf_key.encode("utf-8"), 0o600)
            _atomic_write(paths["leaf_cert"], leaf_cert.encode("utf-8"), 0o644)

        root_fingerprint = crypto.get_cert_fingerprint_b64url(root_cert)
        issuer = f"did:x509:0:sha256:{root_fingerprint}::eku:{SCITT_EKU}"
        return crypto.Signer(
            leaf_key,
            issuer=issuer,
            algorithm="ES256",
            x5c=[leaf_cert, root_cert],
        )


def _sign_evidence(evidence: bytes, signer: Any, content_type: str) -> bytes:
    from pyscitt import crypto

    return crypto.sign_statement(
        signer,
        evidence,
        content_type=content_type,
        cwt=True,
    )


def _submit_and_verify(url: str, development: bool, signed_statement: bytes) -> tuple[Any, list[dict[str, Any]]]:
    from pyscitt.client import Client
    from pyscitt.verify import StaticTrustStore, verify_transparent_statement

    client = Client(url, development=development)
    try:
        submission = client.submit_signed_statement_and_wait(signed_statement)
        trust_store = StaticTrustStore(cose_keys=client.get_scitt_keys())
        details = verify_transparent_statement(
            submission.response_bytes,
            trust_store,
            signed_statement,
        )
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
) -> dict[str, Any]:
    """Submit exact verified evidence bytes and persist only a verified result."""

    if not evidence:
        raise ScittRegistrationError("cannot register an empty evidence bundle")
    if not content_type.startswith("application/vnd.master-thesis.") or not content_type.endswith("+cbor"):
        raise ScittRegistrationError("SCITT content type must be a master-thesis CBOR media type")
    service_url = (url or DEFAULT_SCITT_URL).rstrip("/")
    if not service_url.startswith("https://"):
        raise ScittRegistrationError("SCITT_URL must use HTTPS")
    development_mode = DEFAULT_SCITT_DEVELOPMENT if development is None else development

    try:
        signer = _load_or_create_signer(Path(signer_dir or DEFAULT_SCITT_SIGNER_DIR))
        signed_statement = _sign_evidence(evidence, signer, content_type)
        submission, receipt_details = _submit_and_verify(
            service_url,
            development_mode,
            signed_statement,
        )
    except ScittRegistrationError:
        raise
    except Exception as exc:
        raise ScittRegistrationError(f"SCITT registration failed: {exc}") from exc

    transparent_statement = submission.response_bytes
    output = Path(transparent_statement_path or DEFAULT_SCITT_STATEMENT_PATH)
    _atomic_write(output, transparent_statement, 0o644)
    return {
        "status": "registered-and-receipt-verified",
        "service_url": service_url,
        "development_tls": development_mode,
        "content_type": content_type,
        "transaction_id": submission.tx,
        "evidence_sha256": hashlib.sha256(evidence).hexdigest(),
        "signed_statement_sha256": hashlib.sha256(signed_statement).hexdigest(),
        "transparent_statement_sha256": hashlib.sha256(transparent_statement).hexdigest(),
        "transparent_statement_bytes": len(transparent_statement),
        "transparent_statement_path": str(output),
        "receipts": receipt_details,
    }

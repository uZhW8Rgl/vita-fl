"""Fail-closed receiver-side publication of Sello receipts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .scitt import (
    SCITT_TOOL_RECEIPT_CONTENT_TYPE,
    encode_publication_bundle,
    public_registration,
    register_verified_evidence,
)

SCITT_BUNDLE_URL_HEADER = "X-Sello-SCITT-Bundle-URL"
SCITT_TRANSACTION_HEADER = "X-Sello-SCITT-Transaction"


class ReceiverTransparencyPublisher:
    def __init__(self, service_id: str, directory: str | None = None) -> None:
        self.service_id = service_id
        self.directory = Path(directory or os.environ.get("SELLO_PUBLICATION_DIR", "/tmp/sello-publications"))

    def publish(self, envelope: bytes, expected_log_url: str) -> tuple[str, dict[str, object]]:
        configured = os.environ.get("SCITT_URL", "").rstrip("/")
        if configured != expected_log_url.rstrip("/"):
            raise RuntimeError("authorization token log URL does not match receiver SCITT_URL")
        registration = register_verified_evidence(
            envelope,
            url=configured,
            content_type=SCITT_TOOL_RECEIPT_CONTENT_TYPE,
            signer_dir=os.environ.get("SCITT_SIGNER_DIR", "/tmp/sello-scitt-signer"),
            identity=f"{self.service_id}-receiver",
        )
        publication_id = hashlib.sha256(
            envelope + registration["_transparent_statement"]
        ).hexdigest()
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{publication_id}.cbor"
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(encode_publication_bundle(registration))
        temporary.chmod(0o600)
        temporary.replace(path)
        return publication_id, public_registration(registration)

    def read(self, publication_id: str) -> bytes:
        if len(publication_id) != 64 or any(char not in "0123456789abcdef" for char in publication_id):
            raise FileNotFoundError("invalid publication identifier")
        return (self.directory / f"{publication_id}.cbor").read_bytes()

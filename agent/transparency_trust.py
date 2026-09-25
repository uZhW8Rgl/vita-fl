"""Read-only public SCITT trust anchors for the authenticated deployment UI.

Authentication is supplied by the existing UI proxy. This export authenticates
which public keys the deployment selected; it is not remote attestation of that
selection. No receipt-controlled URL or private signing material is accepted.
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import time
from typing import Any
from urllib.parse import urlsplit

import cbor2
import httpx

MAX_KEYSET_BYTES = 64 * 1024
MAX_KEYS = 32
KEYSET_PATH = "/.well-known/scitt-keys"
REQUEST_TIMEOUT_SECONDS = 10.0


class TrustAnchorError(RuntimeError):
    """The configured service did not supply a bounded public key set."""


def _client(url: str, development: bool) -> Any:
    from pyscitt.client import Client

    client = Client(url, development=development)
    # The SDK normally inherits ambient proxies and follows redirects. This
    # public, fixed-path export must stay at the operator-configured origin.
    client.session.close()
    client.session = httpx.Client(
        base_url=url,
        verify=not development,
        trust_env=False,
        follow_redirects=False,
        timeout=REQUEST_TIMEOUT_SECONDS,
        params={"api-version": client.api_version} if client.api_version else None,
    )
    return client


def _configuration() -> tuple[str, bool]:
    url = os.environ.get("SCITT_URL", "https://127.0.0.1:8000").rstrip("/")
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and not parsed.path
            and not any(character.isspace() for character in url)
        )
        # Force malformed/out-of-range ports to fail before opening a client.
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise TrustAnchorError("SCITT trust anchors unavailable")
    development = os.environ.get("SCITT_DEVELOPMENT", "1").lower() in {"1", "true", "yes"}
    return url, development


class _ExactCborReader:
    """Prevent CBOR decoder read-ahead from hiding trailing bytes."""

    def __init__(self, raw: bytes):
        self.stream = io.BytesIO(raw)

    def read(self, size: int = -1) -> bytes:
        # This small, bounded key set is decoded bytewise. New cbor2 versions
        # otherwise buffer 4096 bytes, including data after the first object.
        return self.stream.read(0 if size == 0 else 1)


def _validate_public_keys(raw: bytes) -> None:
    from pyscitt.verify import StaticTrustStore

    stream = _ExactCborReader(raw)
    keys = cbor2.CBORDecoder(stream).decode()
    if stream.stream.read(1) or not isinstance(keys, list) or not 1 <= len(keys) <= MAX_KEYS:
        raise ValueError("invalid public key set")
    kids = set()
    for key in keys:
        # Current CCF SCITT public keys are EC2. Reject private EC parameter -4
        # and unknown fields rather than accidentally exporting sensitive data.
        if not isinstance(key, dict) or set(key) - {1, 2, 3, 4, 5, -1, -2, -3}:
            raise ValueError("invalid public key fields")
        if key.get(1) != 2 or not {-1, -2, -3}.issubset(key):
            raise ValueError("unsupported public key type")
        kid = key.get(2)
        if not isinstance(kid, (bytes, str)) or not 1 <= len(kid) <= 256:
            raise ValueError("invalid public key identifier")
        normalized_kid = kid.encode() if isinstance(kid, str) else kid
        if normalized_kid in kids:
            raise ValueError("duplicate public key identifier")
        kids.add(normalized_kid)
    store = StaticTrustStore(cose_keys=keys)
    if len(store.trust_store_keys) != len(keys):
        raise ValueError("incomplete public key set")


def fetch_scitt_trust_anchors() -> dict[str, Any]:
    """Fetch only public keys from the deployment's fixed SCITT configuration."""
    client = None
    try:
        url, development = _configuration()
        client = _client(url, development)
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        chunks = []
        size = 0
        with client.session.stream("GET", KEYSET_PATH) as response:
            if response.status_code != 200:
                raise ValueError("unexpected key-set response")
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > MAX_KEYSET_BYTES or time.monotonic() > deadline:
                    raise ValueError("key-set response exceeds bounds")
                chunks.append(chunk)
        raw = b"".join(chunks)
        _validate_public_keys(raw)
        return {
            "schema_version": 1,
            "service_url": url,
            "scitt_keys_cbor_base64": base64.b64encode(raw).decode("ascii"),
            "scitt_keys_sha256": hashlib.sha256(raw).hexdigest(),
            "trust_bootstrap": "authenticated-deployment-ui",
        }
    except Exception:
        # Do not leak upstream response bodies, URLs with credentials, private
        # key fields, or environment contents in an HTTP error response.
        raise TrustAnchorError("SCITT trust anchors unavailable") from None
    finally:
        if client is not None:
            client.session.close()

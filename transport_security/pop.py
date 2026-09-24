"""Registered-agent proof of possession for the optional RA-TLS transport.

This is the versioned VITA-FL PoP profile, not an RFC 9449 DPoP implementation.
The token's ``cnf.jkt`` uses the SHA-256 thumbprint of the canonical public
Ed25519 JWK. Each proof commits to the exact HTTP body, public URL (including
query), method, token and attestation-session identifier. Agent identity comes
from the operator-provisioned registry, never a public key supplied in a proof.

The bounded replay cache is process-local. The receiver must use one worker, or
provide a shared cache with equivalent atomic consume semantics. Attestation
sessions must not survive a receiver restart that discards this replay cache.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

PROOF_HEADER = "X-Vita-PoP"
PROOF_TYPE = "vita-fl-pop-v1+jwt"
MAX_PROOF_AGE_SECONDS = 60
MAX_PROOF_CLOCK_SKEW_SECONDS = 5
MAX_PROOF_BYTES = 4096
MAX_REPLAY_ENTRIES = 4096
_SUBJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SESSION = re.compile(r"[A-Za-z0-9._~-]{16,256}")
_HEADER = {"alg": "EdDSA", "typ": PROOF_TYPE}
_CLAIMS = {"sub", "htm", "htu", "body_sha256", "ath", "sid", "iat", "jti"}


class PoPAuthenticationError(ValueError):
    """An agent identity or per-request possession proof is invalid."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise PoPAuthenticationError("invalid PoP base64url value")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError as exc:
        raise PoPAuthenticationError("invalid PoP base64url value") from exc
    if _b64encode(raw) != value:
        raise PoPAuthenticationError("non-canonical PoP base64url value")
    return raw


def _key_bytes(value: str | bytes, name: str) -> bytes:
    if isinstance(value, bytes):
        raw = value
    elif isinstance(value, str):
        text = value.strip().removeprefix("0x")
        if re.fullmatch(r"[0-9a-fA-F]{64}", text):
            raw = bytes.fromhex(text)
        else:
            try:
                raw = base64.b64decode(text + "=" * (-len(text) % 4), altchars=b"-_", validate=True)
            except ValueError as exc:
                raise PoPAuthenticationError(f"{name} must be a hex or base64 32-byte key") from exc
    else:
        raise PoPAuthenticationError(f"{name} must be a 32-byte key")
    if len(raw) != 32:
        raise PoPAuthenticationError(f"{name} must be a 32-byte key")
    return raw


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def public_key_thumbprint(public_key: bytes) -> str:
    """Return the SHA-256 JWK thumbprint for a raw Ed25519 public key."""
    raw = _key_bytes(public_key, "agent PoP public key")
    jwk = {"crv": "Ed25519", "kty": "OKP", "x": _b64encode(raw)}
    return _b64encode(hashlib.sha256(_json_bytes(jwk)).digest())


def tee_transport_mode() -> str:
    mode = os.environ.get("TEE_TRANSPORT_MODE", "mtls").strip().casefold()
    if mode not in {"mtls", "ratls"}:
        raise PoPAuthenticationError("TEE_TRANSPORT_MODE must be mtls or ratls")
    return mode


def _subject(value: str) -> str:
    if not isinstance(value, str) or not _SUBJECT.fullmatch(value):
        raise PoPAuthenticationError("invalid registered agent subject")
    return value


@dataclass(frozen=True)
class PoPIdentity:
    """Immutable key generation retained for a tool call and its receipt download."""

    subject: str
    signing_seed: bytes = field(repr=False)
    thumbprint: str
    receiver_origin: str | None = None
    trusted_service_key: bytes | None = None

    def __post_init__(self) -> None:
        _subject(self.subject)
        seed = _key_bytes(self.signing_seed, "agent PoP signing seed")
        object.__setattr__(self, "signing_seed", seed)
        if not isinstance(self.thumbprint, str) or not self.thumbprint.isascii():
            raise PoPAuthenticationError("invalid agent PoP thumbprint")
        if not secrets.compare_digest(public_key_thumbprint(bytes(SigningKey(seed).verify_key)), self.thumbprint):
            raise PoPAuthenticationError("agent PoP thumbprint does not match its signing key")
        if self.trusted_service_key is not None:
            _key_bytes(self.trusted_service_key, "trusted receiver signing key")

    @property
    def public_key(self) -> bytes:
        return bytes(SigningKey(self.signing_seed).verify_key)


@dataclass(frozen=True)
class AuthenticatedPoP:
    subject: str
    thumbprint: str

    @property
    def fingerprint(self) -> str:
        return self.thumbprint


def capture_pop_identity() -> PoPIdentity:
    seed = _key_bytes(os.environ.get("AGENT_POP_SIGNING_SEED", ""), "AGENT_POP_SIGNING_SEED")
    subject = _subject(os.environ.get("SELLO_OWNER_SUBJECT", "master-thesis-agent"))
    return PoPIdentity(subject, seed, public_key_thumbprint(bytes(SigningKey(seed).verify_key)))


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise PoPAuthenticationError("duplicate agent PoP registry subject")
        result[key] = value
    return result


def registered_agents(registry: Mapping[str, str | bytes] | None = None) -> dict[str, bytes]:
    """Read only authenticated configuration; a request cannot register a new key."""
    if registry is None:
        raw = os.environ.get("AGENT_POP_REGISTRY", "")
        if not raw or len(raw) > 256 * 1024:
            raise PoPAuthenticationError("AGENT_POP_REGISTRY is required and must be bounded JSON")
        try:
            registry = json.loads(raw, object_pairs_hook=_unique_object)
        except (ValueError, TypeError) as exc:
            raise PoPAuthenticationError("AGENT_POP_REGISTRY is invalid JSON") from exc
    if not isinstance(registry, Mapping) or not 0 < len(registry) <= 1024:
        raise PoPAuthenticationError("AGENT_POP_REGISTRY must contain registered agent keys")
    result = {_subject(subject): _key_bytes(key, "registered agent PoP key") for subject, key in registry.items()}
    if len(set(result.values())) != len(result):
        raise PoPAuthenticationError("a PoP key must identify exactly one agent subject")
    return result


def load_agent_registry() -> dict[str, bytes]:
    """Validate operator-provisioned receiver policy before accepting connections."""
    return registered_agents()


def public_request_url(value: str) -> str:
    """Normalize only HTTPS authority casing; retain the exact raw path/query."""
    if not isinstance(value, str) or any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise PoPAuthenticationError("PoP URL must be an ASCII public HTTPS URL")
    try:
        parts = urlsplit(value)
        port = parts.port
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
            or "#" in value
            or (port is not None and port <= 0)
        ):
            raise ValueError("invalid origin")
    except ValueError as exc:
        raise PoPAuthenticationError("PoP URL must use the public HTTPS origin") from exc
    # Preserve even an explicitly empty query delimiter; request targets bind bytes.
    query = "?" + parts.query if "?" in value else ""
    return urlunsplit(("https", parts.netloc.lower(), parts.path or "/", "", "")) + query


def _method(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z]+", value):
        raise PoPAuthenticationError("PoP HTTP method must be uppercase")
    return value


def _session(value: str) -> str:
    if not isinstance(value, str) or not _SESSION.fullmatch(value):
        raise PoPAuthenticationError("invalid attestation session identifier")
    return value


def _hash_bytes(value: bytes, name: str) -> str:
    if not isinstance(value, bytes):
        raise PoPAuthenticationError(f"{name} must be exact bytes")
    return _b64encode(hashlib.sha256(value).digest())


def _token_hash(token: str) -> str:
    if not isinstance(token, str) or not token or len(token) > 16384:
        raise PoPAuthenticationError("invalid PoP authorization token")
    try:
        raw = token.encode("ascii")
    except UnicodeError as exc:
        raise PoPAuthenticationError("invalid PoP authorization token") from exc
    return _hash_bytes(raw, "authorization token")


def proof_headers(
    identity: PoPIdentity,
    *,
    method: str,
    url: str,
    body: bytes,
    token: str,
    session_id: str,
    now: int | None = None,
) -> dict[str, str]:
    """Sign one request after the server attestation session has been verified."""
    claims = {
        "sub": identity.subject,
        "htm": _method(method),
        "htu": public_request_url(url),
        "body_sha256": _hash_bytes(body, "request body"),
        "ath": _token_hash(token),
        "sid": _session(session_id),
        "iat": int(time.time()) if now is None else int(now),
        "jti": _b64encode(secrets.token_bytes(16)),
    }
    signing_input = f"{_b64encode(_json_bytes(_HEADER))}.{_b64encode(_json_bytes(claims))}".encode("ascii")
    signature = SigningKey(identity.signing_seed).sign(signing_input).signature
    return {PROOF_HEADER: f"{signing_input.decode('ascii')}.{_b64encode(signature)}"}


class ReplayCache:
    """Bounded atomic replay rejection; capacity exhaustion rejects new proofs."""

    def __init__(self, capacity: int = MAX_REPLAY_ENTRIES) -> None:
        if type(capacity) is not int or capacity <= 0:
            raise ValueError("PoP replay-cache capacity must be positive")
        self.capacity = capacity
        self._entries: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def consume(self, thumbprint: str, jti: str, *, expires_at: int, now: int) -> None:
        with self._lock:
            self._entries = {key: expiry for key, expiry in self._entries.items() if expiry >= now}
            key = (thumbprint, jti)
            if key in self._entries:
                raise PoPAuthenticationError("agent PoP proof was replayed")
            if len(self._entries) >= self.capacity:
                raise PoPAuthenticationError("agent PoP replay cache is full")
            self._entries[key] = expires_at


_DEFAULT_REPLAY_CACHE = ReplayCache()


def verify_pop_proof(
    headers: Mapping[str, str],
    *,
    method: str,
    url: str,
    body: bytes,
    token: str,
    session_id: str,
    registry: Mapping[str, str | bytes] | None = None,
    replay_cache: ReplayCache | None = None,
    now: int | None = None,
) -> AuthenticatedPoP:
    """Authenticate a registered key and atomically consume its bound request proof.

    Callers must also verify the issuer-signed token's subject, scope, audience,
    validity and ``cnf.jkt`` against the returned identity before running work.
    ``url`` and ``session_id`` must come from trusted server configuration/state.
    """
    supplied = [value for key, value in headers.items() if key.casefold() == PROOF_HEADER.casefold()]
    if len(supplied) != 1 or not isinstance(supplied[0], str) or not 0 < len(supplied[0]) <= MAX_PROOF_BYTES:
        raise PoPAuthenticationError("exactly one bounded agent PoP proof is required")
    parts = supplied[0].split(".")
    if len(parts) != 3:
        raise PoPAuthenticationError("agent PoP proof must be compact JWS")
    header_raw, claims_raw, signature = map(_b64decode, parts)
    try:
        header, claims = json.loads(header_raw), json.loads(claims_raw)
    except (ValueError, UnicodeError) as exc:
        raise PoPAuthenticationError("invalid agent PoP JSON") from exc
    if (
        header != _HEADER
        or not isinstance(claims, dict)
        or set(claims) != _CLAIMS
        or _json_bytes(header) != header_raw
        or _json_bytes(claims) != claims_raw
    ):
        raise PoPAuthenticationError("unsupported or non-canonical agent PoP profile")
    subject = _subject(claims.get("sub"))
    public_key = registered_agents(registry).get(subject)
    if public_key is None:
        raise PoPAuthenticationError("agent PoP subject is not registered")
    try:
        VerifyKey(public_key).verify(f"{parts[0]}.{parts[1]}".encode("ascii"), signature)
    except (BadSignatureError, ValueError) as exc:
        raise PoPAuthenticationError("agent PoP signature does not match its registered key") from exc
    current = int(time.time()) if now is None else int(now)
    issued_at = claims.get("iat")
    if (
        type(issued_at) is not int
        or issued_at > current + MAX_PROOF_CLOCK_SKEW_SECONDS
        or issued_at < current - MAX_PROOF_AGE_SECONDS
    ):
        raise PoPAuthenticationError("agent PoP proof is outside its validity window")
    jti = claims.get("jti")
    if not isinstance(jti, str) or len(_b64decode(jti)) != 16:
        raise PoPAuthenticationError("agent PoP proof requires a 128-bit identifier")
    expected = {
        "htm": _method(method),
        "htu": public_request_url(url),
        "body_sha256": _hash_bytes(body, "request body"),
        "ath": _token_hash(token),
        "sid": _session(session_id),
    }
    for name, value in expected.items():
        actual = claims.get(name)
        if not isinstance(actual, str) or not actual.isascii() or not secrets.compare_digest(actual, value):
            raise PoPAuthenticationError(f"agent PoP {name} binding does not match this request")
    thumbprint = public_key_thumbprint(public_key)
    cache = _DEFAULT_REPLAY_CACHE if replay_cache is None else replay_cache
    cache.consume(thumbprint, jti, expires_at=issued_at + MAX_PROOF_AGE_SECONDS, now=current)
    return AuthenticatedPoP(subject, thumbprint)

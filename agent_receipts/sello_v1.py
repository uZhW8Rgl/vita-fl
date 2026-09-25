"""Sello-inspired receiver receipts for the thesis prototype.

The wire format follows the construction in arXiv:2606.04193: an Ed25519
COSE_Sign1 envelope covers an RFC 9180 HPKE ciphertext. The encrypted,
canonical-CBOR body binds the exact request and response byte hashes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import cbor2
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey
from pyhpke import AEADId, CipherSuite, KDFId, KEMId, KEMKey

COSE_SIGN1_TAG = 18
COSE_ALG_EDDSA = -8
SELLO_VERSION = "0.1.0"
SELLO_VERSION_LABEL = -70000
SELLO_TOKEN_REF_LABEL = -70001
SELLO_LOG_URL_LABEL = -70002
HPKE_INFO = b"master-thesis/sello-v1/tool-receipt"
MAX_CLOCK_SKEW_SECONDS = 60
MAX_TOKEN_LIFETIME_SECONDS = 300


class ReceiptVerificationError(ValueError):
    """A token or receipt failed a mandatory cryptographic check."""


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def b64url_decode(value: str) -> bytes:
    if not isinstance(value, str):
        raise ReceiptVerificationError("base64url value must be text")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise ReceiptVerificationError("invalid base64url value") from exc


def _canonical_json(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _canonical_cbor(value: Any) -> bytes:
    return cbor2.dumps(value, canonical=True)


def _loads_canonical_cbor(value: bytes, name: str) -> Any:
    try:
        decoded = cbor2.loads(value)
    except Exception as exc:
        raise ReceiptVerificationError(f"invalid {name} CBOR") from exc
    if _canonical_cbor(decoded) != value:
        raise ReceiptVerificationError(f"{name} is not canonical CBOR")
    return decoded


def _suite() -> CipherSuite:
    return CipherSuite.new(
        KEMId.DHKEM_X25519_HKDF_SHA256,
        KDFId.HKDF_SHA256,
        AEADId.CHACHA20_POLY1305,
    )


def _hpke_public(raw: bytes):
    if len(raw) != 32:
        raise ReceiptVerificationError("owner HPKE public key must contain 32 bytes")
    return KEMKey.from_pyca_cryptography_key(X25519PublicKey.from_public_bytes(raw))


def _hpke_private(raw: bytes):
    if len(raw) != 32:
        raise ReceiptVerificationError("owner HPKE private key must contain 32 bytes")
    return KEMKey.from_pyca_cryptography_key(X25519PrivateKey.from_private_bytes(raw))


def authorization_origin(value: str) -> str:
    """Validate the exact HTTPS receiver origin used as token audience."""
    if not isinstance(value, str):
        raise ReceiptVerificationError("authorization audience must be an HTTPS origin")
    parts = urllib.parse.urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise ReceiptVerificationError("authorization audience must be an HTTPS origin")
    return urllib.parse.urlunsplit(("https", parts.netloc.lower(), "", "", ""))


def _certificate_thumbprint(value: str) -> str:
    raw = b64url_decode(value)
    if len(raw) != 32 or b64url_encode(raw) != value:
        raise ReceiptVerificationError("authorization token has invalid certificate thumbprint")
    return value


def create_authorization_token(
    signing_key: SigningKey,
    owner_hpke_public_key: bytes,
    *,
    subject: str,
    log_urls: list[str],
    audience: str,
    cert_thumbprint: str | None = None,
    pop_thumbprint: str | None = None,
    scopes: list[str],
    lifetime_seconds: int = MAX_TOKEN_LIFETIME_SECONDS,
    now: int | None = None,
) -> str:
    issued_at = int(time.time()) if now is None else int(now)
    if not subject or not isinstance(subject, str):
        raise ReceiptVerificationError("authorization token requires a subject")
    if not isinstance(lifetime_seconds, int) or not 0 < lifetime_seconds <= MAX_TOKEN_LIFETIME_SECONDS:
        raise ReceiptVerificationError("authorization token lifetime must be between 1 and 300 seconds")
    if not scopes or not all(isinstance(scope, str) and scope for scope in scopes):
        raise ReceiptVerificationError("authorization token requires explicit scopes")
    if (cert_thumbprint is None) == (pop_thumbprint is None):
        raise ReceiptVerificationError("authorization token requires exactly one client key binding")
    confirmation = (
        {"x5t#S256": _certificate_thumbprint(cert_thumbprint)}
        if cert_thumbprint is not None
        else {"jkt": _certificate_thumbprint(pop_thumbprint)}
    )
    header = {"alg": "EdDSA", "typ": "JWT"}
    claims = {
        "aud": authorization_origin(audience),
        "cnf": confirmation,
        "exp": issued_at + lifetime_seconds,
        "iat": issued_at,
        "jti": b64url_encode(secrets.token_bytes(16)),
        "owner_hpke_pk": b64url_encode(owner_hpke_public_key),
        "sello_logs": log_urls,
        "sub": subject,
        "scope": sorted(set(scopes)),
    }
    protected = b64url_encode(_canonical_json(header))
    payload = b64url_encode(_canonical_json(claims))
    signing_input = f"{protected}.{payload}".encode("ascii")
    signature = signing_key.sign(signing_input).signature
    return f"{protected}.{payload}.{b64url_encode(signature)}"


def verify_authorization_token(
    token: str,
    issuer_key: VerifyKey | bytes,
    *,
    now: int | None = None,
    expected_audience: str | None = None,
    required_scope: str | None = None,
    expected_subject: str | None = None,
    cert_thumbprint: str | None = None,
    expected_pop_thumbprint: str | None = None,
) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        raise ReceiptVerificationError("authorization token is not compact JWS")
    header_raw, claims_raw, signature_raw = map(b64url_decode, parts)
    try:
        header = json.loads(header_raw)
        claims = json.loads(claims_raw)
    except Exception as exc:
        raise ReceiptVerificationError("authorization token JSON is invalid") from exc
    if header != {"alg": "EdDSA", "typ": "JWT"} or not isinstance(claims, dict):
        raise ReceiptVerificationError("authorization token profile is unsupported")
    key = issuer_key if isinstance(issuer_key, VerifyKey) else VerifyKey(issuer_key)
    try:
        key.verify(f"{parts[0]}.{parts[1]}".encode("ascii"), signature_raw)
    except BadSignatureError as exc:
        raise ReceiptVerificationError("authorization token signature is invalid") from exc
    current = int(time.time()) if now is None else int(now)
    if type(claims.get("iat")) is not int or type(claims.get("exp")) is not int:
        raise ReceiptVerificationError("authorization token has invalid validity claims")
    if not 0 < claims["exp"] - claims["iat"] <= MAX_TOKEN_LIFETIME_SECONDS:
        raise ReceiptVerificationError("authorization token exceeds the permitted lifetime")
    if claims["iat"] > current + MAX_CLOCK_SKEW_SECONDS or claims["exp"] < current - MAX_CLOCK_SKEW_SECONDS:
        raise ReceiptVerificationError("authorization token is outside its validity window")
    if (
        not isinstance(claims.get("sub"), str)
        or not claims["sub"]
        or not isinstance(claims.get("jti"), str)
        or not claims["jti"]
    ):
        raise ReceiptVerificationError("authorization token lacks subject or identifier")
    audience = authorization_origin(claims.get("aud"))
    if expected_audience is not None and audience != authorization_origin(expected_audience):
        raise ReceiptVerificationError("authorization token audience does not match this receiver")
    confirmation = claims.get("cnf")
    if not isinstance(confirmation, dict) or set(confirmation) not in ({"x5t#S256"}, {"jkt"}):
        raise ReceiptVerificationError("authorization token requires exactly one supported client key binding")
    if cert_thumbprint is not None and expected_pop_thumbprint is not None:
        raise ReceiptVerificationError("authorization verification requires one transport binding profile")
    binding = next(iter(confirmation))
    fingerprint = _certificate_thumbprint(confirmation[binding])
    if cert_thumbprint is not None and (
        binding != "x5t#S256" or not secrets.compare_digest(fingerprint, cert_thumbprint)
    ):
        raise ReceiptVerificationError("authorization token is bound to another client certificate")
    if expected_pop_thumbprint is not None and (
        binding != "jkt" or not secrets.compare_digest(fingerprint, expected_pop_thumbprint)
    ):
        raise ReceiptVerificationError("authorization token is bound to another agent PoP key")
    if expected_subject is not None and claims["sub"] != expected_subject:
        raise ReceiptVerificationError("authorization token subject does not match the authenticated agent")
    scopes = claims.get("scope")
    if not isinstance(scopes, list) or not scopes or not all(isinstance(scope, str) and scope for scope in scopes):
        raise ReceiptVerificationError("authorization token requires explicit scopes")
    if required_scope is not None and required_scope not in scopes:
        raise ReceiptVerificationError("authorization token does not permit this operation")
    _hpke_public(b64url_decode(claims.get("owner_hpke_pk", "")))
    if (
        not isinstance(claims.get("sello_logs"), list)
        or not claims["sello_logs"]
        or not all(isinstance(url, str) and url.startswith("https://") for url in claims["sello_logs"])
    ):
        raise ReceiptVerificationError("authorization token has invalid Sello log policy")
    return claims


@dataclass(frozen=True)
class VerifiedReceipt:
    body: dict[str, Any]
    kid: bytes
    token_ref: bytes
    log_url: str
    envelope_sha256: str


class SelloReceiver:
    """Receiver-side token validation, HPKE sealing, and COSE signing."""

    def __init__(self, service_id: str, signing_key: SigningKey | bytes, token_issuer_key: VerifyKey | bytes):
        self.service_id = service_id
        self.signing_key = signing_key if isinstance(signing_key, SigningKey) else SigningKey(signing_key)
        self.token_issuer_key = (
            token_issuer_key if isinstance(token_issuer_key, VerifyKey) else VerifyKey(token_issuer_key)
        )
        self.kid = hashlib.sha256(bytes(self.signing_key.verify_key)).digest()[:16]

    @property
    def public_key(self) -> bytes:
        return bytes(self.signing_key.verify_key)

    def issue(
        self,
        token: str,
        *,
        action_type: str,
        action_input: bytes,
        action_output: bytes,
        result_status: str,
        service_defined_fields: dict[str, Any] | None = None,
        now: int | None = None,
    ) -> bytes:
        if result_status not in {"success", "error", "denied"}:
            raise ValueError("invalid receipt result status")
        claims = verify_authorization_token(token, self.token_issuer_key, now=now)
        log_url = claims["sello_logs"][0]
        token_ref = hashlib.sha256(token.encode("ascii")).digest()
        body = {
            "action-input-hash": hashlib.sha256(action_input).digest(),
            "action-output-hash": hashlib.sha256(action_output).digest(),
            "action-type": action_type,
            "agent-identifier": hashlib.sha256(token_ref + claims["sub"].encode()).hexdigest(),
            "result-status": result_status,
            "service-defined-fields": {
                "service-identifier": self.service_id,
                **(service_defined_fields or {}),
            },
            "timestamp": datetime.fromtimestamp(int(time.time()) if now is None else int(now), timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
        }
        protected_map = {
            1: COSE_ALG_EDDSA,
            4: self.kid,
            SELLO_VERSION_LABEL: SELLO_VERSION,
            SELLO_TOKEN_REF_LABEL: token_ref,
            SELLO_LOG_URL_LABEL: log_url,
        }
        protected = _canonical_cbor(protected_map)
        owner_key = _hpke_public(b64url_decode(claims["owner_hpke_pk"]))
        enc, sender = _suite().create_sender_context(owner_key, info=HPKE_INFO)
        ciphertext = sender.seal(_canonical_cbor(body), aad=protected)
        payload = _canonical_cbor({1: enc, 2: ciphertext})
        signature = self.signing_key.sign(_canonical_cbor(["Signature1", protected, b"", payload])).signature
        return _canonical_cbor(cbor2.CBORTag(COSE_SIGN1_TAG, [protected, {}, payload, signature]))


def verify_receipt_signature(
    envelope: bytes,
    service_key: VerifyKey | bytes,
    *,
    log_urls: list[str],
    token_ref: bytes | None = None,
) -> dict[str, Any]:
    """Verify the public COSE envelope without a token or owner private key."""
    tagged = _loads_canonical_cbor(envelope, "receipt")
    if not isinstance(tagged, cbor2.CBORTag) or tagged.tag != COSE_SIGN1_TAG:
        raise ReceiptVerificationError("receipt is not tagged COSE_Sign1")
    if not isinstance(tagged.value, (list, tuple)) or len(tagged.value) != 4:
        raise ReceiptVerificationError("receipt COSE_Sign1 shape is invalid")
    protected, unprotected, payload, signature = tagged.value
    if not all(isinstance(value, bytes) for value in (protected, payload, signature)) or unprotected != {}:
        raise ReceiptVerificationError("receipt COSE fields are invalid")
    headers = _loads_canonical_cbor(protected, "protected header")
    if (
        not isinstance(headers, dict)
        or headers.get(1) != COSE_ALG_EDDSA
        or headers.get(SELLO_VERSION_LABEL) != SELLO_VERSION
    ):
        raise ReceiptVerificationError("receipt protected profile is unsupported")
    key = service_key if isinstance(service_key, VerifyKey) else VerifyKey(service_key)
    kid = hashlib.sha256(bytes(key)).digest()[:16]
    if headers.get(4) != kid:
        raise ReceiptVerificationError("receipt key identifier does not match the receiver")
    signed_token_ref = headers.get(SELLO_TOKEN_REF_LABEL)
    if not isinstance(signed_token_ref, bytes) or len(signed_token_ref) != 32:
        raise ReceiptVerificationError("receipt token reference is invalid")
    if token_ref is not None and signed_token_ref != token_ref:
        raise ReceiptVerificationError("receipt is bound to another authorization token")
    log_url = headers.get(SELLO_LOG_URL_LABEL)
    if not isinstance(log_url, str) or log_url not in log_urls:
        raise ReceiptVerificationError("receipt log URL violates owner policy")
    try:
        key.verify(_canonical_cbor(["Signature1", protected, b"", payload]), signature)
    except BadSignatureError as exc:
        raise ReceiptVerificationError("receiver receipt signature is invalid") from exc
    return {"protected": protected, "payload": payload, "kid": kid, "token_ref": signed_token_ref, "log_url": log_url}


class SelloVerifier:
    """Read-only owner verification; possession of an issuer signing key is unnecessary."""

    def __init__(self, hpke_private_key: bytes, service_keys: dict[str, VerifyKey | bytes], *, log_urls: list[str]):
        self.hpke_private_key = hpke_private_key
        self.service_keys = service_keys
        self.log_urls = log_urls

    @property
    def hpke_public_key(self) -> bytes:
        return X25519PrivateKey.from_private_bytes(self.hpke_private_key).public_key().public_bytes_raw()

    def verify(
        self,
        envelope: bytes,
        token: str,
        *,
        expected_service: str,
        expected_action: str,
        action_input: bytes,
        action_output: bytes,
        trusted_service_key: VerifyKey | bytes | None = None,
    ) -> VerifiedReceipt:
        service_key = (
            trusted_service_key if trusted_service_key is not None else self.service_keys.get(expected_service)
        )
        if service_key is None:
            raise ReceiptVerificationError("receiver service is absent from the trusted registry")
        signed = verify_receipt_signature(
            envelope, service_key, log_urls=self.log_urls, token_ref=hashlib.sha256(token.encode("ascii")).digest()
        )
        protected, payload = signed["protected"], signed["payload"]
        kid, token_ref, log_url = signed["kid"], signed["token_ref"], signed["log_url"]
        encrypted = _loads_canonical_cbor(payload, "encrypted payload")
        if not isinstance(encrypted, dict) or set(encrypted) != {1, 2}:
            raise ReceiptVerificationError("receipt HPKE payload is invalid")
        recipient = _suite().create_recipient_context(
            encrypted[1], _hpke_private(self.hpke_private_key), info=HPKE_INFO
        )
        plaintext = recipient.open(encrypted[2], aad=protected)
        body = _loads_canonical_cbor(plaintext, "receipt body")
        if not isinstance(body, dict) or body.get("action-type") != expected_action:
            raise ReceiptVerificationError("receipt action type does not match the tool call")
        if body.get("action-input-hash") != hashlib.sha256(action_input).digest():
            raise ReceiptVerificationError("receipt input hash does not match the exact request")
        if body.get("action-output-hash") != hashlib.sha256(action_output).digest():
            raise ReceiptVerificationError("receipt output hash does not match the exact response")
        fields = body.get("service-defined-fields")
        if not isinstance(fields, dict) or fields.get("service-identifier") != expected_service:
            raise ReceiptVerificationError("receipt service identity does not match the receiver")
        return VerifiedReceipt(
            body=body,
            kid=kid,
            token_ref=token_ref,
            log_url=log_url,
            envelope_sha256=hashlib.sha256(envelope).hexdigest(),
        )


class SelloOwner(SelloVerifier):
    """Agent-owner token creation and receiver-receipt verification."""

    def __init__(
        self,
        token_signing_key: SigningKey | bytes,
        hpke_private_key: bytes,
        service_keys: dict[str, VerifyKey | bytes],
        *,
        subject: str,
        log_urls: list[str],
    ) -> None:
        self.token_signing_key = (
            token_signing_key if isinstance(token_signing_key, SigningKey) else SigningKey(token_signing_key)
        )
        self.hpke_private_key = hpke_private_key
        self.service_keys = {
            service: key if isinstance(key, VerifyKey) else VerifyKey(key) for service, key in service_keys.items()
        }
        self.subject = subject
        self.log_urls = log_urls

    @property
    def token_issuer_public_key(self) -> bytes:
        return bytes(self.token_signing_key.verify_key)

    @property
    def hpke_public_key(self) -> bytes:
        private = X25519PrivateKey.from_private_bytes(self.hpke_private_key)
        return private.public_key().public_bytes_raw()

    def token(
        self,
        *,
        audience: str,
        cert_thumbprint: str | None = None,
        pop_thumbprint: str | None = None,
        scopes: list[str],
        now: int | None = None,
    ) -> str:
        return create_authorization_token(
            self.token_signing_key,
            self.hpke_public_key,
            subject=self.subject,
            log_urls=self.log_urls,
            audience=audience,
            cert_thumbprint=cert_thumbprint,
            pop_thumbprint=pop_thumbprint,
            scopes=scopes,
            now=now,
        )

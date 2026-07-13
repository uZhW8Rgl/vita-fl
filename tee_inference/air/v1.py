"""Strict AIR v1 COSE_Sign1 emitter and verifier.

The wire shape follows draft-tsyrulnikov-rats-attested-inference-receipt and
the cyntrisec/air-v1 v1.0.1 conformance corpus.
"""

from __future__ import annotations

import io
import time
from dataclasses import dataclass
from typing import Any

import cbor2
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey, VerifyKey

CWT_ISS = 1
CWT_IAT = 6
CWT_CTI = 7
EAT_NONCE = 10
EAT_PROFILE = 265
MODEL_ID = -65537
MODEL_VERSION = -65538
MODEL_HASH = -65539
REQUEST_HASH = -65540
RESPONSE_HASH = -65541
ATTESTATION_DOC_HASH = -65542
ENCLAVE_MEASUREMENTS = -65543
POLICY_VERSION = -65544
SEQUENCE_NUMBER = -65545
EXECUTION_TIME_MS = -65546
MEMORY_PEAK_MB = -65547
SECURITY_MODE = -65548
MODEL_HASH_SCHEME = -65549

AIR_PROFILE = "https://spec.cyntrisec.com/air/v1"
COSE_SIGN1_TAG = 18
PROTECTED_HEADER = {1: -8, 3: 61}
ALLOWED_SCHEMES = {"sha256-single", "sha256-concat", "sha256-manifest"}
ALLOWED_SECURITY_MODES = {"production", "evaluation"}
ALLOWED_MEASUREMENT_TYPES = {"nitro-pcr", "tdx-mrtd-rtmr"}
REQUIRED_CLAIMS = {
    CWT_ISS,
    CWT_IAT,
    CWT_CTI,
    EAT_PROFILE,
    MODEL_ID,
    MODEL_VERSION,
    MODEL_HASH,
    REQUEST_HASH,
    RESPONSE_HASH,
    ATTESTATION_DOC_HASH,
    ENCLAVE_MEASUREMENTS,
    POLICY_VERSION,
    SEQUENCE_NUMBER,
    EXECUTION_TIME_MS,
    MEMORY_PEAK_MB,
    SECURITY_MODE,
}
ALLOWED_CLAIMS = REQUIRED_CLAIMS | {EAT_NONCE, MODEL_HASH_SCHEME}


class AirVerificationError(ValueError):
    """Fail-closed AIR verification error with a stable error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class AirClaims:
    issuer: str
    issued_at: int
    cti: bytes
    model_id: str
    model_version: str
    model_hash: bytes
    request_hash: bytes
    response_hash: bytes
    attestation_doc_hash: bytes
    enclave_measurements: dict[str, Any]
    policy_version: str
    sequence_number: int
    execution_time_ms: int
    memory_peak_mb: int
    security_mode: str
    nonce: bytes | None = None
    model_hash_scheme: str = "sha256-manifest"

    def as_map(self) -> dict[int, Any]:
        claims: dict[int, Any] = {
            CWT_ISS: self.issuer,
            CWT_IAT: self.issued_at,
            CWT_CTI: self.cti,
            EAT_PROFILE: AIR_PROFILE,
            MODEL_ID: self.model_id,
            MODEL_VERSION: self.model_version,
            MODEL_HASH: self.model_hash,
            REQUEST_HASH: self.request_hash,
            RESPONSE_HASH: self.response_hash,
            ATTESTATION_DOC_HASH: self.attestation_doc_hash,
            ENCLAVE_MEASUREMENTS: self.enclave_measurements,
            POLICY_VERSION: self.policy_version,
            SEQUENCE_NUMBER: self.sequence_number,
            EXECUTION_TIME_MS: self.execution_time_ms,
            MEMORY_PEAK_MB: self.memory_peak_mb,
            SECURITY_MODE: self.security_mode,
            MODEL_HASH_SCHEME: self.model_hash_scheme,
        }
        if self.nonce is not None:
            claims[EAT_NONCE] = self.nonce
        return claims


@dataclass(frozen=True)
class AirPolicy:
    expected_nonce: bytes | None = None
    expected_model_hash: bytes | None = None
    expected_request_hash: bytes | None = None
    expected_response_hash: bytes | None = None
    expected_platform: str | None = None
    expected_model_id: str | None = None
    expected_security_mode: str | None = None
    allow_evaluation_mode: bool = False
    require_nonce: bool = False
    max_age_seconds: int = 0
    clock_skew_seconds: int = 0


def _canonical(value: Any) -> bytes:
    return cbor2.dumps(value, canonical=True)


def _loads_exact(raw: bytes, code: str) -> Any:
    stream = io.BytesIO(raw)
    try:
        value = cbor2.CBORDecoder(stream).decode()
    except Exception as exc:
        raise AirVerificationError(code, f"invalid CBOR: {exc}") from exc
    if stream.read(1):
        raise AirVerificationError(code, "trailing CBOR bytes")
    if _canonical(value) != raw:
        raise AirVerificationError("NON_DETERMINISTIC_CBOR", "CBOR is not deterministic or has duplicate keys")
    return value


def _require_bytes(value: Any, length: int, name: str) -> bytes:
    if not isinstance(value, bytes) or len(value) != length:
        raise AirVerificationError("BAD_CLAIM", f"{name} must be a {length}-byte bstr")
    return value


def _validate_measurements(value: Any) -> None:
    if not isinstance(value, dict):
        raise AirVerificationError("BAD_MEASUREMENTS", "enclave measurements must be a map")
    measurement_type = value.get("measurement_type")
    if measurement_type not in ALLOWED_MEASUREMENT_TYPES:
        raise AirVerificationError("BAD_MEASUREMENT_TYPE", "unknown measurement type")
    allowed = {"measurement_type", "pcr0", "pcr1", "pcr2"}
    optional = {"pcr3", "pcr4", "pcr8"} if measurement_type == "nitro-pcr" else {"pcr3", "pcr4"}
    if not set(value).issubset(allowed | optional) or not {"pcr0", "pcr1", "pcr2"}.issubset(value):
        raise AirVerificationError("BAD_MEASUREMENTS", "missing or unknown measurement field")
    for key in set(value) - {"measurement_type"}:
        _require_bytes(value[key], 48, key)


def _validate_claims(claims: Any) -> dict[int, Any]:
    if not isinstance(claims, dict):
        raise AirVerificationError("BAD_CLAIMS", "AIR payload must be a map")
    keys = set(claims)
    if not REQUIRED_CLAIMS.issubset(keys) or not keys.issubset(ALLOWED_CLAIMS):
        raise AirVerificationError("BAD_CLAIMS", "missing or unknown AIR claim")
    if claims[EAT_PROFILE] != AIR_PROFILE:
        raise AirVerificationError("BAD_PROFILE", "unexpected EAT profile")
    for key, name in ((CWT_ISS, "issuer"), (MODEL_ID, "model_id"), (MODEL_VERSION, "model_version"), (POLICY_VERSION, "policy_version")):
        if not isinstance(claims[key], str) or not claims[key]:
            raise AirVerificationError("BAD_CLAIM", f"{name} must be non-empty text")
    for key, name in ((CWT_IAT, "iat"), (SEQUENCE_NUMBER, "sequence"), (EXECUTION_TIME_MS, "execution time"), (MEMORY_PEAK_MB, "memory peak")):
        if isinstance(claims[key], bool) or not isinstance(claims[key], int) or claims[key] < 0:
            raise AirVerificationError("BAD_CLAIM", f"{name} must be an unsigned integer")
    _require_bytes(claims[CWT_CTI], 16, "cti")
    for key, name in ((MODEL_HASH, "model_hash"), (REQUEST_HASH, "request_hash"), (RESPONSE_HASH, "response_hash"), (ATTESTATION_DOC_HASH, "attestation_doc_hash")):
        digest = _require_bytes(claims[key], 32, name)
        if digest == bytes(32):
            raise AirVerificationError("ZERO_HASH", f"{name} must not be zero")
    nonce = claims.get(EAT_NONCE)
    if nonce is not None and (not isinstance(nonce, bytes) or not 8 <= len(nonce) <= 64):
        raise AirVerificationError("BAD_NONCE", "nonce must contain 8 to 64 bytes")
    if claims[SECURITY_MODE] not in ALLOWED_SECURITY_MODES:
        raise AirVerificationError("BAD_SECURITY_MODE", "unknown security mode")
    scheme = claims.get(MODEL_HASH_SCHEME)
    if scheme is not None and scheme not in ALLOWED_SCHEMES:
        raise AirVerificationError("BAD_MODEL_HASH_SCHEME", "unknown model hash scheme")
    _validate_measurements(claims[ENCLAVE_MEASUREMENTS])
    return claims


def emit_receipt(claims: AirClaims, signing_key: SigningKey | bytes) -> bytes:
    """Emit a deterministic, tagged AIR v1 COSE_Sign1 receipt."""

    claims_map = _validate_claims(claims.as_map())
    protected = _canonical(PROTECTED_HEADER)
    payload = _canonical(claims_map)
    key = signing_key if isinstance(signing_key, SigningKey) else SigningKey(signing_key)
    to_sign = _canonical(["Signature1", protected, b"", payload])
    signature = key.sign(to_sign).signature
    return _canonical(cbor2.CBORTag(COSE_SIGN1_TAG, [protected, {}, payload, signature]))


def _apply_policy(claims: dict[int, Any], policy: AirPolicy, now: int) -> None:
    comparisons = (
        (policy.expected_nonce, claims.get(EAT_NONCE), "NONCE_MISMATCH"),
        (policy.expected_model_hash, claims[MODEL_HASH], "MODEL_HASH_MISMATCH"),
        (policy.expected_request_hash, claims[REQUEST_HASH], "REQUEST_HASH_MISMATCH"),
        (policy.expected_response_hash, claims[RESPONSE_HASH], "RESPONSE_HASH_MISMATCH"),
        (policy.expected_platform, claims[ENCLAVE_MEASUREMENTS]["measurement_type"], "PLATFORM_MISMATCH"),
        (policy.expected_model_id, claims[MODEL_ID], "MODEL_ID_MISMATCH"),
        (policy.expected_security_mode, claims[SECURITY_MODE], "SECURITY_MODE_MISMATCH"),
    )
    for expected, actual, code in comparisons:
        if expected is not None and expected != actual:
            raise AirVerificationError(code, code.replace("_", " ").lower())
    if policy.require_nonce and EAT_NONCE not in claims:
        raise AirVerificationError("NONCE_REQUIRED", "receipt has no nonce")
    if claims[SECURITY_MODE] == "evaluation" and not policy.allow_evaluation_mode:
        raise AirVerificationError("EVALUATION_NOT_ALLOWED", "evaluation receipts are not allowed")
    if policy.max_age_seconds:
        iat = claims[CWT_IAT]
        if iat > now + policy.clock_skew_seconds or now - iat > policy.max_age_seconds + policy.clock_skew_seconds:
            raise AirVerificationError("TIMESTAMP_STALE", "receipt timestamp is outside the accepted window")


def verify_receipt(
    receipt: bytes,
    public_key: VerifyKey | bytes,
    policy: AirPolicy | None = None,
    *,
    now: int | None = None,
) -> dict[int, Any]:
    """Verify envelope, Ed25519 signature, claims, and caller policy."""

    if len(receipt) > 65_536:
        raise AirVerificationError("RECEIPT_TOO_LARGE", "receipt exceeds 65536 bytes")
    tagged = _loads_exact(receipt, "COSE_DECODE_FAILED")
    if not isinstance(tagged, cbor2.CBORTag) or tagged.tag != COSE_SIGN1_TAG:
        raise AirVerificationError("BAD_COSE_TAG", "AIR requires CBOR tag 18")
    value = tagged.value
    # cbor2 6.x decodes arrays contained in semantic tags as immutable tuples,
    # while older releases returned lists. Both represent the same CBOR array.
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise AirVerificationError("BAD_COSE", "COSE_Sign1 must contain four elements")
    protected, unprotected, payload, signature = value
    if not isinstance(protected, bytes) or not isinstance(payload, bytes) or not isinstance(signature, bytes):
        raise AirVerificationError("BAD_COSE", "invalid COSE_Sign1 field types")
    if unprotected != {}:
        raise AirVerificationError("UNPROTECTED_NOT_EMPTY", "AIR unprotected header must be empty")
    if len(signature) != 64:
        raise AirVerificationError("BAD_SIG_LENGTH", "Ed25519 signature must be 64 bytes")
    header = _loads_exact(protected, "BAD_PROTECTED_HEADER")
    if header != PROTECTED_HEADER:
        raise AirVerificationError("BAD_PROTECTED_HEADER", "AIR requires EdDSA and application/cwt")
    key = public_key if isinstance(public_key, VerifyKey) else VerifyKey(public_key)
    try:
        key.verify(_canonical(["Signature1", protected, b"", payload]), signature)
    except BadSignatureError as exc:
        raise AirVerificationError("SIG_FAILED", "Ed25519 signature verification failed") from exc
    claims = _validate_claims(_loads_exact(payload, "BAD_PAYLOAD"))
    _apply_policy(claims, policy or AirPolicy(), int(time.time()) if now is None else now)
    return claims

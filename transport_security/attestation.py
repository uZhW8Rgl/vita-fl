"""Challenge-bound TLS/AIR evidence and fail-closed Phala TDX appraisal.

The remote verifier authenticates Intel quote cryptography. Application identity,
TLS key ownership, freshness and deployment policy are checked independently.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

import cbor2
from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

SESSION_DOMAIN = b"MasterThesis.RATLS.session.v1\x00"
SESSION_MAX_LIFETIME = 300
SESSION_CLOCK_SKEW = 30
MAX_SESSION_EVIDENCE_BYTES = 262_144
MAX_QUOTE_BYTES = 65_536
MAX_API_RESPONSE_BYTES = 1_048_576
DEFAULT_PHALA_VERIFY_URL = "https://cloud-api.phala.com/api/v1/attestations/verify"
TDX_HEADER_SIZE = 48
TDX_REPORTDATA_OFFSET = TDX_HEADER_SIZE + 520
TDX_REPORTDATA_SIZE = 64
TDX_MEASUREMENT_OFFSETS = {"mrtd": 136, "rtmr0": 328, "rtmr1": 376, "rtmr2": 424, "rtmr3": 472}
SESSION_CLAIM_KEYS = {
    "version",
    "challenge",
    "tls_spki_sha256",
    "air_public_key",
    "sello_public_key",
    "origin",
    "issued_at",
    "expires_at",
}


class AttestationVerificationError(ValueError):
    """Attestation or its binding to the intended connection was rejected."""


def ratls_enabled() -> bool:
    mode = os.environ.get("TEE_TRANSPORT_MODE", "mtls").strip().lower()
    if mode not in {"mtls", "ratls"}:
        raise AttestationVerificationError("TEE_TRANSPORT_MODE must be mtls or ratls")
    return mode == "ratls"


def canonical(value: Any) -> bytes:
    return cbor2.dumps(value, canonical=True)


def _bytes(value: Any, size: int, name: str) -> bytes:
    if not isinstance(value, bytes) or len(value) != size:
        raise AttestationVerificationError(f"{name} must contain exactly {size} bytes")
    return value


def normalize_origin(value: str) -> str:
    if not isinstance(value, str):
        raise AttestationVerificationError("attestation origin must be HTTPS")
    parts = urllib.parse.urlsplit(value)
    try:
        port = parts.port
    except ValueError as exc:
        raise AttestationVerificationError("invalid attestation origin port") from exc
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise AttestationVerificationError("attestation origin must be an origin-only HTTPS URL")
    return urllib.parse.urlunsplit(("https", parts.netloc.lower(), "", "", ""))


def tls_spki_sha256(certificate_der: bytes) -> bytes:
    try:
        certificate = x509.load_der_x509_certificate(certificate_der)
        spki = certificate.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    except (ValueError, TypeError) as exc:
        raise AttestationVerificationError("invalid TLS peer certificate") from exc
    return hashlib.sha256(spki).digest()


def validate_session_claims(claims: Any) -> dict[str, Any]:
    if not isinstance(claims, dict) or set(claims) != SESSION_CLAIM_KEYS:
        raise AttestationVerificationError("session claims contain missing or unknown fields")
    if type(claims["version"]) is not int or claims["version"] != 1:
        raise AttestationVerificationError("unsupported attestation session version")
    for name in ("challenge", "tls_spki_sha256", "air_public_key", "sello_public_key"):
        _bytes(claims[name], 32, name)
    if normalize_origin(claims["origin"]) != claims["origin"]:
        raise AttestationVerificationError("session origin must be canonical")
    for name in ("issued_at", "expires_at"):
        if type(claims[name]) is not int or claims[name] < 0:
            raise AttestationVerificationError(f"invalid session {name}")
    if not 0 < claims["expires_at"] - claims["issued_at"] <= SESSION_MAX_LIFETIME:
        raise AttestationVerificationError("invalid attestation session lifetime")
    return claims


def session_report_data(claims: Mapping[str, Any]) -> bytes:
    value = validate_session_claims(dict(claims))
    return hashlib.sha512(SESSION_DOMAIN + canonical(value)).digest()


def encode_session_evidence(claims: dict[str, Any], quote: bytes, event_log: str, app_compose: str) -> bytes:
    evidence = canonical(
        {
            "version": 1,
            "claims": validate_session_claims(claims),
            "quote": quote,
            "event_log": event_log,
            "app_compose": app_compose,
        }
    )
    decode_session_evidence(evidence)
    return evidence


def decode_session_evidence(evidence: bytes) -> dict[str, Any]:
    if not isinstance(evidence, bytes) or not 0 < len(evidence) <= MAX_SESSION_EVIDENCE_BYTES:
        raise AttestationVerificationError("session evidence exceeds the allowed size")
    try:
        value = cbor2.loads(evidence)
        if canonical(value) != evidence:
            raise AttestationVerificationError("session evidence is not deterministic CBOR")
    except (ValueError, TypeError, cbor2.CBORDecodeError) as exc:
        raise AttestationVerificationError("invalid canonical session evidence") from exc
    if not isinstance(value, dict) or set(value) != {"version", "claims", "quote", "event_log", "app_compose"}:
        raise AttestationVerificationError("invalid session evidence fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise AttestationVerificationError("unsupported session evidence version")
    validate_session_claims(value["claims"])
    if not isinstance(value["quote"], bytes) or not 0 < len(value["quote"]) <= MAX_QUOTE_BYTES:
        raise AttestationVerificationError("invalid session quote size")
    if not isinstance(value["event_log"], str) or not isinstance(value["app_compose"], str):
        raise AttestationVerificationError("session deployment evidence must be text")
    return value


@dataclass(frozen=True)
class SessionBinding:
    session_id: str
    claims: Mapping[str, Any]
    evidence: bytes

    @classmethod
    def from_evidence(cls, evidence: bytes) -> "SessionBinding":
        value = decode_session_evidence(evidence)
        return cls(hashlib.sha256(evidence).hexdigest(), MappingProxyType(value["claims"]), evidence)


@dataclass(frozen=True)
class VerifiedSession(SessionBinding):
    quote_verification: Mapping[str, Any]


def parse_tdx_quote(quote: bytes) -> dict[str, Any]:
    # This parser only extracts fields. It never substitutes for quote signature verification.
    if not isinstance(quote, bytes) or not 636 < len(quote) <= MAX_QUOTE_BYTES:
        raise AttestationVerificationError("invalid TDX quote length")
    if int.from_bytes(quote[:2], "little") != 4 or int.from_bytes(quote[4:8], "little") != 0x81:
        raise AttestationVerificationError("only Intel TDX Quote V4 is supported")
    if int.from_bytes(quote[2:4], "little") != 2:
        raise AttestationVerificationError("unsupported TDX attestation key type")
    signature_size = int.from_bytes(quote[632:636], "little")
    signature_end = 636 + signature_size
    if signature_size == 0 or signature_end > len(quote):
        raise AttestationVerificationError("TDX quote signature length is inconsistent")
    # dstack can return a zero-padded quote buffer. Match the on-chain V4Parser:
    # only zero bytes may follow the declared signature section. Keep the full
    # original quote for evidence hashes and cryptographic verification.
    if any(quote[signature_end:]):
        raise AttestationVerificationError("TDX quote contains nonzero trailing data")
    attributes = int.from_bytes(quote[TDX_HEADER_SIZE + 120 : TDX_HEADER_SIZE + 128], "little")
    if attributes & 1:
        raise AttestationVerificationError("debug TDX guests are forbidden")
    return {
        **{
            name: quote[TDX_HEADER_SIZE + offset : TDX_HEADER_SIZE + offset + 48]
            for name, offset in TDX_MEASUREMENT_OFFSETS.items()
        },
        "reportdata": quote[TDX_REPORTDATA_OFFSET : TDX_REPORTDATA_OFFSET + TDX_REPORTDATA_SIZE],
    }


def allowed_platform_measurements() -> tuple[dict[str, bytes], ...]:
    raw = os.environ.get("RATLS_ALLOWED_PLATFORM_MEASUREMENTS", "")
    try:
        values = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise AttestationVerificationError("RATLS_ALLOWED_PLATFORM_MEASUREMENTS must contain a JSON allowlist") from exc
    if not isinstance(values, list) or not 1 <= len(values) <= 32:
        raise AttestationVerificationError("TDX platform allowlist must contain 1 to 32 measurement tuples")
    result = []
    for value in values:
        if not isinstance(value, dict) or set(value) != {"mrtd", "rtmr0", "rtmr1", "rtmr2"}:
            raise AttestationVerificationError("each platform tuple must contain mrtd and rtmr0 through rtmr2")
        entry = {}
        for name, digest in value.items():
            if not isinstance(digest, str) or not re.fullmatch(r"[a-fA-F0-9]{96}", digest):
                raise AttestationVerificationError(f"platform {name} must be a 48-byte hexadecimal measurement")
            entry[name] = bytes.fromhex(digest)
        result.append(entry)
    return tuple(result)


def _api_configuration() -> tuple[str, float]:
    endpoint = os.environ.get("PHALA_ATTESTATION_VERIFY_URL", DEFAULT_PHALA_VERIFY_URL)
    parsed = urllib.parse.urlsplit(endpoint)
    configured = os.environ.get("PHALA_ATTESTATION_ALLOWED_HOSTS", "cloud-api.phala.com")
    hosts = {host.strip().lower() for host in configured.split(",") if host.strip()}
    try:
        valid_port = parsed.port in {None, 443}
    except ValueError as exc:
        raise AttestationVerificationError("invalid Phala verifier URL port") from exc
    if (
        parsed.scheme != "https"
        or parsed.hostname not in hosts
        or not valid_port
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/api/v1/attestations/verify"
    ):
        raise AttestationVerificationError("Phala verifier URL violates the HTTPS endpoint allowlist")
    try:
        timeout = float(os.environ.get("PHALA_ATTESTATION_TIMEOUT_SECONDS", "20"))
    except ValueError as exc:
        raise AttestationVerificationError("invalid Phala verifier timeout") from exc
    if not 0 < timeout <= 30:
        raise AttestationVerificationError("Phala verifier timeout must be positive and at most 30 seconds")
    return endpoint, timeout


def validate_attestation_configuration() -> None:
    allowed_platform_measurements()
    _api_configuration()


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AttestationVerificationError("Phala verifier redirects are forbidden")


def _unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise AttestationVerificationError("duplicate Phala response field")
        result[key] = value
    return result


def _hex_field(value: Any, size: int, name: str) -> bytes:
    if not isinstance(value, str) or not re.fullmatch(r"(?:0x)?[a-fA-F0-9]{" + str(size * 2) + "}", value):
        raise AttestationVerificationError(f"invalid Phala quote {name}")
    return bytes.fromhex(value.removeprefix("0x"))


def _read_phala_response(opener, request, *, deadline: float, media_type: str, limit: int, operation: str) -> bytes:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AttestationVerificationError("Phala quote verification deadline exceeded")
    try:
        with opener.open(request, timeout=remaining) as response:
            if response.status != 200:
                raise AttestationVerificationError(f"{operation} returned HTTP {response.status}")
            if response.headers.get_content_type() != media_type:
                raise AttestationVerificationError(f"{operation} returned an unexpected content type")
            raw = response.read(limit + 1)
    except urllib.error.HTTPError as exc:
        raise AttestationVerificationError(f"{operation} is unavailable (HTTP {exc.code})") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise AttestationVerificationError(f"{operation} is unavailable") from exc
    if time.monotonic() >= deadline:
        raise AttestationVerificationError("Phala quote verification deadline exceeded")
    if len(raw) > limit:
        raise AttestationVerificationError(f"{operation} response exceeds the size limit")
    return raw


def verify_quote_with_phala(quote: bytes, expected_report_data: bytes) -> dict[str, Any]:
    fields = parse_tdx_quote(quote)
    if not hmac.compare_digest(fields["reportdata"], _bytes(expected_report_data, 64, "expected REPORTDATA")):
        raise AttestationVerificationError("quote REPORTDATA does not match the requested binding")
    allowed = allowed_platform_measurements()
    if not any(all(hmac.compare_digest(fields[name], digest) for name, digest in entry.items()) for entry in allowed):
        raise AttestationVerificationError("TDX base measurements violate the platform allowlist")
    endpoint, timeout = _api_configuration()
    deadline = time.monotonic() + timeout
    request = urllib.request.Request(
        endpoint,
        data=json.dumps({"hex": quote.hex()}, separators=(",", ":")).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "vita-fl-attestation/1",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        _NoRedirect(),
    )
    raw = _read_phala_response(
        opener,
        request,
        deadline=deadline,
        media_type="application/json",
        limit=MAX_API_RESPONSE_BYTES,
        operation="Phala quote verification",
    )
    try:
        value = json.loads(raw, object_pairs_hook=_unique_json)
    except (ValueError, UnicodeError) as exc:
        raise AttestationVerificationError("Phala verifier returned invalid JSON") from exc
    if not isinstance(value, dict) or value.get("success") is not True:
        raise AttestationVerificationError("Phala rejected the quote")
    verified_quote = value.get("quote")
    if not isinstance(verified_quote, dict) or verified_quote.get("verified") is not True:
        raise AttestationVerificationError("Phala did not cryptographically verify the quote")
    provider_checksum = _hex_field(value.get("checksum"), 32, "checksum").hex()
    body = verified_quote.get("body")
    if not isinstance(body, dict):
        raise AttestationVerificationError("Phala response lacks the verified quote body")
    for name, expected in fields.items():
        actual = _hex_field(body.get(name), len(expected), name)
        if not hmac.compare_digest(actual, expected):
            raise AttestationVerificationError(f"Phala verified {name} differs from the submitted quote")
    # The provider checksum is an opaque lookup identifier, not necessarily the
    # SHA-256 of the submitted bytes. Bind its verified record to the exact quote
    # through the provider's raw endpoint on the same approved HTTPS origin.
    parsed_endpoint = urllib.parse.urlsplit(endpoint)
    raw_url = urllib.parse.urlunsplit(
        (parsed_endpoint.scheme, parsed_endpoint.netloc, f"/api/v1/attestations/raw/{provider_checksum}", "", "")
    )
    raw_request = urllib.request.Request(
        raw_url,
        headers={"Accept": "application/octet-stream", "User-Agent": "vita-fl-attestation/1"},
        method="GET",
    )
    verified_raw = _read_phala_response(
        opener,
        raw_request,
        deadline=deadline,
        media_type="application/octet-stream",
        limit=MAX_QUOTE_BYTES,
        operation="Phala raw quote retrieval",
    )
    if not hmac.compare_digest(verified_raw, quote):
        raise AttestationVerificationError("Phala verified raw quote differs from the submitted quote")
    return {
        "provider": "phala",
        "endpoint": endpoint,
        "verified": True,
        "quote_sha256": hashlib.sha256(quote).hexdigest(),
        "provider_checksum": provider_checksum,
        "verified_at": int(time.time()),
        "platform_measurements": {name: value.hex() for name, value in fields.items() if name != "reportdata"},
    }


def verify_session_evidence(
    evidence: bytes,
    challenge: bytes,
    peer_cert_der: bytes,
    expected_origin: str,
    expected_sello_key: bytes,
    *,
    deployment_validator: Callable[[str, str, bytes], Any] | None = None,
    now: int | None = None,
) -> VerifiedSession:
    if deployment_validator is None:
        raise AttestationVerificationError("a deployment policy validator is required")
    value = decode_session_evidence(evidence)
    claims = value["claims"]
    if not hmac.compare_digest(claims["challenge"], _bytes(challenge, 32, "client challenge")):
        raise AttestationVerificationError("attestation challenge does not match this connection")
    if not hmac.compare_digest(claims["tls_spki_sha256"], tls_spki_sha256(peer_cert_der)):
        raise AttestationVerificationError("attested TLS key is not the connected TLS peer key")
    if not hmac.compare_digest(claims["sello_public_key"], _bytes(expected_sello_key, 32, "registered Sello key")):
        raise AttestationVerificationError("attested Sello key differs from the admitted receiver key")
    if claims["origin"] != normalize_origin(expected_origin):
        raise AttestationVerificationError("attested origin differs from the intended receiver")
    current = int(time.time()) if now is None else now
    if claims["issued_at"] > current + SESSION_CLOCK_SKEW or current >= claims["expires_at"]:
        raise AttestationVerificationError("attestation session is stale or issued in the future")
    appraisal = verify_quote_with_phala(value["quote"], session_report_data(claims))
    deployment_validator(value["event_log"], value["app_compose"], value["quote"])
    return VerifiedSession(
        hashlib.sha256(evidence).hexdigest(),
        MappingProxyType(claims),
        evidence,
        MappingProxyType(appraisal),
    )


def air_report_data(public_key: bytes, manifest_hash: bytes, request_hash: bytes, session: SessionBinding) -> bytes:
    """AIR bundle v2 binds its signing identity and request to this TLS session."""
    evidence = SessionBinding.from_evidence(session.evidence)
    if evidence.session_id != session.session_id or dict(evidence.claims) != dict(session.claims):
        raise AttestationVerificationError("inconsistent session binding")
    identity = hashlib.sha256(
        b"MasterThesis.AIR.key.v2"
        + _bytes(public_key, 32, "AIR public key")
        + _bytes(manifest_hash, 32, "model manifest hash")
        + bytes.fromhex(session.session_id)
        + _bytes(session.claims["tls_spki_sha256"], 32, "TLS SPKI hash")
    ).digest()
    return identity + _bytes(request_hash, 32, "AIR request hash")

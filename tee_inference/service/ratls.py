"""Issue short-lived TLS/AIR sessions from the worker's real dstack socket."""

from __future__ import annotations

import hmac
import json
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding

from tee_inference.service.attestation import DstackClient, derive_air_signing_key
from transport_security.attestation import (
    SESSION_MAX_LIFETIME,
    AttestationVerificationError,
    SessionBinding,
    encode_session_evidence,
    normalize_origin,
    parse_tdx_quote,
    session_report_data,
    tls_spki_sha256,
)


class RatlsSessionManager:
    """Bounded, process-local session cache; unknown/expired sessions fail closed.

    Expiry is enforced when a protected operation is admitted. An operation
    already admitted may finish after that deadline and still emit its receipt.
    """

    def __init__(
        self,
        sello_public_key: bytes,
        origin: str,
        *,
        client: DstackClient | None = None,
        certificate_path: str | Path | None = None,
        lifetime_seconds: int = SESSION_MAX_LIFETIME,
        max_sessions: int = 256,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(sello_public_key, bytes) or len(sello_public_key) != 32:
            raise AttestationVerificationError("Sello session key must contain 32 bytes")
        if type(lifetime_seconds) is not int or not 0 < lifetime_seconds <= SESSION_MAX_LIFETIME:
            raise AttestationVerificationError("invalid RA-TLS session lifetime")
        if type(max_sessions) is not int or not 1 <= max_sessions <= 4096:
            raise AttestationVerificationError("invalid RA-TLS session cache bound")
        self.client = client if client is not None else DstackClient()
        self.air_public_key = bytes(derive_air_signing_key(self.client).verify_key)
        self.sello_public_key = sello_public_key
        self.origin = normalize_origin(origin)
        self.certificate_path = Path(certificate_path or os.environ.get("TLS_CERT_PATH", ""))
        self.lifetime_seconds = lifetime_seconds
        self.max_sessions = max_sessions
        self.clock = clock
        self._lock = threading.RLock()
        self._sessions: OrderedDict[str, SessionBinding] = OrderedDict()
        self._tls_key_hash()  # Reject missing or malformed server identity at startup.

    def _tls_key_hash(self) -> bytes:
        try:
            certificate = x509.load_pem_x509_certificate(self.certificate_path.read_bytes())
            return tls_spki_sha256(certificate.public_bytes(Encoding.DER))
        except (OSError, ValueError) as exc:
            raise AttestationVerificationError("RA-TLS server certificate is unavailable") from exc

    def _prune(self, now: int) -> None:
        expired = [key for key, value in self._sessions.items() if value.claims["expires_at"] <= now]
        for key in expired:
            del self._sessions[key]

    def create_session(self, challenge: bytes) -> bytes:
        if not isinstance(challenge, bytes) or len(challenge) != 32:
            raise AttestationVerificationError("attestation challenge must contain exactly 32 bytes")
        now = int(self.clock())
        with self._lock:
            self._prune(now)
            if len(self._sessions) >= self.max_sessions:
                raise AttestationVerificationError("RA-TLS session capacity is exhausted")
        claims = {
            "version": 1,
            "challenge": challenge,
            "tls_spki_sha256": self._tls_key_hash(),
            "air_public_key": self.air_public_key,
            "sello_public_key": self.sello_public_key,
            "origin": self.origin,
            "issued_at": now,
            "expires_at": now + self.lifetime_seconds,
        }
        report_data = session_report_data(claims)
        quote_result = self.client.call("/GetQuote", {"report_data": report_data.hex()})
        info = self.client.call("/Info", {})
        try:
            quote = bytes.fromhex(str(quote_result["quote"]).removeprefix("0x"))
            fields = parse_tdx_quote(quote)
            if not hmac.compare_digest(fields["reportdata"], report_data):
                raise AttestationVerificationError("dstack quote does not contain the session REPORTDATA")
            tcb = info.get("tcb_info", {})
            tcb = json.loads(tcb) if isinstance(tcb, str) else tcb
            if not isinstance(tcb, dict):
                raise AttestationVerificationError("dstack returned invalid TCB information")
            app_compose = tcb["app_compose"]
            event_log = quote_result["event_log"]
            if not isinstance(event_log, str):
                event_log = json.dumps(event_log, separators=(",", ":"))
            if not isinstance(app_compose, str):
                app_compose = json.dumps(app_compose, separators=(",", ":"))
        except (KeyError, TypeError, ValueError) as exc:
            raise AttestationVerificationError("dstack returned invalid session evidence") from exc
        evidence = encode_session_evidence(claims, quote, event_log, app_compose)
        session = SessionBinding.from_evidence(evidence)
        with self._lock:
            current = int(self.clock())
            self._prune(current)
            if current >= claims["expires_at"] or not hmac.compare_digest(
                claims["tls_spki_sha256"], self._tls_key_hash()
            ):
                raise AttestationVerificationError("TLS identity changed or session expired during quote generation")
            # Another quote request may have filled the cache while this quote
            # was generated. Never evict a session already issued to a client.
            if len(self._sessions) >= self.max_sessions:
                raise AttestationVerificationError("RA-TLS session capacity is exhausted")
            self._sessions[session.session_id] = session
        return evidence

    def require_session(self, session_id: str, *, tls_spki: bytes | None = None) -> SessionBinding:
        with self._lock:
            self._prune(int(self.clock()))
            if not isinstance(session_id, str) or session_id not in self._sessions:
                raise AttestationVerificationError("unknown or expired RA-TLS session")
            session = self._sessions[session_id]
            current_key = self._tls_key_hash()
            if not hmac.compare_digest(session.claims["tls_spki_sha256"], current_key):
                del self._sessions[session_id]
                raise AttestationVerificationError("RA-TLS session belongs to a previous TLS key")
            if tls_spki is not None and (
                not isinstance(tls_spki, bytes) or not hmac.compare_digest(tls_spki, current_key)
            ):
                raise AttestationVerificationError("RA-TLS session does not match this TLS connection")
            return session

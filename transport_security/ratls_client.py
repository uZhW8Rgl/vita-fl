"""Authenticate a TLS connection before sending Sello credentials or inputs.

Only a public random challenge crosses the initially unauthenticated channel.
The fresh TDX quote authenticates its actual TLS key and the approved workload.
No implicit reconnect, redirect, proxy, or resumption can bypass that check.
"""

from __future__ import annotations

import http.client
import os
import secrets
import socket as sockets
import ssl
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from cryptography import x509

from .attestation import verify_session_evidence
from .pop import PoPIdentity, proof_headers

MAX_SESSION_EVIDENCE = 256 * 1024
MAX_BOOTSTRAP_SECONDS = 30


def verify_deployment(event_log: str, app_compose: str, quote: bytes) -> None:
    # Lazy import: this transport is also imported by the legacy ZK client.
    from agent.tee_inference_client import (
        DEFAULT_TEE_IMAGE_DIGEST,
        _required_contract_policy,
        _verify_app_compose,
        _verify_rtmr3,
    )

    events, _ = _verify_rtmr3(event_log, quote)
    _verify_app_compose(
        app_compose,
        events,
        os.environ.get("TEE_INFERENCE_IMAGE_DIGEST", DEFAULT_TEE_IMAGE_DIGEST),
        *_required_contract_policy(),
    )


class _SingleConnection(http.client.HTTPSConnection):
    def connect(self):
        if getattr(self, "_connected_once", False):
            raise ValueError("RA-TLS must re-attest before reconnecting")
        self._connected_once = True
        super().connect()


class AttestedResponse:
    def __init__(self, response, connection, session):
        self._response = response
        self._connection = connection
        self.verified_session = session

    def __getattr__(self, name):
        return getattr(self._response, name)

    def close(self):
        try:
            self._response.close()
        finally:
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def open_attested(request, timeout: float, identity: PoPIdentity):
    if not isinstance(request, urllib.request.Request):
        raise ValueError("RA-TLS requires an explicit authorized HTTP request")
    parts = urlsplit(request.full_url)
    origin = urlunsplit((parts.scheme, parts.netloc.lower(), "", "", ""))
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.fragment
        or origin != identity.receiver_origin
        or identity.trusted_service_key is None
    ):
        raise ValueError("RA-TLS request must match the admission-bound receiver origin and key")
    if request.data is not None and not isinstance(request.data, bytes):
        raise ValueError("RA-TLS requires an exact byte request body")
    headers = dict(request.header_items())
    lowered = {name.lower(): value for name, value in headers.items()}
    if any(
        name in lowered
        for name in (
            "host",
            "transfer-encoding",
            "content-length",
            "x-vita-pop",
            "x-vita-attestation-session",
            "x-vita-tls-spki",
        )
    ):
        raise ValueError("RA-TLS transport controls connection and authentication headers")
    authorization = lowered.get("authorization", "")
    if not authorization.startswith("Bearer ") or not authorization[7:]:
        raise ValueError("RA-TLS requires a key-bound Sello token")
    # The certificate is not a Web-PKI assertion: its SPKI is authenticated below
    # by the hardware quote. No protected HTTP headers/body are sent before then.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 3600:
        raise ValueError("RA-TLS timeout must be between zero and 3600 seconds")
    bootstrap_budget = min(timeout, MAX_BOOTSTRAP_SECONDS)
    connection = _SingleConnection(parts.hostname, parts.port or 443, timeout=bootstrap_budget, context=context)
    deadline_expired = threading.Event()
    deadline_timer = None
    try:
        connection.connect()
        socket = connection.sock

        def abort_bootstrap():
            deadline_expired.set()
            try:
                socket.shutdown(sockets.SHUT_RDWR)
            except OSError:
                pass
            socket.close()

        # socket timeouts alone reset on every read; this also bounds slow headers
        # and a peer dribbling evidence bytes without ever completing its body.
        deadline_timer = threading.Timer(bootstrap_budget, abort_bootstrap)
        deadline_timer.daemon = True
        deadline_timer.start()
        certificate_der = socket.getpeercert(binary_form=True)
        certificate = x509.load_der_x509_certificate(certificate_der)
        now = datetime.now(timezone.utc)
        if not certificate.not_valid_before_utc <= now < certificate.not_valid_after_utc:
            raise ValueError("RA-TLS certificate is outside its validity period")
        challenge = secrets.token_bytes(32)
        connection.request(
            "POST",
            "/v1/attestation",
            body=challenge,
            headers={"Content-Type": "application/octet-stream", "Accept": "application/cbor"},
        )
        bootstrap = connection.getresponse()
        evidence = bootstrap.read(MAX_SESSION_EVIDENCE + 1)
        if bootstrap.status != 200 or len(evidence) > MAX_SESSION_EVIDENCE:
            raise ValueError("RA-TLS session attestation failed or exceeds its size limit")
        if bootstrap.will_close or connection.sock is not socket:
            raise ValueError("RA-TLS peer closed the attestation connection")
        session = verify_session_evidence(
            evidence,
            challenge,
            certificate_der,
            origin,
            identity.trusted_service_key,
            deployment_validator=verify_deployment,
        )
        deadline_timer.cancel()
        deadline_timer.join()
        if deadline_expired.is_set():
            raise TimeoutError("RA-TLS attestation deadline exceeded")
        if connection.sock is not socket or socket.fileno() < 0:
            raise ValueError("RA-TLS connection disappeared before authorization")
        socket.settimeout(timeout)
        method = request.get_method()
        headers.update(
            proof_headers(
                identity,
                method=method,
                url=request.full_url,
                body=request.data or b"",
                token=authorization[7:],
                session_id=session.session_id,
            )
        )
        headers["X-Vita-Attestation-Session"] = session.session_id
        headers["Connection"] = "close"
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        connection.request(method, path, body=request.data, headers=headers)
        response = connection.getresponse()
        wrapped = AttestedResponse(response, connection, session)
        if 300 <= response.status < 400:
            raise ValueError("Inference redirects are forbidden")
        if response.status >= 400:
            error = urllib.error.HTTPError(
                request.full_url, response.status, response.reason, response.headers, wrapped
            )
            error.verified_session = session
            raise error
        return wrapped
    except urllib.error.HTTPError:
        raise  # HTTPError owns the response and closes its authenticated socket.
    except BaseException as exc:
        connection.close()
        if deadline_expired.is_set():
            raise TimeoutError("RA-TLS attestation deadline exceeded") from exc
        raise
    finally:
        if deadline_timer is not None:
            deadline_timer.cancel()

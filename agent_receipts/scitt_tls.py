"""Authenticated SCITT transport; receipt metadata never selects TLS policy."""

from __future__ import annotations

import os
import re
import ssl
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

MAX_CERTIFICATE_BYTES = 64 * 1024
PHALA_LOG_HOST = re.compile(r"([0-9a-fA-F]{40})-8000s\.(.+\.phala\.network)")


def https_origin(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
        or any(c.isspace() for c in value)
    ):
        raise ValueError("SCITT URL must be an HTTPS origin")
    _ = parsed.port
    return urlunsplit(parsed)


def configured_log_origins() -> set[str]:
    values = [os.environ.get("SCITT_URL", "https://127.0.0.1:8000")]
    values.extend(os.environ.get("SELLO_LOG_URLS", "").split(","))
    return {https_origin(value.strip()) for value in values if value.strip()}


def _bootstrap_url(origin: str) -> str:
    """Use Web-PKI HTTPS to the *configured* runtime, never to a receipt URL."""
    explicit = os.environ.get("SCITT_CA_URL", "")
    if explicit:
        return https_origin(explicit) + "/service-cert.pem"
    parsed = urlsplit(origin)
    match = PHALA_LOG_HOST.fullmatch(parsed.hostname or "")
    if not match or parsed.port is not None:
        raise ValueError("SCITT certificate bootstrap requires the configured Phala TLS-passthrough origin")
    return f"https://{match[1]}-8002.{match[2]}/service-cert.pem"


def tls_context(origin: str) -> ssl.SSLContext:
    path = os.environ.get("SCITT_CA_CERT", "")
    bootstrap_mode = os.environ.get("SCITT_GATEWAY_CA_BOOTSTRAP", "0").lower()
    bootstrap = bootstrap_mode in {"1", "true", "yes"} or (
        bootstrap_mode == "auto" and PHALA_LOG_HOST.fullmatch(urlsplit(origin).hostname or "") is not None
    )
    if path:
        pem = Path(path).read_bytes()
    elif bootstrap or os.environ.get("SCITT_CA_URL"):
        # Certificate distribution is authenticated by the platform gateway's
        # Web-PKI identity. The log connection itself remains TLS passthrough.
        if origin not in configured_log_origins():
            raise ValueError("SCITT origin is not configured locally")
        with httpx.Client(verify=True, trust_env=False, follow_redirects=False, timeout=10) as client:
            with client.stream("GET", _bootstrap_url(origin)) as response:
                if response.status_code != 200:
                    raise ValueError("SCITT certificate bootstrap failed")
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_CERTIFICATE_BYTES:
                        raise ValueError("SCITT certificate exceeds size limit")
                    chunks.append(chunk)
                pem = b"".join(chunks)
    else:
        return ssl.create_default_context()
    if not pem or len(pem) > MAX_CERTIFICATE_BYTES or b"PRIVATE KEY" in pem:
        raise ValueError("invalid SCITT public certificate")
    # A dedicated store: a supplied CCF identity is not combined with arbitrary
    # public roots. Chain validation, hostname checks and expiry remain enabled.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_verify_locations(cadata=pem.decode("ascii"))
    return context


def create_client(url: str, *, development: bool = False):
    from pyscitt.client import Client

    if development or os.environ.get("SCITT_DEVELOPMENT", "0").lower() in {"1", "true", "yes"}:
        raise ValueError("Unverified SCITT TLS is disabled")
    origin = https_origin(url)
    context = tls_context(origin)
    expected = httpx.URL(origin)

    def enforce_origin(request: httpx.Request) -> None:
        if (request.url.scheme, request.url.host, request.url.port) != (expected.scheme, expected.host, expected.port):
            raise ValueError("SCITT redirect changed the configured HTTPS origin")

    client = Client(origin, development=False)
    client.session.close()
    client.session = httpx.Client(
        base_url=origin,
        verify=context,
        trust_env=False,
        follow_redirects=False,
        timeout=30,
        params={"api-version": client.api_version} if client.api_version else None,
        event_hooks={"request": [enforce_origin]},
    )
    return client

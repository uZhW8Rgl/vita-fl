"""Offline public trust-anchor export tests; no SCITT service is contacted."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import socket
import sys
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import cbor2
import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from agent import transparency_trust as trust


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        pytest.fail("Trust-anchor unit tests must not open network connections")

    monkeypatch.setattr(socket, "create_connection", denied)
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setenv("SCITT_URL", "https://scitt.example")
    monkeypatch.setenv("SCITT_DEVELOPMENT", "false")


@pytest.fixture
def key():
    public = ec.derive_private_key(1, ec.SECP256R1()).public_key().public_numbers()
    return {1: 2, 2: b"offline-public-key", 3: -7, -1: 1, -2: public.x.to_bytes(32), -3: public.y.to_bytes(32)}


@pytest.fixture
def provider(monkeypatch, key):
    state = SimpleNamespace(raw=cbor2.dumps([key]), status=200, calls=[], clients=[], stream=None)

    def handle(request):
        state.calls.append(request)
        return httpx.Response(
            state.status,
            content=state.raw if state.stream is None else None,
            stream=state.stream,
            headers={"Location": "https://untrusted.example/private"} if state.status == 302 else {},
        )

    def client(url, development):
        session = httpx.Client(base_url=url, transport=httpx.MockTransport(handle), follow_redirects=False)
        state.clients.append((url, development, session))
        return SimpleNamespace(session=session)

    monkeypatch.setattr(trust, "_client", client)
    return state


def rejected(provider):
    with pytest.raises(trust.TrustAnchorError) as error:
        trust.fetch_scitt_trust_anchors()
    assert str(error.value) == "SCITT trust anchors unavailable"
    assert all(session.is_closed for _, _, session in provider.clients)


def test_export_preserves_exact_public_key_bytes_and_identifies_bootstrap(provider):
    result = trust.fetch_scitt_trust_anchors()
    assert result == {
        "schema_version": 1,
        "service_url": "https://scitt.example",
        "scitt_keys_cbor_base64": base64.b64encode(provider.raw).decode(),
        "scitt_keys_sha256": hashlib.sha256(provider.raw).hexdigest(),
        "trust_bootstrap": "authenticated-deployment-ui",
    }
    assert len(provider.calls) == 1
    assert provider.calls[0].method == "GET"
    assert str(provider.calls[0].url) == "https://scitt.example/.well-known/scitt-keys"
    assert provider.clients[0][:2] == ("https://scitt.example", False)
    assert provider.clients[0][2].is_closed


@pytest.mark.parametrize("count", [1, 32, 33])
def test_key_count_bounds(provider, key, count):
    provider.raw = cbor2.dumps([{**key, 2: f"key-{index}".encode()} for index in range(count)])
    if count <= 32:
        assert trust.fetch_scitt_trust_anchors()["scitt_keys_sha256"] == hashlib.sha256(provider.raw).hexdigest()
    else:
        rejected(provider)


@pytest.mark.parametrize(
    "fault",
    [
        "private",
        "unknown",
        "duplicate-kid",
        "equivalent-text-kid",
        "empty-kid",
        "missing-coordinate",
        "bad-point",
        "wrong-kty",
    ],
)
def test_only_valid_distinct_public_ec_keys_are_exported(provider, key, fault):
    keys = [copy.deepcopy(key)]
    if fault == "private":
        keys[0][-4] = b"private-material-must-not-leak"
    elif fault == "unknown":
        keys[0][999] = "private-material-must-not-leak"
    elif fault == "duplicate-kid":
        keys.append(copy.deepcopy(key))
    elif fault == "equivalent-text-kid":
        keys.append({**key, 2: key[2].decode()})
    elif fault == "empty-kid":
        keys[0][2] = b""
    elif fault == "missing-coordinate":
        del keys[0][-2]
    elif fault == "bad-point":
        keys[0][-2] = keys[0][-3] = bytes(32)
    else:
        keys[0][1] = 3
    provider.raw = cbor2.dumps(keys)
    rejected(provider)


@pytest.mark.parametrize("raw", [b"", b"invalid", cbor2.dumps([]), cbor2.dumps({1: 2})])
def test_invalid_cbor_keyset_shapes_fail_closed(provider, raw):
    provider.raw = raw
    rejected(provider)


@pytest.mark.parametrize("trailing", [cbor2.dumps({"private": "not-exportable"}), b"\x78"])
def test_trailing_cbor_object_is_rejected(provider, trailing):
    provider.raw += trailing
    rejected(provider)


@pytest.mark.parametrize("status", [302, 401, 500])
def test_http_failures_and_redirects_are_not_followed_or_reflected(provider, status):
    provider.status = status
    provider.raw = b"upstream-private-error-detail"
    rejected(provider)
    assert len(provider.calls) == 1


class Chunks(httpx.SyncByteStream):
    def __init__(self, count=80, callback=None):
        self.count, self.callback, self.read_count = count, callback, 0

    def __iter__(self):
        for _ in range(self.count):
            self.read_count += 1
            if self.callback:
                self.callback()
            yield b"x" * 1024


def test_stream_is_aborted_after_64_kib(provider):
    provider.stream = Chunks()
    rejected(provider)
    assert provider.stream.read_count == 65


def test_total_deadline_bounds_streaming(provider, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(trust.time, "monotonic", lambda: clock.now)
    provider.stream = Chunks(callback=lambda: setattr(clock, "now", 111.0))
    rejected(provider)
    assert provider.stream.read_count == 1


@pytest.mark.parametrize(
    "url",
    [
        "http://scitt.example",
        "https://user:secret@scitt.example",
        "https://scitt.example/path",
        "https://scitt.example?url=evil",
        "https://scitt.example/#secret",
        "https://scitt.example:99999",
    ],
)
def test_configuration_requires_an_https_origin_without_selectors(provider, monkeypatch, url):
    monkeypatch.setenv("SCITT_URL", url)
    rejected(provider)
    assert provider.clients == []


def test_defaults_match_scitt_publisher(provider, monkeypatch):
    monkeypatch.delenv("SCITT_URL")
    monkeypatch.delenv("SCITT_DEVELOPMENT")
    result = trust.fetch_scitt_trust_anchors()
    assert result["service_url"] == "https://127.0.0.1:8000"
    assert provider.clients[0][:2] == ("https://127.0.0.1:8000", True)


@pytest.mark.parametrize("development", [False, True])
def test_sdk_transport_disables_proxies_redirects_and_has_bounded_timeout(monkeypatch, development):
    import pyscitt.client

    original_session = Mock()
    sdk = SimpleNamespace(session=original_session, api_version="2024-01")
    sdk_constructor = Mock(return_value=sdk)
    monkeypatch.setattr(pyscitt.client, "Client", sdk_constructor)
    strict_session = Mock()
    http_constructor = Mock(return_value=strict_session)
    monkeypatch.setattr(trust.httpx, "Client", http_constructor)
    assert trust._client("https://scitt.example", development) is sdk
    sdk_constructor.assert_called_once_with("https://scitt.example", development=development)
    original_session.close.assert_called_once()
    assert sdk.session is strict_session
    assert http_constructor.call_args.kwargs == {
        "base_url": "https://scitt.example",
        "verify": not development,
        "trust_env": False,
        "follow_redirects": False,
        "timeout": 10.0,
        "params": {"api-version": "2024-01"},
    }


@pytest.fixture
def app(monkeypatch):
    import uvicorn

    from agent import run_agent

    captured = {}

    class Server:
        def __init__(self, config):
            captured["app"] = config.app

        async def serve(self):
            pass

    monkeypatch.setitem(sys.modules, "mcp_server", SimpleNamespace(reset_mcp_tool_call_metrics=lambda: None))
    monkeypatch.setattr(run_agent, "AgentRuntime", lambda args: SimpleNamespace())
    monkeypatch.setattr(uvicorn, "Server", Server)
    asyncio.run(run_agent.serve_agent(SimpleNamespace(host="127.0.0.1", port=0)))
    return captured["app"]


def request(app, method="GET", suffix=""):
    async def invoke():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://ui.example") as client:
            return await client.request(method, "/api/transparency/trust-anchors" + suffix)

    return asyncio.run(invoke())


def test_route_ignores_url_selectors_and_exports_only_configured_service(app, provider):
    response = request(app, suffix="?url=https://untrusted.example&development=true")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json()["service_url"] == "https://scitt.example"
    assert provider.clients[0][:2] == ("https://scitt.example", False)
    assert len(provider.calls) == 1


def test_route_runs_fetch_off_event_loop_and_sanitizes_failure(app, monkeypatch):
    caller_thread = threading.get_ident()
    threads = []

    def fail():
        threads.append(threading.get_ident())
        raise trust.TrustAnchorError("upstream-private-detail")

    monkeypatch.setattr(trust, "fetch_scitt_trust_anchors", fail)
    response = request(app)
    assert threads and threads[0] != caller_thread
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "no-store"
    assert response.json() == {"detail": "SCITT trust anchors unavailable"}


def test_route_is_read_only(app, provider):
    response = request(app, method="POST")
    assert response.status_code == 405
    assert provider.calls == []

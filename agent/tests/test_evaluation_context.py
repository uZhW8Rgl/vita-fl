"""Real signed AIR fixtures; synthetic quote appraisal is explicitly offline."""

import asyncio
import copy
import hashlib
import json
import sys
from types import SimpleNamespace

import cbor2
import httpx
import pytest

from agent import evaluation_context as context


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setenv("AGENT_EVALUATION_ENABLED", "true")
    with context._LOCK:
        context._CONTEXTS.clear()
    yield
    with context._LOCK:
        context._CONTEXTS.clear()


@pytest.fixture
def signed_fixture(tmp_path, monkeypatch):
    from agent.tests.test_tee_inference_client import IMAGE_DIGEST
    from tee_inference.tests.test_ratls import air_bundle

    f = air_bundle.__wrapped__(tmp_path, monkeypatch)
    reference = context.remember("a" * 32, f.bundle, cbor2.loads(f.bundle)[2], f.verified_session, IMAGE_DIGEST)
    return f, reference


def test_signed_positive_and_specific_negative_cases_preserve_original_context(signed_fixture):
    f, ref = signed_fixture
    original = context.require_context("a" * 32, ref)
    assert context.verify_candidate(original, f.bundle)["accepted"] is True
    for field, expected in (
        (3, "RESPONSE_HASH_MISMATCH"),
        (2, "REQUEST_MISMATCH"),
        (7, "RTMR3_MISMATCH"),
        (8, "COMPOSE_HASH_MISMATCH"),
        (12, "SESSION_MISMATCH"),
    ):
        altered = copy.deepcopy(cbor2.loads(f.bundle))
        if field in {2, 3}:
            nested = cbor2.loads(altered[field])
            key = 2 if field == 2 else 6
            nested[key] = bytes([nested[key][0] ^ 1]) + nested[key][1:]
            altered[field] = cbor2.dumps(nested, canonical=True)
        elif field == 7:
            events = json.loads(altered[field])
            events = events if isinstance(events, list) else events["events"]
            events.append(copy.deepcopy(next(e for e in events if e.get("imr") == 3)))
            altered[field] = json.dumps(events)
        elif field == 8:
            altered[field] += " "
        else:
            altered[field] = bytes([altered[field][0] ^ 1]) + altered[field][1:]
        candidate = cbor2.dumps(altered, canonical=True)
        reply = context.verify_candidate(original, candidate)
        assert reply["code"] == expected
        assert reply["candidate_evidence_sha256"] == hashlib.sha256(candidate).hexdigest()
    assert context.require_context("a" * 32, ref).evidence == f.bundle
    assert context.verify_candidate(original, f.bundle)["code"] == "ACCEPTED"


def test_genuine_signed_other_request_cannot_be_reused_in_original_context(signed_fixture):
    f, ref = signed_fixture
    original = context.require_context("a" * 32, ref)
    request = cbor2.loads(f.request)
    request[2] = b"d" * 16
    request = cbor2.dumps(request, canonical=True)
    response = cbor2.loads(f.response)
    response[2] = b"d" * 16
    response[3] = hashlib.sha256(request).digest()
    other = f.emitter.emit(request, cbor2.dumps(response, canonical=True), session=f.session)
    reference = context.remember("b" * 32, other, request, f.verified_session, original.image)
    assert context.verify_candidate(context.require_context("b" * 32, reference), other)["accepted"]
    assert context.verify_candidate(original, other)["code"] == "REQUEST_MISMATCH"


def test_interface_disabled_and_unknown_or_other_chat_context_are_not_accepted(signed_fixture, monkeypatch):
    _, ref = signed_fixture
    with pytest.raises(context.EvaluationUnavailable):
        context.require_context("b" * 32, ref)
    with pytest.raises(context.EvaluationUnavailable):
        context.require_context("a" * 32, dict(ref, expires_at=0))
    monkeypatch.setenv("AGENT_EVALUATION_ENABLED", "false")
    with pytest.raises(context.EvaluationUnavailable):
        context.require_context("a" * 32, ref)


def test_context_expiry_and_size_limits(signed_fixture, monkeypatch):
    f, ref = signed_fixture
    value = context.require_context("a" * 32, ref)
    with pytest.raises(context.EvaluationUnavailable):
        context.verify_candidate(value, b"x" * (context.MAX_EVIDENCE_BYTES + 1))
    monkeypatch.setattr(context.time, "monotonic", lambda: value.monotonic_expiry + 1)
    with pytest.raises(context.EvaluationUnavailable):
        context.require_context("a" * 32, ref)


def test_unrelated_error_never_becomes_a_passing_negative_test(signed_fixture):
    f, ref = signed_fixture

    def broken(*args, **kwargs):
        raise OSError("network unavailable")

    with pytest.raises(OSError):
        context.verify_candidate(context.require_context("a" * 32, ref), f.bundle, verifier=broken)


def test_chat_runtime_binds_job_and_records_actual_reverification(signed_fixture):
    f, ref = signed_fixture
    from agent.run_agent import AgentRuntime

    runtime = AgentRuntime(None)
    s = runtime.create_session()
    with pytest.raises(context.EvaluationUnavailable):
        asyncio.run(runtime.verify_tee_evidence(s["id"], "a" * 32, f.bundle))

    s["state"]["tee_reverification"] = ref
    result = asyncio.run(runtime.verify_tee_evidence(s["id"], "a" * 32, f.bundle))
    assert result["code"] == "ACCEPTED" and s["busy"] is False
    assert json.loads(s["messages"][-1]["events"][0]["detail"]) == result
    s["busy"] = True
    with pytest.raises(ValueError, match="busy"):
        asyncio.run(runtime.verify_tee_evidence(s["id"], "a" * 32, f.bundle))


def evaluation_app(monkeypatch):
    import uvicorn

    from agent import run_agent

    runtime = run_agent.AgentRuntime(None)
    monkeypatch.setattr(run_agent, "AgentRuntime", lambda _: runtime)
    monkeypatch.setitem(sys.modules, "mcp_server", SimpleNamespace(reset_mcp_tool_call_metrics=lambda: None))
    captured = {}

    class Server:
        def __init__(self, config):
            captured["app"] = config.app

        async def serve(self):
            pass

    monkeypatch.setattr(uvicorn, "Server", Server)
    asyncio.run(run_agent.serve_agent(SimpleNamespace(host="127.0.0.1", port=9999)))
    return runtime, captured["app"]


def test_http_reverification_is_opt_in_bounded_and_bound_to_original_chat(signed_fixture, monkeypatch):
    import base64

    f, ref = signed_fixture
    runtime, app = evaluation_app(monkeypatch)
    own = runtime.create_session()
    own["messages"].append({"role": "user", "content": "fixture original invocation"})
    other = runtime.create_session()
    own["state"]["tee_reverification"] = ref
    payload = {"job_id": "a" * 32, "evidence_base64": base64.b64encode(f.bundle).decode()}

    async def verify():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://internal-agent"
        ) as client:
            path = f"/api/sessions/{own['id']}/tee-inference/verify"
            good = await client.post(path, json=payload)
            assert good.status_code == 200 and good.json()["code"] == "ACCEPTED"
            denied = await client.post(f"/api/sessions/{other['id']}/tee-inference/verify", json=payload)
            assert denied.status_code == 400
            assert (await client.post(path, json={**payload, "job_id": []})).status_code == 400
            assert (await client.post(path, json={**payload, "evidence_base64": "?"})).status_code == 400
            assert (
                await client.post(
                    path, json={**payload, "evidence_base64": "a" * (4 * ((context.MAX_EVIDENCE_BYTES + 2) // 3) + 1)}
                )
            ).status_code == 413
            monkeypatch.setenv("AGENT_EVALUATION_ENABLED", "false")
            assert (await client.post(path, json=payload)).status_code == 404

    asyncio.run(verify())
    assert own["busy"] is False
    assert len(own["messages"]) == 2 and not other["messages"]


def test_http_transport_probe_requires_prepared_idle_chat_and_records_observation(monkeypatch):
    from agent import evaluation_transport

    runtime, app = evaluation_app(monkeypatch)
    session = runtime.create_session()
    calls = []

    def run(case, job, state):
        calls.append((case, job))
        return "AUTH_REJECTED", {"accepted": False, "reason_matches": True}

    monkeypatch.setattr(evaluation_transport, "run_case", run)

    async def verify():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://internal-agent"
        ) as client:
            path = f"/api/sessions/{session['id']}/evaluation/transport/changed_body"
            body = {"challenge": "a" * 64}
            assert (await client.post(path, json=body)).status_code == 409
            session["state"]["latest_tee_job"] = {"job_id": "b" * 32}
            session["busy"] = True
            assert (await client.post(path, json=body)).status_code == 409
            session["busy"] = False
            response = await client.post(path, json=body)
            assert response.status_code == 200 and response.json()["code"] == "AUTH_REJECTED"
            assert json.loads(session["messages"][-1]["events"][0]["detail"]) == response.json()
            monkeypatch.setenv("AGENT_EVALUATION_ENABLED", "false")
            assert (await client.post(path, json=body)).status_code == 404

    asyncio.run(verify())
    assert calls == [("changed_body", "b" * 32)] and session["busy"] is False

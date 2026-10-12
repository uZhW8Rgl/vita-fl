"""Opt-in live PoP/session probes using the agent's existing identity in memory.

Requests go to the frozen registry receiver; no signing seed, token or proof is
returned to the experiment host. Positive responses undergo normal Sello checks.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import secrets
import ssl
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from transport_security.attestation import verify_session_evidence
from transport_security.pop import proof_headers
from transport_security.ratls_client import MAX_SESSION_EVIDENCE, verify_deployment


def _connection(origin, timeout=30):
    parts = urlsplit(origin)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    connection = http.client.HTTPSConnection(parts.hostname, parts.port or 443, timeout=timeout, context=context)
    connection.connect()
    certificate = connection.sock.getpeercert(binary_form=True)
    parsed = x509.load_der_x509_certificate(certificate)
    now = datetime.now(timezone.utc)
    if not parsed.not_valid_before_utc <= now < parsed.not_valid_after_utc:
        connection.close()
        raise ValueError("expired TLS certificate")
    spki = parsed.public_key().public_bytes(Encoding.DER, PublicFormat.SubjectPublicKeyInfo)
    return connection, certificate, hashlib.sha256(spki).hexdigest()


def establish(call):
    connection, certificate, peer = _connection(call.receiver_origin)
    try:
        challenge = secrets.token_bytes(32)
        connection.request(
            "POST", "/v1/attestation", body=challenge, headers={"Content-Type": "application/octet-stream"}
        )
        response = connection.getresponse()
        raw = response.read(MAX_SESSION_EVIDENCE + 1)
        if response.status != 200 or len(raw) > MAX_SESSION_EVIDENCE:
            raise ValueError("attestation unavailable")
        session = verify_session_evidence(
            raw,
            challenge,
            certificate,
            call.receiver_origin,
            call.trusted_service_key,
            deployment_validator=verify_deployment,
        )
        return session, peer
    finally:
        connection.close()


def send(call, session, peer, method, path, body, headers):
    # Authenticate this actual peer before sending credential-bearing bytes.
    # Test requests deliberately replay session/proof fields; the ordinary
    # production client still performs fresh attestation on every connection.
    connection, _, actual = _connection(call.receiver_origin)
    try:
        if actual != peer or actual != session.claims["tls_spki_sha256"].hex():
            raise ValueError("TLS key changed before the request")
        connection.request(method, path, body=body, headers={**headers, "Connection": "close"})
        response = connection.getresponse()
        raw = response.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("response size limit")
        return response.status, response.headers, raw
    finally:
        connection.close()


def run_case(case, job_id, state):
    try:
        from .evaluation_context import enabled
        from .sello_client import _registered_tee_receiver, begin_receiver_call, complete_receiver_call
    except ImportError:
        from evaluation_context import enabled
        from sello_client import _registered_tee_receiver, begin_receiver_call, complete_receiver_call
    from agent_receipts.environment import owner_from_environment

    if not enabled():
        raise ValueError("evaluation disabled")
    origin, _ = _registered_tee_receiver()
    action = "generate_random_tee_chestmnist_image"
    input_bytes = b'{"index":null}'
    call = begin_receiver_call(action, "tee-inference", input_bytes, receiver_base_url=origin)
    path = "/v1/jobs"
    session, peer = establish(call)
    if case == "restart_old_session":
        old = state.get("transport_reference")
        if not old or old[1] == peer:
            raise ValueError("confirmed receiver key change required")
        try:
            from .tee_inference_client import fetch_latest_verified_tee_model_bundle
        except ImportError:
            from tee_inference_client import fetch_latest_verified_tee_model_bundle
        fetch_latest_verified_tee_model_bundle(endpoint=origin)
        session = old[0]
        # The new peer is authenticated independently above, but the request
        # deliberately retains an unknown pre-restart session identifier.
    else:
        state["transport_reference"] = (session, peer)
    owner = owner_from_environment()
    identity = call.client_identity
    token = call.token
    if case == "expired_token":
        token = owner.token(
            audience=origin,
            pop_thumbprint=identity.thumbprint,
            scopes=[action, "receipts:read"],
            now=int(time.time()) - 400,
        )
    if case == "expired_session":
        deadline = session.claims["expires_at"] + 1
        while time.time() < deadline:
            time.sleep(min(1, deadline - time.time()))
        token = owner.token(audience=origin, pop_thumbprint=identity.thumbprint, scopes=[action, "receipts:read"])
    proof_body = input_bytes
    proof_method = "GET" if case == "changed_method" else "POST"
    proof_url = origin + path + ("?other=1" if case == "changed_url" else "")
    proof_token = token + "different" if case == "changed_token" else token
    proof_sid = "0" * 64 if case == "changed_session" else session.session_id
    proof_time = int(time.time()) - 61 if case == "expired_proof" else None
    headers = {
        "Authorization": "Bearer " + token,
        "X-Vita-Attestation-Session": session.session_id,
        **proof_headers(
            identity,
            method=proof_method,
            url=proof_url,
            body=proof_body,
            token=proof_token,
            session_id=proof_sid,
            now=proof_time,
        ),
    }
    body = b'{"index":0}' if case == "changed_body" else input_bytes

    def transmit():
        # Restart uses the new, independently attested TLS key while sending
        # the old session. The peer/session mismatch is intentional here only.
        binding = session
        if case == "restart_old_session":
            from transport_security.attestation import SessionBinding

            binding = SessionBinding(
                session.session_id, {**session.claims, "tls_spki_sha256": bytes.fromhex(peer)}, session.evidence
            )
        return send(call, binding, peer, "POST", path, body, headers)

    replies = []
    if case == "concurrent_same_proof":
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: transmit(), range(2)))
    else:
        replies = [transmit()]
        if case == "same_proof_twice":
            replies.append(transmit())
    accepted = 0
    for status, response_headers, raw in replies:
        if status == 200:
            complete_receiver_call(
                call, response_headers, raw, status, receiver_base_url=origin, verified_session=session
            )
            accepted += 1
        elif status == 401:
            parsed = json.loads(raw)
            if parsed.get("error") != "Invalid attested agent authorization":
                raise ValueError("unexpected rejection reason")
        else:
            raise ValueError("unexpected HTTP outcome")
    facts = {"tls_peer_verified": True, "side_effect_count": accepted}
    if case == "same_proof_twice":
        facts.update(accepted=replies[0][0] == 200, second_accepted=replies[1][0] == 200)
        code = "REPLAY_REJECTED" if [v[0] for v in replies] == [200, 401] else "REPLAY_ACCEPTED"
    elif case == "concurrent_same_proof":
        facts["accepted_count"] = accepted
        code = "EXACTLY_ONE_ACCEPTED" if accepted == 1 else "REPLAY_ACCEPTED"
    else:
        facts.update(accepted=accepted == 1, reason_matches=accepted == 0)
        code = "ACCEPTED" if accepted else "AUTH_REJECTED"
    return code, facts

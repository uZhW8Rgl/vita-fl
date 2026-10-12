"""Bounded, opt-in re-verification of evidence from an already verified call.

Only public evidence and a previously authenticated session are retained. This
module neither signs substituted evidence nor executes a new model prediction.
The chat runtime must bind access to the session that produced the job.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

MAX_EVIDENCE_BYTES = 2 * 1024 * 1024
CAPACITY = 32
TTL_SECONDS = 300
_LOCK = threading.Lock()
_CONTEXTS = OrderedDict()


class EvaluationUnavailable(ValueError):
    pass


def enabled():
    return os.environ.get("AGENT_EVALUATION_ENABLED", "").strip() == "true"


@dataclass(frozen=True)
class Context:
    job_id: str
    evidence: bytes
    request: bytes
    session: object
    image: str
    expires_at: int
    monotonic_expiry: float

    def public(self):
        return {
            "job_id": self.job_id,
            "original_evidence_sha256": hashlib.sha256(self.evidence).hexdigest(),
            "ratls_session_id": self.session.session_id,
            "expires_at": self.expires_at,
        }


def remember(job_id, evidence, request, session, image):
    if not enabled():
        return None
    if not re.fullmatch(r"[a-f0-9]{32}", job_id) or session is None:
        raise EvaluationUnavailable("verified RA-TLS job required")
    if not isinstance(evidence, bytes) or not 0 < len(evidence) <= MAX_EVIDENCE_BYTES:
        raise EvaluationUnavailable("evidence size limit")
    now = time.monotonic()
    value = Context(job_id, evidence, request, session, image, int(time.time()) + TTL_SECONDS, now + TTL_SECONDS)
    with _LOCK:
        for key, old in list(_CONTEXTS.items()):
            if old.monotonic_expiry <= now:
                del _CONTEXTS[key]
        # Never overwrite the original reference with a substituted candidate.
        context_key = (job_id, hashlib.sha256(evidence).hexdigest())
        if context_key in _CONTEXTS:
            return _CONTEXTS[context_key].public()
        if len(_CONTEXTS) >= CAPACITY:
            raise EvaluationUnavailable("evaluation context capacity exhausted")
        _CONTEXTS[context_key] = value
    return value.public()


def require_context(job_id, expected):
    if not enabled():
        raise EvaluationUnavailable("evaluation interface disabled")
    with _LOCK:
        if not isinstance(expected, dict):
            raise EvaluationUnavailable("job is not bound to this chat session")
        context_key = (job_id, expected.get("original_evidence_sha256"))
        value = _CONTEXTS.get(context_key)
        if value is None or value.monotonic_expiry <= time.monotonic():
            _CONTEXTS.pop(context_key, None)
            raise EvaluationUnavailable("unknown or expired evaluation context")
        if value.public() != expected:
            raise EvaluationUnavailable("job is not bound to this chat session")
        return value


def verify_candidate(context, candidate, *, verifier=None):
    if not isinstance(candidate, bytes) or not 0 < len(candidate) <= MAX_EVIDENCE_BYTES:
        raise EvaluationUnavailable("evidence size limit")
    try:
        from .tee_inference_client import TeeInferenceVerificationError, verify_tee_inference_bundle
    except ImportError:
        from tee_inference_client import TeeInferenceVerificationError, verify_tee_inference_bundle
    from tee_inference.air.v1 import AirVerificationError

    code = "ACCEPTED"
    try:
        (verifier or verify_tee_inference_bundle)(
            candidate, context.request, context.image, verified_session=context.session
        )
    except AirVerificationError as exc:
        # These are the production AIR verifier's stable public rejection codes.
        if not re.fullmatch(r"[A-Z0-9_]{1,64}", exc.code):
            raise RuntimeError("unexpected AIR rejection code") from None
        code = exc.code
    except TeeInferenceVerificationError as exc:
        known = {
            "bundle request does not match the submitted request": "REQUEST_MISMATCH",
            "replayed RTMR3 does not match the signed quote": "RTMR3_MISMATCH",
            "app_compose does not match the measured compose-hash event": "COMPOSE_HASH_MISMATCH",
            "AIR evidence belongs to another RA-TLS session": "SESSION_MISMATCH",
            "request or response is bound to another model manifest": "MANIFEST_MISMATCH",
            "attested inference image digest violates local policy": "IMAGE_POLICY_MISMATCH",
        }
        if str(exc) not in known:
            # Infrastructure, parsing and unrelated failures are execution errors,
            # not a successful result for the tested mutation.
            raise RuntimeError("unclassified verifier failure") from None
        code = known[str(exc)]
    return {
        "ok": True,
        "execution_mode": "cloud-agent",
        "accepted": code == "ACCEPTED",
        "code": code,
        **context.public(),
        "candidate_evidence_sha256": hashlib.sha256(candidate).hexdigest(),
    }

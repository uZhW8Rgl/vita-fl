"""Owner-side verification of receiver-published SCITT tool receipts.

RA-TLS calls also export and register their original session evidence for later
appraisal. The JSON assessment records what the agent observed from Phala's API;
it is not a Phala-signed attestation. The agent signs the supplementary SCITT
statement, and publication failure withholds a successful verified tool result.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from agent_receipts.environment import RECEIPT_HEADER, decode_receipt_header, owner_from_environment
from agent_receipts.receiver_log import SCITT_BUNDLE_URL_HEADER, SCITT_TRANSACTION_HEADER
from agent_receipts.scitt import (
    encode_publication_bundle,
    register_verified_evidence,
    verify_publication_bundle,
)
from transport_security.attestation import VerifiedSession, validate_attestation_configuration
from transport_security.client import (
    ClientIdentity,
    capture_client_identity,
    open_receiver,
    validate_client_configuration,
)
from transport_security.pop import PoPIdentity, capture_pop_identity, tee_transport_mode

MAX_PUBLICATION_BUNDLE_BYTES = 2 * 1024 * 1024
SESSION_CONTENT_TYPE = "application/vnd.master-thesis.ratls-session+cbor"


@dataclass(frozen=True)
class ReceiverCall:
    action: str
    service: str
    token: str
    action_input: bytes
    client_identity: ClientIdentity | PoPIdentity
    trusted_service_key: bytes | None = None
    receiver_origin: str | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def _origin_only_url(value: str, name: str) -> str:
    parts = urllib.parse.urlsplit(value.strip())
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise RuntimeError(f"{name} must be an origin-only HTTPS URL")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc.lower(), "", "", ""))


def _registered_tee_receiver() -> tuple[str, bytes]:
    try:
        from .blockchain_source import read_sello_receiver
    except ImportError:
        from blockchain_source import read_sello_receiver

    rpc_url = (os.environ.get("EXPECTED_RUNTIME_RPC_URL") or os.environ.get("RPC_URL") or "").strip()
    registry_address = (
        os.environ.get("EXPECTED_DEVICE_REGISTRY_ADDRESS") or os.environ.get("REGISTRY_ADDRESS") or ""
    ).strip()
    participant_address = (
        os.environ.get("TEE_INFERENCE_PARTICIPANT_ADDRESS") or os.environ.get("ACCOUNT_ADDRESS") or ""
    ).strip()
    if not rpc_url or not registry_address or not participant_address:
        raise RuntimeError("TEE Sello verification requires RPC, DeviceRegistry, and participant configuration")
    record = read_sello_receiver(rpc_url, registry_address, participant_address)
    return _origin_only_url(str(record["public_ip"]), "registered TEE receiver endpoint"), bytes(
        record["sello_receipt_key"]
    )


def validate_receiver_security() -> None:
    """Fail at startup if owner authorization or the selected client identity is absent."""

    if owner_from_environment() is None:
        raise RuntimeError("Sello owner configuration is mandatory")
    if tee_transport_mode() == "ratls":
        capture_pop_identity()
        validate_attestation_configuration()
    else:
        validate_client_configuration()


def begin_receiver_call(
    action: str,
    service: str,
    action_input: bytes,
    *,
    receiver_base_url: str | None = None,
) -> ReceiverCall:
    owner = owner_from_environment()
    if owner is None:
        raise RuntimeError("Sello owner configuration is mandatory")
    if not receiver_base_url:
        raise RuntimeError("Receiver URL is required for audience-bound authorization")
    receiver_origin = _origin_only_url(receiver_base_url, "receiver endpoint")
    trusted_service_key = None
    if service == "tee-inference":
        if not receiver_base_url:
            raise RuntimeError("TEE Sello verification requires the receiver URL before the call")
        receiver_origin, trusted_service_key = _registered_tee_receiver()
        if _origin_only_url(receiver_base_url, "TEE receiver endpoint") != receiver_origin:
            raise RuntimeError("TEE receiver endpoint does not match its admission-bound DeviceRegistry record")
    mode = tee_transport_mode()
    if service == "tee-inference" and mode == "ratls":
        client_identity = replace(
            capture_pop_identity(),
            receiver_origin=receiver_origin,
            trusted_service_key=trusted_service_key,
        )
        token_binding = {"pop_thumbprint": client_identity.thumbprint}
    else:
        client_identity = capture_client_identity()
        token_binding = {"cert_thumbprint": client_identity.thumbprint}
    return ReceiverCall(
        action=action,
        service=service,
        token=owner.token(
            audience=receiver_origin,
            scopes=sorted({action, "receipts:read"}),
            **token_binding,
        ),
        action_input=action_input,
        client_identity=client_identity,
        trusted_service_key=trusted_service_key,
        receiver_origin=receiver_origin,
    )


def complete_receiver_call(
    call: ReceiverCall,
    response_headers: Any,
    response_body: bytes,
    status_code: int,
    *,
    receiver_base_url: str | None = None,
    publication_bundle: bytes | None = None,
    verified_session: VerifiedSession | None = None,
) -> dict[str, Any]:
    if call is None:
        raise RuntimeError("A receiver call with mandatory authorization is required")
    owner = owner_from_environment()
    if owner is None:
        raise RuntimeError("Sello owner configuration disappeared during a receiver call")
    if call.receiver_origin is not None:
        if not receiver_base_url:
            raise RuntimeError("TEE receiver URL disappeared during a Sello-verified call")
        if _origin_only_url(receiver_base_url, "TEE receiver endpoint") != call.receiver_origin:
            raise RuntimeError("TEE receiver endpoint changed during a Sello-verified call")
    pop_call = isinstance(call.client_identity, PoPIdentity)
    if pop_call:
        if not isinstance(verified_session, VerifiedSession):
            raise RuntimeError("RA-TLS Sello verification requires the original verified session")
        if (
            verified_session.claims.get("origin") != call.receiver_origin
            or verified_session.claims.get("sello_public_key") != call.trusted_service_key
        ):
            raise RuntimeError("verified session does not match the admitted Sello receiver")
        tls_key_hash = verified_session.claims.get("tls_spki_sha256")
        if not isinstance(tls_key_hash, bytes) or len(tls_key_hash) != 32:
            raise RuntimeError("verified session lacks its TLS key binding")
    header = response_headers.get(RECEIPT_HEADER)
    envelope = decode_receipt_header(header)
    verified = owner.verify(
        envelope,
        call.token,
        expected_service=call.service,
        expected_action=call.action,
        action_input=call.action_input,
        action_output=response_body,
        trusted_service_key=call.trusted_service_key,
    )
    if pop_call:
        fields = verified.body.get("service-defined-fields", {})
        if (
            fields.get("attested-session-id") != verified_session.session_id
            or fields.get("tls-spki-sha256") != tls_key_hash.hex()
        ):
            raise RuntimeError("Sello receipt does not match the original attested TLS session")
    expected_status = "success" if status_code < 400 else "error"
    if verified.body.get("result-status") != expected_status:
        raise RuntimeError("receiver receipt status does not match the HTTP result")

    bundle_path = response_headers.get(SCITT_BUNDLE_URL_HEADER)
    transaction_header = response_headers.get(SCITT_TRANSACTION_HEADER)
    if publication_bundle is None:
        if not receiver_base_url or not bundle_path:
            raise RuntimeError("receiver response lacks its SCITT publication bundle reference")
        if not bundle_path.startswith("/v1/sello/receipts/"):
            raise RuntimeError("receiver returned an invalid SCITT publication bundle path")
        bundle_url = urllib.parse.urljoin(receiver_base_url.rstrip("/") + "/", bundle_path.lstrip("/"))
        expected_origin = urllib.parse.urlsplit(receiver_base_url)
        actual_origin = urllib.parse.urlsplit(bundle_url)
        if (actual_origin.scheme, actual_origin.netloc) != (expected_origin.scheme, expected_origin.netloc):
            raise RuntimeError("receiver SCITT bundle URL changes the trusted receiver origin")
        request = urllib.request.Request(bundle_url, headers=call.headers, method="GET")
        with open_receiver(request, timeout=30, identity=call.client_identity) as response:
            publication_bundle = response.read(MAX_PUBLICATION_BUNDLE_BYTES + 1)
        if len(publication_bundle) > MAX_PUBLICATION_BUNDLE_BYTES:
            raise RuntimeError("receiver SCITT publication bundle is too large")
    transparency = verify_publication_bundle(envelope, publication_bundle)
    if transparency.get("service_url", "").rstrip("/") != verified.log_url.rstrip("/"):
        raise RuntimeError("verified receiver receipt and SCITT publication use different logs")
    if transaction_header and str(transparency.get("transaction_id")) != transaction_header:
        raise RuntimeError("receiver SCITT transaction header does not match the inclusion bundle")

    try:
        from .transparency_index import record_transparency_entry
    except ImportError:
        from transparency_index import record_transparency_entry

    call_id = hashlib.sha256(
        verified.token_ref + verified.kid + call.action.encode() + verified.envelope_sha256.encode()
    ).hexdigest()
    directory = Path("/tmp/agent-tool-receipts") / call_id
    directory.mkdir(parents=True, exist_ok=True)
    envelope_path = directory / "receiver-receipt.cose"
    envelope_path.write_bytes(envelope)
    model_id = ""
    try:
        decoded_output = json.loads(response_body)
        if isinstance(decoded_output, dict):
            model_id = str(decoded_output.get("model_id") or decoded_output.get("manifest_sha256") or "")
    except Exception:
        pass
    session_audit = None
    if pop_call:
        session_digest = hashlib.sha256(verified_session.evidence).hexdigest()
        if verified_session.session_id != session_digest:
            raise RuntimeError("verified session identifier does not match its exported evidence")
        evidence_path = directory / "attestation-session.cbor"
        assessment_path = directory / "attestation-session-verification.json"
        session_bundle_path = directory / "attestation-session-publication.cbor"
        statement_path = directory / "attestation-session-transparent-statement.cose"
        assessment = {
            "session_id": verified_session.session_id,
            "evidence_sha256": session_digest,
            "tls_spki_sha256": tls_key_hash.hex(),
            "receiver_origin": call.receiver_origin,
            "assessment_source": "agent-observed-phala-api-result",
            "phala_signed": False,
            "verifier_assessment": dict(verified_session.quote_verification),
        }
        evidence_path.write_bytes(verified_session.evidence)
        assessment_path.write_text(json.dumps(assessment, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        (directory / "receiver-publication.cbor").write_bytes(publication_bundle)
        session_registration = register_verified_evidence(
            verified_session.evidence,
            url=verified.log_url,
            transparent_statement_path=str(statement_path),
            content_type=SESSION_CONTENT_TYPE,
            identity=owner.subject,
        )
        if session_registration.get("evidence_sha256") != session_digest:
            raise RuntimeError("SCITT session statement does not bind the verified session evidence")
        if session_registration.get("service_url", "").rstrip("/") != verified.log_url.rstrip("/"):
            raise RuntimeError("SCITT session publication violates the verified receipt log policy")
        session_bundle_path.write_bytes(encode_publication_bundle(session_registration))
        session_record = record_transparency_entry(
            evidence_type="attestation-session",
            job_id=call_id,
            model_id=model_id,
            transparency=session_registration,
            verification=assessment,
        )
        session_audit = {
            **assessment,
            "evidence_path": str(evidence_path),
            "assessment_path": str(assessment_path),
            "publication_bundle_path": str(session_bundle_path),
            "transaction_id": session_registration["transaction_id"],
            "transparency_record_id": session_record["record_id"],
        }
    record = record_transparency_entry(
        evidence_type="agent-tool-receipt",
        job_id=call_id,
        model_id=model_id,
        transparency=transparency,
        verification={
            "action": call.action,
            "receiver": call.service,
            "result_status": verified.body["result-status"],
            "receiver_kid": verified.kid.hex(),
            "token_ref": verified.token_ref.hex(),
            "input_sha256": hashlib.sha256(call.action_input).hexdigest(),
            "output_sha256": hashlib.sha256(response_body).hexdigest(),
            "receiver_receipt_sha256": verified.envelope_sha256,
            **({"attestation_session": session_audit} if session_audit is not None else {}),
        },
    )
    return {
        "status": "receiver-published-and-scitt-receipt-verified",
        "action": call.action,
        "receiver": call.service,
        "call_id": call_id,
        "receiver_receipt_sha256": verified.envelope_sha256,
        "transaction_id": transparency["transaction_id"],
        "transparency_record_id": record["record_id"],
        **({"attestation_session": session_audit} if session_audit is not None else {}),
    }

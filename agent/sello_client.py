"""Owner-side verification of receiver-published SCITT tool receipts."""

from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_receipts.environment import RECEIPT_HEADER, decode_receipt_header, owner_from_environment
from agent_receipts.receiver_log import SCITT_BUNDLE_URL_HEADER, SCITT_TRANSACTION_HEADER
from agent_receipts.scitt import verify_publication_bundle

MAX_PUBLICATION_BUNDLE_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class ReceiverCall:
    action: str
    service: str
    token: str
    action_input: bytes
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


def begin_receiver_call(
    action: str,
    service: str,
    action_input: bytes,
    *,
    receiver_base_url: str | None = None,
) -> ReceiverCall | None:
    owner = owner_from_environment()
    if owner is None:
        return None
    trusted_service_key = None
    receiver_origin = None
    if service == "tee-inference":
        if not receiver_base_url:
            raise RuntimeError("TEE Sello verification requires the receiver URL before the call")
        receiver_origin, trusted_service_key = _registered_tee_receiver()
        if _origin_only_url(receiver_base_url, "TEE receiver endpoint") != receiver_origin:
            raise RuntimeError("TEE receiver endpoint does not match its admission-bound DeviceRegistry record")
    return ReceiverCall(
        action=action,
        service=service,
        token=owner.token(),
        action_input=action_input,
        trusted_service_key=trusted_service_key,
        receiver_origin=receiver_origin,
    )


def complete_receiver_call(
    call: ReceiverCall | None,
    response_headers: Any,
    response_body: bytes,
    status_code: int,
    *,
    receiver_base_url: str | None = None,
    publication_bundle: bytes | None = None,
) -> dict[str, Any] | None:
    if call is None:
        return None
    owner = owner_from_environment()
    if owner is None:
        raise RuntimeError("Sello owner configuration disappeared during a receiver call")
    if call.receiver_origin is not None:
        if not receiver_base_url:
            raise RuntimeError("TEE receiver URL disappeared during a Sello-verified call")
        if _origin_only_url(receiver_base_url, "TEE receiver endpoint") != call.receiver_origin:
            raise RuntimeError("TEE receiver endpoint changed during a Sello-verified call")
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
        with urllib.request.urlopen(bundle_url, timeout=30) as response:
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
    }

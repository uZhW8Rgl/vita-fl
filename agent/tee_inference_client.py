"""Run a ChestMNIST TEE inference and fail closed unless its evidence verifies."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import struct
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import cbor2
import numpy as np

from agent_receipts.scitt import public_registration
from tee_inference.air.v1 import (
    ATTESTATION_DOC_HASH,
    MODEL_ID,
    MODEL_VERSION,
    SEQUENCE_NUMBER,
    AirPolicy,
    verify_receipt,
)
from tee_inference.protocol.v1 import (
    LABELS,
    decode_manifest,
    decode_request,
    decode_response,
    encode_deterministic,
)

DEFAULT_TEE_INFERENCE_URL = os.environ.get(
    "TEE_INFERENCE_URL",
    "",
).rstrip("/")
DEFAULT_TEE_IMAGE_DIGEST = os.environ.get(
    "TEE_INFERENCE_IMAGE_DIGEST",
    "sha256:34bae2ce708d2c481bb6147a8d7a13711aac44bbadace52102f071545527dcd0",
)
DEFAULT_CHESTMNIST_TEST_DATA = os.environ.get(
    "CHESTMNIST_TEST_DATA",
    "data/chestmnist/test_data/test-data.npz",
)
DEFAULT_EVIDENCE_PATH = os.environ.get(
    "TEE_INFERENCE_EVIDENCE_PATH",
    "tee_inference/out/latest-evidence.cbor",
)
DEFAULT_SCITT_STATEMENT_PATH = os.environ.get(
    "SCITT_TRANSPARENT_STATEMENT_PATH",
    "tee_inference/out/latest-transparent-statement.cose",
)
MAX_HEALTH_BYTES = 16_384
MAX_EVIDENCE_BYTES = 1_048_576
TDX_HEADER_SIZE = 48
TDX_RTMR3_OFFSET = TDX_HEADER_SIZE + 472
TDX_REPORTDATA_OFFSET = TDX_HEADER_SIZE + 520
TDX_RTMR_SIZE = 48
TDX_REPORTDATA_SIZE = 64
RTMR_EVENT_TYPE = 0x08000001
EVM_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")


class TeeInferenceVerificationError(RuntimeError):
    """The remote result or its evidence failed a mandatory verification."""


def resolve_tee_inference_url(endpoint: str | None = None) -> str:
    configured = (endpoint or os.environ.get("TEE_INFERENCE_URL") or "").strip()
    if not configured:
        try:
            from .blockchain_source import read_device_record
        except ImportError:
            from blockchain_source import read_device_record

        rpc_url = (
            os.environ.get("EXPECTED_RUNTIME_RPC_URL")
            or os.environ.get("RPC_URL")
            or ""
        ).strip()
        registry_address = (
            os.environ.get("EXPECTED_DEVICE_REGISTRY_ADDRESS")
            or os.environ.get("REGISTRY_ADDRESS")
            or ""
        ).strip()
        participant_address = (
            os.environ.get("TEE_INFERENCE_PARTICIPANT_ADDRESS")
            or os.environ.get("ACCOUNT_ADDRESS")
            or ""
        ).strip()
        if not rpc_url or not registry_address or not participant_address:
            raise TeeInferenceVerificationError(
                "TEE inference endpoint is not configured and its registered participant identity cannot be resolved"
            )
        try:
            configured = str(
                read_device_record(
                    rpc_url,
                    registry_address,
                    participant_address,
                )["public_ip"]
            ).strip()
        except (KeyError, RuntimeError, ValueError) as exc:
            raise TeeInferenceVerificationError(
                f"registered TEE inference endpoint resolution failed: {exc}"
            ) from exc

    parts = urllib.parse.urlsplit(configured)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or parts.path not in {"", "/"}
    ):
        raise TeeInferenceVerificationError(
            "TEE inference endpoint must be an origin-only HTTPS URL"
        )
    return configured.rstrip("/")


def _read_limited(response: Any, limit: int, name: str) -> bytes:
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise TeeInferenceVerificationError(f"{name} exceeds {limit} bytes")
    return raw


def _prepare_model(base_url: str, timeout: int) -> dict[str, Any]:
    request = urllib.request.Request(f"{base_url.rstrip('/')}/v1/prepare", data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            value = json.loads(_read_limited(response, MAX_HEALTH_BYTES, "prepare response"))
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", errors="replace")
        raise TeeInferenceVerificationError(f"TEE inference preparation returned HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise TeeInferenceVerificationError(f"TEE inference preparation failed: {exc}") from exc
    if not isinstance(value, dict) or value.get("status") != "ok":
        raise TeeInferenceVerificationError("TEE inference preparation response is not ready")
    return value


def _json_request(
    base_url: str,
    path: str,
    timeout: int,
    payload: dict[str, Any] | None = None,
    *,
    receipt_action: str | None = None,
) -> dict[str, Any]:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    headers = {} if data is None else {"Content-Type": "application/json"}
    receiver_call = None
    if receipt_action is not None:
        try:
            from .sello_client import begin_receiver_call
        except ImportError:
            from sello_client import begin_receiver_call
        receiver_call = begin_receiver_call(receipt_action, "tee-inference", data or b"")
        if receiver_call is not None:
            headers.update(receiver_call.headers)
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/{path.lstrip('/')}",
        data=data,
        headers=headers,
        method="GET" if payload is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = _read_limited(response, MAX_HEALTH_BYTES, f"{path} response")
            receipt_result = None
            if receiver_call is not None:
                try:
                    from .sello_client import complete_receiver_call
                except ImportError:
                    from sello_client import complete_receiver_call
                receipt_result = complete_receiver_call(
                    receiver_call, response.headers, raw, response.status, receiver_base_url=base_url
                )
            value = json.loads(raw)
            if receipt_result is not None:
                value["tool_receipt"] = receipt_result
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096)
        if receiver_call is not None:
            try:
                from .sello_client import complete_receiver_call
            except ImportError:
                from sello_client import complete_receiver_call
            complete_receiver_call(receiver_call, exc.headers, raw, exc.code, receiver_base_url=base_url)
        body = raw.decode("utf-8", errors="replace")
        raise TeeInferenceVerificationError(f"TEE inference {path} returned HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise TeeInferenceVerificationError(f"TEE inference {path} request failed: {exc}") from exc
    if not isinstance(value, dict) or not value.get("ok"):
        raise TeeInferenceVerificationError(f"TEE inference {path} response is not successful")
    return value


def fetch_latest_verified_tee_model_bundle(
    *, endpoint: str | None = None, timeout_seconds: int = 120
) -> dict[str, Any]:
    """Make the TEE fetch, decrypt, and verify the current on-chain model bundle."""

    base_url = resolve_tee_inference_url(endpoint)
    prepared = _json_request(
        base_url,
        "/v1/models/fetch",
        timeout_seconds,
        {},
        receipt_action="fetch_latest_verified_tee_model_bundle",
    )
    return {
        "skill": "fetch_latest_verified_tee_model_bundle",
        "stage": "verified-model-ready",
        **prepared,
        "endpoint": base_url,
    }


def generate_random_tee_chestmnist_image(
    *, index: int | None = None, endpoint: str | None = None, timeout_seconds: int = 120
) -> dict[str, Any]:
    """Create a model-bound ChestMNIST job inside the TEE container."""

    base_url = resolve_tee_inference_url(endpoint)
    job = _json_request(
        base_url,
        "/v1/jobs",
        timeout_seconds,
        {"index": index},
        receipt_action="generate_random_tee_chestmnist_image",
    )
    return {
        "skill": "generate_random_tee_chestmnist_image",
        "stage": "tee-query-ready",
        **job,
        "endpoint": base_url,
    }


def _post_job_inference(base_url: str, job_id: str, timeout: int) -> tuple[bytes, dict[str, Any] | None]:
    action_input = json.dumps({"job_id": job_id}, sort_keys=True, separators=(",", ":")).encode()
    try:
        from .sello_client import begin_receiver_call
    except ImportError:
        from sello_client import begin_receiver_call
    receiver_call = begin_receiver_call("run_and_verify_tee_inference", "tee-inference", action_input)
    headers = {"Accept": "application/cbor"}
    if receiver_call is not None:
        headers.update(receiver_call.headers)
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/jobs/{job_id}/run",
        data=b"",
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.headers.get_content_type() != "application/cbor":
                raise TeeInferenceVerificationError("unexpected TEE job response content type")
            raw = _read_limited(response, MAX_EVIDENCE_BYTES, "TEE job evidence bundle")
            receipt_result = None
            if receiver_call is not None:
                try:
                    from .sello_client import complete_receiver_call
                except ImportError:
                    from sello_client import complete_receiver_call
                receipt_result = complete_receiver_call(
                    receiver_call, response.headers, raw, response.status, receiver_base_url=base_url
                )
            return raw, receipt_result
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096)
        if receiver_call is not None:
            try:
                from .sello_client import complete_receiver_call
            except ImportError:
                from sello_client import complete_receiver_call
            complete_receiver_call(receiver_call, exc.headers, raw, exc.code, receiver_base_url=base_url)
        body = raw.decode("utf-8", errors="replace")
        raise TeeInferenceVerificationError(f"TEE inference job returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise TeeInferenceVerificationError(f"TEE inference job request failed: {exc}") from exc


def _post_inference(base_url: str, request_bytes: bytes, timeout: int) -> bytes:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/infer",
        data=request_bytes,
        headers={"Content-Type": "application/cbor", "Accept": "application/cbor"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = response.headers.get_content_type()
            if content_type != "application/cbor":
                raise TeeInferenceVerificationError(f"unexpected inference content type: {content_type}")
            return _read_limited(response, MAX_EVIDENCE_BYTES, "evidence bundle")
    except urllib.error.HTTPError as exc:
        body = exc.read(4096).decode("utf-8", errors="replace")
        raise TeeInferenceVerificationError(f"TEE inference returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise TeeInferenceVerificationError(f"TEE inference request failed: {exc}") from exc


def _decode_bundle(raw: bytes) -> dict[int, Any]:
    try:
        bundle = cbor2.loads(raw)
    except Exception as exc:
        raise TeeInferenceVerificationError(f"invalid evidence CBOR: {exc}") from exc
    if not isinstance(bundle, dict) or set(bundle) != set(range(1, 11)):
        raise TeeInferenceVerificationError("evidence bundle must contain exactly keys 1 through 10")
    if encode_deterministic(bundle) != raw:
        raise TeeInferenceVerificationError("evidence bundle is not deterministic CBOR")
    if bundle[1] != 1:
        raise TeeInferenceVerificationError("unsupported evidence bundle version")
    return bundle


def _normalize_digest(value: str) -> str:
    match = re.search(r"sha256:([0-9a-fA-F]{64})$", value.strip())
    if not match:
        raise TeeInferenceVerificationError("expected image policy must end in sha256:<64 hex chars>")
    return "sha256:" + match.group(1).lower()


def _normalize_runtime_rpc_url(value: str, name: str) -> str:
    candidate = value.strip().rstrip("/")
    try:
        parsed = urllib.parse.urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise TeeInferenceVerificationError(f"{name} must define a valid runtime RPC URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise TeeInferenceVerificationError(f"{name} must define a plain HTTP(S) runtime RPC endpoint")
    host = parsed.hostname.lower()
    authority = f"{host}:{port}" if port is not None else host
    return f"{parsed.scheme.lower()}://{authority}"


def _required_contract_policy() -> tuple[str, str, int, str]:
    gm_storage = os.environ.get("EXPECTED_GM_STORAGE_ADDRESS", "")
    registry = os.environ.get("EXPECTED_DEVICE_REGISTRY_ADDRESS", "")
    chain_id_text = os.environ.get("EXPECTED_CHAIN_ID", "")
    if not EVM_ADDRESS_RE.fullmatch(gm_storage):
        raise TeeInferenceVerificationError("EXPECTED_GM_STORAGE_ADDRESS must define the local GMStorage policy")
    if not EVM_ADDRESS_RE.fullmatch(registry):
        raise TeeInferenceVerificationError(
            "EXPECTED_DEVICE_REGISTRY_ADDRESS must define the local DeviceRegistry policy"
        )
    try:
        chain_id = int(chain_id_text, 0)
    except ValueError as exc:
        raise TeeInferenceVerificationError("EXPECTED_CHAIN_ID must define the local chain policy") from exc
    if chain_id <= 0:
        raise TeeInferenceVerificationError("EXPECTED_CHAIN_ID must define the local chain policy")
    runtime_rpc_url = _normalize_runtime_rpc_url(
        os.environ.get("EXPECTED_RUNTIME_RPC_URL", ""),
        "EXPECTED_RUNTIME_RPC_URL",
    )
    return gm_storage.lower(), registry.lower(), chain_id, runtime_rpc_url


def _compose_environment(compose_file: str) -> dict[str, str]:
    lines = compose_file.splitlines()
    headers = [
        (position, len(match.group(1)))
        for position, line in enumerate(lines)
        if (match := re.fullmatch(r"(\s*)environment:\s*(?:#.*)?", line))
    ]
    if len(headers) != 1:
        raise TeeInferenceVerificationError("attested Compose must contain exactly one mapping-style environment block")
    position, header_indent = headers[0]
    environment: dict[str, str] = {}
    for line in lines[position + 1 :]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= header_indent:
            break
        match = re.fullmatch(
            r"\s*([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*",
            line,
        )
        if not match:
            raise TeeInferenceVerificationError("attested Compose environment must use scalar key/value entries")
        key, raw_value = match.groups()
        if key in environment:
            raise TeeInferenceVerificationError(f"attested Compose environment repeats {key}")
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] == '"':
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError as exc:
                raise TeeInferenceVerificationError(
                    f"attested Compose environment has invalid quoted value for {key}"
                ) from exc
        elif len(raw_value) >= 2 and raw_value[0] == raw_value[-1] == "'":
            value = raw_value[1:-1].replace("''", "'")
        else:
            value = raw_value
        if not isinstance(value, str):
            raise TeeInferenceVerificationError(f"attested Compose environment value for {key} must be text")
        environment[key] = value
    return environment


def _verify_model_provenance(
    manifest: dict[int, Any],
    expected_gm_storage: str,
    expected_chain_id: int,
) -> None:
    provenance = manifest[5]
    if not isinstance(provenance, dict):
        raise TeeInferenceVerificationError("model manifest provenance must be a map")
    chain_id = provenance.get(1)
    if isinstance(chain_id, bool) or not isinstance(chain_id, int) or chain_id != expected_chain_id:
        raise TeeInferenceVerificationError("model manifest chain ID violates local contract policy")
    gm_storage = provenance.get(2)
    if (
        not isinstance(gm_storage, bytes)
        or len(gm_storage) != 20
        or gm_storage != bytes.fromhex(expected_gm_storage[2:])
    ):
        raise TeeInferenceVerificationError("model manifest GMStorage address violates local contract policy")


def _verify_app_compose(
    app_compose: str,
    events: list[dict[str, Any]],
    expected_image_digest: str,
    expected_gm_storage: str,
    expected_registry: str,
    expected_chain_id: int,
    expected_runtime_rpc_url: str,
) -> str:
    compose_events = [event for event in events if event.get("event") == "compose-hash"]
    if len(compose_events) != 1:
        raise TeeInferenceVerificationError("RTMR3 log must contain exactly one compose-hash event")
    payload = str(compose_events[0].get("event_payload", ""))
    if not re.fullmatch(r"[0-9a-fA-F]{64}", payload):
        raise TeeInferenceVerificationError("compose-hash event payload is not a SHA-256 digest")
    if hashlib.sha256(app_compose.encode("utf-8")).hexdigest() != payload.lower():
        raise TeeInferenceVerificationError("app_compose does not match the measured compose-hash event")
    try:
        app = json.loads(app_compose)
    except json.JSONDecodeError as exc:
        raise TeeInferenceVerificationError(f"app_compose is not valid JSON: {exc}") from exc
    compose_file = app.get("docker_compose_file") if isinstance(app, dict) else None
    if not isinstance(compose_file, str):
        raise TeeInferenceVerificationError("app_compose lacks docker_compose_file")
    image_refs = re.findall(r"(?m)^\s*image:\s*[\"']?([^\s\"'#]+)[\"']?\s*$", compose_file)
    if len(image_refs) != 1:
        raise TeeInferenceVerificationError("attested Compose must contain exactly one image reference")
    measured_digest = _normalize_digest(image_refs[0])
    if measured_digest != _normalize_digest(expected_image_digest):
        raise TeeInferenceVerificationError("attested inference image digest violates local policy")
    environment = _compose_environment(compose_file)
    measured_gm_storage = environment.get("EXPECTED_GM_STORAGE_ADDRESS", "")
    measured_registry = environment.get("EXPECTED_DEVICE_REGISTRY_ADDRESS", "")
    measured_chain_id = environment.get("EXPECTED_CHAIN_ID", "")
    if not EVM_ADDRESS_RE.fullmatch(measured_gm_storage):
        raise TeeInferenceVerificationError("attested Compose lacks a valid GMStorage trust-root pin")
    if measured_gm_storage.lower() != expected_gm_storage:
        raise TeeInferenceVerificationError("attested Compose GMStorage pin violates local contract policy")
    if not EVM_ADDRESS_RE.fullmatch(measured_registry):
        raise TeeInferenceVerificationError("attested Compose lacks a valid DeviceRegistry trust-root pin")
    if measured_registry.lower() != expected_registry:
        raise TeeInferenceVerificationError("attested Compose DeviceRegistry pin violates local contract policy")
    try:
        parsed_chain_id = int(measured_chain_id, 0)
    except ValueError as exc:
        raise TeeInferenceVerificationError("attested Compose lacks a valid chain-ID trust-root pin") from exc
    if parsed_chain_id != expected_chain_id:
        raise TeeInferenceVerificationError("attested Compose chain-ID pin violates local contract policy")
    measured_runtime_rpc_url = _normalize_runtime_rpc_url(
        environment.get("RPC_URL", ""),
        "attested Compose RPC_URL",
    )
    if measured_runtime_rpc_url != expected_runtime_rpc_url:
        raise TeeInferenceVerificationError("attested Compose runtime RPC endpoint violates local deployment policy")
    return image_refs[0]


def _verify_rtmr3(event_log: str, quote: bytes) -> tuple[list[dict[str, Any]], bytes]:
    try:
        all_events = json.loads(event_log)
    except json.JSONDecodeError as exc:
        raise TeeInferenceVerificationError(f"event log is not valid JSON: {exc}") from exc
    if not isinstance(all_events, list):
        raise TeeInferenceVerificationError("event log must be an array")
    events = [event for event in all_events if isinstance(event, dict) and event.get("imr") == 3]
    if not events:
        raise TeeInferenceVerificationError("event log contains no RTMR3 events")
    state = bytes(TDX_RTMR_SIZE)
    for event in events:
        if event.get("event_type") != RTMR_EVENT_TYPE:
            raise TeeInferenceVerificationError("unexpected RTMR3 event type")
        name = event.get("event")
        payload_hex = event.get("event_payload")
        if not isinstance(name, str) or not isinstance(payload_hex, str):
            raise TeeInferenceVerificationError("RTMR3 event name and payload must be strings")
        try:
            payload = bytes.fromhex(payload_hex)
        except ValueError as exc:
            raise TeeInferenceVerificationError("RTMR3 event payload is not hexadecimal") from exc
        serialized = RTMR_EVENT_TYPE.to_bytes(4, "little") + b":" + name.encode("utf-8") + b":" + payload
        event_digest = hashlib.sha384(serialized).digest()
        state = hashlib.sha384(state + event_digest).digest()
    quote_rtmr3 = quote[TDX_RTMR3_OFFSET : TDX_RTMR3_OFFSET + TDX_RTMR_SIZE]
    if len(quote_rtmr3) != TDX_RTMR_SIZE or state != quote_rtmr3:
        raise TeeInferenceVerificationError("replayed RTMR3 does not match the signed quote")
    return events, state


def verify_tee_inference_bundle(
    bundle_bytes: bytes,
    exact_request: bytes,
    expected_image_digest: str,
) -> dict[str, Any]:
    """Verify all locally checkable AIR, quote-binding, RTMR3, and image-policy claims."""

    bundle = _decode_bundle(bundle_bytes)
    if bundle[2] != exact_request:
        raise TeeInferenceVerificationError("bundle request does not match the submitted request")
    request = decode_request(exact_request)
    response_bytes = bundle[3]
    if not isinstance(response_bytes, bytes):
        raise TeeInferenceVerificationError("bundle response must be bytes")
    response = decode_response(response_bytes)
    manifest_bytes = bundle[9]
    if not isinstance(manifest_bytes, bytes):
        raise TeeInferenceVerificationError("bundle manifest must be bytes")
    manifest = decode_manifest(manifest_bytes)
    expected_gm_storage, expected_registry, expected_chain_id, expected_runtime_rpc_url = _required_contract_policy()
    _verify_model_provenance(
        manifest,
        expected_gm_storage,
        expected_chain_id,
    )
    manifest_digest = hashlib.sha256(manifest_bytes).digest()
    request_digest = hashlib.sha256(exact_request).digest()
    response_digest = hashlib.sha256(response_bytes).digest()
    if request[3] != manifest_digest or response[4] != manifest_digest:
        raise TeeInferenceVerificationError("request or response is bound to another model manifest")
    if response[2] != request[2] or response[3] != request_digest:
        raise TeeInferenceVerificationError("response is not bound to the exact request")

    quote = bundle[6]
    public_key = bundle[5]
    receipt = bundle[4]
    report_data = bundle[10]
    if not all(isinstance(value, bytes) for value in (quote, public_key, receipt, report_data)):
        raise TeeInferenceVerificationError("receipt, key, quote, and REPORTDATA must be byte strings")
    if len(quote) < TDX_REPORTDATA_OFFSET + TDX_REPORTDATA_SIZE or int.from_bytes(quote[:2], "little") != 4:
        raise TeeInferenceVerificationError("evidence does not contain a complete TDX Quote V4")
    identity_hash = hashlib.sha256(b"MasterThesis.AIR.key.v1" + public_key + manifest_digest).digest()
    expected_report_data = identity_hash + request_digest
    quote_report_data = quote[TDX_REPORTDATA_OFFSET : TDX_REPORTDATA_OFFSET + TDX_REPORTDATA_SIZE]
    if report_data != expected_report_data or quote_report_data != expected_report_data:
        raise TeeInferenceVerificationError("REPORTDATA does not bind AIR key, manifest, and request to the quote")

    claims = verify_receipt(
        receipt,
        public_key,
        AirPolicy(
            expected_nonce=request[5],
            expected_model_hash=manifest_digest,
            expected_request_hash=request_digest,
            expected_response_hash=response_digest,
            expected_platform="tdx-mrtd-rtmr",
            expected_model_id=manifest[2],
            expected_security_mode="production",
            max_age_seconds=300,
            clock_skew_seconds=30,
        ),
    )
    if claims[MODEL_VERSION] != manifest[3] or claims[MODEL_ID] != manifest[2]:
        raise TeeInferenceVerificationError("AIR model identity does not match the supplied manifest")
    if claims[ATTESTATION_DOC_HASH] != hashlib.sha256(quote).digest():
        raise TeeInferenceVerificationError("AIR receipt does not bind the supplied TDX quote")

    event_log = bundle[7]
    app_compose = bundle[8]
    if not isinstance(event_log, str) or not isinstance(app_compose, str):
        raise TeeInferenceVerificationError("event log and app_compose must be text")
    events, replayed_rtmr3 = _verify_rtmr3(event_log, quote)
    image_reference = _verify_app_compose(
        app_compose,
        events,
        expected_image_digest,
        expected_gm_storage,
        expected_registry,
        expected_chain_id,
        expected_runtime_rpc_url,
    )

    logits = struct.unpack("<14d", response[5])
    probabilities = struct.unpack("<14d", response[6])
    if not all(math.isfinite(value) for value in logits + probabilities):
        raise TeeInferenceVerificationError("inference output contains a non-finite value")
    if not all(0.0 <= value <= 1.0 for value in probabilities):
        raise TeeInferenceVerificationError("inference probability is outside [0,1]")
    expected_decisions = bytes(int(value >= 0.5) for value in probabilities)
    if response[7] != expected_decisions:
        raise TeeInferenceVerificationError("decision vector does not follow the manifest threshold")

    return {
        "verification_scope": ("air-signature-reportdata-rtmr3-compose-image-contract-endpoint-trust-root-policy"),
        "dcap_collateral_verified": False,
        "manifest_sha256": manifest_digest.hex(),
        "model_id": manifest[2].hex() if isinstance(manifest[2], bytes) else str(manifest[2]),
        "model_version": manifest[3],
        "image_reference": image_reference,
        "quote_sha256": hashlib.sha256(quote).hexdigest(),
        "quote_bytes": len(quote),
        "reportdata": report_data.hex(),
        "rtmr3": replayed_rtmr3.hex(),
        "rtmr3_event_count": len(events),
        "air_sequence": claims[SEQUENCE_NUMBER],
        "probabilities": probabilities,
        "decisions": response[7],
        "duration_microseconds": response[8],
        "bundle_sha256": hashlib.sha256(bundle_bytes).hexdigest(),
    }


def _load_sample(dataset_path: str, index: int | None) -> tuple[int, bytes, np.ndarray]:
    path = Path(dataset_path)
    if not path.is_file():
        raise TeeInferenceVerificationError(f"ChestMNIST test dataset is missing: {path}")
    with np.load(path, allow_pickle=False) as dataset:
        images = dataset["images"]
        labels = dataset["labels"]
        if images.ndim != 3 or images.shape[1:] != (28, 28) or images.dtype != np.uint8:
            raise TeeInferenceVerificationError("ChestMNIST images must be uint8 [N,28,28]")
        if labels.shape != (images.shape[0], 14):
            raise TeeInferenceVerificationError("ChestMNIST labels must be [N,14]")
        selected = secrets.randbelow(images.shape[0]) if index is None else int(index)
        if not 0 <= selected < images.shape[0]:
            raise TeeInferenceVerificationError(f"sample index must be in [0,{images.shape[0] - 1}]")
        return selected, images[selected].tobytes(order="C"), labels[selected].astype(np.uint8, copy=True)


def _job_output_path(configured_path: str, job_id: str, filename: str) -> Path:
    configured = Path(configured_path)
    return configured.parent / job_id / filename


def run_and_verify_tee_inference(
    job_id: str,
    *,
    endpoint: str | None = None,
    expected_image_digest: str | None = None,
    evidence_path: str | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Run one prepared TEE job, verify all evidence, and register it with SCITT."""

    base_url = resolve_tee_inference_url(endpoint)
    if not re.fullmatch(r"[0-9a-f]{32}", str(job_id)):
        raise TeeInferenceVerificationError("job_id must contain exactly 32 lowercase hexadecimal characters")

    metadata = _json_request(base_url, f"/v1/jobs/{job_id}", timeout_seconds)
    bundle_bytes, tool_receipt = _post_job_inference(base_url, job_id, timeout_seconds)
    bundle = _decode_bundle(bundle_bytes)
    exact_request = bundle[2]
    if not isinstance(exact_request, bytes):
        raise TeeInferenceVerificationError("evidence bundle request must be bytes")
    verified = verify_tee_inference_bundle(
        bundle_bytes,
        exact_request,
        expected_image_digest or DEFAULT_TEE_IMAGE_DIGEST,
    )

    try:
        from .scitt_client import register_verified_evidence
    except ImportError:
        from scitt_client import register_verified_evidence
    transparent_statement = _job_output_path(
        DEFAULT_SCITT_STATEMENT_PATH,
        job_id,
        "transparent-statement.cose",
    )
    transparency = register_verified_evidence(
        bundle_bytes,
        transparent_statement_path=str(transparent_statement),
    )
    if transparency["evidence_sha256"] != verified["bundle_sha256"]:
        raise TeeInferenceVerificationError("SCITT statement does not bind the verified evidence bundle")

    output = _job_output_path(evidence_path or DEFAULT_EVIDENCE_PATH, job_id, "evidence.cbor")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".cbor.tmp")
    temporary.write_bytes(bundle_bytes)
    temporary.replace(output)

    probabilities = verified.pop("probabilities")
    decisions = verified.pop("decisions")
    try:
        from .transparency_index import record_transparency_entry
    except ImportError:
        from transparency_index import record_transparency_entry
    record = record_transparency_entry(
        evidence_type="tee-inference-receipt",
        job_id=job_id,
        model_id=str(verified["model_id"]),
        transparency=transparency,
        verification={
            "verification_scope": verified["verification_scope"],
            "bundle_sha256": verified["bundle_sha256"],
            "quote_sha256": verified["quote_sha256"],
            "rtmr3": verified["rtmr3"],
            "image_reference": verified["image_reference"],
        },
    )
    return {
        "skill": "run_and_verify_tee_inference",
        "stage": "verified-and-transparency-logged",
        "ok": True,
        "endpoint": base_url,
        "job_id": job_id,
        "sample_index": metadata["source_index"],
        "ground_truth": metadata["ground_truth"],
        "predicted_labels": [LABELS[position] for position, value in enumerate(decisions) if value],
        "probabilities": {LABELS[position]: value for position, value in enumerate(probabilities)},
        "evidence_path": str(output),
        "verification": verified,
        "transparency_log": public_registration(transparency),
        "transparency_record_id": record["record_id"],
        "tool_receipt": tool_receipt,
    }


def run_verified_tee_inference(
    *,
    index: int | None = None,
    endpoint: str | None = None,
    expected_image_digest: str | None = None,
    dataset_path: str | None = None,
    evidence_path: str | None = None,
    timeout_seconds: int = 120,
) -> dict[str, Any]:
    """Compatibility wrapper that executes the new three-tool TEE workflow."""

    del dataset_path
    fetch_latest_verified_tee_model_bundle(endpoint=endpoint, timeout_seconds=timeout_seconds)
    job = generate_random_tee_chestmnist_image(
        index=index,
        endpoint=endpoint,
        timeout_seconds=timeout_seconds,
    )
    return run_and_verify_tee_inference(
        str(job["job_id"]),
        endpoint=endpoint,
        expected_image_digest=expected_image_digest,
        evidence_path=evidence_path,
        timeout_seconds=timeout_seconds,
    )

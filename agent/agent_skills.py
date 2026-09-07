"""Central skill layer for the thesis agent.

This module keeps the public agent skills independent from MCP, LangChain,
or CLI adapters. Those surfaces should call into this module instead of
reimplementing the workflow themselves.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class SkillSpec:
    """Describes one public agent skill."""

    name: str
    summary: str
    stages: tuple[str, ...]


FETCH_LATEST_VERIFIED_MODEL_BUNDLE_SKILL = SkillSpec(
    name="fetch_latest_verified_model_bundle",
    summary="Resolve the current on-chain model bundle, decrypt it, verify it, and export the verified model.",
    stages=(
        "resolve_bundle",
        "decrypt_bundle",
        "verify_signature",
        "export_model",
    ),
)

GENERATE_RANDOM_CHESTMNIST_IMAGE_SKILL = SkillSpec(
    name="generate_random_chestmnist_image",
    summary="Prepare one ChestMNIST sample and write the EZKL input artifacts for a later proof step.",
    stages=(
        "select_sample",
        "prepare_query",
    ),
)

GENERATE_ZK_INFERENCE_PROOF_SKILL = SkillSpec(
    name="generate_zk_inference_proof",
    summary="Run EZKL against the already prepared model and query artifacts to generate the proof output.",
    stages=(
        "load_artifacts",
        "run_ezkl",
    ),
)

FETCH_LATEST_VERIFIED_ZK_MODEL_BUNDLE_SKILL = SkillSpec(
    name="fetch_latest_verified_zk_model_bundle",
    summary="Make the ZK TEE fetch, decrypt, verify, and export the current on-chain model.",
    stages=("resolve_bundle", "decrypt_bundle", "verify_signature", "export_model"),
)

GENERATE_RANDOM_ZK_CHESTMNIST_IMAGE_SKILL = SkillSpec(
    name="generate_random_zk_chestmnist_image",
    summary="Create a model-bound ChestMNIST proof job inside the ZK TEE.",
    stages=("select_sample", "prepare_query"),
)

GENERATE_AND_VERIFY_ZK_INFERENCE_PROOF_SKILL = SkillSpec(
    name="generate_and_verify_zk_inference_proof",
    summary="Generate and verify an EZKL proof for the prepared ZK job.",
    stages=("generate_witness", "generate_proof", "verify_proof"),
)

FETCH_LATEST_VERIFIED_TEE_MODEL_BUNDLE_SKILL = SkillSpec(
    name="fetch_latest_verified_tee_model_bundle",
    summary="Make the TEE fetch, decrypt, and verify the current on-chain model bundle.",
    stages=("resolve_bundle", "decrypt_bundle", "verify_signature"),
)

GENERATE_RANDOM_TEE_CHESTMNIST_IMAGE_SKILL = SkillSpec(
    name="generate_random_tee_chestmnist_image",
    summary="Create a model-bound ChestMNIST inference job inside the TEE.",
    stages=("select_sample", "bind_sample_to_model"),
)

RUN_AND_VERIFY_TEE_INFERENCE_SKILL = SkillSpec(
    name="run_and_verify_tee_inference",
    summary=(
        "Run one ChestMNIST inference in the Phala TEE and return it only after "
        "AIR, quote-binding, RTMR3, and image-policy verification."
    ),
    stages=(
        "select_sample",
        "run_tee_inference",
        "verify_air_receipt",
        "verify_tdx_binding",
        "verify_rtmr3_and_image",
        "register_scitt_statement",
        "verify_scitt_receipt",
        "persist_evidence",
    ),
)

AGENT_SKILL_SPECS: tuple[SkillSpec, ...] = (
    FETCH_LATEST_VERIFIED_ZK_MODEL_BUNDLE_SKILL,
    GENERATE_RANDOM_ZK_CHESTMNIST_IMAGE_SKILL,
    GENERATE_AND_VERIFY_ZK_INFERENCE_PROOF_SKILL,
    FETCH_LATEST_VERIFIED_TEE_MODEL_BUNDLE_SKILL,
    GENERATE_RANDOM_TEE_CHESTMNIST_IMAGE_SKILL,
    RUN_AND_VERIFY_TEE_INFERENCE_SKILL,
)


def zk_inference_enabled() -> bool:
    """Allow deployments to disable ZK tools while retaining the local tool API."""
    return os.environ.get("ZK_INFERENCE_ENABLED", "true").strip().lower() not in {"0", "false", "no", "off"}


def describe_agent_skills() -> str:
    lines: list[str] = ["Available agent skills:"]
    for spec in AGENT_SKILL_SPECS:
        if not zk_inference_enabled() and "_zk_" in spec.name:
            continue
        lines.append(f"- {spec.name}: {spec.summary}")
        lines.append(f"  stages={', '.join(spec.stages)}")
    return "\n".join(lines)


def normalize_optional_index(index: Any) -> int | None:
    if index is None or isinstance(index, dict):
        return None
    normalized = str(index).strip().lower()
    if normalized in {"", "none", "null"}:
        return None
    return int(normalized)


def resolve_preferred_sample_index(
    session_state: dict[str, Any],
    requested_index: Any,
) -> int | None:
    normalized = normalize_optional_index(requested_index)
    if normalized is not None:
        return normalized
    selection = session_state.get("latest_selection")
    if not isinstance(selection, dict):
        return None
    remembered_index = selection.get("source_index")
    return remembered_index if isinstance(remembered_index, int) else None


def structured_skill_result(
    *,
    skill: str,
    stage: str,
    ok: bool,
    bundle_payload: dict[str, Any] | None = None,
    selection_payload: dict[str, Any] | None = None,
    export_payload: dict[str, Any] | None = None,
    ezkl_payload: dict[str, Any] | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "skill": skill,
        "stage": stage,
        "ok": ok,
    }
    if bundle_payload is not None:
        result["bundle"] = bundle_payload.get("bundle", {})
        result["download"] = bundle_payload.get("download", {})
        result["verification"] = bundle_payload.get("verification", {})
    if selection_payload is not None:
        result["selection"] = selection_payload.get("selection", selection_payload)
    if export_payload is not None:
        result["export"] = export_payload
    if ezkl_payload is not None:
        result["ezkl"] = ezkl_payload
    if note:
        result["note"] = note
    return result


def format_verified_bundle_summary(payload: dict[str, Any]) -> str:
    bundle = payload.get("bundle", {})
    download = payload.get("download", {})
    verification = payload.get("verification", {})
    decryption = download.get("decryption", {})
    decryption_round = decryption.get("round", "unknown") if isinstance(decryption, dict) else "unknown"
    decryption_recipient = decryption.get("recipient_address", "unknown") if isinstance(decryption, dict) else "unknown"
    decryption_key_source = (
        decryption.get("private_key_source", "unknown") if isinstance(decryption, dict) else "unknown"
    )
    return "\n".join(
        [
            f"model_cid={bundle.get('model_cid', 'unknown')}",
            f"signature_cid={bundle.get('signature_cid', 'unknown')}",
            f"last_aggregator={bundle.get('last_aggregator', 'unknown')}",
            f"encrypted_bundle={download.get('encrypted_bundle', 'unknown')}",
            f"model_path={download.get('model_path', 'unknown')}",
            f"signature_path={download.get('signature_path', 'unknown')}",
            f"decryption_ok={bool(decryption)}",
            f"decryption_round={decryption_round}",
            f"decryption_recipient={decryption_recipient}",
            f"decryption_key_source={decryption_key_source}",
            f"signature_verified={verification.get('ok', 'unknown')}",
            f"verification_error={verification.get('error') or 'none'}",
        ]
    )


def format_selection_summary(payload: dict[str, Any]) -> str:
    selection = payload.get("selection", payload)
    files = selection.get("files", {}) if isinstance(selection, dict) else {}
    return "\n".join(
        [
            f"dataset={selection.get('dataset', 'unknown')}",
            f"source_index={selection.get('source_index', 'unknown')}",
            f"random_selection={selection.get('random_selection', 'unknown')}",
            f"true_label={selection.get('true_label', 'unknown')}",
            f"single_image_npy={files.get('single_image_npy', 'unknown')}",
            f"ezkl_input_json={files.get('ezkl_input_json', 'unknown')}",
        ]
    )


def run_fetch_latest_verified_model_bundle_skill(
    *,
    out_dir: str,
    fetch_bundle_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    payload = fetch_bundle_fn(out_dir=out_dir)
    verification = payload.get("verification", {})
    return structured_skill_result(
        skill=FETCH_LATEST_VERIFIED_MODEL_BUNDLE_SKILL.name,
        stage=("verified_bundle_ready" if verification.get("ok", False) else "signature_verification"),
        ok=bool(verification.get("ok", False)),
        bundle_payload=payload,
        export_payload=(payload.get("export") if isinstance(payload.get("export"), dict) else None),
    )


def run_generate_random_chestmnist_image_skill(
    *,
    index: int | None,
    out_dir: str,
    input_json: str,
    create_query_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    selection_payload = create_query_fn(
        index=normalize_optional_index(index),
        out_dir=out_dir,
        input_json=input_json,
    )
    return structured_skill_result(
        skill=GENERATE_RANDOM_CHESTMNIST_IMAGE_SKILL.name,
        stage=("create_single_image_query" if selection_payload.get("ok", True) else "create_single_image_query"),
        ok=bool(selection_payload.get("ok", True)),
        selection_payload=selection_payload,
    )


def run_generate_zk_inference_proof_skill(
    *,
    workdir: str,
    model: str,
    data: str,
    skip_calibration: bool,
    run_ezkl_fn: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    ezkl_payload = run_ezkl_fn(
        workdir=workdir,
        model=model,
        data=data,
        skip_calibration=skip_calibration,
    )
    return structured_skill_result(
        skill=GENERATE_ZK_INFERENCE_PROOF_SKILL.name,
        stage="done" if ezkl_payload.get("ok", False) else "run_ezkl",
        ok=bool(ezkl_payload.get("ok", False)),
        ezkl_payload=ezkl_payload,
    )


def default_input_json_path(workdir: str) -> str:
    return str(Path(workdir) / "input.json")

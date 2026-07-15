#!/usr/bin/env python3
"""MCP server exposing the ZK and verified TEE inference workflows as tools."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from prometheus_client import Gauge

try:
    from .agent_skills import (
        normalize_optional_index,
        run_fetch_latest_verified_model_bundle_skill,
        run_generate_random_chestmnist_image_skill,
        run_generate_zk_inference_proof_skill,
    )
except ImportError:
    from agent_skills import (
        normalize_optional_index,
        run_fetch_latest_verified_model_bundle_skill,
        run_generate_random_chestmnist_image_skill,
        run_generate_zk_inference_proof_skill,
    )

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - used by deterministic local mode
    FastMCP = None


ZK_INFERENCE_URL = os.environ.get("ZK_INFERENCE_URL", "").rstrip("/")
MCP_TOOL_CALLS_CURRENT = Gauge(
    "agent_mcp_tool_calls_current",
    "Number of public MCP tool calls handled in the current agent chat session.",
    ["tool_name"],
)
PUBLIC_MCP_TOOL_NAMES = (
    "fetch_latest_verified_tee_model_bundle",
    "generate_random_tee_chestmnist_image",
    "run_and_verify_tee_inference",
    "fetch_latest_verified_zk_model_bundle",
    "generate_random_zk_chestmnist_image",
    "generate_and_verify_zk_inference_proof",
)


def reset_mcp_tool_call_metrics() -> None:
    for tool_name in PUBLIC_MCP_TOOL_NAMES:
        MCP_TOOL_CALLS_CURRENT.labels(tool_name=tool_name).set(0)


def record_mcp_tool_call(tool_name: str) -> None:
    MCP_TOOL_CALLS_CURRENT.labels(tool_name=tool_name).inc()


reset_mcp_tool_call_metrics()


class LocalToolUnavailableError(RuntimeError):
    """Raised when the local zk_inference implementation is not available in this runtime."""


def _latest_downloaded_model_path() -> str:
    downloads_dir = Path(os.environ.get("AGENT_DOWNLOAD_DIR", str(Path(__file__).resolve().parent / "downloads")))
    candidates = [path for path in downloads_dir.glob("*.bin") if path.is_file() and not path.name.endswith(".sig")]
    if not candidates:
        raise RuntimeError("No downloaded model bundle is available yet in agent/downloads.")
    return str(max(candidates, key=lambda path: path.stat().st_mtime))


def _normalize_model_path(model_path: Any) -> str:
    if model_path is None or isinstance(model_path, dict):
        return _latest_downloaded_model_path()
    normalized = str(model_path).strip()
    if (
        not normalized
        or normalized in {"downloaded_model", "/path/to/downloaded/model", "/path/to/model"}
        or normalized.startswith("<output of ")
        or normalized.endswith((".pt", ".onnx", ".json"))
    ):
        return _latest_downloaded_model_path()
    return normalized


def _normalize_artifact_name(value: Any, default: str) -> str:
    if value is None or isinstance(value, dict):
        return default
    normalized = str(value).strip()
    if not normalized or normalized.startswith("<output of "):
        return default
    return normalized


def _local_tools():
    from zk_inference.service_tools import (
        create_single_image_query as create_single_image_query_impl,
        export_model as export_model_impl,
        run_ezkl as run_ezkl_impl,
    )

    return {
        "export_model": export_model_impl,
        "create_single_image_query": create_single_image_query_impl,
        "run_ezkl": run_ezkl_impl,
    }


def _remote_call(endpoint: str, payload: dict[str, Any], timeout: int = 120) -> dict[str, Any]:
    """Unified helper for POST requests to the remote zk_inference service."""
    if not ZK_INFERENCE_URL:
        raise RuntimeError("ZK_INFERENCE_URL is not set and local zk_inference tools are unavailable.")

    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{ZK_INFERENCE_URL}/{endpoint.lstrip('/')}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"zk_inference service returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach zk_inference service at {ZK_INFERENCE_URL}: {exc}") from exc


def _require_local_tool(name: str):
    try:
        return _local_tools()[name]
    except ModuleNotFoundError as exc:
        raise LocalToolUnavailableError(
            "This MCP tool requires the local zk_inference package inside the agent environment. "
            "Use the remote zk-inference service, or add zk_inference to the image."
        ) from exc


class _NoMCP:
    def tool(self):
        return lambda function: function

    def run(self) -> None:
        raise RuntimeError("MCP server mode requires: pip install -r agent/requirements.txt")


mcp = FastMCP("master-thesis-zk-inference") if FastMCP is not None else _NoMCP()


def export_model(model_path: str, out_dir: str = "zk_inference/out") -> dict[str, Any]:
    """Internal helper: export an aggregated .bin model into ONNX/EZKL artifacts."""
    payload = {"model_path": _normalize_model_path(model_path), "out_dir": out_dir}
    try:
        local_tool = _require_local_tool("export_model")
        return local_tool(**payload)
    except LocalToolUnavailableError:
        return _remote_call("export-model", payload)


def _create_single_image_query(
    index: int | None = None,
    images: str | None = None,
    labels: str | None = None,
    out_dir: str = "zk_inference/single_query",
    input_json: str = "zk_inference/out/input.json",
) -> dict[str, Any]:
    """Internal helper: prepare one dataset image and write the EZKL input.json."""
    payload = {
        "index": normalize_optional_index(index),
        "images": images,
        "labels": labels,
        "out_dir": out_dir,
        "input_json": input_json,
    }
    try:
        return _require_local_tool("create_single_image_query")(**payload)
    except LocalToolUnavailableError:
        return _remote_call("create-query", payload)


def _run_ezkl(
    workdir: str = "zk_inference/out",
    model: str = "model_logits.onnx",
    data: str = "input.json",
    skip_calibration: bool = True,
) -> dict[str, Any]:
    """Internal helper: run EZKL against an existing prepared input artifact."""
    payload = {
        "workdir": workdir or "zk_inference/out",
        "model": _normalize_artifact_name(model, "model_logits.onnx"),
        "data": _normalize_artifact_name(data, "input.json"),
        "skip_calibration": skip_calibration,
    }
    try:
        return _require_local_tool("run_ezkl")(**payload)
    except LocalToolUnavailableError:
        return _remote_call("run-ezkl", payload, timeout=600)


def _fetch_current_onchain_model_bundle(out_dir: str = "zk_inference/out") -> dict[str, Any]:
    """Internal helper: resolve current CIDs, fetch artifacts, verify them, and export the model."""
    try:
        from .blockchain_source import (
            load_env_file,
            DEFAULT_ENV_FILE,
            normalize_host_rpc_url,
            normalize_ipfs_api_url,
            read_current_bundle_from_contract,
            fetch_onchain_bundle,
            verify_download_with_registry,
        )
        from .ipfs_bundle import DEFAULT_DOWNLOAD_DIR, DEFAULT_IPFS_API_URL
    except ImportError:
        from blockchain_source import (
            load_env_file,
            DEFAULT_ENV_FILE,
            normalize_host_rpc_url,
            normalize_ipfs_api_url,
            read_current_bundle_from_contract,
            fetch_onchain_bundle,
            verify_download_with_registry,
        )
        from ipfs_bundle import DEFAULT_DOWNLOAD_DIR, DEFAULT_IPFS_API_URL

    env_file = load_env_file(DEFAULT_ENV_FILE)
    rpc_url = normalize_host_rpc_url(os.environ.get("RPC_URL") or env_file.get("RPC_URL", "http://127.0.0.1:8545"))
    gm_storage_address = os.environ.get("GM_STORAGE_ADDRESS") or env_file.get("GM_STORAGE_ADDRESS", "")
    registry_address = os.environ.get("REGISTRY_ADDRESS") or env_file.get("REGISTRY_ADDRESS", "")
    ipfs_api_url = normalize_ipfs_api_url(os.environ.get("IPFS_API_URL", DEFAULT_IPFS_API_URL))

    bundle = read_current_bundle_from_contract(rpc_url, gm_storage_address, registry_address=registry_address)
    download = fetch_onchain_bundle(bundle, DEFAULT_DOWNLOAD_DIR, ipfs_api_url=ipfs_api_url)
    verification = verify_download_with_registry(bundle, download, rpc_url, registry_address)
    payload: dict[str, Any] = {"bundle": bundle, "download": download, "verification": verification}
    if verification.get("ok", False):
        payload["export"] = export_model(download.get("model_path"), out_dir=out_dir)
    return payload


@mcp.tool()
def fetch_latest_verified_zk_model_bundle() -> str:
    """Fetch, decrypt, verify, and export the current on-chain model inside the ZK TEE."""

    record_mcp_tool_call("fetch_latest_verified_zk_model_bundle")
    result = _remote_call("/v1/models/fetch", {}, timeout=600)
    return json.dumps(
        {"skill": "fetch_latest_verified_zk_model_bundle", "stage": "verified-model-ready", **result},
        indent=2,
    )


@mcp.tool()
def generate_random_zk_chestmnist_image(index: int | None = None) -> str:
    """Select a ChestMNIST sample inside the ZK TEE and return its model-bound job ID."""

    record_mcp_tool_call("generate_random_zk_chestmnist_image")
    result = _remote_call("/v1/jobs", {"index": normalize_optional_index(index)})
    return json.dumps(
        {"skill": "generate_random_zk_chestmnist_image", "stage": "zk-query-ready", **result},
        indent=2,
    )


@mcp.tool()
def generate_and_verify_zk_inference_proof(job_id: str) -> str:
    """Generate and cryptographically verify an EZKL proof for a prepared ZK job."""

    record_mcp_tool_call("generate_and_verify_zk_inference_proof")
    if len(job_id) != 32 or any(character not in "0123456789abcdef" for character in job_id):
        raise ValueError("job_id must contain exactly 32 lowercase hexadecimal characters")
    result = _remote_call(f"/v1/jobs/{job_id}/run-and-verify", {}, timeout=900)
    return json.dumps(
        {"skill": "generate_and_verify_zk_inference_proof", "stage": "proof-verified", **result},
        indent=2,
    )


def fetch_latest_verified_model_bundle(out_dir: str = "zk_inference/out") -> str:
    """Skill-oriented bundle fetch: resolve, decrypt, verify, and export the current on-chain model bundle."""
    record_mcp_tool_call("fetch_latest_verified_model_bundle")
    result = run_fetch_latest_verified_model_bundle_skill(
        out_dir=out_dir,
        fetch_bundle_fn=lambda *, out_dir: _fetch_current_onchain_model_bundle(out_dir=out_dir),
    )
    return json.dumps(result, indent=2)


def generate_random_chestmnist_image(
    index: int | None = None,
    query_dir: str = "zk_inference/single_query",
    input_json: str = "zk_inference/out/input.json",
) -> str:
    """Skill-oriented sample preparation: choose a ChestMNIST sample and write the EZKL input artifacts."""
    record_mcp_tool_call("generate_random_chestmnist_image")
    result = run_generate_random_chestmnist_image_skill(
        index=normalize_optional_index(index),
        out_dir=query_dir,
        input_json=input_json,
        create_query_fn=_create_single_image_query,
    )
    return json.dumps(result, indent=2)


def generate_zk_inference_proof(
    workdir: str = "zk_inference/out",
    model: str = "model_logits.onnx",
    data: str = "input.json",
    skip_calibration: bool = True,
) -> str:
    """Skill-oriented proof execution: run EZKL against already prepared artifacts."""
    record_mcp_tool_call("generate_zk_inference_proof")
    result = run_generate_zk_inference_proof_skill(
        workdir=workdir,
        model=_normalize_artifact_name(model, "model_logits.onnx"),
        data=_normalize_artifact_name(data, "input.json"),
        skip_calibration=skip_calibration,
        run_ezkl_fn=lambda **payload: _run_ezkl(
            workdir=payload.get("workdir", "zk_inference/out"),
            model=_normalize_artifact_name(payload.get("model"), "model_logits.onnx"),
            data=_normalize_artifact_name(payload.get("data"), "input.json"),
            skip_calibration=bool(payload.get("skip_calibration", True)),
        ),
    )
    return json.dumps(result, indent=2)


@mcp.tool()
def fetch_latest_verified_tee_model_bundle() -> str:
    """Fetch, decrypt, and verify the current on-chain model inside the TEE."""

    record_mcp_tool_call("fetch_latest_verified_tee_model_bundle")
    try:
        from .tee_inference_client import fetch_latest_verified_tee_model_bundle as run_impl
    except ImportError:
        from tee_inference_client import fetch_latest_verified_tee_model_bundle as run_impl
    return json.dumps(run_impl(), indent=2)


@mcp.tool()
def generate_random_tee_chestmnist_image(index: int | None = None) -> str:
    """Select a ChestMNIST sample inside the TEE and return its model-bound job ID."""

    record_mcp_tool_call("generate_random_tee_chestmnist_image")
    try:
        from .tee_inference_client import generate_random_tee_chestmnist_image as run_impl
    except ImportError:
        from tee_inference_client import generate_random_tee_chestmnist_image as run_impl
    return json.dumps(run_impl(index=normalize_optional_index(index)), indent=2)


@mcp.tool()
def run_and_verify_tee_inference(job_id: str) -> str:
    """Run a prepared TEE job, verify its evidence, and register it with SCITT."""

    record_mcp_tool_call("run_and_verify_tee_inference")
    try:
        from .tee_inference_client import run_and_verify_tee_inference as run_impl
    except ImportError:
        from tee_inference_client import run_and_verify_tee_inference as run_impl
    return json.dumps(run_impl(job_id), indent=2)


def run_verified_tee_inference(
    index: int | None = None,
) -> str:
    """Run ChestMNIST inference in the Phala TEE and return only a verified result.

    This single fail-closed tool selects the requested (or a random) test image,
    calls the TEE, verifies the AIR signature and all request/model/response
    hashes, compares REPORTDATA with Quote V4, replays RTMR3, checks the measured
    app_compose and configured image digest, submits the exact evidence bundle
    to SCITT-CCF, verifies the returned CCF receipt, and stores both artifacts.
    Endpoints and policies come exclusively from trusted process configuration,
    not from tool-call arguments.
    """
    record_mcp_tool_call("run_verified_tee_inference")
    try:
        from .tee_inference_client import run_verified_tee_inference as run_impl
    except ImportError:
        from tee_inference_client import run_verified_tee_inference as run_impl
    result = run_impl(
        index=normalize_optional_index(index),
    )
    return json.dumps(result, indent=2)


if __name__ == "__main__":
    mcp.run()

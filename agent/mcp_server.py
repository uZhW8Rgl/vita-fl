#!/usr/bin/env python3
"""MCP server exposing the zk_inference pipeline as tools."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from prometheus_client import Gauge

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
    "fetch_current_onchain_model_bundle",
    "create_single_image_query",
    "run_ezkl",
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
    downloads_dir = Path("agent/downloads")
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


def _normalize_optional_index(index: Any) -> int | None:
    if index is None or isinstance(index, dict):
        return None
    normalized = str(index).strip().lower()
    if normalized in {"", "none", "null"}:
        return None
    return int(normalized)


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


@mcp.tool()
def create_single_image_query(
    index: int | None = None,
    images: str | None = None,
    labels: str | None = None,
    out_dir: str = "zk_inference/single_query",
    input_json: str = "zk_inference/out/input.json",
) -> dict[str, Any]:
    """Prepare one dataset image plus EZKL input.json for the active dataset."""
    record_mcp_tool_call("create_single_image_query")
    payload = {
        "index": _normalize_optional_index(index),
        "images": images,
        "labels": labels,
        "out_dir": out_dir,
        "input_json": input_json,
    }
    try:
        return _require_local_tool("create_single_image_query")(**payload)
    except LocalToolUnavailableError:
        return _remote_call("create-query", payload)


@mcp.tool()
def run_ezkl(
    workdir: str = "zk_inference/out",
    model: str = "model_logits.onnx",
    data: str = "input.json",
    skip_calibration: bool = True,
) -> dict[str, Any]:
    """Run EZKL setup, witness generation, proof generation, and verification."""
    record_mcp_tool_call("run_ezkl")
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


@mcp.tool()
def fetch_current_onchain_model_bundle(out_dir: str = "zk_inference/out") -> str:
    """Read current CIDs, fetch both IPFS artifacts, verify the signature, and export for EZKL."""
    record_mcp_tool_call("fetch_current_onchain_model_bundle")
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
    return json.dumps(payload, indent=2)


if __name__ == "__main__":
    mcp.run()

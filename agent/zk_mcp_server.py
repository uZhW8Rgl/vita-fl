#!/usr/bin/env python3
"""MCP server exposing the zk_inference pipeline as tools."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - used by deterministic local mode
    FastMCP = None


ZK_INFERENCE_URL = os.environ.get("ZK_INFERENCE_URL", "").rstrip("/")


def _local_tools():
    from zk_inference.service_tools import (
        create_single_image_query as create_single_image_query_impl,
        export_model as export_model_impl,
        prepare_mnist_sample as prepare_mnist_sample_impl,
        prove_single_image as prove_single_image_impl,
        run_ezkl as run_ezkl_impl,
    )

    return {
        "export_model": export_model_impl,
        "prepare_mnist_sample": prepare_mnist_sample_impl,
        "create_single_image_query": create_single_image_query_impl,
        "run_ezkl": run_ezkl_impl,
        "prove_single_image": prove_single_image_impl,
    }


def _remote_prove_single_image(
    *,
    model_path: str,
    signature_path: str,
    index: int | None,
    workdir: str,
    query_dir: str,
    skip_calibration: bool,
) -> dict[str, Any]:
    if not ZK_INFERENCE_URL:
        raise RuntimeError("ZK_INFERENCE_URL is not set and local zk_inference tools are unavailable.")

    payload = json.dumps(
        {
            "model_path": model_path,
            "signature_path": signature_path,
            "index": index,
            "workdir": workdir,
            "query_dir": query_dir,
            "skip_calibration": skip_calibration,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        ZK_INFERENCE_URL + "/prove-single-image",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=600) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"zk_inference service returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach zk_inference service at {ZK_INFERENCE_URL}: {exc}") from exc


def _remote_prepare_mnist_sample(
    *,
    index: int | None,
    images: str,
    labels: str,
    out_dir: str,
    input_json: str,
    metadata_out: str,
    seed: int | None,
) -> dict[str, Any]:
    if not ZK_INFERENCE_URL:
        raise RuntimeError("ZK_INFERENCE_URL is not set and local zk_inference tools are unavailable.")

    payload = json.dumps(
        {
            "index": index,
            "images": images,
            "labels": labels,
            "out_dir": out_dir,
            "input_json": input_json,
            "metadata_out": metadata_out,
            "seed": seed,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        ZK_INFERENCE_URL + "/prepare-mnist-sample",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
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
        raise RuntimeError(
            "This MCP tool requires the local zk_inference package inside the agent environment. "
            "Use prove_single_image through the remote zk-inference service, or add zk_inference to the image."
        ) from exc


class _NoMCP:
    def tool(self):
        return lambda function: function

    def run(self) -> None:
        raise RuntimeError("MCP server mode requires: pip install -r agent/requirements.txt")


mcp = FastMCP("master-thesis-zk-inference") if FastMCP is not None else _NoMCP()


@mcp.tool()
def export_model(model_path: str, out_dir: str = "zk_inference/out") -> dict[str, Any]:
    """Export an aggregated .bin model into ONNX/EZKL artifacts."""
    return _require_local_tool("export_model")(model_path, out_dir)


@mcp.tool()
def prepare_mnist_sample(
    index: int | None = None,
    images: str = "data/mnist/data/t10k-images.idx3-ubyte",
    labels: str = "data/mnist/data/t10k-labels.idx1-ubyte",
    out_dir: str = "zk_inference/single_query",
    input_json: str = "zk_inference/out/input.json",
    metadata_out: str = "zk_inference/single_query/selection.json",
    seed: int | None = None,
) -> dict[str, Any]:
    """Extract one MNIST sample, defaulting to a fresh random image on each run."""
    try:
        local_tool = _require_local_tool("prepare_mnist_sample")
    except RuntimeError:
        return _remote_prepare_mnist_sample(
            index=index,
            images=images,
            labels=labels,
            out_dir=out_dir,
            input_json=input_json,
            metadata_out=metadata_out,
            seed=seed,
        )
    return local_tool(
        index=index,
        images=images,
        labels=labels,
        out_dir=out_dir,
        input_json=input_json,
        metadata_out=metadata_out,
        seed=seed,
    )


@mcp.tool()
def create_single_image_query(
    model_path: str,
    index: int | None = None,
    images: str = "data/mnist/data/t10k-images.idx3-ubyte",
    labels: str = "data/mnist/data/t10k-labels.idx1-ubyte",
    out_dir: str = "zk_inference/single_query",
    input_json: str = "zk_inference/out/input.json",
) -> dict[str, Any]:
    """Create a one-image MNIST query and EZKL input.json for the model."""
    return _require_local_tool("create_single_image_query")(
        model_path,
        index=index,
        images=images,
        labels=labels,
        out_dir=out_dir,
        input_json=input_json,
    )


@mcp.tool()
def run_ezkl(
    workdir: str = "zk_inference/out",
    model: str = "model_logits.onnx",
    data: str = "input.json",
    skip_calibration: bool = True,
) -> dict[str, Any]:
    """Run EZKL setup, witness generation, proof generation, and verification."""
    return _require_local_tool("run_ezkl")(workdir=workdir, model=model, data=data, skip_calibration=skip_calibration)


@mcp.tool()
def prove_single_image(
    model_path: str,
    signature_path: str,
    index: int | None = None,
    workdir: str = "zk_inference/out",
    query_dir: str = "zk_inference/single_query",
    skip_calibration: bool = True,
) -> dict[str, Any]:
    """Export a model, create one-image input, run EZKL, and return prediction/proof paths."""
    try:
        local_tool = _require_local_tool("prove_single_image")
    except RuntimeError:
        return _remote_prove_single_image(
            model_path=model_path,
            signature_path=signature_path,
            index=index,
            workdir=workdir,
            query_dir=query_dir,
            skip_calibration=skip_calibration,
        )
    return local_tool(
        model_path=model_path,
        signature_path=signature_path,
        index=index,
        workdir=workdir,
        query_dir=query_dir,
        skip_calibration=skip_calibration,
    )


if __name__ == "__main__":
    mcp.run()

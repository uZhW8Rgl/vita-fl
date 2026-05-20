#!/usr/bin/env python3
"""MCP server exposing the local zk_inference pipeline as tools."""

from __future__ import annotations

from typing import Any

from zk_inference.service_tools import (
    create_single_image_query as create_single_image_query_impl,
    export_model as export_model_impl,
    prepare_mnist_sample as prepare_mnist_sample_impl,
    prove_single_image as prove_single_image_impl,
    run_ezkl as run_ezkl_impl,
)

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover - used by deterministic local mode
    FastMCP = None


class _NoMCP:
    def tool(self):
        return lambda function: function

    def run(self) -> None:
        raise RuntimeError("MCP server mode requires: pip install -r agent/requirements.txt")


mcp = FastMCP("master-thesis-zk-inference") if FastMCP is not None else _NoMCP()


@mcp.tool()
def export_model(model_path: str, out_dir: str = "zk_inference/out") -> dict[str, Any]:
    """Export an aggregated .bin model into ONNX/EZKL artifacts."""
    return export_model_impl(model_path, out_dir)


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
    return prepare_mnist_sample_impl(
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
    return create_single_image_query_impl(
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
    return run_ezkl_impl(workdir=workdir, model=model, data=data, skip_calibration=skip_calibration)


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
    return prove_single_image_impl(
        model_path=model_path,
        signature_path=signature_path,
        index=index,
        workdir=workdir,
        query_dir=query_dir,
        skip_calibration=skip_calibration,
    )


if __name__ == "__main__":
    mcp.run()

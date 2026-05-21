#!/usr/bin/env python3
"""
Export the model produced by the neural-network worker into PyTorch and ONNX artifacts for EZKL.

The model definition, binary reader, and constants are imported from
dfl/neural_network/cli.py so zk_inference follows the active
Python worker implementation instead of duplicating model-layout assumptions.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterable

try:
    import torch
    import torch.nn as nn
except ImportError:  # pragma: no cover - handled at runtime
    torch = None
    nn = None


REPO_ROOT = Path(__file__).resolve().parents[1]
for import_root in (REPO_ROOT / "dfl", REPO_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

try:
    from neural_network.cli import (  # type: ignore
        BATCH_SIZE,
        INPUT_SIZE,
        MODEL_LAYOUT,
        FederatedCNN,
        read_model_bin,
    )
except ImportError as exc:  # pragma: no cover - handled at runtime
    raise RuntimeError(
        "Could not import neural_network.cli. Run this script "
        "from the repository root and ensure dfl/neural_network is present."
    ) from exc


DEFAULT_MODEL_CANDIDATES = (REPO_ROOT / "dfl" / "node_server" / "data" / "results_iid" / "aggregated.bin",)


def metadata_path(path: Path) -> str:
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


def ensure_torch() -> None:
    if torch is None or nn is None:
        raise RuntimeError("PyTorch is required. Install it with: pip install torch")


def default_model_path() -> Path:
    for candidate in DEFAULT_MODEL_CANDIDATES:
        if candidate.exists():
            return candidate
    latest_ipfs_model = latest_ipfs_output_model()
    if latest_ipfs_model is not None:
        return latest_ipfs_model
    legacy_model = REPO_ROOT / "data" / "mnist" / "data" / "results_iid" / "aggregated.bin"
    if legacy_model.exists():
        return legacy_model
    return DEFAULT_MODEL_CANDIDATES[0]


def latest_ipfs_output_model() -> Path | None:
    ipfs_output_dir = REPO_ROOT / "IPFS output"
    if not ipfs_output_dir.exists():
        return None
    models = sorted(ipfs_output_dir.glob("*-aggregated.bin"))
    if not models:
        return None
    return models[-1]


class SoftmaxWrapper(nn.Module):
    def __init__(self, model: FederatedCNN) -> None:
        super().__init__()
        self.model = model

    def forward(self, x: "torch.Tensor") -> "torch.Tensor":
        return torch.softmax(self.model(x), dim=1)


class LogitsWrapper(nn.Module):
    def __init__(self, model: FederatedCNN) -> None:
        super().__init__()
        self.model = model

    def forward(self, x: "torch.Tensor") -> "torch.Tensor":
        return self.model(x)


def load_worker_model(model_path: Path) -> FederatedCNN:
    ensure_torch()
    model = read_model_bin(model_path).eval()
    return model


def clone_as_float32(model: FederatedCNN) -> FederatedCNN:
    cloned = FederatedCNN()
    cloned.load_state_dict({key: value.detach().to(torch.float32) for key, value in model.state_dict().items()})
    cloned.eval()
    return cloned


def generate_sample_inputs(batch_size: int, seed: int) -> "torch.Tensor":
    generator = torch.Generator().manual_seed(seed)
    return (torch.rand((batch_size, INPUT_SIZE), generator=generator, dtype=torch.float64) * 2.0) - 1.0


def check_float_export_parity(model_fp64: FederatedCNN, model_fp32: FederatedCNN, batch_size: int, seed: int) -> dict:
    inputs_fp64 = generate_sample_inputs(batch_size=batch_size, seed=seed)
    with torch.no_grad():
        expected_logits = model_fp64(inputs_fp64)
        expected_probs = torch.softmax(expected_logits, dim=1)
        actual_probs = torch.softmax(model_fp32(inputs_fp64.to(torch.float32)), dim=1).to(torch.float64)
    abs_diff = (expected_probs - actual_probs).abs()
    return {
        "comparison": "neural_network_float64_vs_export_float32_softmax",
        "batch_size": batch_size,
        "seed": seed,
        "max_abs_diff": float(abs_diff.max().item()),
        "mean_abs_diff": float(abs_diff.mean().item()),
    }


def tensor_summary(tensor: "torch.Tensor") -> dict:
    values = tensor.detach().cpu().to(torch.float64)
    return {
        "shape": list(values.shape),
        "min": float(values.min().item()),
        "max": float(values.max().item()),
        "mean": float(values.mean().item()),
    }


def inspect_model(model_path: Path) -> None:
    model = load_worker_model(model_path)
    summary = {name: tensor_summary(value) for name, value in model.state_dict().items()}
    print(json.dumps(summary, indent=2))


def write_manifest(
    manifest_path: Path,
    model_path: Path,
    export_dir: Path,
    model: FederatedCNN,
    parity: dict,
    export_onnx: bool,
    export_batch_size: int,
) -> None:
    manifest = {
        "source_model": metadata_path(model_path),
        "source_implementation": "dfl/neural_network/cli.py",
        "export_dir": metadata_path(export_dir),
        "model": {
            "class": "FederatedCNN",
            "input_size": INPUT_SIZE,
            "batch_size_training": BATCH_SIZE,
            "layout": [{"name": name, "shape": list(shape)} for name, shape in MODEL_LAYOUT],
            "hidden_activations": "tanh",
            "architecture": "conv(1->8, k=3, s=2, p=1) -> tanh -> conv(8->16, k=3, s=2, p=1) -> tanh -> flatten -> linear(784->32) -> tanh -> linear(32->10)",
            "native_output": "logits",
        },
        "files": {
            "torch_state_dict_fp64": "model_state_dict_fp64.pt",
            "torch_state_dict_fp32": "model_state_dict.pt",
            "onnx_probabilities_model": "model.onnx" if export_onnx else None,
            "onnx_logits_model": "model_logits.onnx" if export_onnx else None,
        },
        "onnx": {
            "static_batch_size": export_batch_size if export_onnx else None,
            "opset_version": 18 if export_onnx else None,
        },
        "parameter_summaries": {name: tensor_summary(value) for name, value in model.state_dict().items()},
        "parity_check": parity,
        "notes": [
            "The model is loaded through neural_network.read_model_bin.",
            "The Python worker's native model output is logits; softmax is only wrapped for probability export.",
            "For EZKL, model_logits.onnx is the preferred artifact.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def export_artifacts(
    model_path: Path,
    output_dir: Path,
    export_onnx: bool,
    batch_size: int,
    export_batch_size: int,
    seed: int,
) -> None:
    ensure_torch()
    model_fp64 = load_worker_model(model_path)
    model_fp32 = clone_as_float32(model_fp64)
    output_dir.mkdir(parents=True, exist_ok=True)

    torch.save(model_fp64.state_dict(), output_dir / "model_state_dict_fp64.pt")
    torch.save(model_fp32.state_dict(), output_dir / "model_state_dict.pt")

    parity = check_float_export_parity(model_fp64, model_fp32, batch_size=batch_size, seed=seed)

    if export_onnx:
        dummy_input = torch.randn(export_batch_size, INPUT_SIZE, dtype=torch.float32)
        torch.onnx.export(
            SoftmaxWrapper(model_fp32).eval(),
            dummy_input,
            str(output_dir / "model.onnx"),
            input_names=["input"],
            output_names=["probabilities"],
            opset_version=18,
            dynamo=False,
        )
        torch.onnx.export(
            LogitsWrapper(model_fp32).eval(),
            dummy_input,
            str(output_dir / "model_logits.onnx"),
            input_names=["input"],
            output_names=["logits"],
            opset_version=18,
            dynamo=False,
        )

    write_manifest(
        manifest_path=output_dir / "export_manifest.json",
        model_path=model_path,
        export_dir=output_dir,
        model=model_fp64,
        parity=parity,
        export_onnx=export_onnx,
        export_batch_size=export_batch_size,
    )

    print(f"Loaded model through neural_network: {model_path}")
    print(f"Export directory: {output_dir}")
    print("Saved:")
    print(f"  - {output_dir / 'model_state_dict_fp64.pt'}")
    print(f"  - {output_dir / 'model_state_dict.pt'}")
    if export_onnx:
        print(f"  - {output_dir / 'model.onnx'}")
        print(f"  - {output_dir / 'model_logits.onnx'}")
    print(f"  - {output_dir / 'export_manifest.json'}")
    print("Parity check:")
    print(json.dumps(parity, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Export the active neural-network federated model into PyTorch and ONNX artifacts."
    )
    parser.add_argument(
        "--model",
        type=Path,
        default=default_model_path(),
        help="Path to the aggregated model file produced by the neural-network worker",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("zk_inference/out"),
        help="Directory for exported artifacts",
    )
    parser.add_argument(
        "--no-onnx",
        action="store_true",
        help="Skip ONNX export and only write PyTorch artifacts",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=4,
        help="Batch size used for the float64-vs-float32 parity test",
    )
    parser.add_argument(
        "--export-batch-size",
        type=int,
        default=1,
        help="Fixed batch size used for ONNX export; EZKL usually prefers static batch=1",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=7,
        help="Random seed for parity-test sample inputs",
    )
    parser.add_argument(
        "--inspect",
        action="store_true",
        help="Only inspect the model and print parameter statistics",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.inspect:
            inspect_model(args.model)
            return 0

        export_artifacts(
            model_path=args.model,
            output_dir=args.out,
            export_onnx=not args.no_onnx,
            batch_size=args.batch_size,
            export_batch_size=args.export_batch_size,
            seed=args.seed,
        )
        return 0
    except Exception as exc:  # pragma: no cover - CLI reporting
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

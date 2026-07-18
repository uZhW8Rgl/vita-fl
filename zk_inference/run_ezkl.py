#!/usr/bin/env python3
"""
Run the EZKL setup / prove / verify flow via the Python API.

This is useful when `pip install ezkl` provides the Python module but no `ezkl`
shell binary in the active virtual environment.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import ezkl


def require_ok(step_name: str, result: object) -> None:
    if result is False:
        raise RuntimeError(f"EZKL step failed: {step_name}")


def print_step(message: str) -> None:
    print(f"[ezkl] {message}")


def load_logrows(settings_path: Path) -> int:
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    return int(settings["run_args"]["logrows"])


def quantized_run_args(scale: int) -> object:
    if scale < 8:
        raise ValueError("EZKL scale must be at least 8")
    run_args = ezkl.PyRunArgs()
    run_args.input_scale = scale
    run_args.param_scale = scale
    return run_args


def ensure_srs(settings_path: Path, srs_path: Path) -> None:
    try:
        result = ezkl.get_srs(
            settings_path=str(settings_path),
            srs_path=str(srs_path),
        )
        if result is False:
            raise RuntimeError("EZKL returned False from get_srs")
        if srs_path.exists():
            return
    except RuntimeError as exc:
        if "no running event loop" not in str(exc):
            raise
        print_step("get_srs needs an event loop in this wheel, falling back to gen_srs for local testing")

    logrows = load_logrows(settings_path)
    ezkl.gen_srs(str(srs_path), logrows)
    if not srs_path.exists():
        raise RuntimeError("EZKL step failed: gen_srs did not create the SRS file")


def run_pipeline(
    workdir: Path,
    model_name: str,
    data_name: str,
    target: str,
    skip_calibration: bool,
    scale: int,
) -> None:
    model_path = workdir / model_name
    data_path = workdir / data_name
    settings_path = workdir / "settings.json"
    compiled_path = workdir / "network.ezkl"
    srs_path = workdir / "kzg.srs"
    vk_path = workdir / "vk.key"
    pk_path = workdir / "pk.key"
    witness_path = workdir / "witness.json"
    proof_path = workdir / "proof.json"

    print_step(f"workdir={workdir}")
    print_step(f"model={model_path}")
    print_step(f"data={data_path}")

    print_step(f"gen_settings scale={scale}")
    require_ok(
        "gen_settings",
        ezkl.gen_settings(
            model=str(model_path),
            output=str(settings_path),
            py_run_args=quantized_run_args(scale),
        ),
    )

    if not skip_calibration:
        print_step(f"calibrate_settings target={target}")
        require_ok(
            "calibrate_settings",
            ezkl.calibrate_settings(
                data=str(data_path),
                model=str(model_path),
                settings=str(settings_path),
                target=target,
            ),
        )

    print_step("compile_circuit")
    require_ok(
        "compile_circuit",
        ezkl.compile_circuit(
            model=str(model_path),
            compiled_circuit=str(compiled_path),
            settings_path=str(settings_path),
        ),
    )

    print_step("get_srs")
    ensure_srs(settings_path, srs_path)

    print_step("setup")
    require_ok(
        "setup",
        ezkl.setup(
            model=str(compiled_path),
            vk_path=str(vk_path),
            pk_path=str(pk_path),
            srs_path=str(srs_path),
        ),
    )

    print_step("gen_witness")
    witness = ezkl.gen_witness(
        data=str(data_path),
        model=str(compiled_path),
        output=str(witness_path),
        vk_path=str(vk_path),
        srs_path=str(srs_path),
    )
    if witness is None:
        raise RuntimeError("EZKL step failed: gen_witness")

    print_step("prove")
    require_ok(
        "prove",
        ezkl.prove(
            witness=str(witness_path),
            model=str(compiled_path),
            pk_path=str(pk_path),
            proof_path=str(proof_path),
            srs_path=str(srs_path),
        ),
    )

    print_step("verify")
    require_ok(
        "verify",
        ezkl.verify(
            proof_path=str(proof_path),
            settings_path=str(settings_path),
            vk_path=str(vk_path),
            srs_path=str(srs_path),
        ),
    )

    print_step("done")
    print(f"Artifacts written to {workdir}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the EZKL pipeline for the exported federated model.")
    parser.add_argument(
        "--workdir",
        type=Path,
        default=Path("zk_inference/out"),
        help="Directory containing model_logits.onnx and input.json",
    )
    parser.add_argument(
        "--model",
        default="model_logits.onnx",
        help="ONNX model file relative to workdir",
    )
    parser.add_argument(
        "--data",
        default="input.json",
        help="Input JSON file relative to workdir",
    )
    parser.add_argument(
        "--target",
        default="resources",
        choices=["resources", "accuracy"],
        help="Calibration target",
    )
    parser.add_argument(
        "--skip-calibration",
        action="store_true",
        help="Skip calibrate_settings for faster debugging",
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=int(os.environ.get("EZKL_SCALE", "8")),
        help="Fixed-point input and parameter scale; EZKL 23 requires at least 8",
    )
    args = parser.parse_args()

    run_pipeline(
        workdir=args.workdir,
        model_name=args.model,
        data_name=args.data,
        target=args.target,
        skip_calibration=args.skip_calibration,
        scale=args.scale,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

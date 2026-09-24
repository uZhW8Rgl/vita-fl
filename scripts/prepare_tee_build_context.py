#!/usr/bin/env python3
"""Stage the tracked TEE image inputs without test caches or local credentials."""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path

# Keep this small explicit boundary; Docker COPY regression tests check completeness.
CONTEXT_INPUTS = (
    ".dockerignore",
    "tee_inference",
    "agent_receipts",
    "pki",
    "transport_security",
    "dfl/neural_network",
    "agent/blockchain_source.py",
    "agent/ipfs_bundle.py",
    "data/chestmnist/test_data/test-data.npz",
)


def excluded_input(path: Path) -> bool:
    return (
        any(part in {"__pycache__", ".pytest_cache"} for part in path.parts)
        or path.name.startswith(".env")
        or path.suffix in {".pyc", ".key"}
        or (path.name.startswith("private_key") and path.suffix == ".pem")
        or "enrollment-token" in path.name
        or path.parts[:2] in {("pki", "state"), ("pki", "secrets")}
    )


def prepare_context(source_root: Path, output: Path) -> int:
    source_root = source_root.resolve()
    output = output.resolve()
    if output == source_root or source_root.is_relative_to(output):
        raise ValueError("The build context must not replace the source checkout")
    for entry in CONTEXT_INPUTS:
        source = source_root / entry
        if not source.exists():
            raise FileNotFoundError(f"Missing TEE build input: {entry}")
        if output == source or (source.is_dir() and output.is_relative_to(source)):
            raise ValueError("The output must be outside the copied source trees")
    if output.exists():
        raise FileExistsError("The build context output must be a new directory")
    tracked = subprocess.run(
        ["git", "-C", str(source_root), "ls-files", "-z", "--", *CONTEXT_INPUTS],
        check=True,
        capture_output=True,
    ).stdout
    paths = [Path(name.decode("utf-8")) for name in tracked.split(b"\0") if name]
    paths = [path for path in paths if not excluded_input(path)]
    if not paths:
        raise ValueError("The source checkout contains no tracked TEE build inputs")
    for relative in paths:
        source = source_root / relative
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"TEE build input must be a regular tracked file: {relative}")
    output.mkdir(parents=True)
    for relative in paths:
        destination = output / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, destination)
    return len(paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = prepare_context(args.source_root, args.output)
    print(f"Staged {count} tracked TEE build inputs in {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

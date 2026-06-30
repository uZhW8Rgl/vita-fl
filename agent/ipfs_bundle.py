#!/usr/bin/env python3
"""Deterministic local-IPFS helpers for model bundle discovery and download."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


DEFAULT_IPFS_API_URL = os.environ.get("IPFS_API_URL", "http://127.0.0.1:5001").rstrip("/")
DEFAULT_IPFS_ROOT = os.environ.get("IPFS_ROOT", "/models")
DEFAULT_DOWNLOAD_DIR = Path(os.environ.get("AGENT_DOWNLOAD_DIR", str(Path(__file__).resolve().parent / "downloads")))
MODEL_RE = re.compile(r"(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z)-aggregated\.bin$")
SIGNATURE_RE = re.compile(r"(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-\d{3}Z)-aggregated\.bin\.sig$")
DEFAULT_PAIR_WINDOW_SECONDS = int(os.environ.get("AGENT_PAIR_WINDOW_SECONDS", "120"))


@dataclasses.dataclass(frozen=True)
class IpfsEntry:
    path: str
    name: str
    type: str
    size: int | None = None
    hash: str | None = None
    mtime: int | None = None

    @property
    def is_model(self) -> bool:
        return self.name.endswith("-aggregated.bin")

    @property
    def is_signature(self) -> bool:
        return self.name.endswith("-aggregated.bin.sig")


def _api_url(api_url: str, endpoint: str, params: dict[str, str | int | bool]) -> str:
    query = urllib.parse.urlencode(params)
    return f"{api_url.rstrip('/')}/api/v0/{endpoint}?{query}"


def _request_json(api_url: str, endpoint: str, params: dict[str, str | int | bool]) -> dict:
    request = urllib.request.Request(_api_url(api_url, endpoint, params), method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_bytes(api_url: str, endpoint: str, params: dict[str, str | int | bool]) -> bytes:
    request = urllib.request.Request(_api_url(api_url, endpoint, params), method="POST")
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def _gateway_url(base_url: str, path: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/ipfs"):
        return f"{normalized}{path}"
    return f"{normalized}{path if path.startswith('/ipfs/') else '/ipfs/' + path.lstrip('/')}"


def _join_ipfs_path(parent: str, child: str) -> str:
    if parent in {"", "/"}:
        return f"/{child}"
    return f"{parent.rstrip('/')}/{child}"


def _entry_from_files_ls(parent: str, item: dict[str, Any]) -> IpfsEntry:
    return IpfsEntry(
        path=_join_ipfs_path(parent, item["Name"]),
        name=item["Name"],
        type="directory" if int(item.get("Type", 0)) == 1 else "file",
        size=int(item["Size"]) if "Size" in item else None,
        hash=item.get("Hash"),
        mtime=int(item["Mtime"]) if item.get("Mtime") else None,
    )


def _entry_from_ls(parent: str, item: dict[str, Any]) -> IpfsEntry:
    if parent.startswith("/ipfs/"):
        path = f"{parent.rstrip('/')}/{item['Name']}"
    else:
        path = _join_ipfs_path(parent, item["Name"])
    return IpfsEntry(
        path=path,
        name=item["Name"],
        type="directory" if int(item.get("Type", 2)) == 1 else "file",
        size=int(item["Size"]) if "Size" in item else None,
        hash=item.get("Hash"),
    )


def list_ipfs_tree(api_url: str = DEFAULT_IPFS_API_URL, root: str = DEFAULT_IPFS_ROOT) -> list[IpfsEntry]:
    """List a local MFS path or immutable /ipfs/<cid> tree recursively."""
    entries: list[IpfsEntry] = []
    stack = [root]
    immutable = root.startswith("/ipfs/")
    endpoint = "ls" if immutable else "files/ls"

    while stack:
        current = stack.pop()
        try:
            params: dict[str, str | int | bool] = {"arg": current}
            if not immutable:
                params["long"] = "true"
            payload = _request_json(api_url, endpoint, params)
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not list IPFS path {current!r} via {api_url}: {exc}") from exc

        if immutable:
            links = payload.get("Objects", [{}])[0].get("Links", [])
            children = [_entry_from_ls(current, item) for item in links]
        else:
            children = [_entry_from_files_ls(current, item) for item in payload.get("Entries", [])]

        entries.extend(children)
        stack.extend(entry.path for entry in children if entry.type == "directory")

    return entries


def _artifact_kind(entry: IpfsEntry, entries_list: list[IpfsEntry]) -> str | None:
    if entry.is_model:
        return "model"
    if entry.is_signature:
        return "signature"
    if entry.type != "file":
        return None
    if entry.name.endswith(".sig"):
        sibling_name = entry.name.removesuffix(".sig")
        if any(candidate.name == sibling_name for candidate in entries_list):
            return "signature"
        return None
    if any(candidate.name == f"{entry.name}.sig" for candidate in entries_list):
        return "model"
    return None


def _candidate_model_entries(entries: Iterable[IpfsEntry]) -> list[IpfsEntry]:
    entries_list = list(entries)
    strict_models = [entry for entry in entries_list if entry.is_model]
    if strict_models:
        return strict_models

    signature_names = {entry.name for entry in entries_list if _artifact_kind(entry, entries_list) == "signature"}
    return [
        entry
        for entry in entries_list
        if _artifact_kind(entry, entries_list) == "model" and f"{entry.name}.sig" in signature_names
    ]


def _model_sort_key(entry: IpfsEntry) -> tuple[str, int, str]:
    match = MODEL_RE.search(entry.name)
    stamp = match.group("stamp") if match else ""
    return stamp, entry.mtime or 0, entry.path


def _parse_artifact_timestamp(name: str) -> dt.datetime | None:
    match = MODEL_RE.search(name) or SIGNATURE_RE.search(name)
    if match is None:
        return None
    stamp = match.group("stamp")
    return dt.datetime.strptime(stamp, "%Y-%m-%dT%H-%M-%S-%fZ").replace(tzinfo=dt.timezone.utc)


def _matching_signature(
    model: IpfsEntry,
    signatures: Iterable[IpfsEntry],
    pair_window_seconds: int = DEFAULT_PAIR_WINDOW_SECONDS,
) -> IpfsEntry | None:
    signatures_list = list(signatures)
    expected_sig_name = f"{model.name}.sig"
    same_parent = model.path.rsplit("/", 1)[0]
    for signature in signatures_list:
        if signature.name == expected_sig_name and signature.path.rsplit("/", 1)[0] == same_parent:
            return signature
    for signature in signatures_list:
        if signature.name == expected_sig_name:
            return signature

    model_time = _parse_artifact_timestamp(model.name)
    if model_time is None:
        return None

    candidates: list[tuple[float, int, IpfsEntry]] = []
    for signature in signatures_list:
        signature_time = _parse_artifact_timestamp(signature.name)
        if signature_time is None:
            continue
        delta = (signature_time - model_time).total_seconds()
        distance = abs(delta)
        if distance <= pair_window_seconds:
            same_parent_score = 0 if signature.path.rsplit("/", 1)[0] == same_parent else 1
            future_score = 0 if delta >= 0 else 1
            candidates.append((distance, same_parent_score + future_score, signature))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1], item[2].path))
    return candidates[0][2]


def select_latest_bundle(
    entries: Iterable[IpfsEntry],
    require_latest_model: bool = False,
    pair_window_seconds: int = DEFAULT_PAIR_WINDOW_SECONDS,
) -> dict[str, Any]:
    entries_list = list(entries)
    models = sorted(_candidate_model_entries(entries_list), key=_model_sort_key)
    if not models:
        raise RuntimeError("No model/signature bundle candidates were found in the selected IPFS path.")

    signatures = [entry for entry in entries_list if _artifact_kind(entry, entries_list) == "signature"]
    newest_model = models[-1]
    skipped_unsigned: list[str] = []

    selected_model: IpfsEntry | None = None
    selected_signature: IpfsEntry | None = None
    for model in reversed(models):
        signature = _matching_signature(model, signatures, pair_window_seconds=pair_window_seconds)
        if signature is None:
            skipped_unsigned.append(model.path)
            if require_latest_model:
                break
            continue
        selected_model = model
        selected_signature = signature
        break

    if selected_model is None or selected_signature is None:
        latest_sig_name = f"{newest_model.name}.sig"
        raise RuntimeError(
            f"No complete model/signature bundle found. Latest model {newest_model.path} "
            f"has no matching signature {latest_sig_name}."
        )

    return {
        "model": dataclasses.asdict(selected_model),
        "signature": dataclasses.asdict(selected_signature),
        "selected_at": int(time.time()),
        "selection_policy": "latest_model_must_be_signed" if require_latest_model else "latest_complete_bundle",
        "pair_window_seconds": pair_window_seconds,
        "latest_model_path": newest_model.path,
        "skipped_unsigned_models": skipped_unsigned,
    }


def cat_path(api_url: str, path: str) -> bytes:
    if "/api/v0" in api_url or api_url.rstrip("/").endswith(":5001"):
        return _request_bytes(api_url, "cat", {"arg": path})
    request = urllib.request.Request(_gateway_url(api_url, path), method="GET")
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def fetch_bundle(bundle: dict[str, Any], out_dir: Path, api_url: str = DEFAULT_IPFS_API_URL) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    model_path = out_dir / bundle["model"]["name"]
    signature_path = out_dir / bundle["signature"]["name"]
    model_path.write_bytes(cat_path(api_url, bundle["model"]["path"]))
    signature_path.write_bytes(cat_path(api_url, bundle["signature"]["path"]))
    return {
        "model_path": str(model_path),
        "signature_path": str(signature_path),
        "model_size": model_path.stat().st_size,
        "signature_size": signature_path.stat().st_size,
    }


def discover_latest(
    api_url: str,
    root: str,
    require_latest_model: bool = False,
    pair_window_seconds: int = DEFAULT_PAIR_WINDOW_SECONDS,
) -> dict[str, Any]:
    entries = list_ipfs_tree(api_url=api_url, root=root)
    return select_latest_bundle(
        entries,
        require_latest_model=require_latest_model,
        pair_window_seconds=pair_window_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Discover and fetch the latest aggregated model bundle from local IPFS."
    )
    parser.add_argument("--api-url", default=DEFAULT_IPFS_API_URL)
    parser.add_argument(
        "--root",
        default=DEFAULT_IPFS_ROOT,
        help="MFS path like /models, or immutable path like /ipfs/<cid>",
    )
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--fetch", action="store_true")
    parser.add_argument(
        "--pair-window-seconds",
        type=int,
        default=DEFAULT_PAIR_WINDOW_SECONDS,
        help="Maximum timestamp distance for pairing a model with a separately written signature.",
    )
    parser.add_argument(
        "--require-latest-model",
        action="store_true",
        help="Fail if the newest model has no matching .sig instead of falling back to the newest complete bundle.",
    )
    args = parser.parse_args(argv)

    bundle = discover_latest(
        api_url=args.api_url,
        root=args.root,
        require_latest_model=args.require_latest_model,
        pair_window_seconds=args.pair_window_seconds,
    )
    result: dict[str, Any] = {"bundle": bundle}
    if args.fetch:
        result["download"] = fetch_bundle(bundle, args.out_dir, api_url=args.api_url)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

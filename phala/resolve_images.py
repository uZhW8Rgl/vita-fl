#!/usr/bin/env python3
"""Resolve release tags to immutable GHCR references before changing any CVMs.

CI overlays one successful build on the last deployed image set. Local starts
resolve image-sources.json, while --pinned explicitly selects env-file pins.
No Docker daemon or third-party Python packages are needed.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
IMAGE_KEYS = (
    "worker_image",
    "smart_contracts_image",
    "control_api_image",
    "ui_image",
    "agent_image",
    "transparency_log_image",
    "zk_inference_image",
)
PIN_RE = re.compile(r"^ghcr\.io/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64}$")
TAG_RE = re.compile(r"^ghcr\.io/([a-z0-9][a-z0-9._/-]*):([a-zA-Z0-9_][a-zA-Z0-9_.-]{0,127})$")
REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


def read_env(path: Path) -> dict[str, str]:
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def resolve_reference(reference: str) -> str:
    if PIN_RE.fullmatch(reference):
        return reference
    match = TAG_RE.fullmatch(reference)
    if not match:
        raise ValueError("Image must be a ghcr.io repository with a tag or sha256 digest")
    repository, tag = match.groups()
    token_url = "https://ghcr.io/token?" + urllib.parse.urlencode(
        {
            "service": "ghcr.io",
            "scope": f"repository:{repository}:pull",
        }
    )
    headers = {"Accept": "application/json"}
    token = os.environ.get("GHCR_TOKEN", "")
    if token:
        username = os.environ.get("GHCR_USERNAME") or os.environ.get("GITHUB_ACTOR", "token")
        credentials = base64.b64encode(f"{username}:{token}".encode()).decode()
        headers["Authorization"] = f"Basic {credentials}"
    try:
        with urllib.request.urlopen(urllib.request.Request(token_url, headers=headers), timeout=30) as response:
            auth = json.load(response)
        bearer = auth.get("token") or auth.get("access_token")
        if not isinstance(bearer, str) or not bearer:
            raise ValueError("GHCR did not return a registry access token")
        request = urllib.request.Request(
            f"https://ghcr.io/v2/{repository}/manifests/{tag}",
            headers={"Accept": ACCEPT, "Authorization": f"Bearer {bearer}"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            manifest = response.read()
            declared_digest = response.headers.get("Docker-Content-Digest")
        digest = "sha256:" + hashlib.sha256(manifest).hexdigest()
        if declared_digest and declared_digest != digest:
            raise ValueError(f"Registry manifest digest mismatch for {reference}")
        parsed = json.loads(manifest)
        if not isinstance(parsed, dict) or parsed.get("schemaVersion") != 2:
            raise ValueError(f"Unsupported registry manifest for {reference}")
        return f"ghcr.io/{repository}@{digest}"
    except urllib.error.HTTPError as exc:
        raise ValueError(f"Cannot resolve {reference}: GHCR HTTP {exc.code}; check tag and package access") from None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise ValueError(
            f"Cannot resolve {reference}: registry unavailable or invalid response ({type(exc).__name__})"
        ) from None


def output_map(state: dict, name: str) -> dict:
    item = state.get(name, {})
    if not isinstance(item, dict):
        raise ValueError(f"Malformed Terraform output {name}")
    value = item.get("value", {})
    if not isinstance(value, dict):
        raise ValueError(f"Malformed Terraform output {name}")
    return value


def assignments(items: list[str], *, revision: bool = False) -> dict[str, str]:
    result = {}
    for item in items:
        key, separator, value = item.partition("=")
        if not separator or key not in IMAGE_KEYS or key in result:
            raise ValueError("Each override must use a unique, known Terraform image variable: key=value")
        if revision and not REVISION_RE.fullmatch(value):
            raise ValueError("Image revisions must be full lowercase Git commit SHAs")
        if not revision and not PIN_RE.fullmatch(value):
            raise ValueError("Build overrides must be immutable ghcr.io references with a full sha256 digest")
        result[key] = value
    return result


def image_values(
    sources: dict,
    state: dict,
    overrides: dict,
    revisions: dict,
    env: dict,
    pinned: bool = False,
    resolver=resolve_reference,
) -> dict:
    if set(sources) != set(IMAGE_KEYS) or not all(isinstance(v, str) for v in sources.values()):
        raise ValueError("Image sources must define exactly the seven Terraform image variables")
    previous = output_map(state, "deployment_images")
    previous_revisions = output_map(state, "deployment_image_revisions")
    if set(previous) - set(IMAGE_KEYS) or set(previous_revisions) - set(IMAGE_KEYS):
        raise ValueError("State contains unknown image variables")
    if not all(isinstance(v, str) and PIN_RE.fullmatch(v) for v in previous.values()):
        raise ValueError("State contains an invalid image digest")
    if not all(isinstance(v, str) and REVISION_RE.fullmatch(v) for v in previous_revisions.values()):
        raise ValueError("State contains an invalid image revision")
    if set(revisions) - set(overrides):
        raise ValueError("A build revision requires a matching image override")
    values = {}
    for key in IMAGE_KEYS:
        reference = overrides.get(key) or previous.get(key)
        if reference is None:
            if pinned:
                reference = env.get(key.upper(), "")
                if not PIN_RE.fullmatch(reference):
                    raise ValueError(f"--pinned requires {key.upper()} with a full ghcr.io sha256 reference")
            else:
                reference = sources[key]
        values[key] = resolver(reference)
    # Discard stale revision metadata whenever an operator replaces a digest.
    combined_revisions = {
        key: revision for key, revision in previous_revisions.items() if previous.get(key) == values[key]
    }
    combined_revisions.update(revisions)
    values["deployment_image_revisions"] = combined_revisions
    return values


def write_values(path: Path, values: dict) -> None:
    path = path.resolve()
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".images-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(values, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", type=Path, default=SCRIPT_DIR / "image-sources.json")
    parser.add_argument(
        "--env-file", type=Path, default=Path(os.environ.get("PHALA_ENV_FILE", SCRIPT_DIR.parent / ".env.phala.anvil"))
    )
    parser.add_argument("--from-state", type=Path, help="File produced by terraform output -json")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--set", action="append", default=[], dest="overrides")
    parser.add_argument("--revision", action="append", default=[])
    parser.add_argument("--pinned", action="store_true")
    args = parser.parse_args()
    try:
        state = json.loads(args.from_state.read_text()) if args.from_state else {}
        if not isinstance(state, dict):
            raise ValueError("Terraform output must be a JSON object")
        values = image_values(
            json.loads(args.sources.read_text()),
            state,
            assignments(args.overrides),
            assignments(args.revision, revision=True),
            read_env(args.env_file),
            args.pinned,
        )
        write_values(args.output, values)
    except (ValueError, OSError) as exc:
        print(f"Image resolution failed: {exc}", file=sys.stderr)
        return 1
    print("Resolved all seven Phala images to immutable digests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

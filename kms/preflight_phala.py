"""Probe custom-KMS API access without changing URLs or restarting a CVM."""

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from dotenv import load_dotenv


def preflight(client, cvm_id, *, check_network_permission=False):
    path = f"/cvms/{cvm_id}/network-config"
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "cvm_id": cvm_id,
        "restarted": False,
        "kms_urls_changed": False,
    }
    response = client.get(path)
    result["network_get_status"] = response.status_code
    if response.is_success and check_network_permission:
        current = response.json()
        response = client.patch(path, json={"kms_urls": current["kms_urls"], "restart": False})
        result["identical_urls_patch_status"] = response.status_code
        result["custom_kms_configuration_available"] = response.is_success
        # Only retain the known feature-gate reason, never arbitrary API payloads.
        if response.status_code == 403:
            known = "Updating KMS URLs is not enabled for this workspace"
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            result["blocker"] = known if detail == known else "Phala denied custom KMS configuration (HTTP 403)"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cvm-id", required=True)
    parser.add_argument("--env-file", type=Path, action="append", default=[])
    parser.add_argument("--check-network-permission", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    for file in args.env_file:
        load_dotenv(file, override=False)
    key = os.environ.get("PHALA_CLOUD_API_KEY") or os.environ.get("PHALA_API_KEY")
    if not key:
        raise SystemExit("PHALA_CLOUD_API_KEY required")
    with httpx.Client(
        base_url="https://cloud-api.phala.com/api/v1",
        headers={"X-API-Key": key, "X-Phala-Version": "2026-01-21"},
        timeout=40,
    ) as client:
        result = preflight(client, args.cvm_id, check_network_permission=args.check_network_permission)
    raw = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x") as target:
            target.write(raw)
    print(raw, end="")


if __name__ == "__main__":
    main()

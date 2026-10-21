"""Render an additive key-service deployment for the existing contract CVM.

Input/output contain environment placeholders, never decrypted credentials.
Keep the original manifest for rollback. Reapply this overlay after Terraform
renders a new base manifest; it is deliberately not part of the default profile.
"""

import argparse
import copy
import json
import re
from pathlib import Path

import yaml


def render(app, *, service_image, worker_image, contracts_image, measurements):
    for image in (service_image, worker_image, contracts_image):
        if not re.fullmatch(r"ghcr\.io/uzhw8rgl/master-thesis-dfl-worker@sha256:[a-f0-9]{64}", image):
            raise ValueError("Explicit reviewed image digests required")
    result = copy.deepcopy(app)
    spec = yaml.safe_load(result["docker_compose_file"])
    services = spec["services"]
    if {"key-service-init", "key-service"} & services.keys():
        raise ValueError("Overlay already applied; review the original base manifest")
    registry = services["control-api"]["environment"]["DYNAMIC_WORKER_EXPECTED_DEVICE_REGISTRY_ADDRESS"]
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", registry):
        raise ValueError("Pinned runtime registry required")
    if services["smart-contracts"]["environment"].get("ETH_WALLET_PRIVATE_KEY") != "${ETH_WALLET_PRIVATE_KEY}":
        raise ValueError("Only encrypted environment placeholders may be rendered")
    contracts = services["smart-contracts"]
    contracts["image"] = contracts_image
    services["rpc-proxy"]["image"] = contracts_image
    contracts["environment"].update(
        EXPECTED_WORKER_IMAGE=worker_image,
        TDX_REFERENCE_QUOTE_PATH="../data/dstack-0.5.12-reference-tdx-quote",
        KEY_SERVICE_ENABLED="1",
    )
    for service in services.values():
        env = service.get("environment", {})
        for name in ("DYNAMIC_WORKER_IMAGE",):
            if name in env:
                env[name] = worker_image
        for name in ("DYNAMIC_WORKER_OS_IMAGE", "PHALA_OS_IMAGE"):
            if name in env:
                env[name] = "dstack-0.5.12"
        if "RATLS_ALLOWED_PLATFORM_MEASUREMENTS" in env:
            env["RATLS_ALLOWED_PLATFORM_MEASUREMENTS"] = json.dumps(measurements, separators=(",", ":"))
    services["key-service"] = {
        "image": service_image,
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges:true"],
        "environment": {
            "KEY_SERVICE_RPC_URL": "http://anvil:8545",
            "KEY_SERVICE_REGISTRY": registry,
            "KEY_SERVICE_TRUST_POLICY": "${KEY_SERVICE_TRUST_POLICY}",
        },
        "volumes": ["/var/run/dstack.sock:/var/run/dstack.sock:ro"],
        "user": "10001:10001",
        "restart": "unless-stopped",
        "depends_on": {"smart-contracts": {"condition": "service_completed_successfully"}},
        "ports": ["19002:8000"],
        "healthcheck": {
            "test": [
                "CMD",
                "node",
                "-e",
                "fetch('http://127.0.0.1:8000/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))",
            ],
            "interval": "10s",
            "timeout": "5s",
            "retries": 12,
        },
    }
    # The independently reviewed public policy is supplied separately. Putting
    # its own expected Compose hash into measured Compose creates a hash cycle.
    result["allowed_envs"] = list(dict.fromkeys([*result.get("allowed_envs", []), "KEY_SERVICE_TRUST_POLICY"]))
    result["docker_compose_file"] = yaml.safe_dump(spec, sort_keys=False)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base")
    parser.add_argument("output")
    parser.add_argument("--images", required=True, help="JSON: service, worker, contracts, immutable references")
    parser.add_argument("--platform-policy", required=True)
    args = parser.parse_args()
    images = json.loads(Path(args.images).read_text())
    policy = json.loads(Path(args.platform_policy).read_text())
    output = render(
        json.loads(Path(args.base).read_text()),
        service_image=images["service"],
        worker_image=images["worker"],
        contracts_image=images["contracts"],
        measurements=policy["allowed_platform_measurements"],
    )
    with Path(args.output).open("x") as target:
        json.dump(output, target, indent=2)
        target.write("\n")


if __name__ == "__main__":
    main()

"""Create public service trust references from independently reviewed deployment bytes.

Do not fetch or approve references from the service being checked. Use the exact
canonical manifest returned by provisioning, before boot, plus independently
accepted platform measurements. Supply the resulting JSON separately through
the encrypted runtime environment and in the workers' measured configuration.
"""

import argparse
import hashlib
import json
import re
from pathlib import Path

import yaml

from key_service.verify_service import validate_policy


def render(manifest_bytes, *, app_id, origin, measurements, expected_compose_hash=None):
    manifest = json.loads(manifest_bytes)
    services = yaml.safe_load(manifest["docker_compose_file"])["services"]
    digest = hashlib.sha256(manifest_bytes.encode()).hexdigest()
    if expected_compose_hash is not None:
        # Provisioning can return a JSON object saved locally with indentation.
        # Its compact serialization must match the independently returned hash.
        compact = json.dumps(manifest, separators=(",", ":"), ensure_ascii=False)
        if expected_compose_hash not in (digest, hashlib.sha256(compact.encode()).hexdigest()):
            raise ValueError("Manifest bytes differ from the independently provisioned Compose hash")
        digest = expected_compose_hash
    policy = {
        "version": 1,
        "appId": app_id,
        "origin": origin,
        "composeHash": digest,
        "serviceImage": services["key-service"]["image"],
        "platformMeasurements": measurements,
    }
    validate_policy(policy)
    if any(not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", s.get("image", "")) for s in services.values()):
        raise ValueError("Pin all co-located runtime images before approving the service manifest")
    return policy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("output")
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--origin", required=True)
    parser.add_argument("--platform-policy", required=True)
    parser.add_argument("--expected-compose-hash", required=True)
    args = parser.parse_args()
    policy = render(
        Path(args.manifest).read_text(),
        app_id=args.app_id,
        origin=args.origin,
        measurements=json.loads(Path(args.platform_policy).read_text())["allowed_platform_measurements"],
        expected_compose_hash=args.expected_compose_hash,
    )
    with Path(args.output).open("x") as target:
        json.dump(policy, target, indent=2)
        target.write("\n")


if __name__ == "__main__":
    main()

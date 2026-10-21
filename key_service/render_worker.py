"""Render an opt-in broker worker from an existing exact dstack app manifest.

No cloud calls. Authorise the resulting measured bytes, not the input manifest.
"""

import argparse
import json
import re
from urllib.parse import urlsplit

import yaml


class IndentedSafeDumper(yaml.SafeDumper):
    def increase_indent(self, flow=False, indentless=False):
        return super().increase_indent(flow, False)


def render(manifest, *, origin, policy, service_id):
    url = urlsplit(origin)
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise ValueError("HTTPS key-service origin required")
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", policy) or not re.fullmatch(r"[0-9a-f]{64}", service_id):
        raise ValueError("explicit service identity and policy required")
    app = json.loads(manifest)
    compose = yaml.safe_load(app["docker_compose_file"])
    if not isinstance(compose.get("services"), dict) or set(compose["services"]) != {"dfl-worker"}:
        raise ValueError("one reviewed dfl-worker service required")
    worker = compose["services"]["dfl-worker"]
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", worker.get("image", "")):
        raise ValueError("immutable worker image required")
    env = worker.get("environment")
    if not isinstance(env, dict) or str(env.get("EXPECTED_CHAIN_ID")) != "31337":
        raise ValueError("Anvil worker environment required")
    env.update(
        {
            "PARTICIPANT_KEY_PROVIDER": "broker",
            "SELLO_SERVICE_KEY_PROVIDER": "broker",
            "KEY_SERVICE_URL": origin.rstrip("/"),
            "KEY_SERVICE_POLICY": policy.lower(),
            "KEY_SERVICE_ID": service_id,
        }
    )
    mounts = worker.setdefault("volumes", [])
    if not isinstance(mounts, list) or any("/var/lib/vita-fl" in str(item) for item in mounts):
        raise ValueError("review existing participant-state mounts before conversion")
    volumes = compose.setdefault("volumes", {})
    if set(volumes) - {"participant-key-state"} or volumes.get("participant-key-state") not in ({}, None):
        raise ValueError("review existing top-level volume definitions")
    mounts.append("participant-key-state:/var/lib/vita-fl")
    volumes["participant-key-state"] = None
    # Keep the exact YAML subset understood by Node and Solidity admission parsers.
    app["docker_compose_file"] = yaml.dump(compose, Dumper=IndentedSafeDumper, sort_keys=False).replace(
        "  participant-key-state: null", "  participant-key-state:"
    )
    return json.dumps(app, separators=(",", ":"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input")
    parser.add_argument("output")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--policy", required=True)
    parser.add_argument("--service-id", required=True)
    args = parser.parse_args()
    with open(args.input, encoding="utf-8") as source:
        result = render(source.read(), origin=args.origin, policy=args.policy, service_id=args.service_id)
    with open(args.output, "x", encoding="utf-8") as target:
        target.write(result)


if __name__ == "__main__":
    main()

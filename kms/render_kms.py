"""Render a pinned, real dstack KMS CVM with local-provider protected root storage."""

import argparse
import hashlib
import json
import shlex
from pathlib import Path
from urllib.parse import urlparse

KMS_IMAGE = "dstacktee/dstack-kms@sha256:e959bc5b4c7664d26543801073cc445d806fcea49a240881f707d1998e2d871c"


def render(*, auth_url, image_download_url, admin_token_hash):
    # Auth policy and OS metadata are trusted inputs to the KMS. Do not silently
    # expose them to manipulation through plain HTTP on a cloud network.
    for name, value in [("auth_url", auth_url), ("image_download_url", image_download_url)]:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or any(character in value for character in "$\r\n\t")
        ):
            raise ValueError(f"{name} requires HTTPS without embedded credentials")
    if len(admin_token_hash) != 64 or any(x not in "0123456789abcdef" for x in admin_token_hash):
        raise ValueError("admin_token_hash must be a SHA-256 hex digest")
    if admin_token_hash == "0" * 64:
        raise ValueError("admin token must be set")
    config = f"""[rpc]
address = "0.0.0.0"
port = 8000
[rpc.tls]
key = "/kms/certs/rpc.key"
certs = "/kms/certs/rpc.crt"
[rpc.tls.mutual]
ca_certs = "/kms/certs/tmp-ca.crt"
mandatory = false
[core]
cert_dir = "/kms/certs"
admin_token_hash = "{admin_token_hash}"
enforce_self_authorization = true
[core.image]
verify = true
cache_dir = "/kms/images"
download_url = {json.dumps(image_download_url)}
download_timeout = "2m"
[core.auth_api]
type = "webhook"
[core.auth_api.webhook]
url = {json.dumps(auth_url.rstrip("/"))}
[core.onboard]
enabled = true
auto_bootstrap_domain = ""
address = "0.0.0.0"
port = 8000
"""
    compose = {
        "services": {
            "kms": {
                "image": KMS_IMAGE,
                "volumes": ["kms-state:/kms", "/var/run/dstack.sock:/var/run/dstack.sock"],
                "ports": ["8000:8000"],
                "restart": "unless-stopped",
                "command": [
                    "sh",
                    "-ec",
                    "umask 077\nmkdir -p /kms/certs /kms/images\nprintf '%s' "
                    + shlex.quote(config)
                    + " > /kms/kms.toml\nexec dstack-kms -c /kms/kms.toml",
                ],
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
            }
        },
        "volumes": {"kms-state": {}},
    }
    return {
        "manifest_version": 2,
        "name": "vita-fl-own-kms",
        "runner": "docker-compose",
        "docker_compose_file": json.dumps(compose, separators=(",", ":")),
        "kms_enabled": False,
        "local_key_provider_enabled": True,
        "no_instance_id": True,
        "public_logs": False,
        "public_sysinfo": False,
        "public_tcbinfo": True,
        "allowed_envs": [],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-url", required=True)
    parser.add_argument("--image-download-url", required=True)
    parser.add_argument("--admin-token-hash", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = render(
        auth_url=args.auth_url,
        image_download_url=args.image_download_url,
        admin_token_hash=args.admin_token_hash,
    )
    raw = json.dumps(manifest, separators=(",", ":")).encode()
    with args.output.open("xb") as target:
        target.write(raw)
    print(json.dumps({"compose_hash": hashlib.sha256(raw).hexdigest(), "output": str(args.output)}))


if __name__ == "__main__":
    main()

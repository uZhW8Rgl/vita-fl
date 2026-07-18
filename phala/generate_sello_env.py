#!/usr/bin/env python3
"""Print a coherent Sello prototype key set for .env.phala.anvil."""

from __future__ import annotations

import base64
import argparse
import json
import os

from nacl.signing import SigningKey


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scitt-url", required=True, help="Public HTTPS gateway URL for contract-runtime port 8000")
    args = parser.parse_args()
    if not args.scitt_url.startswith("https://"):
        parser.error("--scitt-url must use HTTPS")
    issuer = SigningKey.generate()
    tee = SigningKey.generate()
    zk = SigningKey.generate()
    print("ENABLE_SELLO_RECEIPTS=true")
    print(f"SELLO_SCITT_URL={args.scitt_url.rstrip('/')}")
    print(f"SELLO_TOKEN_ISSUER_SIGNING_SEED={encode(bytes(issuer))}")
    print(f"SELLO_OWNER_HPKE_PRIVATE_KEY={encode(os.urandom(32))}")
    print(f"SELLO_TOKEN_ISSUER_PUBLIC_KEY={encode(bytes(issuer.verify_key))}")
    print(f"SELLO_TEE_SERVICE_SIGNING_SEED={encode(bytes(tee))}")
    print(f"SELLO_ZK_SERVICE_SIGNING_SEED={encode(bytes(zk))}")
    registry = {
        "tee-inference": encode(bytes(tee.verify_key)),
        "zk-inference": encode(bytes(zk.verify_key)),
    }
    print("SELLO_SERVICE_REGISTRY=" + json.dumps(registry, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

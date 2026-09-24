#!/usr/bin/env python3
"""Print independent owner, agent-PoP and ZK Sello keys for .env.phala.anvil.

The TEE receiver derives its signing key from dstack and therefore has no
operator-provisioned seed or static registry entry.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re

from nacl.signing import SigningKey


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scitt-url", required=True, help="Public HTTPS gateway URL for contract-runtime port 8000")
    parser.add_argument(
        "--agent-subject", default="master-thesis-agent", help="Authorized subject for the receiver PoP registry"
    )
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.agent_subject):
        parser.error("--agent-subject must be a simple registered agent identifier")
    if not args.scitt_url.startswith("https://"):
        parser.error("--scitt-url must use HTTPS")
    issuer = SigningKey.generate()
    zk = SigningKey.generate()
    agent_pop = SigningKey.generate()
    print(f"SELLO_SCITT_URL={args.scitt_url.rstrip('/')}")
    print(f"SELLO_TOKEN_ISSUER_SIGNING_SEED={encode(bytes(issuer))}")
    print(f"PKI_AGENT_SUBJECT={args.agent_subject}")
    print(f"AGENT_POP_SIGNING_SEED={encode(bytes(agent_pop))}")
    print(
        "AGENT_POP_REGISTRY="
        + json.dumps({args.agent_subject: encode(bytes(agent_pop.verify_key))}, separators=(",", ":"))
    )
    print(f"SELLO_OWNER_HPKE_PRIVATE_KEY={encode(os.urandom(32))}")
    print(f"SELLO_TOKEN_ISSUER_PUBLIC_KEY={encode(bytes(issuer.verify_key))}")
    print(f"SELLO_ZK_SERVICE_SIGNING_SEED={encode(bytes(zk))}")
    registry = {
        "zk-inference": encode(bytes(zk.verify_key)),
    }
    print("SELLO_SERVICE_REGISTRY=" + json.dumps(registry, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

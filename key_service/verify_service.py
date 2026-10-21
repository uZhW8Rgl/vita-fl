"""Appraise the service's quote against independently configured references.

Used by both the worker and service self-authorisation. There is no mock mode.
The service's complete Compose is measured because it shares a CVM with other
containers. No root keys or participant credentials enter this process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from urllib.parse import urlsplit

import yaml

from key_service.verify import deployment_bindings, unique_json
from transport_security.attestation import DEFAULT_PHALA_VERIFY_URL, parse_tdx_quote, verify_quote_with_phala

POLICY_FIELDS = {"version", "appId", "composeHash", "serviceImage", "origin", "platformMeasurements"}
CLAIM_FIELDS = {
    "version",
    "protocol",
    "nonce",
    "issuedAt",
    "expiresAt",
    "appId",
    "composeHash",
    "trustPolicyHash",
    "chainId",
    "registry",
    "policy",
    "serviceId",
    "codeHash",
    "serviceInstanceId",
    "servicePublicKey",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def validate_policy(policy):
    if (
        not isinstance(policy, dict)
        or set(policy) != POLICY_FIELDS
        or type(policy["version"]) is not int
        or policy["version"] != 1
    ):
        raise ValueError("invalid service policy")
    if not re.fullmatch(r"0x[a-f0-9]{40}", policy["appId"]) or not re.fullmatch(r"[a-f0-9]{64}", policy["composeHash"]):
        raise ValueError("invalid service measurement reference")
    if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", policy["serviceImage"]):
        raise ValueError("digest-pinned service image required")
    url = urlsplit(policy["origin"])
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username
        or url.password
        or url.path
        or url.query
        or url.fragment
    ):
        raise ValueError("service HTTPS origin required")
    platforms = policy["platformMeasurements"]
    if not isinstance(platforms, list) or not 1 <= len(platforms) <= 32:
        raise ValueError("explicit service platform references required")
    for platform in platforms:
        if not isinstance(platform, dict) or set(platform) != {"mrtd", "rtmr0", "rtmr1", "rtmr2"}:
            raise ValueError("invalid service platform tuple")
        if any(not isinstance(x, str) or not re.fullmatch(r"[a-f0-9]{96}", x) for x in platform.values()):
            raise ValueError("invalid service platform measurement")
    return policy


def report_data(claims):
    return hashlib.sha512(b"VITA-FL/key-service/identity/v1\x00" + canonical(claims)).digest()


def verify(value, *, appraise=verify_quote_with_phala, now=None):
    if not isinstance(value, dict) or set(value) != {"claims", "evidence", "policy"}:
        raise ValueError("invalid service appraisal input")
    policy = validate_policy(value["policy"])
    claims, evidence = value["claims"], value["evidence"]
    now = int(time.time()) if now is None else now
    if (
        not isinstance(claims, dict)
        or set(claims) != CLAIM_FIELDS
        or type(claims["version"]) is not int
        or claims["version"] != 1
    ):
        raise ValueError("invalid service claims")
    if claims["protocol"] != "onchain-v3" or claims["chainId"] != "31337":
        raise ValueError("invalid service protocol")
    for name in ("nonce", "serviceId", "serviceInstanceId", "servicePublicKey"):
        if not isinstance(claims[name], str) or not re.fullmatch(r"[a-f0-9]{64}", claims[name]):
            raise ValueError("invalid service binding")
    for name in ("registry", "policy"):
        if not isinstance(claims[name], str) or not re.fullmatch(r"0x[a-f0-9]{40}", claims[name]):
            raise ValueError("invalid service contract")
    if not isinstance(claims["codeHash"], str) or not re.fullmatch(r"0x[a-f0-9]{64}", claims["codeHash"]):
        raise ValueError("invalid contract code hash")
    if any(type(claims[name]) is not int for name in ("issuedAt", "expiresAt")) or not (
        now - 150 <= claims["issuedAt"] <= now + 30
        and now < claims["expiresAt"] <= claims["issuedAt"] + 120
        and claims["issuedAt"] < claims["expiresAt"]
    ):
        raise ValueError("service evidence expired")
    if claims["trustPolicyHash"] != hashlib.sha256(canonical(policy)).hexdigest() or any(
        claims[name] != policy[name] for name in ("appId", "composeHash")
    ):
        raise ValueError("service trust policy mismatch")
    if not isinstance(evidence, dict) or set(evidence) != {"quote", "eventLog", "appCompose"}:
        raise ValueError("invalid service evidence")
    if not isinstance(evidence["quote"], str) or not re.fullmatch(r"(?:[a-fA-F0-9]{2}){637,65536}", evidence["quote"]):
        raise ValueError("invalid service quote")
    quote = bytes.fromhex(evidence["quote"])
    expected = report_data(claims)
    fields = parse_tdx_quote(quote)
    if fields["reportdata"] != expected:
        raise ValueError("service REPORTDATA mismatch")
    if not any(all(fields[k].hex() == v for k, v in p.items()) for p in policy["platformMeasurements"]):
        raise ValueError("service platform mismatch")
    bindings = deployment_bindings(quote, evidence["eventLog"], evidence["appCompose"], single_image=False)
    if any(bindings[k] != policy[k] for k in bindings):
        raise ValueError("service deployment mismatch")
    manifest = json.loads(evidence["appCompose"], object_pairs_hook=unique_json)
    services = yaml.safe_load(manifest["docker_compose_file"])["services"]
    if services.get("key-service", {}).get("image") != policy["serviceImage"]:
        raise ValueError("service image mismatch")
    if any(
        not isinstance(s, dict) or not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", s.get("image", ""))
        for s in services.values()
    ):
        raise ValueError("all co-located runtime images must be pinned")
    # The service identity verifier has its own platform references and a fixed
    # HTTPS verifier origin; worker/runtime environment cannot loosen either.
    os.environ["RATLS_ALLOWED_PLATFORM_MEASUREMENTS"] = json.dumps(policy["platformMeasurements"])
    os.environ["PHALA_ATTESTATION_VERIFY_URL"] = DEFAULT_PHALA_VERIFY_URL
    os.environ["PHALA_ATTESTATION_ALLOWED_HOSTS"] = "cloud-api.phala.com"
    result = appraise(quote, expected)
    if not isinstance(result, dict) or result.get("verified") is not True:
        raise ValueError("service quote authentication required")
    return {"verified": True, **bindings}


def main():
    try:
        raw = sys.stdin.buffer.read(1048577)
        if len(raw) > 1048576:
            raise ValueError("service evidence too large")
        result = verify(json.loads(raw, object_pairs_hook=unique_json))
        print(json.dumps(result, separators=(",", ":")))
    except Exception:
        print("service attestation rejected", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

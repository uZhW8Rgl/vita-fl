"""No secret keys: authenticate a quote and its deployment bindings before release.

The HTTP service passes its own expected claims, not a client-selected REPORTDATA.
Cryptographic DCAP appraisal uses the existing fail-closed online verifier.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys

from transport_security.attestation import parse_tdx_quote, validate_attestation_configuration, verify_quote_with_phala

DOMAIN = b"VITA-FL/key-service/attestation/v1\x00"
MAX_INPUT = 524288
CLAIM_KEYS = {
    "version",
    "challenge",
    "participant",
    "appId",
    "composeHash",
    "registry",
    "chainId",
    "policy",
    "serviceId",
    "recipientPublicKey",
}


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def validate_claims(claims):
    if not isinstance(claims, dict) or set(claims) != CLAIM_KEYS:
        raise ValueError("invalid release claims")
    if type(claims["version"]) is not int or claims["version"] != 1 or claims["chainId"] != "31337":
        raise ValueError("Anvil v1 claims required")
    for name in ("participant", "appId", "registry", "policy"):
        if not isinstance(claims[name], str) or not re.fullmatch(r"0x[0-9a-f]{40}", claims[name]):
            raise ValueError("invalid address claim")
    for name in ("challenge", "composeHash", "serviceId", "recipientPublicKey"):
        if not isinstance(claims[name], str) or not re.fullmatch(r"[0-9a-f]{64}", claims[name]):
            raise ValueError("invalid digest claim")
    return claims


def report_data(claims):
    value = json.dumps(validate_claims(claims), sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha512(DOMAIN + value).digest()


def deployment_bindings(quote, event_log, app_compose, *, single_image=True):
    fields = parse_tdx_quote(quote)
    events = json.loads(event_log, object_pairs_hook=unique_json)
    if not isinstance(events, list) or not 1 <= len(events) <= 512:
        raise ValueError("invalid measured event log")
    state = bytes(48)
    measured = {}
    for event in events:
        if not isinstance(event, dict) or type(event.get("imr")) is not int:
            raise ValueError("invalid measured event")
        if event["imr"] != 3:
            continue
        if event.get("event_type") != 0x08000001:
            raise ValueError("unsupported event type")
        name, payload = event.get("event"), event.get("event_payload")
        if (
            not isinstance(name, str)
            or not isinstance(payload, str)
            or not re.fullmatch(r"(?:[0-9a-fA-F]{2})*", payload)
        ):
            raise ValueError("invalid measured event payload")
        raw = bytes.fromhex(payload)
        digest = hashlib.sha384((0x08000001).to_bytes(4, "little") + b":" + name.encode() + b":" + raw).digest()
        state = hashlib.sha384(state + digest).digest()
        if name in {"app-id", "compose-hash"}:
            if name in measured:
                raise ValueError("duplicate deployment measurement")
            measured[name] = raw
    if state != fields["rtmr3"]:
        raise ValueError("RTMR3 replay mismatch")
    if set(measured) != {"app-id", "compose-hash"} or len(measured["app-id"]) != 20:
        raise ValueError("missing deployment measurement")
    if not isinstance(app_compose, str) or hashlib.sha256(app_compose.encode()).digest() != measured["compose-hash"]:
        raise ValueError("Compose does not match authenticated measurement")
    app = json.loads(app_compose, object_pairs_hook=unique_json)
    compose = app.get("docker_compose_file") if isinstance(app, dict) else None
    if not isinstance(compose, str):
        raise ValueError("Compose manifest required")
    images = re.findall(r"(?m)^\s*image:\s*[\"\']?([^\s\"\'#]+)[\"\']?\s*$", compose)
    if (
        not images
        or (single_image and len(images) != 1)
        or any(not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", image) for image in images)
    ):
        raise ValueError("digest-pinned workload images required")
    return {"appId": "0x" + measured["app-id"].hex(), "composeHash": measured["compose-hash"].hex()}


def verify(value, *, appraise=verify_quote_with_phala):
    if not isinstance(value, dict) or set(value) != {"claims", "evidence"}:
        raise ValueError("invalid appraisal input")
    claims = validate_claims(value["claims"])
    evidence = value["evidence"]
    if not isinstance(evidence, dict) or set(evidence) != {"quote", "eventLog", "appCompose"}:
        raise ValueError("invalid evidence fields")
    if not isinstance(evidence["quote"], str) or not re.fullmatch(r"(?:[a-fA-F0-9]{2}){637,65536}", evidence["quote"]):
        raise ValueError("invalid quote encoding")
    quote = bytes.fromhex(evidence["quote"])
    expected = report_data(claims)
    if parse_tdx_quote(quote)["reportdata"] != expected:
        raise ValueError("release REPORTDATA mismatch")
    binding = deployment_bindings(quote, evidence["eventLog"], evidence["appCompose"])
    if any(binding[name] != claims[name] for name in binding):
        raise ValueError("release deployment mismatch")
    appraisal = appraise(quote, expected)
    if not isinstance(appraisal, dict) or appraisal.get("verified") is not True:
        raise ValueError("quote authentication required")
    return {"verified": True, **binding}


def main():
    try:
        if sys.argv[1:] == ["--check-config"]:
            validate_attestation_configuration()
            return 0
        raw = sys.stdin.buffer.read(MAX_INPUT + 1)
        if len(raw) > MAX_INPUT:
            raise ValueError("appraisal input too large")
        result = verify(json.loads(raw, object_pairs_hook=unique_json))
        print(json.dumps(result, separators=(",", ":")))
    except Exception:
        # Do not expose evidence, provider responses, credentials, or stack traces.
        print("attestation rejected", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

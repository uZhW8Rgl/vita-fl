"""Labelled synthetic evidence tests; no fixture establishes hardware authenticity."""

import copy
import hashlib
import json
import shutil
import subprocess

import pytest
import yaml

from key_service.test_verify import fixture as worker_fixture
from key_service.verify_service import canonical, report_data, verify
from transport_security.attestation import TDX_HEADER_SIZE, TDX_MEASUREMENT_OFFSETS

NOW = 1700000000


def fixture():
    original = worker_fixture()
    image = "example/service@sha256:" + "22" * 32
    manifest = json.dumps(
        {
            "docker_compose_file": yaml.safe_dump(
                {
                    "services": {
                        "key-service": {"image": image},
                        "anvil": {"image": "example/anvil@sha256:" + "33" * 32},
                    }
                }
            )
        }
    )
    digest = hashlib.sha256(manifest.encode()).hexdigest()
    policy = {
        "version": 1,
        "appId": original["claims"]["appId"],
        "composeHash": digest,
        "serviceImage": image,
        "origin": "https://keys.example",
        "platformMeasurements": [{k: "44" * 48 for k in ("mrtd", "rtmr0", "rtmr1", "rtmr2")}],
    }
    claims = {k: original["claims"][k] for k in ("chainId", "registry", "policy", "serviceId")}
    claims.update(
        version=1,
        protocol="onchain-v3",
        nonce="55" * 32,
        issuedAt=NOW,
        expiresAt=NOW + 120,
        appId=policy["appId"],
        composeHash=digest,
        trustPolicyHash=hashlib.sha256(canonical(policy)).hexdigest(),
        codeHash="0x" + "66" * 32,
        serviceInstanceId="77" * 32,
        servicePublicKey="88" * 32,
    )
    events = json.loads(original["evidence"]["eventLog"])
    events[1]["event_payload"] = digest
    state = bytes(48)
    for event in events:
        event_hash = hashlib.sha384(
            (0x08000001).to_bytes(4, "little")
            + b":"
            + event["event"].encode()
            + b":"
            + bytes.fromhex(event["event_payload"])
        ).digest()
        state = hashlib.sha384(state + event_hash).digest()
    quote = bytearray.fromhex(original["evidence"]["quote"])
    for name in ("mrtd", "rtmr0", "rtmr1", "rtmr2"):
        offset = TDX_HEADER_SIZE + TDX_MEASUREMENT_OFFSETS[name]
        quote[offset : offset + 48] = bytes.fromhex(policy["platformMeasurements"][0][name])
    quote[520:568] = state
    quote[568:632] = report_data(claims)
    return {
        "claims": claims,
        "policy": policy,
        "evidence": {"quote": quote.hex(), "eventLog": json.dumps(events), "appCompose": manifest},
    }


def test_full_runtime_identity_and_signer_are_bound_to_authenticated_quote():
    value = fixture()
    assert verify(value, appraise=lambda *_: {"verified": True}, now=NOW)["verified"]
    with pytest.raises(ValueError, match="authentication"):
        verify(value, appraise=lambda *_: {"verified": False}, now=NOW)


@pytest.mark.parametrize(
    "name", ["nonce", "servicePublicKey", "serviceInstanceId", "registry", "policy", "serviceId", "codeHash"]
)
def test_quote_cannot_be_rebound_to_another_signer_request_or_contract(name):
    value = fixture()
    value["claims"][name] = ("0x" if value["claims"][name].startswith("0x") else "") + "99" * (
        20 if name in ("registry", "policy") else 32
    )
    with pytest.raises(ValueError, match="REPORTDATA"):
        verify(value, appraise=lambda *_: {"verified": True}, now=NOW)


def test_changed_service_configuration_platform_debug_and_expiry_are_denied():
    original = fixture()
    variants = []
    changed = copy.deepcopy(original)
    changed["evidence"]["appCompose"] += " "
    variants.append(changed)
    changed = copy.deepcopy(original)
    changed["policy"]["serviceImage"] = "evil/service@sha256:" + "99" * 32
    variants.append(changed)
    changed = copy.deepcopy(original)
    quote = bytearray.fromhex(changed["evidence"]["quote"])
    quote[168] = 1
    changed["evidence"]["quote"] = quote.hex()
    variants.append(changed)
    changed = copy.deepcopy(original)
    quote = bytearray.fromhex(changed["evidence"]["quote"])
    quote[184] ^= 1
    changed["evidence"]["quote"] = quote.hex()
    variants.append(changed)
    for value in variants:
        with pytest.raises(ValueError):
            verify(value, appraise=lambda *_: {"verified": True}, now=NOW)
    with pytest.raises(ValueError, match="expired"):
        verify(original, appraise=lambda *_: {"verified": True}, now=NOW + 121)


def test_public_policy_builder_requires_all_cvm_images_pinned():
    from key_service.render_service_policy import render

    value = fixture()
    assert (
        render(
            value["evidence"]["appCompose"],
            app_id=value["policy"]["appId"],
            origin=value["policy"]["origin"],
            measurements=value["policy"]["platformMeasurements"],
        )
        == value["policy"]
    )
    altered = value["evidence"]["appCompose"].replace("example/anvil@sha256:" + "33" * 32, "example/anvil:latest")
    with pytest.raises(ValueError, match="Pin all"):
        render(
            altered,
            app_id=value["policy"]["appId"],
            origin=value["policy"]["origin"],
            measurements=value["policy"]["platformMeasurements"],
        )
    with pytest.raises(ValueError, match="independently provisioned"):
        render(
            value["evidence"]["appCompose"],
            app_id=value["policy"]["appId"],
            origin=value["policy"]["origin"],
            measurements=value["policy"]["platformMeasurements"],
            expected_compose_hash="99" * 32,
        )


def test_node_and_python_bind_identical_service_claims_and_policy():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for service protocol interoperability")
    value = fixture()
    output = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            "import {serviceReportData,servicePolicyHash} from './dfl/node_server/dist/service_attestation.js';"
            "let s='';for await(const c of process.stdin)s+=c;const x=JSON.parse(s);"
            "console.log(JSON.stringify({report:serviceReportData(x.claims).toString('hex'),policy:servicePolicyHash(x.policy)}));",
        ],
        input=json.dumps(value),
        text=True,
        capture_output=True,
        check=True,
    )
    result = json.loads(output.stdout)
    assert result["report"] == report_data(value["claims"]).hex()
    assert result["policy"] == hashlib.sha256(canonical(value["policy"])).hexdigest()

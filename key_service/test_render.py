import json
import shutil
import subprocess

import pytest
import yaml

from key_service.render_worker import render


def trust(origin="https://keys.example"):
    return {
        "version": 1,
        "appId": "0x" + "11" * 20,
        "composeHash": "22" * 32,
        "serviceImage": "example/service@sha256:" + "33" * 32,
        "origin": origin,
        "platformMeasurements": [{k: "44" * 48 for k in ("mrtd", "rtmr0", "rtmr1", "rtmr2")}],
    }


def source():
    return json.dumps(
        {
            "docker_compose_file": yaml.safe_dump(
                {
                    "services": {
                        "dfl-worker": {
                            "image": "example/worker@sha256:" + "ab" * 32,
                            "environment": {"EXPECTED_CHAIN_ID": "31337"},
                            "tmpfs": ["/run/vita-fl:mode=0700"],
                        }
                    }
                }
            )
        }
    )


def test_broker_profile_preserves_runtime_protection_and_separates_ciphertext_storage():
    result = json.loads(
        render(
            source(),
            origin="https://keys.example",
            policy="0x" + "12" * 20,
            service_id="34" * 32,
            service_trust_policy=trust(),
        )
    )
    compose = yaml.safe_load(result["docker_compose_file"])
    worker = compose["services"]["dfl-worker"]
    assert worker["environment"]["PARTICIPANT_KEY_PROVIDER"] == "broker"
    assert worker["environment"]["SELLO_SERVICE_KEY_PROVIDER"] == "broker"
    assert worker["environment"]["KEY_SERVICE_PROTOCOL"] == "onchain-v3"
    assert json.loads(worker["environment"]["KEY_SERVICE_TRUST_POLICY"]) == trust()
    assert worker["tmpfs"] == ["/run/vita-fl:mode=0700"]
    assert worker["volumes"] == ["participant-key-state:/var/lib/vita-fl"]
    assert compose["volumes"] == {"participant-key-state": None}


@pytest.mark.parametrize("origin", ["http://keys.example", "https://keys.example/redirect", "https://u:p@keys.example"])
def test_untrusted_broker_origin_is_rejected(origin):
    with pytest.raises(ValueError):
        render(
            source(), origin=origin, policy="0x" + "12" * 20, service_id="34" * 32, service_trust_policy=trust(origin)
        )


def test_renderer_refuses_public_chain():
    with pytest.raises(ValueError, match="Anvil"):
        render(
            source().replace("31337", "1"),
            origin="https://keys.example",
            policy="0x" + "12" * 20,
            service_id="34" * 32,
            service_trust_policy=trust(),
        )


def test_real_admission_parser_accepts_profile_and_binds_broker_origin():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for production-parser interoperability")
    app = json.loads(source())
    app.update(manifest_version=2, runner="docker-compose")
    identities = []
    for origin in ("https://keys.example", "https://substitute.example"):
        value = render(
            json.dumps(app),
            origin=origin,
            policy="0x" + "12" * 20,
            service_id="34" * 32,
            service_trust_policy=trust(origin),
        )
        result = subprocess.run(
            [
                node,
                "--input-type=module",
                "-e",
                "import {deriveWorkerPolicyIdentity} from './dfl/node_server/dist/worker_policy.js';"
                "let s='';for await(const c of process.stdin)s+=c;"
                "console.log(JSON.stringify(deriveWorkerPolicyIdentity(s)));",
            ],
            input=value,
            text=True,
            capture_output=True,
            check=True,
        )
        identities.append(json.loads(result.stdout))
    assert identities[0]["imageDigest"] == identities[1]["imageDigest"]
    assert identities[0]["workerPolicyHash"] != identities[1]["workerPolicyHash"]


def test_worker_policy_binds_the_independent_service_references():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node required for production-parser interoperability")
    app = json.loads(source())
    app.update(manifest_version=2, runner="docker-compose")
    policies = []
    for digest in ("34", "56"):
        references = trust()
        references["composeHash"] = digest * 32
        value = render(
            json.dumps(app),
            origin="https://keys.example",
            policy="0x" + "12" * 20,
            service_id="34" * 32,
            service_trust_policy=references,
        )
        output = subprocess.run(
            [
                node,
                "--input-type=module",
                "-e",
                "import {deriveWorkerPolicyIdentity} from './dfl/node_server/dist/worker_policy.js';"
                "let s='';for await(const c of process.stdin)s+=c;"
                "console.log(deriveWorkerPolicyIdentity(s).workerPolicyHash);",
            ],
            input=value,
            text=True,
            capture_output=True,
            check=True,
        )
        policies.append(output.stdout.strip())
    assert policies[0] != policies[1]

import hashlib
import json

import pytest
import yaml

from key_service import runtime_bootstrap
from key_service.render_runtime import render


def base():
    return {
        "manifest_version": 2,
        "docker_compose_file": yaml.safe_dump(
            {
                "services": {
                    "anvil": {"command": ["--gas-limit", "1000000000"]},
                    "ipfs": {"tmpfs": ["/data/ipfs"]},
                    "rpc-proxy": {"image": "old-contracts"},
                    "control-api": {
                        "environment": {
                            "DYNAMIC_WORKER_EXPECTED_DEVICE_REGISTRY_ADDRESS": "0x" + "11" * 20,
                            "PHALA_CLOUD_API_KEY": "${PHALA_CLOUD_API_KEY}",
                            "DYNAMIC_WORKER_IMAGE": "old-worker",
                        }
                    },
                    "smart-contracts": {
                        "command": ["bash", "-c", "exec bash /dfl/smart_contracts/starter_docker.sh"],
                        "environment": {"ETH_WALLET_PRIVATE_KEY": "${ETH_WALLET_PRIVATE_KEY}"},
                    },
                }
            }
        ),
    }


def test_runtime_overlay_keeps_admin_credentials_out_of_serving_process():
    image = "ghcr.io/uzhw8rgl/master-thesis-dfl-worker@sha256:" + "ab" * 32
    app = base()
    rendered = render(app, service_image=image, worker_image=image, contracts_image=image, measurements=[])
    services = yaml.safe_load(rendered["docker_compose_file"])["services"]
    assert services["key-service"]["user"] == "10001:10001"
    assert set(services["key-service"]["environment"]) == {"KEY_SERVICE_RPC_URL", "KEY_SERVICE_STATE_DIRECTORY"}
    assert services["key-service-init"]["environment"]["ETH_WALLET_PRIVATE_KEY"] == "${ETH_WALLET_PRIVATE_KEY}"
    assert services["control-api"]["environment"]["PHALA_CLOUD_API_KEY"] == "${PHALA_CLOUD_API_KEY}"
    assert "--state" in services["anvil"]["command"]
    assert "tmpfs" not in services["ipfs"]
    assert "key-service" not in yaml.safe_load(app["docker_compose_file"])["services"]
    with pytest.raises(ValueError, match="already applied"):
        render(rendered, service_image=image, worker_image=image, contracts_image=image, measurements=[])


def test_persisted_bootstrap_checks_chain_and_never_redeploys(monkeypatch, tmp_path):
    state = tmp_path / "contracts.json"
    registry = "0x" + "11" * 20
    configuration = hashlib.sha256(
        json.dumps(
            {
                name: ""
                for name in (
                    "EXPECTED_WORKER_IMAGE",
                    "TDX_REFERENCE_QUOTE_PATH",
                    "TRAINING_WORKER_POLICY_APP_COMPOSE_B64",
                    "INFERENCE_WORKER_POLICY_APP_COMPOSE_B64",
                )
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    state.write_text(
        json.dumps(
            {
                "registry": registry,
                "configuration": configuration,
                "code": {registry: hashlib.sha256(b"code").hexdigest()},
            }
        )
    )
    monkeypatch.setenv("KEY_SERVICE_REGISTRY", registry)
    for name in (
        "EXPECTED_WORKER_IMAGE",
        "TDX_REFERENCE_QUOTE_PATH",
        "TRAINING_WORKER_POLICY_APP_COMPOSE_B64",
        "INFERENCE_WORKER_POLICY_APP_COMPOSE_B64",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(runtime_bootstrap, "Path", lambda _: state)
    monkeypatch.setattr(
        runtime_bootstrap.subprocess, "run", lambda *a, **k: pytest.fail("must retain deployed contracts")
    )
    monkeypatch.setattr(
        runtime_bootstrap, "rpc", lambda method, params: "0x7a69" if method == "eth_chainId" else "0x636f6465"
    )
    runtime_bootstrap.main()
    monkeypatch.setattr(runtime_bootstrap, "rpc", lambda method, params: "0x7a69" if method == "eth_chainId" else "0x")
    with pytest.raises(RuntimeError, match="missing or changed"):
        runtime_bootstrap.main()

import pytest
import yaml

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
    assert set(services["key-service"]["environment"]) == {"KEY_SERVICE_RPC_URL", "KEY_SERVICE_REGISTRY"}
    assert "volumes" not in services["key-service"]
    assert services["key-service"]["read_only"]
    assert "key-service-init" not in services
    assert services["smart-contracts"]["environment"]["KEY_SERVICE_ENABLED"] == "1"
    original = yaml.safe_load(app["docker_compose_file"])["services"]
    for name in ("anvil", "ipfs"):
        assert services[name] == original[name]
    assert services["smart-contracts"]["command"] == original["smart-contracts"]["command"]
    assert "volumes" not in yaml.safe_load(rendered["docker_compose_file"])
    assert "key-service" not in yaml.safe_load(app["docker_compose_file"])["services"]
    with pytest.raises(ValueError, match="already applied"):
        render(rendered, service_image=image, worker_image=image, contracts_image=image, measurements=[])

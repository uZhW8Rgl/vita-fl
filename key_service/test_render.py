import json

import pytest
import yaml

from key_service.render_worker import render


def source():
    return json.dumps(
        {
            "docker_compose_file": yaml.safe_dump(
                {
                    "services": {
                        "worker": {
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
    result = json.loads(render(source(), origin="https://keys.example", policy="0x" + "12" * 20, service_id="34" * 32))
    compose = yaml.safe_load(result["docker_compose_file"])
    worker = compose["services"]["worker"]
    assert worker["environment"]["PARTICIPANT_KEY_PROVIDER"] == "broker"
    assert worker["environment"]["SELLO_SERVICE_KEY_PROVIDER"] == "broker"
    assert worker["tmpfs"] == ["/run/vita-fl:mode=0700"]
    assert worker["volumes"] == ["participant-keys:/var/lib/vita-fl"]
    assert compose["volumes"] == {"participant-keys": {}}


@pytest.mark.parametrize("origin", ["http://keys.example", "https://keys.example/redirect", "https://u:p@keys.example"])
def test_untrusted_broker_origin_is_rejected(origin):
    with pytest.raises(ValueError):
        render(source(), origin=origin, policy="0x" + "12" * 20, service_id="34" * 32)


def test_renderer_refuses_public_chain():
    with pytest.raises(ValueError, match="Anvil"):
        render(
            source().replace("31337", "1"), origin="https://keys.example", policy="0x" + "12" * 20, service_id="34" * 32
        )

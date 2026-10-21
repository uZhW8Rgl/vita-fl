"""Python must consume the same restored Sello seed as the Node supervisor."""

import pytest

from agent_receipts.dstack_key import tee_sello_signing_seed


def test_python_sello_consumes_broker_runtime_key_without_rederivation():
    seed = bytes(range(32))
    requested = []

    def reader(name):
        requested.append(name)
        return seed

    assert (
        tee_sello_signing_seed(
            {"DOCKER": "phala", "PARTICIPANT_KEY_PROVIDER": "broker", "SELLO_SERVICE_KEY_PROVIDER": "broker"},
            local_root=b"unrelated local root is ignored",
            runtime_key_reader=reader,
        )
        == seed
    )
    assert requested == ["sello-signing.seed"]


@pytest.mark.parametrize("sello", ["ephemeral", "kms", "env"])
def test_broker_profile_cannot_substitute_another_sello_provider(sello):
    with pytest.raises(RuntimeError, match="must match"):
        tee_sello_signing_seed(
            {"DOCKER": "phala", "PARTICIPANT_KEY_PROVIDER": "broker", "SELLO_SERVICE_KEY_PROVIDER": sello},
            runtime_key_reader=lambda _: pytest.fail("mismatched provider must not read a key"),
        )

import hashlib
import json
import shlex
import tomllib
from pathlib import Path

import pytest
from render_kms import KMS_IMAGE, render


def test_manifest_keeps_real_kms_checks_and_protects_root_storage():
    manifest = render(
        auth_url="https://auth.example",
        image_download_url="https://images.example/{OS_IMAGE_HASH}.tar.gz",
        admin_token_hash="ab" * 32,
    )
    assert manifest["kms_enabled"] is False
    assert manifest["local_key_provider_enabled"] is True
    assert manifest["no_instance_id"] is True
    assert manifest["allowed_envs"] == []
    assert manifest["public_logs"] is False
    compose = json.loads(manifest["docker_compose_file"])
    assert compose["services"]["kms"]["image"] == KMS_IMAGE
    assert "/var/run/dstack.sock:/var/run/dstack.sock" in compose["services"]["kms"]["volumes"]
    command = compose["services"]["kms"]["command"]
    assert command[:2] == ["sh", "-ec"]
    tokens = shlex.split(command[2])
    config = tomllib.loads(tokens[tokens.index("printf") + 2])
    assert config["core"]["image"]["verify"] is True
    assert config["core"]["enforce_self_authorization"] is True
    assert config["core"]["auth_api"]["type"] == "webhook"
    assert config["core"]["onboard"]["auto_bootstrap_domain"] == ""
    assert config["rpc"]["tls"]["mutual"]["mandatory"] is False  # public metadata is needed at boot


@pytest.mark.parametrize(
    "field,value",
    [
        ("auth_url", "http://auth.example"),
        ("auth_url", "https://user:pass@auth.example"),
        ("image_download_url", "http://images.example"),
        ("auth_url", "https://auth.example/$ENV_SUBSTITUTION"),
        ("admin_token_hash", ""),
        ("admin_token_hash", "0" * 64),
    ],
)
def test_invalid_trust_configuration_is_rejected(field, value):
    args = {
        "auth_url": "https://auth.example",
        "image_download_url": "https://images.example",
        "admin_token_hash": "ab" * 32,
    }
    args[field] = value
    with pytest.raises(ValueError):
        render(**args)


def test_vendored_upstream_sources_match_recorded_hashes():
    root = Path(__file__).resolve().parents[1] / "smart_contracts" / "vendor"
    provenance = json.loads((root / "dstack-v0.5.9" / "provenance.json").read_text())
    assert provenance["commit"] == "282eeb27d22d8f091ad0fa5a90e638f85cf68751"
    assert len(provenance["files"]) >= 20
    for name, expected in provenance["files"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
    rewrite = provenance["import_namespace_rewrite"]
    for name, expected in provenance["upstream_files_sha256"].items():
        restored = (root / name).read_bytes().replace(rewrite["to"].encode(), rewrite["from"].encode())
        assert hashlib.sha256(restored).hexdigest() == expected, name

"""Actual random generation and cross-language sharing across two workloads."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from nacl.exceptions import BadSignatureError
from nacl.signing import SigningKey

from agent_receipts.dstack_key import tee_sello_signing_seed
from agent_receipts.runtime_keys import AIR_RUNTIME_KEY, SELLO_RUNTIME_KEY, read_runtime_signing_seed
from tee_inference.service.attestation import derive_air_signing_key

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")


def _initialize(directory):
    module = (ROOT / "dfl/node_server/dist/runtime_keys.js").as_uri()
    script = (
        'import fs from "node:fs/promises"; const read=fs.readFile; '
        'fs.readFile=(name,...args)=>name==="/proc/swaps"'
        '?Promise.resolve("Filename Type Size Used Priority\\n"):read(name,...args); '
        f"import {{initializeRuntimeSigningKeys}} from {json.dumps(module)}; "
        "await initializeRuntimeSigningKeys(process.argv[1]);"
    )
    subprocess.run(
        [NODE, "--input-type=module", "-e", script, str(directory)], check=True, capture_output=True, text=True
    )


@pytest.fixture
def simulated_no_swap(monkeypatch):
    # The development host has disk swap. Model the deployment's no-swap
    # precondition without changing the host; RNG, tmpfs and files remain real.
    original = Path.read_text

    def read_text(path, *args, **kwargs):
        if str(path) == "/proc/swaps":
            return "Filename Type Size Used Priority\n"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


@pytest.mark.skipif(NODE is None, reason="Node toolchain is required for cross-language integration")
def test_new_workloads_have_new_keys_and_registration_matches_python_receiver(simulated_no_swap):
    with tempfile.TemporaryDirectory(prefix="vita-key-test-", dir="/dev/shm") as temporary:
        directory = Path(temporary)
        _initialize(directory)
        sello = read_runtime_signing_seed(SELLO_RUNTIME_KEY, directory)
        air = read_runtime_signing_seed(AIR_RUNTIME_KEY, directory)
        assert sello != air
        assert (directory / SELLO_RUNTIME_KEY).stat().st_mode & 0o777 == 0o600
        # A second initializer cannot overwrite an already running workload.
        with pytest.raises(subprocess.CalledProcessError):
            _initialize(directory)
        assert read_runtime_signing_seed(SELLO_RUNTIME_KEY, directory) == sello
        public_module = (ROOT / "dfl/node_server/dist/sello_key.js").as_uri()
        script = (
            "import fs from 'node:fs/promises'; "
            f"import {{ed25519PublicKeyFromSeed}} from {json.dumps(public_module)}; "
            "const seed=await fs.readFile(process.argv[1]); "
            "console.log(ed25519PublicKeyFromSeed(seed).toString('hex'));"
        )
        observed = subprocess.run(
            [NODE, "--input-type=module", "-e", script, str(directory / SELLO_RUNTIME_KEY)],
            check=True,
            capture_output=True,
            text=True,
        )

        def reader(name):
            return read_runtime_signing_seed(name, directory)

        seed = tee_sello_signing_seed({"DOCKER": "phala"}, runtime_key_reader=reader)
        assert observed.stdout.strip() == bytes(SigningKey(seed).verify_key).hex()
        with patch("tee_inference.service.attestation.read_runtime_signing_seed", reader):
            client = type("NoSecretRPC", (), {"call": lambda *_: pytest.fail("GetKey must not be called")})()
            first_air = derive_air_signing_key(client)
            session_air = derive_air_signing_key(client)
            assert first_air.verify_key == session_air.verify_key
        signature = first_air.sign(b"inference-evidence").signature
        first_air.verify_key.verify(b"inference-evidence", signature)
        # Destroy all private seed files, then initialize a new workload.
        for name in (SELLO_RUNTIME_KEY, AIR_RUNTIME_KEY):
            (directory / name).unlink()
        _initialize(directory)
        new_air = SigningKey(read_runtime_signing_seed(AIR_RUNTIME_KEY, directory))
        assert read_runtime_signing_seed(SELLO_RUNTIME_KEY, directory) != sello
        assert bytes(new_air) != air
        with pytest.raises(BadSignatureError):
            new_air.verify_key.verify(b"inference-evidence", signature)
        # The old public key remains sufficient to verify historical evidence.
        first_air.verify_key.verify(b"inference-evidence", signature)


def test_runtime_keys_reject_disk_storage_and_external_key_fallback(tmp_path):
    tmp_path.chmod(0o700)
    (tmp_path / SELLO_RUNTIME_KEY).write_bytes(os.urandom(32))
    with pytest.raises(RuntimeError, match="tmpfs"):
        read_runtime_signing_seed(SELLO_RUNTIME_KEY, tmp_path)
    for provider in ("dstack", "env"):
        with pytest.raises(RuntimeError, match="ephemeral"):
            tee_sello_signing_seed({"DOCKER": "phala", "SELLO_SERVICE_KEY_PROVIDER": provider})

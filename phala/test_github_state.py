"""Offline GitHub CAS, encrypted state recovery and real Terraform checks."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from phala.github_state import LOCK_FILE, STATE_FILE, APIError, GitHubState, StateError, crypt, private_write


def state(serial=0, lineage="test-lineage", value="original"):
    return {
        "version": 4,
        "terraform_version": "1.9.8",
        "serial": serial,
        "lineage": lineage,
        "outputs": {"value": {"value": value, "type": "string"}},
        "resources": [],
    }


def fake_crypt(data, passphrase, *, decrypt=False):
    prefix = (passphrase + ":").encode()
    if decrypt:
        if not data.startswith(prefix):
            raise StateError("Could not decrypt state")
        return data[len(prefix) :]
    return prefix + data


class FakeGitHub:
    """Contents writes require create-only or matching SHA under one lock."""

    def __init__(self):
        self.files = {}
        self.branch = True
        self.guard = threading.Lock()
        self.calls = []
        self.fail_state_upload = False
        self.large_content = False
        self.access_denied = False

    def request(self, method, path, payload=None):
        with self.guard:
            self.calls.append((method, path, payload))
            if self.access_denied:
                raise APIError(403)
            if path == "/repos/owner/repository":
                return {"default_branch": "main"}
            if "/git/ref/heads/" in path:
                if not self.branch and path.endswith("phala-state"):
                    raise APIError(404)
                return {"object": {"sha": "a" * 40}}
            if path.endswith("/git/refs"):
                self.branch = True
                return {}
            if "/git/blobs/" in path:
                sha = path.rsplit("/", 1)[-1]
                value = next(value for value in self.files.values() if self.sha(value) == sha)
                return {"encoding": "base64", "content": base64.b64encode(value).decode()}
            name = path.split("/contents/", 1)[1].split("?", 1)[0]
            if method == "GET":
                if not self.branch or name not in self.files:
                    raise APIError(404)
                content = self.files[name]
                return {
                    "type": "file",
                    "sha": self.sha(content),
                    "encoding": "none" if self.large_content else "base64",
                    "content": "" if self.large_content else base64.b64encode(content).decode(),
                }
            if not self.branch or payload["branch"] != "phala-state":
                raise APIError(404)
            if name in self.files:
                if payload.get("sha") != self.sha(self.files[name]):
                    raise APIError(409)
            elif payload.get("sha"):
                raise APIError(409)
            if method == "PUT":
                if name == STATE_FILE and self.fail_state_upload:
                    raise APIError(503)
                self.files[name] = base64.b64decode(payload["content"])
            elif method == "DELETE":
                self.files.pop(name)
            else:
                raise AssertionError(method)
            return {}

    @staticmethod
    def sha(content):
        return hashlib.sha1(content).hexdigest()


class GitHubStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.api = FakeGitHub()
        encryption = patch("phala.github_state.crypt", side_effect=fake_crypt)
        encryption.start()
        self.addCleanup(encryption.stop)
        self.client = self.make_client("first")

    def make_client(self, directory):
        environment = {
            "PHALA_STATE_REPOSITORY": "owner/repository",
            "PHALA_STATE_PASSPHRASE": "x" * 40,
            "GH_TOKEN": "token",
        }
        client = GitHubState.from_environment(self.directory / directory, environment)
        client.request = self.api.request
        return client

    def write(self, client, value):
        private_write(client.path, json.dumps(value).encode())

    def initialize(self):
        self.write(self.client, state())
        with self.client.session(initialize=True):
            pass

    def test_explicit_init_creates_branch_and_preserves_existing_lineage(self):
        self.api.branch = False
        self.initialize()
        self.assertTrue(self.api.branch)
        self.assertEqual(self.client.remote_state()[1], state())
        self.assertNotIn(LOCK_FILE, self.api.files)
        self.assertEqual(self.client.path.stat().st_mode & 0o777, 0o600)

    def test_fresh_init_creates_valid_empty_state(self):
        with self.client.session(initialize=True):
            current = self.client.remote_state()[1]
            self.assertEqual(current["serial"], 0)
            self.assertEqual(current["resources"], [])
            self.assertTrue(current["lineage"])

    def test_missing_branch_and_missing_snapshot_require_explicit_init(self):
        for branch in (False, True):
            with self.subTest(branch=branch):
                self.api.branch = branch
                with self.assertRaisesRegex(StateError, "init-github-state"):
                    with self.client.session():
                        self.fail("must not deploy")
                self.assertNotIn(LOCK_FILE, self.api.files)

    def test_restore_reads_files_larger_than_contents_inline_limit(self):
        self.initialize()
        other = self.make_client("other")
        self.api.large_content = True
        with other.session():
            self.assertEqual(json.loads(other.path.read_bytes()), state())
        self.assertTrue(any("/git/blobs/" in path for _, path, _ in self.api.calls))

    def test_noop_does_not_commit_new_ciphertext(self):
        self.initialize()
        self.api.calls.clear()
        with self.client.session():
            self.client.sync()
        writes = [path for method, path, _ in self.api.calls if method == "PUT" and path.endswith(STATE_FILE)]
        self.assertEqual(writes, [])

    def test_writer_version_change_at_same_serial_is_safe_and_does_not_commit(self):
        self.initialize()
        self.api.calls.clear()
        with self.client.session():
            self.write(self.client, state() | {"terraform_version": "1.11.4"})
            self.client.sync()
        writes = [path for method, path, _ in self.api.calls if method == "PUT" and path.endswith(STATE_FILE)]
        self.assertEqual(writes, [])
        with self.client.session():
            self.assertEqual(json.loads(self.client.path.read_bytes())["serial"], 0)

    def test_exception_persists_partial_terraform_state_before_unlock(self):
        self.initialize()
        with self.assertRaisesRegex(RuntimeError, "apply failed"):
            with self.client.session():
                self.write(self.client, state(1, value="partially-created"))
                raise RuntimeError("apply failed")
        self.assertEqual(self.client.remote_state()[1]["serial"], 1)
        self.assertNotIn(LOCK_FILE, self.api.files)

    def test_cancel_keeps_lock_even_if_snapshot_uploaded(self):
        self.initialize()
        with self.assertRaisesRegex(StateError, "interrupted.*retained"):
            with self.client.session():
                self.write(self.client, state(1))
                raise KeyboardInterrupt
        self.assertEqual(self.client.remote_state()[1]["serial"], 1)
        self.assertIn(LOCK_FILE, self.api.files)

    def test_upload_failure_keeps_lock_and_private_encrypted_recovery(self):
        self.initialize()
        self.api.fail_state_upload = True
        with self.assertRaisesRegex(StateError, "State upload failed.*retained"):
            with self.client.session():
                self.write(self.client, state(1))
        self.assertIn(LOCK_FILE, self.api.files)
        self.assertTrue(self.client.recovery_path.is_file())
        self.assertEqual(self.client.recovery_path.stat().st_mode & 0o777, 0o600)
        recovered = json.loads(fake_crypt(self.client.recovery_path.read_bytes(), self.client.passphrase, decrypt=True))
        self.assertEqual(recovered["state"]["serial"], 1)
        self.assertEqual(self.client.remote_state()[1]["serial"], 0)

    def test_concurrent_deployment_cannot_acquire_or_overwrite(self):
        self.initialize()
        other = self.make_client("other")
        with self.client.session():
            with self.assertRaisesRegex(StateError, "locked"):
                with other.session():
                    self.fail("second deployment entered")
            self.assertEqual(self.client.current_lock()[1]["id"], self.client.lock_id)
        self.assertFalse(other.path.exists())

    def test_actual_simultaneous_lock_creation_has_one_winner(self):
        clients = [self.client, self.make_client("other")]
        barrier = threading.Barrier(2)
        outcomes = []

        def acquire(client):
            barrier.wait()
            try:
                client.acquire()
                outcomes.append("acquired")
            except StateError:
                outcomes.append("locked")

        threads = [threading.Thread(target=acquire, args=(client,)) for client in clients]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        self.assertCountEqual(outcomes, ["acquired", "locked"])

    def test_nested_session_keeps_parent_lock_and_current_local_changes(self):
        self.initialize()
        with self.client.session():
            self.write(self.client, state(1))
            nested = GitHubState.from_environment(self.client.directory, dict(self.client.environment))
            nested.request = self.api.request
            with nested.session():
                self.assertEqual(json.loads(nested.path.read_bytes())["serial"], 1)
                nested.sync()
            self.client.require_lock()
        self.assertNotIn(LOCK_FILE, self.api.files)

    def test_forged_nested_lock_id_is_rejected(self):
        self.initialize()
        self.client.environment["PHALA_STATE_LOCK_ID"] = "not-an-owner"
        self.client.lock_id = "not-an-owner"
        with self.assertRaisesRegex(StateError, "owned by another"):
            with self.client.session():
                self.fail("must not join")

    def test_newer_local_foreign_lineage_and_same_serial_conflict_are_not_overwritten(self):
        self.initialize()
        for value in (state(1), state(lineage="foreign"), state(value="conflict")):
            with self.subTest(value=value):
                self.write(self.client, value)
                with self.assertRaises(StateError):
                    with self.client.session():
                        self.fail("must not deploy")
                self.assertEqual(json.loads(self.client.path.read_bytes()), value)
                self.assertNotIn(LOCK_FILE, self.api.files)

    def test_remote_missing_during_sync_does_not_create_new_state(self):
        self.initialize()
        with self.assertRaisesRegex(StateError, "disappeared"):
            with self.client.session():
                self.api.files.pop(STATE_FILE)
                self.client.sync()
        self.assertNotIn(STATE_FILE, self.api.files)
        self.assertIn(LOCK_FILE, self.api.files)

    def test_access_error_never_means_empty_state(self):
        self.api.access_denied = True
        with self.assertRaisesRegex(StateError, "403"):
            with self.client.session(initialize=True):
                self.fail("must not initialize")
        self.assertFalse(self.client.path.exists())

    def test_missing_file_with_deleted_branch_is_not_treated_as_empty(self):
        self.api.branch = False
        with self.assertRaisesRegex(StateError, "branch is missing"):
            self.client.file(STATE_FILE)

    def test_manual_unlock_requires_exact_id_and_observed_sha(self):
        self.initialize()
        self.client.acquire()
        correct = self.client.lock_id
        with self.assertRaisesRegex(StateError, "owned by another"):
            self.client.unlock("wrong")
        self.client.unlock(correct)
        self.assertNotIn(LOCK_FILE, self.api.files)
        deletion = self.api.calls[-1]
        self.assertEqual(deletion[0], "DELETE")
        self.assertIn("sha", deletion[2])

    def test_envelope_is_bound_to_repository_and_branch(self):
        self.initialize()
        changed = self.make_client("other")
        changed.repository = "another/repository"
        with self.assertRaisesRegex(StateError, "does not belong"):
            changed.remote_state()

    def test_encrypted_recovery_uploads_newer_state_then_unlocks(self):
        self.initialize()
        self.client.acquire()
        identifier = self.client.lock_id
        recovery = self.directory / "downloaded-recovery.gpg"
        private_write(recovery, self.client.encrypted(state(2, value="recovered")))
        self.client.recover(recovery, identifier)
        self.assertEqual(self.client.remote_state()[1], state(2, value="recovered"))
        self.assertNotIn(LOCK_FILE, self.api.files)
        self.assertTrue(self.client.path.with_name("terraform.tfstate.before-recovery.backup").exists())

    def test_raw_recovery_rejects_wrong_lock_id_lineage_and_stale_serial(self):
        self.initialize()
        with self.client.session():
            self.write(self.client, state(1))
        self.client.acquire()
        identifier = self.client.lock_id
        recovery = self.directory / "recover.tfstate"
        for candidate, token in (
            (state(2), "wrong"),
            (state(2, lineage="foreign"), identifier),
            (state(0), identifier),
        ):
            with self.subTest(candidate=candidate, token=token):
                private_write(recovery, json.dumps(candidate).encode())
                with self.assertRaises(StateError):
                    self.client.recover(recovery, token)
                self.assertIn(LOCK_FILE, self.api.files)
                self.assertEqual(self.client.remote_state()[1]["serial"], 1)

    def test_failed_recovery_upload_retains_lock_and_snapshot(self):
        self.initialize()
        self.client.acquire()
        recovery = self.directory / "recover.tfstate"
        private_write(recovery, json.dumps(state(1)).encode())
        self.api.fail_state_upload = True
        with self.assertRaisesRegex(StateError, "upload failed"):
            self.client.recover(recovery, self.client.lock_id)
        self.assertIn(LOCK_FILE, self.api.files)
        self.assertTrue(self.client.recovery_path.exists())

    def test_failed_recovery_unlock_retains_lock_after_successful_upload(self):
        self.initialize()
        self.client.acquire()
        recovery = self.directory / "recover.tfstate"
        private_write(recovery, json.dumps(state(1)).encode())
        with patch.object(self.client, "unlock", side_effect=APIError(503)), self.assertRaises(APIError):
            self.client.recover(recovery, self.client.lock_id)
        self.assertEqual(self.client.remote_state()[1]["serial"], 1)
        self.assertIn(LOCK_FILE, self.api.files)

    def test_weak_passphrase_and_default_branch_names_fail_before_network(self):
        for update in (
            {"PHALA_STATE_PASSPHRASE": "short"},
            {"PHALA_STATE_BRANCH": "main"},
            {"PHALA_STATE_REPOSITORY": "https://evil.example"},
        ):
            with self.subTest(update=update):
                with self.assertRaises(StateError):
                    GitHubState.from_environment(self.directory, self.client.environment | update)


class RealEncryptionTests(unittest.TestCase):
    def test_real_gpg_roundtrip_and_wrong_key_fail_without_secret_output(self):
        data = json.dumps({"sensitive": "private-terraform-secret"}).encode()
        key = "a-long-random-state-passphrase-for-testing"
        encrypted = crypt(data, key)
        self.assertNotIn(b"private-terraform-secret", encrypted)
        self.assertEqual(crypt(encrypted, key, decrypt=True), data)
        with self.assertRaisesRegex(StateError, "Could not decrypt") as result:
            crypt(encrypted, "the-wrong-passphrase", decrypt=True)
        self.assertNotIn("private-terraform-secret", str(result.exception))
        self.assertNotIn(key, str(result.exception))

    def test_passphrase_is_only_supplied_on_stdin(self):
        with patch(
            "phala.github_state.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=b"encrypted", stderr=b""),
        ) as run:
            crypt(b"state", "secret-passphrase")
        self.assertNotIn("secret-passphrase", " ".join(run.call_args.args[0]))
        self.assertEqual(run.call_args.kwargs["input"], b"secret-passphrase\n")


class TerraformIntegrationTests(unittest.TestCase):
    def test_real_terraform_apply_restore_and_destroy_offline(self):
        binary = Path(__file__).parent / "bin" / "terraform"
        if not binary.is_file():
            self.skipTest("Repository Terraform binary unavailable")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            api = FakeGitHub()
            environment = dict(
                os.environ,
                PHALA_STATE_REPOSITORY="owner/repository",
                PHALA_STATE_PASSPHRASE="x" * 40,
                GH_TOKEN="test-token",
            )
            first = GitHubState.from_environment(root / "first", dict(environment))
            second = GitHubState.from_environment(root / "second", dict(environment))
            for client in (first, second):
                client.request = api.request
                client.directory.mkdir()
                (client.directory / "main.tf").write_text(
                    'resource "terraform_data" "example" { input = "offline" }\n'
                    'output "value" { value = terraform_data.example.output }\n'
                )

            def terraform(client, *arguments):
                result = subprocess.run(
                    [str(binary.resolve()), *arguments, "-no-color"],
                    cwd=client.directory,
                    env=client.environment,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            with first.session(initialize=True):
                terraform(first, "init", "-input=false")
                terraform(first, "apply", "-auto-approve", "-input=false")
                first.sync()
                self.assertEqual(first.remote_state()[1]["outputs"]["value"]["value"], "offline")
            with second.session():
                terraform(second, "init", "-input=false")
                # A no-op apply with another writer version keeps the serial;
                # restoring and saving must not misdiagnose this as divergence.
                before = json.loads(second.path.read_bytes())
                before["terraform_version"] = "1.11.4"
                private_write(second.path, json.dumps(before).encode())
                terraform(second, "apply", "-auto-approve", "-input=false")
                self.assertEqual(json.loads(second.path.read_bytes())["serial"], before["serial"])
                second.sync()
                terraform(second, "destroy", "-auto-approve", "-input=false")
                second.sync()
                self.assertEqual(second.remote_state()[1]["resources"], [])
            self.assertNotIn(LOCK_FILE, api.files)


if __name__ == "__main__":
    unittest.main()

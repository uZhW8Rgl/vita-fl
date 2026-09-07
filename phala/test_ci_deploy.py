"""Checks for build ordering, preserved state, and private CI configuration."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from phala import ci_config, ci_deploy
from phala.github_state import GitHubState, StateError


class CommandCancellationTests(unittest.TestCase):
    def test_sigterm_reaches_main_cancellation_handler(self):
        script = """
import os
import signal
import sys
from unittest.mock import patch
from phala import ci_deploy

sys.argv = [
    "ci_deploy.py", "--image-variable", "worker_image",
    "--image-reference", "ghcr.io/example/worker@sha256:" + "a" * 64,
    "--revision", "b" * 40,
]

def terminate(*_args):
    os.kill(os.getpid(), signal.SIGTERM)

previous = signal.getsignal(signal.SIGTERM)
with patch.object(ci_deploy, "deploy", side_effect=terminate):
    result = ci_deploy.main()
assert signal.getsignal(signal.SIGTERM) == previous
raise SystemExit(result)
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parent.parent,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 130, result.stderr)
        self.assertIn("Phala deployment interrupted", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_signalled_child_retains_remote_deployment_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = GitHubState(Path(temporary), {}, "example/repo", "token", "x" * 32, "phala-state")
            with (
                patch.object(state, "ensure_branch"),
                patch.object(state, "acquire"),
                patch.object(state, "restore"),
                patch.object(state, "sync") as sync,
                patch.object(state, "unlock") as unlock,
                self.assertRaisesRegex(StateError, "interrupted"),
            ):
                with state.session():
                    ci_deploy.checked(
                        [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
                        capture=True,
                    )
            sync.assert_called_once()
            unlock.assert_not_called()

    def test_wrapped_cancellation_exit_is_not_an_ordinary_failure(self):
        for code in (130, 137, 143):
            with self.subTest(code=code), self.assertRaises(KeyboardInterrupt):
                ci_deploy.checked([sys.executable, "-c", f"raise SystemExit({code})"], capture=True)

    def test_normal_failure_does_not_expose_captured_state(self):
        with self.assertRaises(ValueError) as caught:
            ci_deploy.checked([sys.executable, "-c", "print('private-state-value'); raise SystemExit(1)"], capture=True)
        self.assertNotIn("private-state-value", str(caught.exception))


class DeploymentRevisionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Deployment test")
        self.git("config", "user.email", "deployment@example.invalid")
        self.commit_count = 0
        self.first = self.commit()
        self.second = self.commit()

    def git(self, *args):
        return subprocess.check_output(["git", *args], cwd=self.root, text=True, stderr=subprocess.DEVNULL).strip()

    def commit(self):
        self.commit_count += 1
        self.git("commit", "--allow-empty", "-qm", f"test revision {self.commit_count}")
        return self.git("rev-parse", "HEAD")

    def test_newer_build_and_same_commit_retry_are_allowed(self):
        self.assertFalse(ci_deploy.should_skip_revision(None, self.first, self.root))
        self.assertFalse(ci_deploy.should_skip_revision(self.first, self.first, self.root))
        self.assertFalse(ci_deploy.should_skip_revision(self.first, self.second, self.root))

    def test_slow_older_build_is_skipped(self):
        self.assertTrue(ci_deploy.should_skip_revision(self.second, self.first, self.root))

    def test_missing_history_fails_instead_of_allowing_rollback(self):
        with self.assertRaisesRegex(ValueError, "full Git history"):
            ci_deploy.should_skip_revision("0" * 40, self.second, self.root)

    def test_force_push_divergence_requires_reconciliation(self):
        self.git("checkout", "--orphan", "unrelated")
        unrelated = self.commit()
        with self.assertRaisesRegex(ValueError, "diverged"):
            ci_deploy.should_skip_revision(self.second, unrelated, self.root)

    def test_image_revision_must_belong_to_current_deployment_branch(self):
        ci_deploy.validate_source_revision(self.first, self.root)
        self.git("checkout", "--orphan", "unrelated")
        self.commit()
        with self.assertRaisesRegex(ValueError, "deployment branch"):
            ci_deploy.validate_source_revision(self.second, self.root)


class DeploymentSequenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.args = argparse.Namespace(
            image_variable="worker_image",
            image_reference="ghcr.io/example/worker@sha256:" + "a" * 64,
            revision="b" * 40,
        )
        source_check = patch.object(ci_deploy, "validate_source_revision")
        source_check.start()
        self.addCleanup(source_check.stop)
        self.lock_held = False
        self.state_environment = {}

        @contextmanager
        def session(*, initialize):
            self.assertFalse(initialize)
            self.lock_held = True
            self.state_environment["PHALA_STATE_LOCK_ID"] = "deployment-lock"
            try:
                yield
            finally:
                self.state_environment.pop("PHALA_STATE_LOCK_ID")
                self.lock_held = False

        def make_state(*, directory, environment):
            self.assertEqual(directory, self.root)
            self.state_environment.update(environment)
            return argparse.Namespace(environment=self.state_environment, session=session)

        state_factory = patch.object(ci_deploy.GitHubState, "from_environment", side_effect=make_state)
        self.state_factory = state_factory.start()
        self.addCleanup(state_factory.stop)

    def test_failed_state_read_prevents_any_resolution_or_apply(self):
        with (
            patch.dict(os.environ, {"TERRAFORM_BIN": "/terraform"}),
            patch.object(ci_deploy, "checked", side_effect=["", ValueError("state failed")]) as run,
            self.assertRaisesRegex(ValueError, "state failed"),
        ):
            ci_deploy.deploy(self.args, self.root)
        self.assertEqual(len(run.call_args_list), 2)
        self.assertIn("--init-only", run.call_args_list[0].args[0])
        self.assertFalse(self.lock_held)

    def test_state_and_exact_new_digest_reach_resolver_before_apply(self):
        previous_images = {"agent_image": "ghcr.io/example/agent@sha256:" + "c" * 64}
        outputs = {"deployment_images": {"value": previous_images}}
        commands = []

        def run(command, **kwargs):
            self.assertTrue(self.lock_held)
            self.assertEqual(kwargs["env"]["PHALA_STATE_LOCK_ID"], "deployment-lock")
            commands.append(command)
            if command[-2:] == ["output", "-json"]:
                return json.dumps(outputs)
            if "--from-state" in command:
                state = Path(command[command.index("--from-state") + 1])
                self.assertEqual(json.loads(state.read_text()), outputs)
                self.assertEqual(state.stat().st_mode & 0o777, 0o600)
                self.assertIn(f"worker_image={self.args.image_reference}", command)
                self.assertIn(f"worker_image={self.args.revision}", command)
                image_file = Path(command[command.index("--output") + 1])
                image_file.write_text("{}")
            if kwargs.get("env", {}).get("PHALA_IMAGE_VARS_FILE"):
                self.assertTrue(Path(kwargs["env"]["PHALA_IMAGE_VARS_FILE"]).is_file())
            return ""

        with patch.dict(os.environ, {"TERRAFORM_BIN": "/terraform"}), patch.object(ci_deploy, "checked", run):
            self.assertEqual(ci_deploy.deploy(self.args, self.root), "deployed")
        self.assertEqual(len(commands), 4)
        self.assertIn("--init-only", commands[0])
        self.assertNotIn("--init-only", commands[-1])
        self.assertFalse(self.lock_held)
        self.assertTrue(all("--backend-config" not in command for command in commands))

    def test_tag_and_invalid_component_rejected_before_state_access(self):
        for field, value in (("image_reference", "ghcr.io/example/worker:phala"), ("image_variable", "private_key")):
            with self.subTest(field=field), patch.object(ci_deploy, "checked") as run:
                args = argparse.Namespace(**vars(self.args))
                setattr(args, field, value)
                with self.assertRaises(ValueError):
                    ci_deploy.deploy(args, self.root)
                run.assert_not_called()
                self.state_factory.assert_not_called()

    def test_invalid_state_json_is_not_treated_as_fresh_deployment(self):
        with (
            patch.dict(os.environ, {"TERRAFORM_BIN": "/terraform"}),
            patch.object(ci_deploy, "checked", side_effect=["", "not json"]),
            self.assertRaisesRegex(ValueError, "Terraform outputs are invalid"),
        ):
            ci_deploy.deploy(self.args, self.root)

    def test_failed_state_restore_prevents_terraform_and_image_resolution(self):
        self.state_factory.side_effect = ValueError("Encrypted state is unavailable")
        with (
            patch.dict(os.environ, {"TERRAFORM_BIN": "/terraform"}),
            patch.object(ci_deploy, "checked") as run,
            self.assertRaisesRegex(ValueError, "Encrypted state is unavailable"),
        ):
            ci_deploy.deploy(self.args, self.root)
        run.assert_not_called()

    def test_stale_build_releases_lock_without_resolving_or_applying(self):
        outputs = {"deployment_image_revisions": {"value": {"worker_image": "c" * 40}}}
        with (
            patch.dict(os.environ, {"TERRAFORM_BIN": "/terraform"}),
            patch.object(ci_deploy, "checked", side_effect=["", json.dumps(outputs)]) as run,
            patch.object(ci_deploy, "should_skip_revision", return_value=True),
        ):
            self.assertEqual(ci_deploy.deploy(self.args, self.root), "skipped")
        self.assertEqual(run.call_count, 2)
        self.assertFalse(self.lock_held)

    def test_failed_apply_exits_through_state_session(self):
        with (
            patch.dict(os.environ, {"TERRAFORM_BIN": "/terraform"}),
            patch.object(ci_deploy, "checked", side_effect=["", "{}", "", ValueError("apply failed")]),
            self.assertRaisesRegex(ValueError, "apply failed"),
        ):
            ci_deploy.deploy(self.args, self.root)
        self.assertFalse(self.lock_held)


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environ = {
            "RUNNER_TEMP": str(self.root / "temporary"),
            "PHALA_ENV_CONTENT": "PHALA_CLOUD_API_KEY=private-value\n",
            "PHALA_STATE_PASSPHRASE": "state-encryption-passphrase",
        }

    def test_env_written_privately_without_backend_or_extra_files(self):
        result = ci_config.prepare_config(self.environ, self.root)
        self.assertEqual(set(result), {"PHALA_ENV_FILE"})
        self.assertEqual(result["PHALA_ENV_FILE"].read_text(), self.environ["PHALA_ENV_CONTENT"])
        for path in result.values():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        self.assertFalse((self.root / ".env.shared").exists())
        self.assertFalse((result["PHALA_ENV_FILE"].parent / "backend.hcl").exists())

    def test_missing_state_key_or_ambiguous_env_configuration_is_rejected(self):
        variants = [
            {"PHALA_STATE_PASSPHRASE": ""},
            {"PHALA_ENV_CONTENT": ""},
            {"PHALA_ENV_PASSPHRASE": "passphrase"},
        ]
        for extra in variants:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                ci_config.prepare_config({**self.environ, **extra}, self.root)

    def test_legacy_shared_secret_does_not_create_a_second_configuration(self):
        self.environ["PHALA_SHARED_ENV_CONTENT"] = "PHALA_CLOUD_API_KEY=obsolete-shared-value\n"
        result = ci_config.prepare_config(self.environ, self.root)
        self.assertEqual(result["PHALA_ENV_FILE"].read_text(), self.environ["PHALA_ENV_CONTENT"])
        self.assertFalse((self.root / ".env.shared").exists())

    def test_existing_unreadable_shared_configuration_is_untouched(self):
        shared = self.root / ".env.shared"
        original = "PHALA_CLOUD_API_KEY=must-not-be-used\nexit 99\n"
        shared.write_text(original)
        shared.chmod(0)
        self.environ["PHALA_SHARED_ENV_CONTENT"] = "PHALA_CLOUD_API_KEY=must-not-overwrite\n"
        try:
            result = ci_config.prepare_config(self.environ, self.root)
            self.assertEqual(result["PHALA_ENV_FILE"].read_text(), self.environ["PHALA_ENV_CONTENT"])
            self.assertEqual(shared.stat().st_mode & 0o777, 0)
        finally:
            shared.chmod(0o600)
        self.assertEqual(shared.read_text(), original)

    def test_encrypted_large_env_passphrase_goes_through_stdin(self):
        ciphertext = self.root / "deployment.env.gpg"
        ciphertext.write_text("encrypted")
        self.environ.update(
            PHALA_ENV_CONTENT="",
            PHALA_ENV_PASSPHRASE="secret-passphrase",
            PHALA_ENV_ENCRYPTED_FILE=ciphertext.name,
        )

        def decrypt(command, **kwargs):
            self.assertNotIn("secret-passphrase", command)
            self.assertEqual(kwargs["input"], "secret-passphrase\n")
            Path(command[command.index("--output") + 1]).write_text("LARGE_CONFIG=" + "x" * 100_000)
            return subprocess.CompletedProcess(command, 0, "", "")

        with patch.object(ci_config.subprocess, "run", decrypt):
            result = ci_config.prepare_config(self.environ, self.root)
        self.assertGreater(result["PHALA_ENV_FILE"].stat().st_size, 48 * 1024)


if __name__ == "__main__":
    unittest.main()

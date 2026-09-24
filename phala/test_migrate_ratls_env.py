"""Keep existing deployment identities stable during one-time RA-TLS migration."""

from __future__ import annotations

import contextlib
import io
import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nacl.signing import SigningKey

from phala.migrate_ratls_env import main, migrate_env_file
from phala.ratls_config import decode_key, read_env_file, validate_ratls_config


class RatlsMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "deployment.env"
        self.policy = [{name: "ab" * 48 for name in ("mrtd", "rtmr0", "rtmr1", "rtmr2")}]
        self.values = {
            "ENABLE_PHALA_AGENT": "true",
            "PKI_WORKER_DNS_NAME": "worker-8443s.example.test",
            "RATLS_ALLOWED_PLATFORM_MEASUREMENTS": json.dumps(self.policy),
            "SELLO_TOKEN_ISSUER_SIGNING_SEED": "22" * 32,
            "SELLO_OWNER_HPKE_PRIVATE_KEY": "33" * 32,
            "SELLO_ZK_SERVICE_SIGNING_SEED": "44" * 32,
            "PHALA_CLOUD_API_KEY": "private-api-marker",
            "PHALA_STATE_PASSPHRASE": "private-state-marker",
        }
        self.write_profile()

    def write_profile(self):
        self.path.write_text(
            "# Keep this comment\n" + "".join(f"{key}={value}\n" for key, value in self.values.items())
        )

    def test_new_identity_is_private_persistent_and_preserves_existing_keys(self):
        before = self.path.read_text()
        changed = migrate_env_file(self.path)
        self.assertEqual(changed, ["AGENT_POP_REGISTRY", "AGENT_POP_SIGNING_SEED", "PKI_AGENT_SUBJECT"])
        after = self.path.read_bytes()
        self.assertTrue(after.decode().startswith(before))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        values = read_env_file(self.path)
        validate_ratls_config(values)
        seed = decode_key(values["AGENT_POP_SIGNING_SEED"], "seed")
        public_key = json.loads(values["AGENT_POP_REGISTRY"])["master-thesis-agent"]
        self.assertEqual(bytes(SigningKey(seed).verify_key), decode_key(public_key, "public key"))
        for key, value in self.values.items():
            self.assertEqual(values[key], value)
        self.assertEqual(migrate_env_file(self.path), [])
        self.assertEqual(self.path.read_bytes(), after)

    def test_existing_seed_derives_registry_without_replacing_other_agents(self):
        other_key = bytes(SigningKey(bytes.fromhex("66" * 32)).verify_key).hex()
        self.values.update(
            PKI_AGENT_SUBJECT="research-agent",
            AGENT_POP_SIGNING_SEED="55" * 32,
            AGENT_POP_REGISTRY=json.dumps({"another-agent": other_key}),
        )
        self.write_profile()
        self.assertEqual(migrate_env_file(self.path), ["AGENT_POP_REGISTRY"])
        result = read_env_file(self.path)
        self.assertEqual(result["AGENT_POP_SIGNING_SEED"], "55" * 32)
        registry = json.loads(result["AGENT_POP_REGISTRY"])
        self.assertEqual(registry["another-agent"], other_key)
        self.assertEqual(set(registry), {"research-agent", "another-agent"})

    def test_existing_registry_identity_is_not_replaced_when_seed_is_missing(self):
        self.values["AGENT_POP_REGISTRY"] = json.dumps({"master-thesis-agent": "77" * 32})
        self.write_profile()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "restore the existing seed"):
            migrate_env_file(self.path)
        self.assertEqual(self.path.read_bytes(), before)

    def test_invalid_configuration_does_not_partially_update_the_profile(self):
        for update in (
            {"RATLS_ALLOWED_PLATFORM_MEASUREMENTS": ""},
            {"RATLS_ALLOWED_PLATFORM_MEASUREMENTS": "[]"},
            {"AGENT_POP_REGISTRY": '{"same":"one","same":"two"}'},
            {"AGENT_POP_SIGNING_SEED": "22" * 32},
            {"AGENT_POP_SIGNING_SEED": "55" * 32, "AGENT_POP_REGISTRY": '{"master-thesis-agent":"' + "66" * 32 + '"}'},
        ):
            with self.subTest(update=list(update)):
                values = dict(self.values)
                self.values.update(update)
                self.write_profile()
                before = self.path.read_bytes()
                with self.assertRaises(ValueError):
                    migrate_env_file(self.path)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(list(self.root.glob(".ratls-env-*")))
                self.values = values

    def test_explicit_public_policy_and_hostname_are_written_as_single_lines(self):
        del self.values["RATLS_ALLOWED_PLATFORM_MEASUREMENTS"]
        del self.values["PKI_WORKER_DNS_NAME"]
        self.write_profile()
        policy_file = self.root / "approved-measurements.json"
        policy_file.write_text(json.dumps(self.policy, indent=2))
        migrate_env_file(
            self.path, worker_dns_name="approved-8443s.example.test", platform_measurements_file=policy_file
        )
        result = read_env_file(self.path)
        self.assertEqual(json.loads(result["RATLS_ALLOWED_PLATFORM_MEASUREMENTS"]), self.policy)
        self.assertEqual(result["PKI_WORKER_DNS_NAME"], "approved-8443s.example.test")
        validate_ratls_config(result)

    def test_fresh_workspace_uses_existing_os_policy_without_a_reserved_hostname(self):
        del self.values["PKI_WORKER_DNS_NAME"]
        del self.values["RATLS_ALLOWED_PLATFORM_MEASUREMENTS"]
        self.write_profile()
        policy = Path(__file__).with_name("ratls-platform-policy.dstack-dev-0.5.9.json")
        migrate_env_file(self.path, platform_measurements_file=policy)
        result = read_env_file(self.path)
        self.assertNotIn("PKI_WORKER_DNS_NAME", result)
        self.assertEqual(json.loads(result["RATLS_ALLOWED_PLATFORM_MEASUREMENTS"]), json.loads(policy.read_text()))
        validate_ratls_config(result)

    def test_hostname_cannot_inject_environment_lines(self):
        before = self.path.read_bytes()
        with self.assertRaises(ValueError):
            migrate_env_file(self.path, worker_dns_name="worker.example.test\nINJECTED=value")
        self.assertEqual(self.path.read_bytes(), before)

    def test_symlink_is_not_replaced(self):
        link = self.root / "link.env"
        link.symlink_to(self.path)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "regular file"):
            migrate_env_file(link)
        self.assertTrue(link.is_symlink())
        self.assertEqual(self.path.read_bytes(), before)

    def test_cli_check_does_not_write_or_print_secrets(self):
        migrate_env_file(self.path)
        before = self.path.read_bytes()
        output = io.StringIO()
        with patch("sys.argv", ["migrate_ratls_env.py", "--env-file", str(self.path), "--check"]):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                self.assertEqual(main(), 0)
        self.assertEqual(self.path.read_bytes(), before)
        values = read_env_file(self.path)
        for name in ("AGENT_POP_SIGNING_SEED", "SELLO_TOKEN_ISSUER_SIGNING_SEED", "PHALA_CLOUD_API_KEY"):
            self.assertNotIn(values[name], output.getvalue())


if __name__ == "__main__":
    unittest.main()

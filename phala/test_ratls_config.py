"""Deployment policy preflight tests use only generated fixture identities."""

import base64
import json
import tempfile
import unittest
from pathlib import Path

from nacl.signing import SigningKey

from phala.ratls_config import VERIFY_URL, decode_key, read_env_file, validate_ratls_config


def encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def configured():
    seed = bytes.fromhex("11" * 32)
    return {
        "ENABLE_PHALA_AGENT": "true",
        "PKI_AGENT_SUBJECT": "research-agent",
        "PKI_WORKER_DNS_NAME": "reserved-8443s.dstack-test.phala.network",
        "AGENT_POP_SIGNING_SEED": encode(seed),
        "AGENT_POP_REGISTRY": json.dumps({"research-agent": encode(bytes(SigningKey(seed).verify_key))}),
        "RATLS_ALLOWED_PLATFORM_MEASUREMENTS": json.dumps(
            [{name: "ab" * 48 for name in ("mrtd", "rtmr0", "rtmr1", "rtmr2")}]
        ),
        "SELLO_TOKEN_ISSUER_SIGNING_SEED": "22" * 32,
        "SELLO_ZK_SERVICE_SIGNING_SEED": "33" * 32,
        "SELLO_OWNER_HPKE_PRIVATE_KEY": "44" * 32,
    }


class ReadEnvironmentTests(unittest.TestCase):
    def test_raw_last_assignment_wins_without_shell_evaluation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "deployment.env"
            path.write_bytes(
                b"# KEY=comment\nKEY=first\nexport KEY=ignored\n KEY=ignored\n"
                b'KEY=\'literal $HOME $(do-not-execute)\' # retained\nJSON={"a":"b=c"}\n'
                b"EMPTY=before\nEMPTY=\nCRLF=value\r\n"
            )
            self.assertEqual(
                read_env_file(path),
                {
                    "KEY": "'literal $HOME $(do-not-execute)' # retained",
                    "JSON": '{"a":"b=c"}',
                    "EMPTY": "",
                    "CRLF": "value\r",
                },
            )

    def test_non_utf8_is_rejected_without_echoing_content(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "deployment.env"
            path.write_bytes(b"SECRET=do-not-print\xff")
            with self.assertRaisesRegex(ValueError, "must be UTF-8") as caught:
                read_env_file(path)
            self.assertNotIn("do-not-print", str(caught.exception))


class RatlsConfigurationTests(unittest.TestCase):
    def test_coherent_agent_policy_and_supported_key_encodings(self):
        for enabled in ("true", "1"):
            for encoded in ("11" * 32, "0x" + "11" * 32, encode(bytes.fromhex("11" * 32))):
                with self.subTest(enabled=enabled, encoding=len(encoded)):
                    values = configured()
                    values.update(ENABLE_PHALA_AGENT=enabled, AGENT_POP_SIGNING_SEED=encoded)
                    validate_ratls_config(values)
                    self.assertEqual(decode_key(encoded, "AGENT_POP_SIGNING_SEED"), bytes.fromhex("11" * 32))

    def test_all_missing_migration_variables_are_reported_together(self):
        with self.assertRaises(ValueError) as caught:
            validate_ratls_config({"ENABLE_PHALA_AGENT": "true"})
        for name in (
            "AGENT_POP_REGISTRY",
            "AGENT_POP_SIGNING_SEED",
            "RATLS_ALLOWED_PLATFORM_MEASUREMENTS",
        ):
            self.assertIn(name, str(caught.exception))

    def test_disabled_agent_still_requires_receiver_policy_but_no_agent_secrets(self):
        values = configured()
        values["ENABLE_PHALA_AGENT"] = "false"
        for name in ("AGENT_POP_SIGNING_SEED", "RATLS_ALLOWED_PLATFORM_MEASUREMENTS"):
            values.pop(name)
        validate_ratls_config(values)
        values.pop("AGENT_POP_REGISTRY")
        with self.assertRaisesRegex(ValueError, "Missing required variables: AGENT_POP_REGISTRY"):
            validate_ratls_config(values)

    def test_registry_rejects_empty_invalid_duplicate_or_ambiguous_identities(self):
        public_key = json.loads(configured()["AGENT_POP_REGISTRY"])["research-agent"]
        invalid = (
            "{}",
            "[]",
            "not-json-do-not-print",
            '{"research-agent":"' + public_key + '","research-agent":"' + public_key + '"}',
            json.dumps({"research-agent": public_key, "second-agent": public_key}),
            json.dumps({"research-agent": "invalid-key-do-not-print"}),
            json.dumps({"bad subject": public_key}),
        )
        for registry in invalid:
            with self.subTest(registry_type=registry[:1]):
                values = {**configured(), "AGENT_POP_REGISTRY": registry}
                with self.assertRaisesRegex(ValueError, "AGENT_POP_REGISTRY") as caught:
                    validate_ratls_config(values)
                self.assertNotIn("do-not-print", str(caught.exception))

    def test_deeply_nested_policy_is_reported_as_a_safe_validation_error(self):
        nested = "[" * 2000 + "0" + "]" * 2000
        for name in ("AGENT_POP_REGISTRY", "RATLS_ALLOWED_PLATFORM_MEASUREMENTS"):
            with self.subTest(field=name), self.assertRaisesRegex(ValueError, name):
                validate_ratls_config({**configured(), name: nested})

    def test_wrong_subject_and_wrong_registered_key_are_rejected(self):
        for change in (
            {"PKI_AGENT_SUBJECT": "different-agent"},
            {"AGENT_POP_SIGNING_SEED": "55" * 32},
        ):
            with self.subTest(field=next(iter(change))), self.assertRaisesRegex(ValueError, "AGENT_POP_REGISTRY"):
                validate_ratls_config({**configured(), **change})

    def test_default_subject_matches_the_deployment_template(self):
        values = configured()
        public_key = json.loads(values["AGENT_POP_REGISTRY"])[values.pop("PKI_AGENT_SUBJECT")]
        values["AGENT_POP_REGISTRY"] = json.dumps({"master-thesis-agent": public_key})
        validate_ratls_config(values)

    def test_pop_seed_cannot_reuse_an_existing_sello_secret(self):
        for name in (
            "SELLO_TOKEN_ISSUER_SIGNING_SEED",
            "SELLO_ZK_SERVICE_SIGNING_SEED",
            "SELLO_OWNER_HPKE_PRIVATE_KEY",
        ):
            with self.subTest(key=name), self.assertRaisesRegex(ValueError, "must be independent of " + name):
                validate_ratls_config({**configured(), name: "11" * 32})

    def test_bad_key_errors_never_contain_the_key_value(self):
        for name in ("AGENT_POP_SIGNING_SEED", "SELLO_TOKEN_ISSUER_SIGNING_SEED", "SELLO_OWNER_HPKE_PRIVATE_KEY"):
            with self.subTest(key=name), self.assertRaises(ValueError) as caught:
                validate_ratls_config({**configured(), name: "invalid-secret-do-not-print"})
            self.assertNotIn("invalid-secret-do-not-print", str(caught.exception))
            self.assertIn(name, str(caught.exception))

    def test_platform_allowlist_requires_exact_nonempty_bounded_measurement_tuples(self):
        valid = json.loads(configured()["RATLS_ALLOWED_PLATFORM_MEASUREMENTS"])[0]
        invalid = (
            "[]",
            "{}",
            "invalid-policy-do-not-print",
            json.dumps([valid] * 33),
            json.dumps([{**valid, "rtmr3": "ab" * 48}]),
            json.dumps([{key: value for key, value in valid.items() if key != "mrtd"}]),
            json.dumps([{**valid, "mrtd": "ab" * 47}]),
            json.dumps([{**valid, "mrtd": "gg" * 48}]),
            json.dumps([{**valid, "mrtd": 0}]),
            "[" + json.dumps(valid)[:-1] + ',"mrtd":"' + "cd" * 48 + '"}]',
        )
        for policy in invalid:
            with (
                self.subTest(length=len(policy)),
                self.assertRaisesRegex(ValueError, "RATLS_ALLOWED_PLATFORM_MEASUREMENTS") as caught,
            ):
                validate_ratls_config({**configured(), "RATLS_ALLOWED_PLATFORM_MEASUREMENTS": policy})
            self.assertNotIn("do-not-print", str(caught.exception))
        validate_ratls_config({**configured(), "RATLS_ALLOWED_PLATFORM_MEASUREMENTS": json.dumps([valid] * 32)})

    def test_dynamic_origin_does_not_require_a_reserved_worker_hostname(self):
        for hostname in (None, ""):
            with self.subTest(hostname=hostname):
                values = configured()
                if hostname is None:
                    values.pop("PKI_WORKER_DNS_NAME")
                else:
                    values["PKI_WORKER_DNS_NAME"] = hostname
                validate_ratls_config(values)

    def test_hostname_and_optional_verifier_endpoint_fail_closed(self):
        for hostname in ("https://receiver.example", "receiver.example:8443", "bad..example", "-bad.example"):
            with self.subTest(hostname=hostname), self.assertRaisesRegex(ValueError, "PKI_WORKER_DNS_NAME"):
                validate_ratls_config({**configured(), "PKI_WORKER_DNS_NAME": hostname})
        validate_ratls_config({**configured(), "PHALA_ATTESTATION_VERIFY_URL": VERIFY_URL})
        with self.assertRaisesRegex(ValueError, "PHALA_ATTESTATION_VERIFY_URL"):
            validate_ratls_config({**configured(), "PHALA_ATTESTATION_VERIFY_URL": "https://attacker.example/"})


if __name__ == "__main__":
    unittest.main()

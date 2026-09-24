"""Check independent caller keys and their provisioned public registry."""

import base64
import contextlib
import io
import json
import unittest
from unittest import mock

from nacl.signing import SigningKey

from phala import generate_sello_env


def decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class SelloEnvironmentGenerationTests(unittest.TestCase):
    def generate(self, subject="research-agent"):
        output = io.StringIO()
        arguments = ["generate_sello_env.py", "--scitt-url", "https://scitt.example.test/", "--agent-subject", subject]
        with mock.patch("sys.argv", arguments), contextlib.redirect_stdout(output):
            self.assertEqual(generate_sello_env.main(), 0)
        return dict(line.split("=", 1) for line in output.getvalue().splitlines())

    def test_pop_identity_matches_registry_and_uses_an_independent_key(self):
        values = self.generate()
        pop_seed = decode(values["AGENT_POP_SIGNING_SEED"])
        issuer_seed = decode(values["SELLO_TOKEN_ISSUER_SIGNING_SEED"])
        zk_seed = decode(values["SELLO_ZK_SERVICE_SIGNING_SEED"])
        self.assertEqual(len(pop_seed), 32)
        self.assertEqual(len({pop_seed, issuer_seed, zk_seed}), 3)
        registry = json.loads(values["AGENT_POP_REGISTRY"])
        self.assertEqual(set(registry), {"research-agent"})
        self.assertEqual(decode(registry["research-agent"]), bytes(SigningKey(pop_seed).verify_key))
        self.assertEqual(values["PKI_AGENT_SUBJECT"], "research-agent")
        self.assertNotIn("research-agent", json.loads(values["SELLO_SERVICE_REGISTRY"]))
        self.assertEqual(values["SELLO_SCITT_URL"], "https://scitt.example.test")

    def test_registry_subject_cannot_inject_extra_environment_lines(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.generate("agent\nINJECTED=value")


if __name__ == "__main__":
    unittest.main()

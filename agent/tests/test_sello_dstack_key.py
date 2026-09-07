from __future__ import annotations

import unittest

from agent_receipts.dstack_key import derive_sello_signing_seed, tee_sello_signing_seed

BASE_ENV = {
    "ACCOUNT_ADDRESS": "0x" + "11" * 20,
    "REGISTRY_ADDRESS": "0x" + "22" * 20,
    "EXPECTED_CHAIN_ID": "31337",
}


class SelloDstackKeyTests(unittest.TestCase):
    def test_derivation_matches_cross_language_vector(self) -> None:
        seed = derive_sello_signing_seed(bytes(range(32)), BASE_ENV)
        self.assertEqual(
            seed.hex(),
            "33cd5c216b863348bfc74d81841283e27c568452bee31162d22e24eab6af4190",
        )

    def test_derivation_is_scoped_to_participant_chain_and_registry(self) -> None:
        root = bytes(range(32))
        baseline = derive_sello_signing_seed(root, BASE_ENV)
        variants = (
            {**BASE_ENV, "ACCOUNT_ADDRESS": "0x" + "12" * 20},
            {**BASE_ENV, "EXPECTED_CHAIN_ID": "31338"},
            {**BASE_ENV, "REGISTRY_ADDRESS": "0x" + "23" * 20},
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.assertNotEqual(derive_sello_signing_seed(root, variant), baseline)

    def test_phala_refuses_environment_key_fallback(self) -> None:
        environment = {
            **BASE_ENV,
            "DOCKER": "phala",
            "SELLO_SERVICE_KEY_PROVIDER": "env",
            "LOCAL_TDX_MOCK": "1",
        }
        with self.assertRaisesRegex(RuntimeError, "require.*dstack"):
            tee_sello_signing_seed(environment, local_root=bytes(32))

    def test_local_root_requires_explicit_mock_mode(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "LOCAL_TDX_MOCK"):
            tee_sello_signing_seed(BASE_ENV, local_root=bytes(32))
        result = tee_sello_signing_seed(
            {**BASE_ENV, "LOCAL_TDX_MOCK": "1"},
            local_root=bytes(range(32)),
        )
        self.assertEqual(
            result.hex(),
            "33cd5c216b863348bfc74d81841283e27c568452bee31162d22e24eab6af4190",
        )


if __name__ == "__main__":
    unittest.main()

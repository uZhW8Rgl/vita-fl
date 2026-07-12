from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

from tee_inference.protocol.v1 import LABELS, encode_deterministic

ROOT = Path(__file__).resolve().parents[2]
VECTOR = ROOT / "tee_inference" / "vectors" / "v1-chestmnist.json"
GENERATOR = ROOT / "tee_inference" / "vectors" / "generate_v1_vectors.py"


class DeterministicCborTests(unittest.TestCase):
    def test_rfc8949_map_key_ordering(self) -> None:
        self.assertEqual(encode_deterministic({10: "a", 1: "b"}).hex(), "a20161620a6161")

    def test_float_is_rejected(self) -> None:
        with self.assertRaises(TypeError):
            encode_deterministic(0.5)

    def test_chestmnist_label_order_is_complete(self) -> None:
        self.assertEqual(len(LABELS), 14)
        self.assertEqual(LABELS[0], "atelectasis")
        self.assertEqual(LABELS[-1], "hernia")

    def test_checked_in_vectors_are_reproducible(self) -> None:
        before = VECTOR.read_bytes()
        subprocess.run([sys.executable, str(GENERATOR)], cwd=ROOT, check=True)
        self.assertEqual(VECTOR.read_bytes(), before)

    def test_vector_hashes_match_exact_cbor(self) -> None:
        vector = json.loads(VECTOR.read_text(encoding="utf-8"))
        for name in ("manifest", "request", "response"):
            raw = bytes.fromhex(vector[name]["deterministic_cbor_hex"])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), vector[name]["sha256_hex"])


if __name__ == "__main__":
    unittest.main()


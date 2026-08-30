from __future__ import annotations

import struct
import sys
import unittest

import torch

from dfl.neural_network.cli import (
    MODEL_LAYOUT,
    FederatedCNN,
    model_from_bytes,
    model_to_bytes,
)
from dfl.neural_network.hybrid_r import client_updates


class ModelFinitenessTests(unittest.TestCase):
    def test_non_finite_model_input_identifies_source_tensor_and_flat_index(self) -> None:
        tensor_name, _ = MODEL_LAYOUT[0]
        flat_index = 7
        source = "/tmp/worker-3/gm.bin"

        for non_finite in (float("nan"), float("inf")):
            with self.subTest(value=non_finite):
                blob = bytearray(model_to_bytes(FederatedCNN().double()))
                struct.pack_into("<d", blob, flat_index * 8, non_finite)

                with self.assertRaises(ValueError) as raised:
                    model_from_bytes(bytes(blob), source=source)

                message = str(raised.exception)
                self.assertIn("non-finite parameter", message)
                self.assertIn(source, message)
                self.assertIn(f"tensor {tensor_name}", message)
                self.assertIn(f"flat index {flat_index}", message)

    def test_non_finite_model_cannot_be_serialized(self) -> None:
        model = FederatedCNN().double()
        with torch.no_grad():
            model.conv1.weight[0, 0, 0, 0] = float("inf")

        with self.assertRaisesRegex(ValueError, "non-finite parameter"):
            model_to_bytes(model)

    def test_overflowing_client_update_is_rejected_before_candidate_selection(self) -> None:
        parent = FederatedCNN().double()
        client = FederatedCNN().double()
        with torch.no_grad():
            for parameter in parent.parameters():
                parameter.fill_(-sys.float_info.max)
            for parameter in client.parameters():
                parameter.fill_(sys.float_info.max)

        with self.assertRaisesRegex(ValueError, "client updates contains a non-finite value"):
            client_updates(parent, [client], MODEL_LAYOUT)


if __name__ == "__main__":
    unittest.main()

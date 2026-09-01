from __future__ import annotations

import struct
import unittest

import torch

from dfl.neural_network.cli import (
    MODEL_LAYOUT,
    FederatedCNN,
    _equal_weight_federated_average,
    model_from_bytes,
    model_to_bytes,
)


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

    def test_non_finite_federated_average_is_rejected(self) -> None:
        first = FederatedCNN().double()
        second = FederatedCNN().double()
        with torch.no_grad():
            second.conv1.weight[0, 0, 0, 0] = float("nan")

        with self.assertRaisesRegex(ValueError, "Federated average contains a non-finite parameter"):
            _equal_weight_federated_average([first, second])


if __name__ == "__main__":
    unittest.main()

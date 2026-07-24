from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import torch

from dfl.neural_network import cli
from dfl.neural_network.cli import FederatedCNN, model_to_bytes, write_model_bin


class ModelFinitenessTests(unittest.TestCase):
    def test_non_finite_model_cannot_be_serialized(self) -> None:
        model = FederatedCNN().double()
        with torch.no_grad():
            model.conv1.weight[0, 0, 0, 0] = float("inf")

        with self.assertRaisesRegex(ValueError, "non-finite parameter"):
            model_to_bytes(model)

    def test_overflowing_federated_average_is_rejected_before_signing(self) -> None:
        model = FederatedCNN().double()
        with torch.no_grad():
            for parameter in model.parameters():
                parameter.fill_(sys.float_info.max)

        with tempfile.TemporaryDirectory() as directory:
            inputs = Path(directory)
            write_model_bin(model, inputs / "worker-a.bin")
            write_model_bin(model, inputs / "worker-b.bin")

            with patch.object(cli, "aggregation_inputs_dir", return_value=inputs):
                with self.assertRaisesRegex(ValueError, "Federated average contains a non-finite parameter"):
                    cli.aggregate(2)


if __name__ == "__main__":
    unittest.main()

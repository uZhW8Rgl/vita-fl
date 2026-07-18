from __future__ import annotations

import importlib
import sys
import types
import unittest
from unittest.mock import patch


class QuantizedRunArgsTests(unittest.TestCase):
    def _runner(self):
        fake_ezkl = types.ModuleType("ezkl")

        class PyRunArgs:
            input_scale = 7
            param_scale = 7
            num_inner_cols = 2

        fake_ezkl.PyRunArgs = PyRunArgs
        sys.modules.pop("zk_inference.run_ezkl", None)
        with patch.dict(sys.modules, {"ezkl": fake_ezkl}):
            return importlib.import_module("zk_inference.run_ezkl")

    def test_scale_eight_is_applied_to_inputs_and_parameters(self) -> None:
        run_args = self._runner().quantized_run_args(8, 4)

        self.assertEqual(run_args.input_scale, 8)
        self.assertEqual(run_args.param_scale, 8)
        self.assertEqual(run_args.num_inner_cols, 4)

    def test_scale_below_eight_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 8"):
            self._runner().quantized_run_args(7, 4)

    def test_non_positive_inner_column_count_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            self._runner().quantized_run_args(8, 0)


if __name__ == "__main__":
    unittest.main()

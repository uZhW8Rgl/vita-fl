from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import torch
from torch import nn

from dfl.neural_network import cli
from dfl.neural_network.hybrid_r import (
    HYBRID_R_V1_HASH,
    canonical_json_bytes,
    coordinate_median,
    flatten_model,
    generate_candidate_updates,
    multi_krum,
    run_hybrid_r,
    select_candidate,
    symmetric_trimmed_mean,
)

TINY_LAYOUT = (("linear.weight", (1, 2)), ("linear.bias", (1,)))


class TinyModel(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.linear = nn.Linear(2, 1)

    def forward(self, inputs: torch.Tensor) -> torch.Tensor:
        return self.linear(inputs)


class HybridRCandidateTests(unittest.TestCase):
    def test_candidate_order_and_values_are_deterministic(self) -> None:
        updates = torch.tensor([[0.0], [1.0], [2.0], [3.0], [100.0]], dtype=torch.float64)

        candidates = generate_candidate_updates(updates)

        self.assertEqual(
            [candidate.name for candidate in candidates],
            [
                "fedavg",
                "coordinate_median",
                "trimmed_mean_q_1",
                "trimmed_mean_q_2",
                "multi_krum",
            ],
        )
        values = {
            candidate.name: None if candidate.update is None else float(candidate.update.item())
            for candidate in candidates
        }
        self.assertAlmostEqual(values["fedavg"], 21.2)
        self.assertEqual(values["coordinate_median"], 2.0)
        self.assertEqual(values["trimmed_mean_q_1"], 2.0)
        self.assertEqual(values["trimmed_mean_q_2"], 2.0)
        self.assertEqual(values["multi_krum"], 1.5)

    def test_even_coordinate_median_averages_middle_two(self) -> None:
        updates = torch.tensor([[100.0], [0.0], [20.0], [10.0]], dtype=torch.float64)
        self.assertEqual(float(coordinate_median(updates).item()), 15.0)

    def test_trimmed_mean_accepts_every_policy_q(self) -> None:
        updates = torch.arange(6, dtype=torch.float64).reshape(6, 1)
        self.assertEqual(float(symmetric_trimmed_mean(updates, 1).item()), 2.5)
        self.assertEqual(float(symmetric_trimmed_mean(updates, 2).item()), 2.5)
        with self.assertRaisesRegex(ValueError, "trim_count"):
            symmetric_trimmed_mean(updates, 3)

    def test_multi_krum_uses_policy_bound_and_stable_input_tie_break(self) -> None:
        updates = torch.zeros((5, 2), dtype=torch.float64)

        selected, metadata = multi_krum(updates)

        torch.testing.assert_close(selected, torch.zeros(2, dtype=torch.float64))
        self.assertEqual(
            metadata,
            {
                "byzantine_bound": 1,
                "neighbor_count": 2,
                "selected_count": 2,
            },
        )

    def test_multi_krum_is_unavailable_below_five_inputs(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 5"):
            multi_krum(torch.zeros((4, 2), dtype=torch.float64))

    def test_multi_krum_nonfinite_distance_invalidates_only_that_candidate(self) -> None:
        maximum = torch.finfo(torch.float64).max
        updates = torch.tensor(
            [[maximum], [0.0], [1.0], [2.0], [3.0]],
            dtype=torch.float64,
        )

        candidates = generate_candidate_updates(updates)
        krum = candidates[-1]

        self.assertEqual(krum.name, "multi_krum")
        self.assertIsNone(krum.update)
        self.assertIn("non-finite", krum.error or "")
        self.assertIsNotNone(candidates[1].update)


class HybridRSelectionTests(unittest.TestCase):
    @staticmethod
    def _linear_client(parent: TinyModel, first_weight: float) -> TinyModel:
        client = TinyModel().double()
        client.load_state_dict(parent.state_dict())
        with torch.no_grad():
            client.linear.weight.zero_()
            client.linear.weight[0, 0] = first_weight
            client.linear.bias.zero_()
        return client

    def test_exact_score_tie_keeps_earlier_candidate(self) -> None:
        decision = select_candidate(
            [
                {"candidate": "fedavg", "loss": 0.8},
                {"candidate": "coordinate_median", "loss": 0.8},
            ],
            parent_loss=1.0,
            max_loss_increase_bps=500,
        )
        self.assertTrue(decision.gate_passed)
        self.assertEqual(decision.selected_candidate, "fedavg")
        self.assertEqual(decision.output_kind, "candidate")

    def test_loss_above_parent_gate_selects_parent_fallback(self) -> None:
        decision = select_candidate(
            [{"candidate": "coordinate_median", "loss": 1.051}],
            parent_loss=1.0,
            max_loss_increase_bps=500,
        )
        self.assertFalse(decision.gate_passed)
        self.assertEqual(decision.output_kind, "parent_fallback")
        self.assertEqual(decision.reason, "loss_gate_exceeded")
        self.assertEqual(decision.allowed_loss, 1.05)

    def test_no_finite_candidate_selects_parent_fallback(self) -> None:
        decision = select_candidate(
            [
                {"candidate": "fedavg", "loss": None},
                {"candidate": "coordinate_median", "loss": math.inf},
            ],
            parent_loss=0.5,
            max_loss_increase_bps=500,
        )
        self.assertFalse(decision.gate_passed)
        self.assertIsNone(decision.selected_candidate)
        self.assertEqual(decision.reason, "no_finite_candidate")

    def test_canonical_json_rejects_nonfinite_numbers(self) -> None:
        with self.assertRaises(ValueError):
            canonical_json_bytes({"loss": float("nan")})
        self.assertEqual(
            canonical_json_bytes({"z": 1, "a": [2, 3]}),
            b'{"a":[2,3],"z":1}',
        )

    def test_end_to_end_tie_selects_fedavg(self) -> None:
        parent = TinyModel().double()
        with torch.no_grad():
            parent.linear.weight.zero_()
            parent.linear.bias.zero_()
        clients = [TinyModel().double() for _ in range(5)]
        for client in clients:
            client.load_state_dict(parent.state_dict())
        images = torch.zeros((2, 2), dtype=torch.float64)
        labels = torch.zeros((2, 1), dtype=torch.float64)

        result = run_hybrid_r(
            parent,
            clients,
            images,
            labels,
            layout=TINY_LAYOUT,
            batch_size=1,
            max_loss_increase_bps=500,
        )

        self.assertTrue(result.decision.gate_passed)
        self.assertEqual(result.decision.selected_candidate, "fedavg")
        torch.testing.assert_close(
            flatten_model(result.output_model, TINY_LAYOUT),
            flatten_model(parent, TINY_LAYOUT),
            rtol=0,
            atol=0,
        )

    def test_end_to_end_gate_republishes_unchanged_parent(self) -> None:
        parent = TinyModel().double()
        with torch.no_grad():
            parent.linear.weight.zero_()
            parent.linear.bias.fill_(-2.0)
        clients = [TinyModel().double() for _ in range(5)]
        for client in clients:
            client.load_state_dict(parent.state_dict())
            with torch.no_grad():
                client.linear.bias.fill_(2.0)
        images = torch.zeros((2, 2), dtype=torch.float64)
        labels = torch.zeros((2, 1), dtype=torch.float64)

        result = run_hybrid_r(
            parent,
            clients,
            images,
            labels,
            layout=TINY_LAYOUT,
            batch_size=2,
            max_loss_increase_bps=500,
        )

        self.assertFalse(result.decision.gate_passed)
        self.assertEqual(result.decision.output_kind, "parent_fallback")
        torch.testing.assert_close(
            flatten_model(result.output_model, TINY_LAYOUT),
            flatten_model(parent, TINY_LAYOUT),
            rtol=0,
            atol=0,
        )

    def test_sign_flip_outlier_loses_to_honest_coordinate_median(self) -> None:
        parent = TinyModel().double()
        with torch.no_grad():
            parent.linear.weight.zero_()
            parent.linear.bias.zero_()
        clients = [
            self._linear_client(parent, 2.0),
            self._linear_client(parent, 2.0),
            self._linear_client(parent, -10.0),
        ]
        images = torch.tensor([[1.0, 0.0], [1.0, 0.0]], dtype=torch.float64)
        labels = torch.ones((2, 1), dtype=torch.float64)

        result = run_hybrid_r(
            parent,
            clients,
            images,
            labels,
            layout=TINY_LAYOUT,
            batch_size=2,
            max_loss_increase_bps=500,
        )
        scores = {entry["candidate"]: entry["loss"] for entry in result.candidate_scores}

        self.assertEqual(result.decision.selected_candidate, "coordinate_median")
        self.assertGreater(scores["fedavg"], scores["coordinate_median"] + 1.0)
        self.assertAlmostEqual(
            scores["coordinate_median"],
            math.log1p(math.exp(-2.0)),
            places=12,
        )
        self.assertEqual(
            float(flatten_model(result.output_model, TINY_LAYOUT)[0].item()),
            2.0,
        )

    def test_two_colluding_updates_are_rejected_deterministically(self) -> None:
        parent = TinyModel().double()
        with torch.no_grad():
            parent.linear.weight.zero_()
            parent.linear.bias.zero_()
        honest = [self._linear_client(parent, 2.0) for _ in range(3)]
        colluding = [self._linear_client(parent, -10.0) for _ in range(2)]
        images = torch.tensor([[1.0, 0.0], [1.0, 0.0]], dtype=torch.float64)
        labels = torch.ones((2, 1), dtype=torch.float64)

        first = run_hybrid_r(
            parent,
            [*honest, *colluding],
            images,
            labels,
            layout=TINY_LAYOUT,
            batch_size=1,
            max_loss_increase_bps=500,
        )
        second = run_hybrid_r(
            parent,
            [*colluding, *honest],
            images,
            labels,
            layout=TINY_LAYOUT,
            batch_size=1,
            max_loss_increase_bps=500,
        )
        first_scores = {entry["candidate"]: entry["loss"] for entry in first.candidate_scores}
        second_scores = {entry["candidate"]: entry["loss"] for entry in second.candidate_scores}

        self.assertEqual(first.decision.selected_candidate, "coordinate_median")
        self.assertEqual(second.decision.selected_candidate, "coordinate_median")
        self.assertGreater(
            first_scores["fedavg"],
            first_scores["coordinate_median"] + 1.0,
        )
        self.assertEqual(
            first_scores["coordinate_median"],
            second_scores["coordinate_median"],
        )
        self.assertEqual(
            first_scores["multi_krum"],
            second_scores["multi_krum"],
        )
        torch.testing.assert_close(
            flatten_model(first.output_model, TINY_LAYOUT),
            flatten_model(second.output_model, TINY_LAYOUT),
            rtol=0,
            atol=0,
        )


class HybridRBindingTests(unittest.TestCase):
    def test_worker_inputs_are_sorted_by_raw_address_bytes(self) -> None:
        paths = [
            Path(f"wb_client_0x{'ff' * 20}.bin"),
            Path(f"wb_client_0x{'00' * 19}01.bin"),
        ]
        ordered = cli._canonical_aggregation_model_paths(paths)
        self.assertEqual(
            [address for address, _ in ordered],
            [f"0x{'00' * 19}01", f"0x{'ff' * 20}"],
        )

    def test_noncanonical_worker_filename_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-canonical"):
            cli._canonical_aggregation_model_paths([Path("worker.bin")])

    def test_algorithm_hash_is_checked_against_local_constant(self) -> None:
        with self.assertRaisesRegex(ValueError, "algorithm hash mismatch"):
            cli._run_hybrid_aggregation(
                [],
                source_round=1,
                round_id=2,
                medical_signer_snapshot={},
                expected_algorithm_hash="0x" + "00" * 32,
                expected_validation_data_hash="0x" + "11" * 32,
                max_loss_increase_bps=500,
            )
        self.assertEqual(
            cli._normalize_bytes32(HYBRID_R_V1_HASH.upper(), "hash"),
            HYBRID_R_V1_HASH,
        )

    def test_validation_hash_mismatch_precedes_provenance_verification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            validation_path = Path(directory) / "validation-data.npz"
            np.savez_compressed(
                validation_path,
                dataset_split=np.bytes_("CHESTMNIST-VAL-V1"),
            )
            snapshot = {
                "registry_address": "0x" + "11" * 20,
                "block_number": 10,
                "block_hash": "0x" + "22" * 32,
                "key_set_version": "1",
            }
            with (
                patch.object(cli, "is_multilabel_dataset", return_value=True),
                patch.object(cli, "validation_dataset_path", return_value=validation_path),
                patch.object(
                    cli,
                    "validation_semantic_sha256",
                    return_value="33" * 32,
                ),
                patch.object(cli, "verify_training_provenance") as verify,
            ):
                with self.assertRaisesRegex(ValueError, "semantic hash mismatch"):
                    cli._load_verified_hybrid_validation(
                        medical_signer_snapshot=snapshot,
                        expected_validation_data_hash="0x" + "44" * 32,
                    )
            verify.assert_not_called()

    def test_evidence_json_round_trip_preserves_candidate_score_array(self) -> None:
        evidence = {
            "version": "VITA-FL-HYBRID-R-EVIDENCE-V1",
            "candidate_scores": [{"candidate": "fedavg", "loss": 0.5, "status": "ok"}],
        }
        self.assertEqual(json.loads(canonical_json_bytes(evidence)), evidence)


if __name__ == "__main__":
    unittest.main()

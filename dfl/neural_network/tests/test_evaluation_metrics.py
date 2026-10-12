"""Undefined labels stay visible rather than silently inflating macro averages."""

import csv

import pytest
import torch

from dfl.neural_network.cli import _append_csv_rows, _evaluate_multilabel, _label_metric_coverage, _summary_fieldnames


def test_macro_f1_reports_finite_denominator_and_zero_handled_comparison():
    logits = torch.tensor([[-100.0, 100.0], [-100.0, -100.0]])
    labels = torch.tensor([[0.0, 1.0], [0.0, 0.0]])
    summary, rows, _ = _evaluate_multilabel(logits, labels, {})
    assert rows[0]["f1"] is None
    assert rows[1]["f1"] == pytest.approx(1)
    assert summary["macro_f1"] == pytest.approx(1)
    assert summary["macro_f1_zero_handled"] == pytest.approx(0.5)
    assert summary["macro_f1_finite_label_count"] == 1
    assert summary["macro_auroc_finite_label_count"] == 1
    assert set(_label_metric_coverage(rows)) <= set(_summary_fieldnames())


def test_undefined_and_nonfinite_metrics_are_counted_explicitly():
    result = _label_metric_coverage([{"f1": None, "auroc": None}, {"f1": float("nan"), "auroc": float("inf")}])
    assert result == {"macro_f1_finite_label_count": 0, "macro_auroc_finite_label_count": 0, "macro_f1_zero_handled": 0}
    assert _label_metric_coverage([])["macro_f1_zero_handled"] is None


def test_new_metrics_do_not_append_wider_rows_under_a_legacy_csv_header(tmp_path):
    path = tmp_path / "old-experiment.csv"
    path.write_text("round,macro_f1\n1,0.8\n")
    _append_csv_rows(path, _summary_fieldnames(), [{"round": 2, "macro_f1": 0.9, "macro_f1_finite_label_count": 14}])
    with path.open() as stream:
        rows = list(csv.DictReader(stream))
    assert rows == [{"round": "1", "macro_f1": "0.8"}, {"round": "2", "macro_f1": "0.9"}]
    fresh = tmp_path / "new-experiment.csv"
    _append_csv_rows(fresh, _summary_fieldnames(), [{"round": 1, "macro_f1_finite_label_count": 14}])
    with fresh.open() as stream:
        assert next(csv.DictReader(stream))["macro_f1_finite_label_count"] == "14"

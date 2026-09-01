"""Inactive research prototype for the former VITA-FL-specific Hybrid-R variant.

The production DFL path deliberately does not import or call this module.  It is
retained only so the historical experiment remains auditable; active rounds use
the equal-weight FedAvg implementation in ``cli.aggregate``.
"""

from __future__ import annotations

import copy
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

import torch
from torch import nn
from torch.nn import functional as F

HYBRID_R_V1_PREIMAGE = (
    "VITA-FL:hybrid-r:v1|model=torch-state-dict|layout=conv1.weight,conv1.bias,"
    "conv2.weight,conv2.bias,fc1.weight,fc1.bias,fc2.weight,fc2.bias|"
    "tensor-order=c-contiguous-row-major|numeric=ieee754-binary64-cpu|"
    "update=client-model-minus-parent-model|input-order=worker-address-ascending|"
    "candidates=fedavg(equal-weight-arithmetic-mean),coordinate-median"
    "(even-count=arithmetic-mean-of-middle-two),trimmed-mean"
    "(q=1..floor((n-1)/2),q-ascending,drop-q-lowest-and-q-highest-per-coordinate),"
    "multi-krum(n>=5,f=floor((n-3)/2),neighbors=n-f-2,select=n-f-2,"
    "single-pass,squared-l2,score=sum-nearest,score-ties=input-order,"
    "selected-update=equal-weight-arithmetic-mean)|candidate-order=fedavg,"
    "coordinate-median,trimmed-mean-q-ascending,multi-krum|"
    "candidate-model=parent-model-plus-candidate-update|"
    "validation=round.validationDataHash|risk=bce-with-logits"
    "(raw-logits,elementwise-mean-over-Nx14,binary64-cpu)|"
    "selection=exact-binary64-less-than;ties=earlier-candidate|"
    "gate=best-loss<=parent-loss*(1+round.maxLossIncreaseBps/10000)|"
    "parent-fallback=unchanged-parent-if-no-finite-candidate-or-gate-fails|"
    "fail-closed=missing-or-hash-mismatched-validation,invalid-model-layout,"
    "nonfinite-parent-loss"
)
HYBRID_R_V1_HASH = "0xfee8d99e620214799109487915a0c3a4f37f5a6fb66cb08dfed75fb5b6573610"
HYBRID_R_EVIDENCE_VERSION = "VITA-FL-HYBRID-R-EVIDENCE-V1"
BASIS_POINTS_DENOMINATOR = 10_000
DEFAULT_MAX_LOSS_INCREASE_BPS = 500

ModelLayout = Sequence[tuple[str, tuple[int, ...]]]


@dataclass(frozen=True)
class CandidateUpdate:
    name: str
    update: torch.Tensor | None
    metadata: dict[str, int]
    error: str | None = None


@dataclass(frozen=True)
class SelectionDecision:
    selected_candidate: str | None
    selected_loss: float | None
    allowed_loss: float
    gate_passed: bool
    output_kind: str
    reason: str


@dataclass(frozen=True)
class HybridRResult:
    output_model: nn.Module
    parent_loss: float
    candidate_scores: list[dict[str, Any]]
    decision: SelectionDecision


def configure_deterministic_cpu() -> None:
    """Constrain aggregation and validation reductions to one deterministic CPU thread."""
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    if hasattr(torch.backends, "mkldnn"):
        torch.backends.mkldnn.enabled = False


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def write_canonical_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(value) + b"\n")


def _require_float64_cpu(tensor: torch.Tensor, label: str) -> None:
    if tensor.dtype != torch.float64:
        raise ValueError(f"{label} must use torch.float64")
    if tensor.device.type != "cpu":
        raise ValueError(f"{label} must reside on CPU")
    if not torch.isfinite(tensor).all():
        raise ValueError(f"{label} contains a non-finite value")


def _require_updates(updates: torch.Tensor) -> None:
    if updates.ndim != 2 or updates.shape[0] < 1 or updates.shape[1] < 1:
        raise ValueError("client updates must have shape (N, D) with N,D >= 1")
    _require_float64_cpu(updates, "client updates")


def _sequential_mean(rows: torch.Tensor) -> torch.Tensor:
    if rows.ndim != 2 or rows.shape[0] < 1:
        raise ValueError("mean requires at least one row")
    result = torch.zeros(rows.shape[1], dtype=torch.float64, device="cpu")
    reciprocal = 1.0 / int(rows.shape[0])
    for index in range(int(rows.shape[0])):
        result.add_(rows[index], alpha=reciprocal)
    if not torch.isfinite(result).all():
        raise ValueError("candidate mean contains a non-finite value")
    return result


def federated_average(updates: torch.Tensor) -> torch.Tensor:
    _require_updates(updates)
    return _sequential_mean(updates)


def coordinate_median(updates: torch.Tensor) -> torch.Tensor:
    _require_updates(updates)
    ordered = torch.sort(updates, dim=0, stable=True).values
    count = int(ordered.shape[0])
    middle = count // 2
    if count % 2:
        result = ordered[middle].clone()
    else:
        # Halving before addition avoids overflow for two same-sign finite maxima.
        result = ordered[middle - 1] * 0.5 + ordered[middle] * 0.5
    if not torch.isfinite(result).all():
        raise ValueError("coordinate median contains a non-finite value")
    return result


def symmetric_trimmed_mean(updates: torch.Tensor, trim_count: int) -> torch.Tensor:
    _require_updates(updates)
    count = int(updates.shape[0])
    if trim_count < 1 or trim_count > (count - 1) // 2:
        raise ValueError(f"trim_count must be in [1, floor((N-1)/2)], got {trim_count} for N={count}")
    ordered = torch.sort(updates, dim=0, stable=True).values
    return _sequential_mean(ordered[trim_count : count - trim_count])


def multi_krum(updates: torch.Tensor) -> tuple[torch.Tensor, dict[str, int]]:
    _require_updates(updates)
    count = int(updates.shape[0])
    if count < 5:
        raise ValueError("Multi-Krum requires at least 5 client updates")

    byzantine_bound = (count - 3) // 2
    neighbor_count = count - byzantine_bound - 2
    selected_count = neighbor_count
    if count <= 2 * byzantine_bound + 2 or neighbor_count < 1:
        raise ValueError("Multi-Krum requires N > 2f + 2")

    # A single Gram matrix avoids materializing an (N,N,D) difference tensor.
    # All reductions run with one torch thread (configured by run_hybrid_r).
    gram = updates @ updates.transpose(0, 1)
    squared_norms = torch.sum(updates * updates, dim=1)
    distances = squared_norms[:, None] + squared_norms[None, :] - 2.0 * gram
    distances.clamp_min_(0.0)
    off_diagonal = ~torch.eye(count, dtype=torch.bool, device="cpu")
    if not torch.isfinite(distances[off_diagonal]).all():
        raise ValueError("Multi-Krum distance matrix contains a non-finite value")
    distances.fill_diagonal_(float("inf"))

    scores = torch.empty(count, dtype=torch.float64, device="cpu")
    for index in range(count):
        nearest = torch.sort(distances[index], stable=True).values[:neighbor_count]
        scores[index] = torch.sum(nearest)
    if not torch.isfinite(scores).all():
        raise ValueError("Multi-Krum score contains a non-finite value")

    # Stable sorting makes canonical input order the score tie-break.
    selected_indices = torch.argsort(scores, stable=True)[:selected_count]
    selected_update = _sequential_mean(updates.index_select(0, selected_indices))
    metadata = {
        "byzantine_bound": byzantine_bound,
        "neighbor_count": neighbor_count,
        "selected_count": selected_count,
    }
    return selected_update, metadata


def generate_candidate_updates(updates: torch.Tensor) -> list[CandidateUpdate]:
    _require_updates(updates)
    count = int(updates.shape[0])
    candidates: list[CandidateUpdate] = []

    def append_candidate(
        name: str,
        builder,
        metadata: dict[str, int] | None = None,
    ) -> None:
        try:
            update = builder()
            _require_float64_cpu(update, f"{name} update")
            candidates.append(CandidateUpdate(name, update, metadata or {}))
        except (RuntimeError, ValueError) as exc:
            candidates.append(
                CandidateUpdate(
                    name,
                    None,
                    metadata or {},
                    f"{type(exc).__name__}: {exc}",
                )
            )

    append_candidate("fedavg", lambda: federated_average(updates))
    append_candidate("coordinate_median", lambda: coordinate_median(updates))
    for trim_count in range(1, (count - 1) // 2 + 1):
        append_candidate(
            f"trimmed_mean_q_{trim_count}",
            lambda trim_count=trim_count: symmetric_trimmed_mean(updates, trim_count),
            {"trim_count": trim_count},
        )
    if count >= 5:
        try:
            krum_update, krum_metadata = multi_krum(updates)
            candidates.append(CandidateUpdate("multi_krum", krum_update, krum_metadata))
        except (RuntimeError, ValueError) as exc:
            candidates.append(
                CandidateUpdate(
                    "multi_krum",
                    None,
                    {
                        "byzantine_bound": (count - 3) // 2,
                        "neighbor_count": count - ((count - 3) // 2) - 2,
                        "selected_count": count - ((count - 3) // 2) - 2,
                    },
                    f"{type(exc).__name__}: {exc}",
                )
            )
    return candidates


def _layout_names(layout: ModelLayout) -> tuple[str, ...]:
    return tuple(name for name, _ in layout)


def flatten_model(model: nn.Module, layout: ModelLayout) -> torch.Tensor:
    state = model.state_dict()
    expected_names = _layout_names(layout)
    if tuple(state.keys()) != expected_names:
        raise ValueError(f"invalid model layout: got {tuple(state.keys())}, expected {expected_names}")

    flattened: list[torch.Tensor] = []
    for name, shape in layout:
        tensor = state[name].detach()
        if tuple(tensor.shape) != shape:
            raise ValueError(f"invalid model tensor shape for {name}: got {tuple(tensor.shape)}, expected {shape}")
        _require_float64_cpu(tensor, f"model tensor {name}")
        flattened.append(tensor.contiguous().reshape(-1))
    result = torch.cat(flattened)
    _require_float64_cpu(result, "flattened model")
    return result


def model_from_vector(template: nn.Module, vector: torch.Tensor, layout: ModelLayout) -> nn.Module:
    _require_float64_cpu(vector, "model vector")
    expected_size = sum(math.prod(shape) for _, shape in layout)
    if vector.ndim != 1 or vector.numel() != expected_size:
        raise ValueError(f"invalid model vector shape: got {tuple(vector.shape)}, expected ({expected_size},)")

    model = copy.deepcopy(template)
    state: dict[str, torch.Tensor] = {}
    offset = 0
    for name, shape in layout:
        count = math.prod(shape)
        state[name] = vector[offset : offset + count].reshape(shape).contiguous().clone()
        offset += count
    model.load_state_dict(state, strict=True)
    model.eval()
    return model


def client_updates(
    parent_model: nn.Module,
    client_models: Iterable[nn.Module],
    layout: ModelLayout,
) -> tuple[torch.Tensor, torch.Tensor]:
    parent_vector = flatten_model(parent_model, layout)
    local_vectors = [flatten_model(model, layout) for model in client_models]
    if not local_vectors:
        raise ValueError("Hybrid-R requires at least one client model")
    updates = torch.stack([vector - parent_vector for vector in local_vectors], dim=0)
    _require_updates(updates)
    return parent_vector, updates


def multilabel_bce_loss(
    model: nn.Module,
    images: torch.Tensor,
    labels: torch.Tensor,
    *,
    batch_size: int,
) -> float | None:
    _require_float64_cpu(images, "validation images")
    _require_float64_cpu(labels, "validation labels")
    if images.ndim != 2 or images.shape[0] < 1:
        raise ValueError("validation images must have shape (N, D) with N >= 1")
    if labels.ndim != 2 or labels.shape[0] != images.shape[0] or labels.shape[1] < 1:
        raise ValueError("validation labels must have shape (N, L)")
    if batch_size < 1:
        raise ValueError("validation batch size must be positive")

    model.eval()
    total_loss = torch.zeros((), dtype=torch.float64, device="cpu")
    element_count = 0
    with torch.no_grad():
        for start in range(0, int(images.shape[0]), batch_size):
            batch_images = images[start : start + batch_size]
            batch_labels = labels[start : start + batch_size]
            logits = model(batch_images)
            if (
                logits.dtype != torch.float64
                or logits.device.type != "cpu"
                or logits.shape != batch_labels.shape
                or not torch.isfinite(logits).all()
            ):
                return None
            losses = F.binary_cross_entropy_with_logits(logits, batch_labels, reduction="none")
            if not torch.isfinite(losses).all():
                return None
            total_loss += torch.sum(losses)
            element_count += int(losses.numel())
    if element_count < 1 or not torch.isfinite(total_loss):
        return None
    value = float((total_loss / element_count).item())
    return value if math.isfinite(value) else None


def select_candidate(
    candidate_scores: Sequence[dict[str, Any]],
    *,
    parent_loss: float,
    max_loss_increase_bps: int,
) -> SelectionDecision:
    if not math.isfinite(parent_loss):
        raise ValueError("parent validation loss is non-finite")
    if not 0 <= max_loss_increase_bps <= 10_000:
        raise ValueError("max_loss_increase_bps must be between 0 and 10000")

    allowed_loss = parent_loss * (1.0 + float(max_loss_increase_bps) / BASIS_POINTS_DENOMINATOR)
    if not math.isfinite(allowed_loss):
        raise ValueError("allowed parent validation loss is non-finite")

    selected_candidate: str | None = None
    selected_loss: float | None = None
    for entry in candidate_scores:
        value = entry.get("loss")
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            continue
        score = float(value)
        # Strict comparison preserves the earlier candidate on exact ties.
        if selected_loss is None or score < selected_loss:
            selected_candidate = str(entry["candidate"])
            selected_loss = score

    if selected_candidate is None or selected_loss is None:
        return SelectionDecision(
            selected_candidate=None,
            selected_loss=None,
            allowed_loss=allowed_loss,
            gate_passed=False,
            output_kind="parent_fallback",
            reason="no_finite_candidate",
        )
    if selected_loss <= allowed_loss:
        return SelectionDecision(
            selected_candidate=selected_candidate,
            selected_loss=selected_loss,
            allowed_loss=allowed_loss,
            gate_passed=True,
            output_kind="candidate",
            reason="accepted",
        )
    return SelectionDecision(
        selected_candidate=selected_candidate,
        selected_loss=selected_loss,
        allowed_loss=allowed_loss,
        gate_passed=False,
        output_kind="parent_fallback",
        reason="loss_gate_exceeded",
    )


def run_hybrid_r(
    parent_model: nn.Module,
    client_models: Sequence[nn.Module],
    validation_images: torch.Tensor,
    validation_labels: torch.Tensor,
    *,
    layout: ModelLayout,
    batch_size: int,
    max_loss_increase_bps: int,
) -> HybridRResult:
    configure_deterministic_cpu()
    parent_model.eval()
    parent_vector, updates = client_updates(parent_model, client_models, layout)
    candidates = generate_candidate_updates(updates)

    parent_loss = multilabel_bce_loss(
        parent_model,
        validation_images,
        validation_labels,
        batch_size=batch_size,
    )
    if parent_loss is None:
        raise ValueError("parent validation loss is non-finite")

    candidate_scores: list[dict[str, Any]] = []
    finite_models: dict[str, nn.Module] = {}
    for candidate in candidates:
        entry: dict[str, Any] = {
            "candidate": candidate.name,
            **candidate.metadata,
        }
        if candidate.update is None:
            entry.update({"loss": None, "status": "invalid_update", "error": candidate.error})
            candidate_scores.append(entry)
            continue

        candidate_vector = parent_vector + candidate.update
        if not torch.isfinite(candidate_vector).all():
            entry.update({"loss": None, "status": "non_finite_model"})
            candidate_scores.append(entry)
            continue
        candidate_model = model_from_vector(parent_model, candidate_vector, layout)
        loss = multilabel_bce_loss(
            candidate_model,
            validation_images,
            validation_labels,
            batch_size=batch_size,
        )
        if loss is None:
            entry.update({"loss": None, "status": "non_finite_loss"})
        else:
            entry.update({"loss": loss, "status": "ok"})
            finite_models[candidate.name] = candidate_model
        candidate_scores.append(entry)

    decision = select_candidate(
        candidate_scores,
        parent_loss=parent_loss,
        max_loss_increase_bps=max_loss_increase_bps,
    )
    if decision.gate_passed:
        output_model = finite_models[decision.selected_candidate or ""]
    else:
        output_model = copy.deepcopy(parent_model)
        output_model.eval()
    return HybridRResult(
        output_model=output_model,
        parent_loss=parent_loss,
        candidate_scores=candidate_scores,
        decision=decision,
    )

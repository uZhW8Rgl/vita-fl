# Authoritative Phala evaluation run

This directory preserves the canonical evidence package for the historical
evaluation run performed with the former VITA-FL-specific candidate-selection
configuration. It is a legacy research artifact, not evidence for the active
equal-weight FedAvg baseline. The run used six Phala `tdx.small` workers, one
bootstrap completion, and 24 successful federated rounds with five client
updates per round. Worker 0 additionally served the final round-25 model through
the TEE-inference path.

The evaluated Tier-1 account exposed eight concurrent TEE slots. Six slots
therefore hosted the worker roster and its six equal ChestMNIST shards; the
remaining two hosted the contract/control runtime and Ollama. This is a
run-specific account-capacity decision, not a protocol or general Phala limit.

## Evidence policy

Earlier local Docker scale experiments and earlier partial Phala runs remain
outside this package as development history. Any publication that reuses
learning results from this directory must identify the legacy aggregation
configuration explicitly and must not describe the measurements as FedAvg.

## Files

- `run_manifest.json`: compact configuration, key results, imbalance baselines,
  gas accounting, pinned images, and final TEE-inference evidence.
- `round_metrics.csv`: all 24 per-round test evaluations and Hybrid-R decisions.
- `plot-metrics.csv`: a LaTeX-safe projection of the same 24 rows used only to
  render the thesis curves; it contains no additional observations.
- `first_best_final.csv`: first, best, and final values used in compact tables.
- `worker_activity.csv`: per-worker roles, recovery event, transactions, and gas.
- `registration_gas_audit.csv`: all six RTMR3-registration receipts, with their
  transaction hashes, block numbers, gas, and exact source-log locations.
- `raw/`: unmodified API exports, Prometheus snapshot, six complete worker logs,
  measured worker compositions, agent session, receipts, transparency records,
  and the final TEE-inference result.

`scripts/build_authoritative_run_assets.py` regenerates all derived files without
contacting a live service:

```bash
python scripts/build_authoritative_run_assets.py
```

## Interpretation boundary

The learning results demonstrate that the distributed protocol completed and
produced a model with non-random ranking ability; they do not constitute a
clinical validation. ChestMNIST has only 5.26% positive label positions in the
test split. Consequently, raw label-wise accuracy is dominated by negative
labels: an all-negative classifier already reaches 94.74% label accuracy while
having macro F1 0 and AUROC 0.5. The observed macro AUROC, F1, and BCE therefore
carry more information about learning than raw accuracy. Rare labels, the fixed
0.5 decision threshold, two local epochs, and the compact CNN explain the modest
F1 values and motivate class-specific threshold calibration in future work.

Gas values are Anvil EVM protocol accounting for the evaluated execution. The
final live dashboard snapshot had discarded eight early receipt events because
the control service reset its in-memory telemetry after worker deployment had
already begun. The immutable worker logs and on-chain roster independently show
that all six registrations succeeded. Consequently, the canonical accounting
is reconstructed by transaction hash from every receipt-derived worker-log
record: 305 worker transactions consumed 560,027,129 gas, including six RTMR3
registrations consuming 472,618,555 gas. With 24 initialization transactions
and 57,323,711 gas, the complete run comprises 329 transactions and 617,350,840
gas. The raw partial snapshot remains unmodified as audit evidence.

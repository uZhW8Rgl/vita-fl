# Authoritative Phala evaluation run

This directory preserves the canonical evidence package for the six-worker
Phala FedAvg run completed on 2026-09-01. It is the sole quantitative run used
by the thesis, journal, and presentation. The run completed one bootstrap and
24 federated rounds with five client updates and two local epochs per round;
Worker 0 then served the final round-25 model through the TEE-inference path.

The evaluated Tier-1 account exposed eight concurrent TEE slots. Six slots
hosted the worker roster and its six equal ChestMNIST shards; the remaining two
hosted the contract/control runtime and Ollama. This is a run-specific capacity
decision, not a protocol or general Phala limit.

## Files

- `run_manifest.json`: configuration, key results, imbalance baselines, gas
  accounting, image pins, and final TEE-inference evidence.
- `round_metrics.csv`: all 24 per-round test evaluations of the equal-weight
  FedAvg model lineage.
- `plot-metrics.csv`: the plotting projection of those same 24 observations.
- `first_best_final.csv`: first, best, and final values used in compact tables.
- `worker_activity.csv`: per-worker training, aggregation, transaction, and gas
  totals.
- `registration_gas_audit.csv`: the six RTMR3-registration receipts.
- `raw/`: the unmodified observability export, final Prometheus scrape, runtime
  status, six complete worker logs, agent session, transparency read models,
  and verified final TEE-inference result.

Regenerate and distribute all compact publication inputs offline with:

```bash
python scripts/build_authoritative_run_assets.py
```

## Interpretation boundary

The learning results demonstrate successful distributed execution and
non-random ranking ability; they are not clinical validation. Only 5.26% of
ChestMNIST test-set label positions are positive, so raw label-wise accuracy is
dominated by negative labels: an all-negative classifier already reaches
94.74% accuracy while having macro F1 0 and AUROC 0.5. Macro AUROC, F1, and BCE
therefore carry more information about learning than raw accuracy here.

The gas figures are receipt-derived Anvil EVM protocol accounting, not public-
network fees or Phala compute cost. All 329 transaction hashes are unique. The
TEE-inference export retains the agent session, result, verification summary,
and transparency-record read models, but not the original `evidence.cbor` or
transparent-statement byte files; those retained objects therefore support the
reported bindings without constituting a fully replayable evidence archive.

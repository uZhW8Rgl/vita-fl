# Local RSA keys

This directory contains generated development keys for the local Compose
profile and is intentionally ignored by Git. Create them before a local start:

```bash
python3 scripts/prepare_dfl_worker_experiment.py \
  --workers 20 \
  --skip-compose \
  --skip-env
```

Phala does not read this directory. Its complete W0--W499 Ethereum and RSA
inventory is stored inline in `.env.phala.anvil` or
`.env.phala.anvil.example`, whichever is selected for the deployment.

The local worker and contract containers receive generated files through
read-only bind mounts. These are prototype credentials and must not be used for
production identities, public-chain funds, or confidential data.

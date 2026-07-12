# Local RSA keys

This directory contains generated development keys and is intentionally ignored by Git.
Create the key pairs required by the local Compose profile before starting it:

```bash
python3 scripts/prepare_dfl_worker_experiment.py \
  --workers 20 \
  --skip-compose \
  --skip-env
```

The local worker and contract containers receive these files through read-only bind mounts.
Production/Phala images do not contain private keys; the Terraform provider encrypts the
configured key material for the target TEE.

Previously committed keys must be treated as compromised. Generating replacements with
`--force-keys` is an explicit rotation and requires updating every dependent deployment.

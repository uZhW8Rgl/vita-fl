# Synthetic medical signer fixtures

These public RSA-2048 keys and self-signed X.509 certificates represent five
synthetic X-ray devices and one synthetic radiologist in the ChestMNIST
prototype. `Deploy.s.sol` installs them in `MedicalSignerRegistry`; workers use
the resulting on-chain allowlist as their trust source at the beginning of
every ChestMNIST training round.

The corresponding private fixture keys are generated under the ignored
`data/chestmnist/provenance/private/` directory. They are used only to create
the signed synthetic training shards and are deliberately excluded from all
worker images. Real deployments should replace these fixtures with a medical
PKI and CA-issued device and professional certificates.

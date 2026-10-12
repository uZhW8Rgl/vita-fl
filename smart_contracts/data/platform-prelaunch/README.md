Public Phala Cloud pre-launch regression fixtures

The v0.0.20 script was retrieved from https://github.com/Phala-Network/phala-pay/blob/main/deploy/phala-cloud-pre-launch.sh on 2026-10-12; its SHA-256 matches the previously trusted production digest. The v0.0.21 script was retrieved from the authenticated Phala compose_file API for an evaluation-created CVM on the same date. Its only changes from v0.0.20 are the version banner and a 300-second Docker image pull timeout. These public scripts contain environment variable references, not credentials. The modified fixture adds unapproved root code and must be rejected. Both parsers consume the same fixtures.

v0.0.20 SHA-256: 982181610f70be9087b1c69b36b719b47b82d37fcef8acc9289ed3bb3095ffe8
v0.0.21 SHA-256: 2589e09ed5910ec59c62ae7260d1b95f1ed56c04bcef07b4bd5f62985b2392f2

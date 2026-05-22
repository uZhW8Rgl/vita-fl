from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path


CHESTMNIST_URL = "https://zenodo.org/records/10519652/files/chestmnist.npz?download=1"
CHESTMNIST_MD5 = "02c8a6516a18b556561a56cbdd36c4a8"


def md5sum(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Download the official ChestMNIST bundle.")
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/chestmnist/chestmnist.npz"),
        help="Destination for chestmnist.npz",
    )
    args = parser.parse_args()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading ChestMNIST to {args.out} ...")
    urllib.request.urlretrieve(CHESTMNIST_URL, args.out)

    actual_md5 = md5sum(args.out)
    if actual_md5 != CHESTMNIST_MD5:
        args.out.unlink(missing_ok=True)
        raise RuntimeError(
            f"MD5 mismatch for {args.out}: got {actual_md5}, expected {CHESTMNIST_MD5}. "
            "The file was removed."
        )

    print(f"Saved {args.out}")
    print(f"MD5 verified: {actual_md5}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run_openssl(arguments: list[str]) -> None:
    subprocess.run(["openssl", *arguments], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def generate_signer(
    private_dir: Path,
    public_dir: Path,
    file_stem: str,
    common_name: str,
    serial: int,
    force: bool,
) -> dict[str, str]:
    private_key = private_dir / f"{file_stem}-private.pem"
    public_key_der = public_dir / f"{file_stem}-public.der"
    certificate_pem = public_dir / f"{file_stem}-certificate.pem"
    certificate_der = public_dir / f"{file_stem}-certificate.der"
    outputs = (private_key, public_key_der, certificate_pem, certificate_der)
    if force or not all(path.exists() for path in outputs):
        private_dir.mkdir(parents=True, exist_ok=True)
        public_dir.mkdir(parents=True, exist_ok=True)
        run_openssl(
            ["genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(private_key)]
        )
        run_openssl(
            [
                "req",
                "-new",
                "-x509",
                "-sha256",
                "-key",
                str(private_key),
                "-out",
                str(certificate_pem),
                "-days",
                "3650",
                "-set_serial",
                str(serial),
                "-subj",
                f"/O=Master Thesis Synthetic Medical PKI/CN={common_name}",
            ]
        )
        run_openssl(
            ["pkey", "-in", str(private_key), "-pubout", "-outform", "DER", "-out", str(public_key_der)]
        )
        run_openssl(["x509", "-in", str(certificate_pem), "-outform", "DER", "-out", str(certificate_der)])
    private_key.chmod(0o600)
    return {
        "file_stem": file_stem,
        "common_name": common_name,
        "public_key_der": str(public_key_der),
        "certificate_der": str(certificate_der),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate synthetic ChestMNIST DICOM signer fixtures.")
    parser.add_argument(
        "--private-dir",
        type=Path,
        default=Path("data/chestmnist/provenance/private"),
        help="Synthetic private keys used only to build the signed fixture dataset",
    )
    parser.add_argument(
        "--public-dir",
        type=Path,
        default=Path("smart_contracts/data/medical_signers"),
        help="Public keys and X.509 certificates deployed to MedicalSignerRegistry",
    )
    parser.add_argument("--force", action="store_true", help="Replace existing synthetic fixture identities")
    args = parser.parse_args()

    signers = []
    for index in range(5):
        signers.append(
            generate_signer(
                args.private_dir,
                args.public_dir,
                f"xray-device-{index}",
                f"Synthetic X-Ray Device {index}",
                1000 + index,
                args.force,
            )
        )
    signers.append(
        generate_signer(
            args.private_dir,
            args.public_dir,
            "radiologist-0",
            "Synthetic Radiologist 0",
            2000,
            args.force,
        )
    )

    manifest_path = args.public_dir / "signers.json"
    manifest_path.write_text(json.dumps({"fixture_only": True, "signers": signers}, indent=2) + "\n", encoding="utf-8")
    print(f"generated {len(signers)} synthetic signer identities")
    print(f"public signer manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

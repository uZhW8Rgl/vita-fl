"""Materialize GitHub environment secrets without putting them in logs/artifacts."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def prepare_config(environ: dict[str, str], root: Path) -> dict[str, Path]:
    """Write a private environment, supporting GitHub's 48 KB secret limit."""
    content = environ.get("PHALA_ENV_CONTENT", "")
    passphrase = environ.get("PHALA_ENV_PASSPHRASE", "")
    if bool(content) == bool(passphrase):
        raise ValueError("Set exactly one of PHALA_ENV_CONTENT or PHALA_ENV_PASSPHRASE.")
    if not environ.get("PHALA_STATE_PASSPHRASE", "").strip():
        raise ValueError("PHALA_STATE_PASSPHRASE is required for the encrypted GitHub state.")
    temporary = Path(environ["RUNNER_TEMP"]) / "vita-fl-phala"
    temporary.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary.chmod(0o700)
    environment_file = temporary / "deployment.env"
    environment_file.touch(mode=0o600)
    environment_file.chmod(0o600)
    if content:
        environment_file.write_text(content, encoding="utf-8")
    else:
        source = root / environ.get("PHALA_ENV_ENCRYPTED_FILE", "phala/deploy.env.gpg")
        if not environ.get("PHALA_ENV_ENCRYPTED_FILE"):
            source = root / "phala/deploy.env.gpg"
        source = source.resolve()
        if not source.is_relative_to(root.resolve()) or not source.is_file():
            raise ValueError("PHALA_ENV_ENCRYPTED_FILE must name an encrypted file in the checkout.")
        # A passphrase argument would be exposed in the process list. GitHub
        # does not mask values inside this large decrypted file automatically.
        result = subprocess.run(
            [
                "gpg",
                "--batch",
                "--yes",
                "--pinentry-mode",
                "loopback",
                "--passphrase-fd",
                "0",
                "--output",
                str(environment_file),
                "--decrypt",
                str(source),
            ],
            input=passphrase + "\n",
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            environment_file.unlink(missing_ok=True)
            raise ValueError("Could not decrypt the deployment environment; check file and passphrase.")
    if not environment_file.stat().st_size:
        raise ValueError("The deployment environment is empty.")
    return {"PHALA_ENV_FILE": environment_file}


def main() -> int:
    os.umask(0o077)
    try:
        paths = prepare_config(dict(os.environ), Path(__file__).resolve().parent.parent)
        with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
            for key, path in paths.items():
                if "\n" in str(path) or "\r" in str(path):
                    raise ValueError("Deployment paths must not contain line breaks.")
                output.write(f"{key}={path}\n")
    except (KeyError, OSError, ValueError) as error:
        print(f"Phala configuration: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

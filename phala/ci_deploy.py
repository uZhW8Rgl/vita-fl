"""Deploy one successful image build while retaining the other images in state."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

if __package__:
    from .github_state import GitHubState
else:
    from github_state import GitHubState

IMAGE_VARIABLES = frozenset(
    {
        "worker_image",
        "smart_contracts_image",
        "agent_image",
        "control_api_image",
        "transparency_log_image",
        "ui_image",
        "zk_inference_image",
    }
)
SHA = re.compile(r"^[0-9a-f]{40}$")
IMAGE = re.compile(r"^ghcr\.io/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$")


def should_skip_revision(previous: str | None, current: str, root: Path) -> bool:
    """Avoid rolling a component back when an older build finishes last."""
    if not SHA.fullmatch(current):
        raise ValueError("Image revision must be a full Git commit SHA.")
    if not previous or previous == current:
        return False
    if not SHA.fullmatch(previous):
        raise ValueError("The stored image revision is not a full Git commit SHA.")

    def ancestor(older: str, newer: str) -> bool:
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", older, newer],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise ValueError("Cannot compare deployment revisions; a checkout with full Git history is required.")
        return result.returncode == 0

    if ancestor(current, previous):
        return True
    if ancestor(previous, current):
        return False
    raise ValueError(
        "Image revision and deployed revision have diverged. "
        "Reconcile the deployment from the intended branch locally before retrying."
    )


def validate_source_revision(revision: str, root: Path) -> None:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", revision, "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise ValueError("The image commit must be part of the checked-out deployment branch's history.")


def checked(command: list[str], *, env: dict[str, str] | None = None, capture: bool = False) -> str:
    result = subprocess.run(command, env=env, capture_output=capture, text=True, check=False)
    if result.returncode < 0 or result.returncode >= 128:
        # A terminated launcher can leave Terraform descendants running. Keep
        # this a cancellation through the surrounding state session so it
        # retains the deployment lock, including wrapped shell exit 128+signal.
        raise KeyboardInterrupt
    if result.returncode:
        # terraform output -json may contain secrets. Never echo its output,
        # including on failure, and never treat a failed state read as empty.
        raise ValueError(f"Deployment command failed ({Path(command[0]).name}, exit {result.returncode}).")
    return result.stdout if capture else ""


def deploy(args: argparse.Namespace, directory: Path) -> str:
    if args.image_variable not in IMAGE_VARIABLES:
        raise ValueError("Unsupported Terraform image variable.")
    if not IMAGE.fullmatch(args.image_reference):
        raise ValueError("The build must supply a ghcr.io image pinned to a full sha256 digest.")
    if not SHA.fullmatch(args.revision):
        raise ValueError("Image revision must be a full Git commit SHA.")
    validate_source_revision(args.revision, directory.parent)
    terraform = os.environ.get("TERRAFORM_BIN") or shutil.which("terraform")
    if not terraform:
        raise ValueError("Terraform is required in PATH or TERRAFORM_BIN.")
    state = GitHubState.from_environment(directory=directory, environment=dict(os.environ))
    with state.session(initialize=False):
        return deploy_locked(args, directory, terraform, state.environment)


def deploy_locked(args: argparse.Namespace, directory: Path, terraform: str, environment: dict[str, str]) -> str:
    """Read pins and reconcile while one GitHub state lock covers all commands."""
    launcher = ["bash", str(directory / "start.sh")]
    checked([*launcher, "--init-only"], env=environment)
    raw_outputs = checked([terraform, f"-chdir={directory}", "output", "-json"], env=environment, capture=True)
    try:
        outputs = json.loads(raw_outputs)
        revisions = outputs.get("deployment_image_revisions", {}).get("value") or {}
        if not isinstance(revisions, dict):
            raise ValueError("Invalid deployment image revisions in Terraform state.")
    except (json.JSONDecodeError, AttributeError) as error:
        raise ValueError("Terraform outputs are invalid; refusing to replace existing image pins.") from error
    if should_skip_revision(revisions.get(args.image_variable), args.revision, directory.parent):
        print(f"Skipping stale build for {args.image_variable}; a newer commit is already selected for deployment.")
        return "skipped"
    with tempfile.TemporaryDirectory(prefix="phala-ci-", dir=environment.get("RUNNER_TEMP")) as temporary:
        state_file = Path(temporary) / "outputs.json"
        image_file = Path(temporary) / "images.tfvars.json"
        state_file.write_text(raw_outputs, encoding="utf-8")
        state_file.chmod(0o600)
        checked(
            [
                sys.executable,
                str(directory / "resolve_images.py"),
                "--output",
                str(image_file),
                "--from-state",
                str(state_file),
                "--set",
                f"{args.image_variable}={args.image_reference}",
                "--revision",
                f"{args.image_variable}={args.revision}",
                *(["--env-file", environment["PHALA_ENV_FILE"]] if environment.get("PHALA_ENV_FILE") else []),
            ],
            env=environment,
        )
        checked(launcher, env=dict(environment, PHALA_IMAGE_VARS_FILE=str(image_file)))
    print(f"Deployed {args.image_variable}: {args.image_reference}")
    summary = environment.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write(f"Phala deployment completed for `{args.image_variable}`.\n\n`{args.image_reference}`\n")
    return "deployed"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-variable", required=True, choices=sorted(IMAGE_VARIABLES))
    parser.add_argument("--image-reference", required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    os.umask(0o077)

    previous_sigterm_handler = signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        deploy(args, Path(__file__).resolve().parent)
    except (OSError, ValueError) as error:
        print(f"Phala deployment: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(
            "Phala deployment interrupted. Confirm all Terraform processes have stopped, "
            "then follow the GitHub state lock recovery instructions before retrying.",
            file=sys.stderr,
        )
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm_handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

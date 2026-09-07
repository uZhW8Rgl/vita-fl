"""Encrypted Terraform snapshots and a deployment lock in a GitHub branch.

The wrapper must hold ``session`` around the entire deployment and call ``sync``
after every Terraform mutation, including failed applies. Lock creation omits
the Contents API SHA (create only); updates/deletes use the observed SHA.
No lock expires automatically. Never run Terraform directly against this state.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import socket
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

STATE_FILE = "terraform.tfstate.gpg"
LOCK_FILE = "deployment-lock.json"
MAX_STATE_BYTES = 64 * 1024 * 1024


class StateError(ValueError):
    """A safe-to-display state coordination error, without response bodies."""


class APIError(StateError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"GitHub state request failed (HTTP {status}); check repository access and branch rules.")


def validate_state(data: bytes) -> dict[str, Any]:
    if len(data) > MAX_STATE_BYTES:
        raise StateError("Terraform state exceeds the supported 64 MiB limit.")
    try:
        state = json.loads(data)
    except (ValueError, UnicodeError) as error:
        raise StateError("Terraform state is not valid JSON.") from error
    if (
        not isinstance(state, dict)
        or state.get("version") != 4
        or not isinstance(state.get("lineage"), str)
        or not state["lineage"]
        or type(state.get("serial")) is not int
        or state["serial"] < 0
        or not isinstance(state.get("resources"), list)
        or not isinstance(state.get("outputs"), dict)
    ):
        raise StateError("Expected Terraform v4 state with lineage, serial, resources and outputs.")
    return state


def same_state(first: dict[str, Any], second: dict[str, Any]) -> bool:
    # Terraform rewrites its writer version on a no-op apply without advancing
    # the serial. This metadata difference is safe when local/CI versions vary.
    return {key: value for key, value in first.items() if key != "terraform_version"} == {
        key: value for key, value in second.items() if key != "terraform_version"
    }


def compatible(older: dict[str, Any], newer: dict[str, Any]) -> None:
    if older["lineage"] != newer["lineage"]:
        raise StateError("Local and GitHub state lineages differ; reconcile the states before deploying.")
    if older["serial"] > newer["serial"]:
        raise StateError("A newer Terraform state would be overwritten; recover it before deploying.")
    if older["serial"] == newer["serial"] and not same_state(older, newer):
        raise StateError("Terraform states differ at the same serial; reconcile them before deploying.")


def private_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def crypt(data: bytes, passphrase: str, *, decrypt: bool = False) -> bytes:
    """GPG receives the passphrase only on stdin, never in argv or logs."""
    with tempfile.TemporaryDirectory(prefix="phala-state-gpg-") as temporary:
        directory = Path(temporary)
        source = directory / "input"
        private_write(source, data)
        command = [
            "gpg",
            "--homedir",
            temporary,
            "--batch",
            "--yes",
            "--no-symkey-cache",
            "--pinentry-mode",
            "loopback",
            "--passphrase-fd",
            "0",
            "--output",
            "-",
        ]
        command += ["--decrypt"] if decrypt else ["--symmetric", "--cipher-algo", "AES256"]
        command.append(str(source))
        try:
            result = subprocess.run(command, input=(passphrase + "\n").encode(), capture_output=True, check=False)
        except OSError as error:
            raise StateError("GPG is required to encrypt and decrypt Terraform state.") from error
        if result.returncode:
            raise StateError(
                "Could not decrypt state; check PHALA_STATE_PASSPHRASE."
                if decrypt
                else "Could not encrypt Terraform state."
            )
        return result.stdout


class GitHubState:
    def __init__(
        self, directory: Path, environment: dict[str, str], repository: str, token: str, passphrase: str, branch: str
    ):
        self.directory = Path(directory)
        self.environment = environment
        self.repository = repository
        self.token = token
        self.passphrase = passphrase
        self.branch = branch
        self.path = self.directory / "terraform.tfstate"
        self.recovery_path = self.directory / "state-recovery.gpg"
        self.lock_id = environment.get("PHALA_STATE_LOCK_ID", "")
        self.base = f"/repos/{repository}"

    @classmethod
    def from_environment(cls, directory: Path, environment: dict[str, str]) -> GitHubState:
        repository = environment.get("PHALA_STATE_REPOSITORY", "")
        branch = environment.get("PHALA_STATE_BRANCH", "phala-state")
        passphrase = environment.get("PHALA_STATE_PASSPHRASE", "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise StateError("Set PHALA_STATE_REPOSITORY to the existing GitHub owner/repository.")
        if not re.fullmatch(r"phala-state(?:-[A-Za-z0-9_-]+)?", branch):
            raise StateError("PHALA_STATE_BRANCH must be phala-state or phala-state-<name>.")
        if len(passphrase) < 32 or "\n" in passphrase or "\r" in passphrase:
            raise StateError("PHALA_STATE_PASSPHRASE must have at least 32 characters and no line breaks.")
        token = next(
            (
                environment.get(name, "")
                for name in ("PHALA_STATE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
                if environment.get(name)
            ),
            "",
        )
        if not token:
            try:
                result = subprocess.run(
                    ["gh", "auth", "token", "--hostname", "github.com"],
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                )
                token = result.stdout.strip() if result.returncode == 0 else ""
            except OSError:
                pass
        if not token:
            raise StateError("Log in with gh auth login, or set GH_TOKEN with repository Contents write permission.")
        return cls(directory, environment, repository, token, passphrase, branch)

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        encoded = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(
            "https://api.github.com" + path,
            data=encoded,
            method=method,
            headers={
                "Accept": "application/vnd.github.object+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "vita-fl-state",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read(MAX_STATE_BYTES * 2 + 1)
            if len(raw) > MAX_STATE_BYTES * 2:
                raise StateError("GitHub state response exceeds the supported size limit.")
            result = json.loads(raw) if raw else {}
            if not isinstance(result, dict):
                raise StateError("GitHub returned an unexpected state response.")
            return result
        except urllib.error.HTTPError as error:
            raise APIError(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise StateError(
                "GitHub state request failed; verify connectivity. "
                "A pending deployment lock is never cleared automatically."
            ) from error
        except (ValueError, UnicodeError) as error:
            if isinstance(error, StateError):
                raise
            raise StateError("GitHub returned an invalid state response.") from error

    def ensure_branch(self, initialize: bool = False) -> None:
        metadata = self.request("GET", self.base)
        default = metadata.get("default_branch")
        if not isinstance(default, str) or not default or default == self.branch:
            raise StateError("The state branch must be separate from the repository default branch.")
        reference = f"{self.base}/git/ref/heads/{urllib.parse.quote(self.branch, safe='')}"
        try:
            self.request("GET", reference)
        except APIError as error:
            if error.status != 404:
                raise
            if not initialize:
                raise StateError("GitHub state branch is missing; run --init-github-state locally first.") from None
            source = self.request("GET", f"{self.base}/git/ref/heads/{urllib.parse.quote(default, safe='')}")
            self.request(
                "POST", f"{self.base}/git/refs", {"ref": f"refs/heads/{self.branch}", "sha": source["object"]["sha"]}
            )

    def file(self, name: str) -> tuple[str, bytes] | None:
        try:
            document = self.request(
                "GET", f"{self.base}/contents/{name}?ref={urllib.parse.quote(self.branch, safe='')}"
            )
        except APIError as error:
            if error.status != 404:
                raise
            # A deleted branch or lost repository permission must not look like
            # a missing file and authorize seeding an unrelated empty state.
            self.ensure_branch()
            return None
        sha = document.get("sha", "")
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha) or document.get("type") != "file":
            raise StateError("GitHub returned invalid state file metadata.")
        if document.get("encoding") == "none":
            document = self.request("GET", f"{self.base}/git/blobs/{sha}")
        try:
            if document.get("encoding") != "base64":
                raise ValueError
            decoded = base64.b64decode("".join(document["content"].split()), validate=True)
            if len(decoded) > MAX_STATE_BYTES:
                raise ValueError
        except (KeyError, TypeError, ValueError) as error:
            raise StateError("GitHub returned invalid or oversized state content.") from error
        return sha, decoded

    def put(self, name: str, content: bytes, sha: str | None = None) -> None:
        payload: dict[str, Any] = {
            "branch": self.branch,
            "message": "Update encrypted Phala deployment state [skip ci]",
            "content": base64.b64encode(content).decode(),
        }
        if sha is not None:
            payload["sha"] = sha
        self.request("PUT", f"{self.base}/contents/{name}", payload)

    def current_lock(self) -> tuple[str, dict[str, Any]] | None:
        entry = self.file(LOCK_FILE)
        if entry is None:
            return None
        try:
            record = json.loads(entry[1])
            if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
                raise ValueError
        except (ValueError, UnicodeError) as error:
            raise StateError("GitHub deployment lock is malformed; manual recovery is required.") from error
        return entry[0], record

    def require_lock(self) -> tuple[str, dict[str, Any]]:
        lock = self.current_lock()
        if not self.lock_id or lock is None or lock[1]["id"] != self.lock_id:
            raise StateError(
                "GitHub deployment lock is missing or owned by another deployment; refusing state changes."
            )
        return lock

    def acquire(self) -> None:
        self.lock_id = str(uuid.uuid4())
        record = {
            "id": self.lock_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "owner": self.environment.get("GITHUB_RUN_ID") or socket.gethostname(),
        }
        try:
            self.put(LOCK_FILE, json.dumps(record).encode())
        except APIError as error:
            if error.status not in (409, 422):
                raise
            lock = self.current_lock()
            identifier = lock[1]["id"] if lock else "unknown"
            raise StateError(
                f"Phala deployment is locked (ID {identifier}). "
                "Wait for its owner, or recover and explicitly unlock after a crash."
            ) from None

    def unlock(self, lock_id: str) -> None:
        self.ensure_branch()
        self.lock_id = lock_id
        sha, _ = self.require_lock()
        self.request(
            "DELETE",
            f"{self.base}/contents/{LOCK_FILE}",
            {
                "branch": self.branch,
                "sha": sha,
                "message": "Release Phala deployment lock [skip ci]",
            },
        )

    def remote_state(self) -> tuple[str, dict[str, Any]] | None:
        entry = self.file(STATE_FILE)
        if entry is None:
            return None
        return entry[0], self.decrypt_state(entry[1])

    def decrypt_state(self, content: bytes) -> dict[str, Any]:
        try:
            envelope = json.loads(crypt(content, self.passphrase, decrypt=True))
            if (
                not isinstance(envelope, dict)
                or envelope.get("format") != 1
                or envelope.get("repository") != self.repository
                or envelope.get("branch") != self.branch
            ):
                raise ValueError
            state = validate_state(json.dumps(envelope["state"]).encode())
        except (KeyError, ValueError, UnicodeError) as error:
            if isinstance(error, StateError):
                raise
            raise StateError("Encrypted state does not belong to this repository and branch.") from error
        return state

    def recover(self, path: Path, lock_id: str) -> None:
        """Explicitly restore an encrypted snapshot or raw state, then unlock.

        The caller must establish that the interrupted deployment has stopped.
        Existing local and remote states may never be silently rolled back.
        """
        self.ensure_branch()
        self.lock_id = lock_id
        self.require_lock()
        content = Path(path).read_bytes()
        if len(content) > MAX_STATE_BYTES:
            raise StateError("Recovery file exceeds the supported 64 MiB limit.")
        recovered = validate_state(content) if content.lstrip().startswith(b"{") else self.decrypt_state(content)
        remote = self.remote_state()
        if remote is not None:
            compatible(remote[1], recovered)
        if self.path.exists():
            local = self.path.read_bytes()
            compatible(validate_state(local), recovered)
            private_write(self.path.with_name("terraform.tfstate.before-recovery.backup"), local)
        private_write(self.path, json.dumps(recovered).encode())
        self.sync(allow_create=remote is None)
        self.unlock(lock_id)

    def encrypted(self, state: dict[str, Any]) -> bytes:
        envelope = {"format": 1, "repository": self.repository, "branch": self.branch, "state": state}
        result = crypt(json.dumps(envelope).encode(), self.passphrase)
        if len(result) > MAX_STATE_BYTES:
            raise StateError("Encrypted Terraform state exceeds the supported 64 MiB limit.")
        return result

    def restore(self, initialize: bool = False) -> None:
        self.require_lock()
        remote = self.remote_state()
        local = validate_state(self.path.read_bytes()) if self.path.exists() else None
        if remote is None:
            if not initialize:
                raise StateError("GitHub state snapshot is missing; seed it explicitly with --init-github-state.")
            if local is None:
                local = {
                    "version": 4,
                    "terraform_version": "1.9.8",
                    "serial": 0,
                    "lineage": str(uuid.uuid4()),
                    "outputs": {},
                    "resources": [],
                }
                private_write(self.path, json.dumps(local).encode())
            self.sync(allow_create=True)
            return
        if local is not None:
            compatible(local, remote[1])
            private_write(self.path.with_name("terraform.tfstate.before-github.backup"), self.path.read_bytes())
        private_write(self.path, json.dumps(remote[1]).encode())

    def sync(self, *, allow_create: bool = False) -> None:
        """Save a snapshot or retain the lock plus an encrypted recovery file."""
        if not self.path.is_file():
            raise StateError("Local Terraform state disappeared; the deployment lock is retained.")
        state = validate_state(self.path.read_bytes())
        encrypted = self.encrypted(state)
        private_write(self.recovery_path, encrypted)
        try:
            self.require_lock()
            remote = self.remote_state()
            if remote is None and not allow_create:
                raise StateError("Remote Terraform snapshot disappeared; refusing to recreate it implicitly.")
            if remote is not None:
                compatible(remote[1], state)
                if same_state(remote[1], state):
                    self.recovery_path.unlink(missing_ok=True)
                    return
            self.require_lock()
            self.put(STATE_FILE, encrypted, remote[0] if remote else None)
        except (StateError, OSError) as error:
            raise StateError(
                f"State upload failed; lock {self.lock_id} retained. "
                f"Preserve {self.recovery_path} and recover before unlocking. {error}"
            ) from error
        self.recovery_path.unlink(missing_ok=True)

    @contextlib.contextmanager
    def session(self, initialize: bool = False) -> Iterator[GitHubState]:
        self.ensure_branch(initialize=initialize)
        if self.environment.get("PHALA_STATE_LOCK_ID"):
            if initialize:
                raise StateError("Cannot initialize GitHub state from a nested deployment session.")
            self.require_lock()
            yield self
            return
        self.acquire()
        try:
            self.restore(initialize=initialize)
        except BaseException:
            # No Terraform operation has run. Preserve locks for ambiguous
            # failed uploads; ordinary validation failures can release safely.
            if not self.recovery_path.exists():
                self.unlock(self.lock_id)
            raise
        self.environment["PHALA_STATE_LOCK_ID"] = self.lock_id
        interrupted = False
        try:
            yield self
        except BaseException as error:
            interrupted = not isinstance(error, Exception)
            raise
        finally:
            self.environment.pop("PHALA_STATE_LOCK_ID", None)
            self.sync()
            if interrupted:
                raise StateError(
                    f"Deployment interrupted; lock {self.lock_id} retained. "
                    "Confirm all Terraform processes have stopped and recover state before unlocking."
                ) from None
            self.unlock(self.lock_id)

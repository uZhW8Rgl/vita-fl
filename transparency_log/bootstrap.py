"""Start or recover a persistent single-node SCITT-CCF service in virtual mode."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

CONSTITUTION_NAMES = ("validate.js", "apply.js", "resolve.js", "actions.js", "scitt.js")


def _atomic_write(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def _ensure_member_credentials(data_dir: Path) -> None:
    from pyscitt import crypto

    member_key = data_dir / "member0_privk.pem"
    member_cert = data_dir / "member0_cert.pem"
    encryption_key = data_dir / "member0_enc_privk.pem"
    encryption_public_key = data_dir / "member0_enc_pubk.pem"
    paths = (member_key, member_cert, encryption_key, encryption_public_key)
    if all(path.is_file() for path in paths):
        return
    if any(path.exists() for path in paths):
        raise RuntimeError("incomplete persisted SCITT member credentials")

    identity_private, _identity_public = crypto.generate_ec_keypair("P-384")
    certificate = crypto.generate_cert(identity_private, cn="master-thesis-scitt-member0")
    recovery_private, recovery_public = crypto.generate_rsa_keypair()
    _atomic_write(member_key, identity_private)
    _atomic_write(member_cert, certificate)
    _atomic_write(encryption_key, recovery_private)
    _atomic_write(encryption_public_key, recovery_public)
    for path in (member_key, encryption_key):
        path.chmod(0o600)


def _copy_constitution(data_dir: Path) -> list[str]:
    installed = Path("/opt/scitt/share/scitt/constitution")
    target = data_dir / "constitution"
    target.mkdir(parents=True, exist_ok=True)
    result: list[str] = []
    for name in CONSTITUTION_NAMES:
        source = installed / name
        destination = target / name
        if not destination.exists():
            shutil.copyfile(source, destination)
        result.append(str(destination))
    return result


def build_config(data_dir: Path, host: str, port: int, recover: bool) -> dict[str, Any]:
    service_cert = data_dir / "service_cert.pem"
    config: dict[str, Any] = {
        "network": {
            "node_to_node_interface": {"bind_address": "0.0.0.0:8001"},
            "rpc_interfaces": {
                "rpc": {
                    "bind_address": f"{host}:{port}",
                    "published_address": f"localhost:{port}",
                    "http_configuration": {"max_body_size": "2MB"},
                }
            },
        },
        "node_certificate": {
            "subject_alt_names": [
                "iPAddress:0.0.0.0",
                "iPAddress:127.0.0.1",
                "dNSName:localhost",
                "dNSName:transparency-log",
            ]
        },
        "logging": {"format": "Json", "host_level": "Info"},
    }
    if recover:
        config["command"] = {
            "type": "Recover",
            "service_certificate_file": str(service_cert),
            "recover": {
                "initial_service_certificate_validity_days": 1,
                "previous_service_identity_file": str(data_dir / "previous_service_cert.pem"),
            },
        }
    else:
        config["command"] = {
            "type": "Start",
            "service_certificate_file": str(service_cert),
            "start": {
                "constitution_files": _copy_constitution(data_dir),
                "members": [
                    {
                        "certificate_file": str(data_dir / "member0_cert.pem"),
                        "encryption_public_key_file": str(data_dir / "member0_enc_pubk.pem"),
                    }
                ],
                "cose_signatures": {
                    "issuer": f"localhost:{port}",
                    "subject": "scitt.ccf.signature.v1",
                },
            },
        }
    return config


def _wait_until_reachable(url: str, process: subprocess.Popen[bytes], timeout: int = 90) -> None:
    import ssl

    context = ssl._create_unverified_context()  # noqa: SLF001 - virtual-mode self-signed service
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"cchost exited during startup with status {process.returncode}")
        try:
            with urllib.request.urlopen(f"{url}/node/network", context=context, timeout=2):
                return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(1)
    raise RuntimeError("SCITT-CCF did not become reachable before the startup deadline")


def _client(data_dir: Path, url: str):
    from pyscitt.client import Client
    from pyscitt.local_key_sign_client import LocalKeySignClient

    auth = LocalKeySignClient(
        (data_dir / "member0_cert.pem").read_text(encoding="utf-8"),
        (data_dir / "member0_privk.pem").read_text(encoding="utf-8"),
    )
    return Client(url, development=True, member_auth=auth)


def _write_trust_store(data_dir: Path, client: Any) -> None:
    import cbor2

    trust_store = data_dir / "trust-store"
    trust_store.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 30
    while True:
        try:
            keys = client.get_scitt_keys()
            (trust_store / "scitt-keys.cbor").write_bytes(cbor2.dumps(keys))
            break
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def _configure_new_service(data_dir: Path, url: str) -> None:
    from pyscitt.cli.governance import setup_local_development

    client = _client(data_dir, url)
    setup_local_development(client, None)
    _write_trust_store(data_dir, client)


def _recover_service(data_dir: Path, url: str) -> None:
    client = _client(data_dir, url)
    client.governance.recover_service((data_dir / "member0_enc_privk.pem").read_text(encoding="utf-8"))
    _write_trust_store(data_dir, client)


def main() -> None:
    data_dir = Path(os.environ.get("SCITT_DATA_DIR", "/data")).resolve()
    host = os.environ.get("SCITT_HOST", "0.0.0.0")
    port = int(os.environ.get("SCITT_PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("SCITT_PORT must be between 1 and 65535")
    data_dir.mkdir(parents=True, exist_ok=True)
    node_dir = data_dir / "node"
    _ensure_member_credentials(data_dir)

    initialized = (data_dir / "initialized").is_file()
    if not initialized:
        # A failed first-time governance setup is not a usable ledger. Keep the
        # member identity, but discard only the incomplete CCF node state so a
        # restart can safely repeat initialization.
        shutil.rmtree(node_dir, ignore_errors=True)
        for path in (
            data_dir / "service_cert.pem",
            data_dir / "previous_service_cert.pem",
            data_dir / "trust-store",
        ):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
    node_dir.mkdir(parents=True, exist_ok=True)
    # CCF leaves this advisory file behind after a graceful shutdown. The
    # bootstrap owns the only cchost child process in this container, so it is
    # safe to remove before starting that child again.
    (node_dir / "my_node.pid").unlink(missing_ok=True)
    if initialized:
        service_cert = data_dir / "service_cert.pem"
        previous_service_cert = data_dir / "previous_service_cert.pem"
        if service_cert.is_file():
            service_cert.replace(previous_service_cert)
        elif not previous_service_cert.is_file():
            raise RuntimeError("persisted SCITT state has no service certificate")

    config_path = data_dir / "config.json"
    _atomic_write(config_path, json.dumps(build_config(data_dir, host, port, initialized), indent=2) + "\n")
    process = subprocess.Popen(["cchost", "--config", str(config_path)], cwd=node_dir)

    def stop(_signum: int, _frame: object) -> None:
        if process.poll() is None:
            process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    url = f"https://127.0.0.1:{port}"
    try:
        _wait_until_reachable(url, process)
        if initialized:
            _recover_service(data_dir, url)
            print("Recovered persistent SCITT-CCF transparency service", flush=True)
        else:
            _configure_new_service(data_dir, url)
            _atomic_write(data_dir / "initialized", "scitt-ccf-ledger 0.18.1\n")
            print("Initialized persistent SCITT-CCF transparency service", flush=True)
        raise SystemExit(process.wait())
    except BaseException:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
        raise


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"SCITT bootstrap failed: {exc}", file=sys.stderr, flush=True)
        raise

"""Dynamic Phala worker lifecycle using the fixed W0-W19 identity pool."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol

MAX_WORKERS = 20
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
PRIVATE_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
PRIVATE_KEY_IN_TEXT_RE = re.compile(r"0x[0-9a-fA-F]{64}")
PEM_IN_TEXT_RE = re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL)
PHALA_API_KEY_IN_TEXT_RE = re.compile(r"phak_[A-Za-z0-9_-]+")


class WorkerConfigurationError(ValueError):
    """The fixed worker inventory or deployment configuration is invalid."""


class WorkerProvisioningError(RuntimeError):
    """Terraform could not establish the requested worker set."""


def redact_terraform_output(value: str) -> str:
    redacted = PEM_IN_TEXT_RE.sub("<redacted-pem>", value)
    redacted = PRIVATE_KEY_IN_TEXT_RE.sub("<redacted-private-key>", redacted)
    return PHALA_API_KEY_IN_TEXT_RE.sub("<redacted-phala-api-key>", redacted)


@dataclass(frozen=True)
class WorkerIdentity:
    slot: int
    account_address: str
    private_key: str
    rsa_private_key: str
    rsa_public_key: str

    @property
    def key(self) -> str:
        return f"worker{self.slot}"

    @property
    def app_name(self) -> str:
        return f"master-thesis-dfl-worker-{self.slot}"

    def terraform_value(self) -> dict[str, Any]:
        return {
            "app_name": self.app_name,
            "account_address": self.account_address,
            "private_key": self.private_key,
            "rsa_private_key": self.rsa_private_key,
            "rsa_public_key": self.rsa_public_key,
            "device_id": self.slot,
        }


@dataclass(frozen=True)
class WorkerDeploymentConfig:
    phala_cloud_api_key: str
    worker_image: str
    rpc_url: str
    kubo_api_url: str
    kubo_gateway_url: str
    telemetry_url: str
    region: str = "US-WEST-1"
    os_image: str = "dstack-dev-0.5.7"
    client_limit: int = 2
    epoch: int = 1
    round: int = 5
    model_submission_deadline_ms: int = 20_000
    gm_update_timeout_ms: int = 30_000
    gm_update_timeout_loops: int = 2
    aggregation_update_estimate_ms: int = 30_000
    gm_update_poll_ms: int = 5_000
    dataset_name: str = "chestmnist"
    train_images_src: str = "/dfl/config/training_data/train-images-0.idx3-ubyte"
    train_labels_src: str = "/dfl/config/training_data/train-labels-0.idx1-ubyte"
    test_images_src: str = "/dfl/config/test_data/t10k-images.idx3-ubyte"
    test_labels_src: str = "/dfl/config/test_data/t10k-labels.idx1-ubyte"
    train_data_src: str = "/dfl/config/chestmnist/training_data/train-data-0.npz"
    test_data_src: str = "/dfl/config/chestmnist/test_data/test-data.npz"
    python_service_url: str = "http://127.0.0.1:8000"
    public_ip: str = "127.0.0.1"
    msg_broker_ip: str = "127.0.0.1"

    def terraform_values(self) -> dict[str, Any]:
        values = asdict(self)
        return values


class WorkerTerraformRunner(Protocol):
    def configure(self, training_config: dict[str, int]) -> None: ...

    def apply(self, workers: dict[str, dict[str, Any]]) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...


class RegistrationChallengeIssuer(Protocol):
    def allow(self, account_address: str) -> None: ...

    def revoke(self, account_address: str) -> None: ...


class DeviceRegistryChallengeIssuer:
    """Open one-shot registration challenges without authorizing a device."""

    def __init__(self, rpc_url: str, kubo_api_url: str, owner_private_key: str) -> None:
        from eth_account import Account
        from eth_utils import keccak, to_checksum_address

        self.rpc_url = rpc_url.rstrip("/")
        self.kubo_api_url = kubo_api_url.rstrip("/")
        self.account = Account.from_key(owner_private_key)
        self.keccak = keccak
        self.to_checksum_address = to_checksum_address
        self._request_id = 0

    def _rpc(self, method: str, params: list[Any]) -> Any:
        self._request_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}).encode()
        request = urllib.request.Request(
            self.rpc_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
        if payload.get("error"):
            raise WorkerProvisioningError(f"JSON-RPC {method} failed")
        return payload.get("result")

    def _registry_address(self) -> str:
        url = self.kubo_api_url + "/api/v0/files/read?arg=" + urllib.parse.quote("/runtime/contracts.json", safe="")
        request = urllib.request.Request(url, method="POST")
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read())
        address = str(payload.get("registry_address", ""))
        if not ADDRESS_RE.fullmatch(address):
            raise WorkerProvisioningError("runtime contract manifest has no valid registry address")
        return self.to_checksum_address(address)

    def _calldata(self, account_address: str, allowed: bool) -> str:
        selector = self.keccak(text="setRegistrationAllowed(address,bool)")[:4]
        address_word = bytes.fromhex(account_address[2:]).rjust(32, b"\0")
        allowed_word = (1 if allowed else 0).to_bytes(32, "big")
        return "0x" + (selector + address_word + allowed_word).hex()

    def _send(self, account_address: str, allowed: bool) -> None:
        registry_address = self._registry_address()
        nonce = int(self._rpc("eth_getTransactionCount", [self.account.address, "pending"]), 16)
        chain_id = int(self._rpc("eth_chainId", []), 16)
        gas_price = int(self._rpc("eth_gasPrice", []), 16)
        transaction = {
            "to": registry_address,
            "data": self._calldata(account_address, allowed),
            "nonce": nonce,
            "chainId": chain_id,
            "gasPrice": gas_price,
        }
        estimate_payload = {
            **transaction,
            "from": self.account.address,
            "gasPrice": hex(gas_price),
            "nonce": hex(nonce),
            "chainId": hex(chain_id),
        }
        estimate_payload.pop("to", None)
        estimate_payload["to"] = registry_address
        gas = int(self._rpc("eth_estimateGas", [estimate_payload]), 16)
        signed = self.account.sign_transaction({**transaction, "gas": gas + gas // 5})
        tx_hash = self._rpc("eth_sendRawTransaction", [signed.raw_transaction.hex()])
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt is not None:
                if int(receipt.get("status", "0x0"), 16) != 1:
                    raise WorkerProvisioningError("registration challenge transaction reverted")
                return
            time.sleep(1)
        raise WorkerProvisioningError("registration challenge transaction timed out")

    def allow(self, account_address: str) -> None:
        self._send(account_address, True)

    def revoke(self, account_address: str) -> None:
        self._send(account_address, False)


def _pem(value: Any, name: str) -> str:
    text = str(value or "").replace("\\n", "\n").strip()
    if not text.startswith("-----BEGIN ") or not text.endswith("-----"):
        raise WorkerConfigurationError(f"{name} is not a PEM value")
    return text


def load_worker_inventory(raw: str, *, maximum: int = MAX_WORKERS) -> tuple[WorkerIdentity, ...]:
    """Decode and validate an ordered, fixed worker identity pool."""

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WorkerConfigurationError(f"DYNAMIC_WORKER_INVENTORY is not valid JSON: {exc}") from exc

    records: list[dict[str, Any]]
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        records = []
        for key, value in payload.items():
            if not isinstance(value, dict):
                raise WorkerConfigurationError(f"inventory entry {key!r} must be an object")
            record = dict(value)
            record.setdefault("slot", int(str(key).removeprefix("worker")))
            records.append(record)
    else:
        raise WorkerConfigurationError("DYNAMIC_WORKER_INVENTORY must be a list or object")

    if not records:
        raise WorkerConfigurationError("worker inventory is empty")
    if len(records) > maximum:
        raise WorkerConfigurationError(f"worker inventory exceeds the maximum of {maximum}")

    identities: list[WorkerIdentity] = []
    for record in records:
        if not isinstance(record, dict):
            raise WorkerConfigurationError("every worker inventory entry must be an object")
        try:
            slot = int(record["slot"])
        except (KeyError, TypeError, ValueError) as exc:
            raise WorkerConfigurationError("every worker inventory entry needs an integer slot") from exc
        address = str(record.get("account_address", "")).strip()
        private_key = str(record.get("private_key", "")).strip()
        if not ADDRESS_RE.fullmatch(address):
            raise WorkerConfigurationError(f"worker{slot} has an invalid account address")
        if not PRIVATE_KEY_RE.fullmatch(private_key):
            raise WorkerConfigurationError(f"worker{slot} has an invalid Ethereum private key")
        identities.append(
            WorkerIdentity(
                slot=slot,
                account_address=address,
                private_key=private_key,
                rsa_private_key=_pem(record.get("rsa_private_key"), f"worker{slot} RSA private key"),
                rsa_public_key=_pem(record.get("rsa_public_key"), f"worker{slot} RSA public key"),
            )
        )

    identities.sort(key=lambda identity: identity.slot)
    expected_slots = list(range(len(identities)))
    actual_slots = [identity.slot for identity in identities]
    if actual_slots != expected_slots:
        raise WorkerConfigurationError(
            f"worker slots must be contiguous from 0; expected {expected_slots}, got {actual_slots}"
        )
    return tuple(identities)


class SubprocessTerraformRunner:
    """Run the isolated dynamic-workers Terraform root with an ephemeral state."""

    def __init__(
        self,
        config: WorkerDeploymentConfig,
        *,
        module_dir: Path,
        state_dir: Path,
        terraform_bin: str = "terraform",
    ) -> None:
        self.config = config
        self.module_dir = module_dir.resolve()
        self.state_dir = state_dir.resolve()
        self.terraform_bin = terraform_bin
        self.state_path = self.state_dir / "terraform.tfstate"
        self.tfvars_path = self.state_dir / "workers.auto.tfvars.json"
        self.tf_data_dir = self.state_dir / ".terraform"

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["TF_DATA_DIR"] = str(self.tf_data_dir)
        environment["TF_IN_AUTOMATION"] = "1"
        return environment

    def configure(self, training_config: dict[str, int]) -> None:
        self.config = replace(
            self.config,
            round=training_config.get("rounds", self.config.round),
            epoch=training_config.get("epoch", self.config.epoch),
            client_limit=training_config.get("client_limit", self.config.client_limit),
        )

    def _run(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        command = [self.terraform_bin, f"-chdir={self.module_dir}", *arguments]
        result = subprocess.run(
            command,
            env=self._environment(),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            output = redact_terraform_output("\n".join(part for part in (result.stdout, result.stderr) if part).strip())
            if not output:
                output = "Terraform produced no diagnostic output"
            output = output[-4000:]
            print(f"Dynamic worker Terraform failed:\n{output}", file=sys.stderr, flush=True)
            raise WorkerProvisioningError(
                f"Terraform failed with exit code {result.returncode}: {output}"
            )
        return result

    def _write_tfvars(self, workers: dict[str, dict[str, Any]]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = self.config.terraform_values()
        payload["workers"] = workers
        temporary = self.tfvars_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.tfvars_path)

    def apply(self, workers: dict[str, dict[str, Any]]) -> dict[str, Any]:
        self._write_tfvars(workers)
        self._run("init", "-input=false", "-no-color")
        self._run(
            "apply",
            "-input=false",
            "-auto-approve",
            "-no-color",
            f"-state={self.state_path}",
            f"-var-file={self.tfvars_path}",
        )
        return self.status()

    def status(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {}
        result = self._run("output", "-json", f"-state={self.state_path}")
        payload = json.loads(result.stdout or "{}")
        workers = payload.get("workers", {}).get("value", {})
        if not isinstance(workers, dict):
            raise WorkerProvisioningError("Terraform worker output has an invalid shape")
        return workers


class PhalaWorkerController:
    """Scale only the first N identities of a fixed and validated pool."""

    def __init__(
        self,
        inventory: tuple[WorkerIdentity, ...],
        runner: WorkerTerraformRunner,
        challenge_issuer: RegistrationChallengeIssuer | None = None,
    ) -> None:
        if not inventory:
            raise WorkerConfigurationError("worker inventory is empty")
        self.inventory = inventory
        self.runner = runner
        self.challenge_issuer = challenge_issuer
        self._lock = threading.Lock()

    @property
    def maximum(self) -> int:
        return len(self.inventory)

    def scale(self, worker_count: int, training_config: dict[str, int] | None = None) -> dict[str, Any]:
        if isinstance(worker_count, bool) or not isinstance(worker_count, int):
            raise WorkerConfigurationError("worker_count must be an integer")
        if not 0 <= worker_count <= self.maximum:
            raise WorkerConfigurationError(f"worker_count must be between 0 and {self.maximum}")
        selected = {identity.key: identity.terraform_value() for identity in self.inventory[:worker_count]}
        with self._lock:
            if training_config is not None:
                self.runner.configure(training_config)
            current = self.runner.status()
            added = [identity for identity in self.inventory[:worker_count] if identity.key not in current]
            if added and self.challenge_issuer is None:
                raise WorkerConfigurationError("registration challenge issuer is not configured")
            for identity in added:
                self.challenge_issuer.allow(identity.account_address)
            try:
                deployments = self.runner.apply(selected)
            except Exception:
                for identity in added:
                    try:
                        self.challenge_issuer.revoke(identity.account_address)
                    except Exception:
                        pass
                raise
        return self._public_status(worker_count, deployments)

    def status(self) -> dict[str, Any]:
        with self._lock:
            deployments = self.runner.status()
        return self._public_status(len(deployments), deployments)

    def _public_status(self, desired: int, deployments: dict[str, Any]) -> dict[str, Any]:
        public_workers = []
        for identity in self.inventory:
            deployment = deployments.get(identity.key)
            if deployment is None:
                continue
            public_workers.append(
                {
                    "slot": identity.slot,
                    "worker": identity.key,
                    "account_address": identity.account_address,
                    **deployment,
                }
            )
        return {
            "mode": "phala",
            "desired_worker_count": desired,
            "deployed_worker_count": len(public_workers),
            "maximum_worker_count": self.maximum,
            "workers": public_workers,
        }


def controller_from_environment() -> PhalaWorkerController:
    """Construct the production controller without ever logging its secrets."""

    required = {
        "DYNAMIC_WORKER_INVENTORY": os.environ.get("DYNAMIC_WORKER_INVENTORY", ""),
        "PHALA_CLOUD_API_KEY": os.environ.get("PHALA_CLOUD_API_KEY", ""),
        "DYNAMIC_WORKER_RPC_URL": os.environ.get("DYNAMIC_WORKER_RPC_URL", ""),
        "DYNAMIC_WORKER_KUBO_API_URL": os.environ.get("DYNAMIC_WORKER_KUBO_API_URL", ""),
        "DYNAMIC_WORKER_KUBO_GATEWAY_URL": os.environ.get("DYNAMIC_WORKER_KUBO_GATEWAY_URL", ""),
        "DYNAMIC_WORKER_IMAGE": os.environ.get("DYNAMIC_WORKER_IMAGE", ""),
        "REGISTRATION_OWNER_PRIVATE_KEY": os.environ.get("REGISTRATION_OWNER_PRIVATE_KEY", ""),
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise WorkerConfigurationError(f"missing dynamic worker configuration: {', '.join(missing)}")

    def integer(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, str(default)))
        except ValueError as exc:
            raise WorkerConfigurationError(f"{name} must be an integer") from exc

    inventory = load_worker_inventory(required["DYNAMIC_WORKER_INVENTORY"])
    telemetry_url = os.environ.get("DYNAMIC_WORKER_TELEMETRY_URL", "").strip()
    if not telemetry_url:
        parsed_rpc_url = urllib.parse.urlsplit(required["DYNAMIC_WORKER_RPC_URL"])
        telemetry_host = re.sub(r"-[0-9]+(?=\.dstack-)", "-8091", parsed_rpc_url.hostname or "")
        if not telemetry_host:
            raise WorkerConfigurationError("could not derive the control API telemetry URL")
        telemetry_url = urllib.parse.urlunsplit(
            (parsed_rpc_url.scheme or "https", telemetry_host, "", "", "")
        )
    config = WorkerDeploymentConfig(
        phala_cloud_api_key=required["PHALA_CLOUD_API_KEY"],
        worker_image=required["DYNAMIC_WORKER_IMAGE"],
        rpc_url=required["DYNAMIC_WORKER_RPC_URL"],
        kubo_api_url=required["DYNAMIC_WORKER_KUBO_API_URL"],
        kubo_gateway_url=required["DYNAMIC_WORKER_KUBO_GATEWAY_URL"],
        telemetry_url=telemetry_url,
        region=os.environ.get("PHALA_REGION", "US-WEST-1"),
        os_image=os.environ.get("PHALA_OS_IMAGE", "dstack-dev-0.5.7"),
        client_limit=integer("CLIENT_LIMIT", 2),
        epoch=integer("EPOCH", 1),
        round=integer("ROUND", 5),
        model_submission_deadline_ms=integer("MODEL_SUBMISSION_DEADLINE_MS", 20_000),
        gm_update_timeout_ms=integer("GM_UPDATE_TIMEOUT_MS", 30_000),
        gm_update_timeout_loops=integer("GM_UPDATE_TIMEOUT_LOOPS", 2),
        aggregation_update_estimate_ms=integer("AGGREGATION_UPDATE_ESTIMATE_MS", 30_000),
        gm_update_poll_ms=integer("GM_UPDATE_POLL_MS", 5_000),
        dataset_name=os.environ.get("DATASET_NAME", "chestmnist"),
        train_images_src=os.environ.get("TRAIN_IMAGES_SRC", "/dfl/config/training_data/train-images-0.idx3-ubyte"),
        train_labels_src=os.environ.get("TRAIN_LABELS_SRC", "/dfl/config/training_data/train-labels-0.idx1-ubyte"),
        test_images_src=os.environ.get("TEST_IMAGES_SRC", "/dfl/config/test_data/t10k-images.idx3-ubyte"),
        test_labels_src=os.environ.get("TEST_LABELS_SRC", "/dfl/config/test_data/t10k-labels.idx1-ubyte"),
        train_data_src=os.environ.get("TRAIN_DATA_SRC", "/dfl/config/chestmnist/training_data/train-data-0.npz"),
        test_data_src=os.environ.get("TEST_DATA_SRC", "/dfl/config/chestmnist/test_data/test-data.npz"),
        python_service_url=os.environ.get("PYTHON_SERVICE_URL", "http://127.0.0.1:8000"),
        public_ip=os.environ.get("PUBLIC_IP", "127.0.0.1"),
        msg_broker_ip=os.environ.get("MSG_BROKER_IP", "127.0.0.1"),
    )
    repository_root = Path(__file__).resolve().parents[1]
    module_dir = Path(os.environ.get("DYNAMIC_WORKER_TERRAFORM_MODULE", repository_root / "phala/dynamic-workers"))
    state_dir = Path(os.environ.get("DYNAMIC_WORKER_STATE_DIR", "/run/master-thesis/dynamic-workers"))
    runner = SubprocessTerraformRunner(
        config,
        module_dir=module_dir,
        state_dir=state_dir,
        terraform_bin=os.environ.get("TERRAFORM_BIN", "terraform"),
    )
    challenge_issuer = DeviceRegistryChallengeIssuer(
        config.rpc_url,
        config.kubo_api_url,
        required["REGISTRATION_OWNER_PRIVATE_KEY"],
    )
    return PhalaWorkerController(inventory, runner, challenge_issuer)

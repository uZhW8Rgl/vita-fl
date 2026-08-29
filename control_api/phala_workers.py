"""Dynamic Phala worker lifecycle using the fixed W0-W499 identity pool."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import urllib.parse
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Protocol

MAX_WORKERS = 500
INVENTORY_CHUNK_PREFIX = "DYNAMIC_WORKER_INVENTORY_"
INVENTORY_CHUNK_RE = re.compile(r"^DYNAMIC_WORKER_INVENTORY_([0-9]{3})$")
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
    expected_device_registry_address: str
    expected_aggregator_address: str
    expected_gm_storage_address: str
    expected_medical_signer_registry_address: str
    expected_chain_id: int
    eth_eur_price: str = "3000"
    eth_usd_price: str = ""
    exchange_rate_source: str = "manual_configuration"
    exchange_rate_timestamp_utc: str = ""
    reference_mainnet_gas_price_gwei: str = ""
    reference_gas_price_source: str = ""
    reference_gas_price_timestamp_utc: str = ""
    region: str = "US-WEST-1"
    os_image: str = "dstack-dev-0.5.7"
    epoch: int = 1
    round: int = 5
    dfl_model_seed: str = "42"
    dfl_train_seed: str = "42"
    dfl_train_optimizer: str = "adamw"
    dfl_train_learning_rate: str = "0.003"
    dfl_train_lr_schedule: str = "constant"
    dfl_train_lr_decay_start_round: str = "20"
    dfl_train_lr_final_factor: str = "0.25"
    dfl_train_weight_decay: str = "0.0001"
    dfl_grad_clip_norm: str = "5"
    dfl_pos_weight_cap: str = "10"
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
    sello_required: bool = False
    sello_scitt_url: str = ""
    sello_tee_service_signing_seed: str = ""
    sello_token_issuer_public_key: str = ""

    def terraform_values(self) -> dict[str, Any]:
        values = asdict(self)
        return values


class WorkerTerraformRunner(Protocol):
    def configure(self, training_config: dict[str, int]) -> None: ...

    def current_training_config(self) -> dict[str, int]: ...

    def apply(self, workers: dict[str, dict[str, Any]]) -> dict[str, Any]: ...

    def status(self) -> dict[str, Any]: ...


def dynamic_worker_inventory_json_from_environment(
    environment: Mapping[str, str] | None = None,
) -> str:
    """Reassemble the legacy inventory or its ordered, size-bounded chunks."""

    values = os.environ if environment is None else environment
    legacy = str(values.get("DYNAMIC_WORKER_INVENTORY", "")).strip()
    malformed_names = sorted(
        name
        for name in values
        if name.startswith(INVENTORY_CHUNK_PREFIX) and INVENTORY_CHUNK_RE.fullmatch(name) is None
    )
    if malformed_names:
        raise WorkerConfigurationError(
            f"invalid dynamic worker inventory chunk name: {malformed_names[0]}"
        )

    indexed_chunks = sorted(
        (
            int(match.group(1)),
            name,
            str(value),
        )
        for name, value in values.items()
        if (match := INVENTORY_CHUNK_RE.fullmatch(name)) is not None
    )
    if legacy and indexed_chunks:
        raise WorkerConfigurationError(
            "configure either DYNAMIC_WORKER_INVENTORY or chunked inventory entries, not both"
        )
    if not indexed_chunks:
        return legacy

    indices = [index for index, _name, _value in indexed_chunks]
    if indices != list(range(len(indices))):
        raise WorkerConfigurationError(
            "dynamic worker inventory chunk indices must be contiguous from 000"
        )

    records: list[Any] = []
    for _index, name, value in indexed_chunks:
        try:
            chunk = json.loads(value)
        except json.JSONDecodeError as exc:
            raise WorkerConfigurationError(f"{name} is not valid JSON: {exc}") from exc
        if not isinstance(chunk, list):
            raise WorkerConfigurationError(f"{name} must contain a JSON array")
        records.extend(chunk)
    return json.dumps(records, separators=(",", ":"))


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
        )

    def current_training_config(self) -> dict[str, int]:
        if self.tfvars_path.is_file():
            try:
                persisted = json.loads(self.tfvars_path.read_text(encoding="utf-8"))
                return {
                    "rounds": int(persisted["round"]),
                    "epoch": int(persisted["epoch"]),
                }
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                pass
        return {
            "rounds": self.config.round,
            "epoch": self.config.epoch,
        }

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
            raise WorkerProvisioningError(f"Terraform failed with exit code {result.returncode}: {output}")
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
    ) -> None:
        if not inventory:
            raise WorkerConfigurationError("worker inventory is empty")
        self.inventory = inventory
        self.runner = runner
        self._lock = threading.Lock()

    @property
    def maximum(self) -> int:
        return len(self.inventory)

    def _validated_training_request(
        self,
        worker_count: int,
        training_config: dict[str, int] | None,
    ) -> dict[str, int] | None:
        if isinstance(worker_count, bool) or not isinstance(worker_count, int):
            raise WorkerConfigurationError("worker_count must be an integer")
        if not 0 <= worker_count <= self.maximum:
            raise WorkerConfigurationError(f"worker_count must be between 0 and {self.maximum}")
        if training_config is None:
            return None
        configured = self.runner.current_training_config()
        requested = {
            "rounds": int(training_config.get("rounds", configured.get("rounds", 1))),
            "epoch": int(training_config.get("epoch", configured.get("epoch", 1))),
        }
        current = self.runner.status()
        if current and requested != configured:
            raise WorkerConfigurationError(
                "training configuration cannot mutate an attested worker compose; "
                "reset the workers and contract runtime before starting a new configuration"
            )
        return requested

    def preflight_scale(
        self,
        worker_count: int,
        training_config: dict[str, int] | None = None,
    ) -> None:
        with self._lock:
            self._validated_training_request(worker_count, training_config)

    def selected_account_addresses(self, worker_count: int) -> list[str]:
        """Return the exact ordered identities that a subsequent scale will use."""
        with self._lock:
            self._validated_training_request(worker_count, None)
            return [
                identity.account_address
                for identity in self.inventory[:worker_count]
            ]

    def scale(self, worker_count: int, training_config: dict[str, int] | None = None) -> dict[str, Any]:
        with self._lock:
            requested = self._validated_training_request(worker_count, training_config)
            selected = {
                identity.key: identity.terraform_value()
                for identity in self.inventory[:worker_count]
            }
            if requested is not None:
                self.runner.configure(requested)
            deployments = self.runner.apply(selected)
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

    inventory_json = dynamic_worker_inventory_json_from_environment()
    required = {
        "PHALA_CLOUD_API_KEY": os.environ.get("PHALA_CLOUD_API_KEY", ""),
        "DYNAMIC_WORKER_RPC_URL": os.environ.get("DYNAMIC_WORKER_RPC_URL", ""),
        "DYNAMIC_WORKER_KUBO_API_URL": os.environ.get("DYNAMIC_WORKER_KUBO_API_URL", ""),
        "DYNAMIC_WORKER_KUBO_GATEWAY_URL": os.environ.get("DYNAMIC_WORKER_KUBO_GATEWAY_URL", ""),
        "DYNAMIC_WORKER_IMAGE": os.environ.get("DYNAMIC_WORKER_IMAGE", ""),
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise WorkerConfigurationError(f"missing dynamic worker configuration: {', '.join(missing)}")
    if not inventory_json:
        raise WorkerConfigurationError("missing dynamic worker configuration: DYNAMIC_WORKER_INVENTORY")

    def integer(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, str(default)))
        except ValueError as exc:
            raise WorkerConfigurationError(f"{name} must be an integer") from exc

    inventory = load_worker_inventory(inventory_json)
    telemetry_url = os.environ.get("DYNAMIC_WORKER_TELEMETRY_URL", "").strip()
    if not telemetry_url:
        parsed_rpc_url = urllib.parse.urlsplit(required["DYNAMIC_WORKER_RPC_URL"])
        telemetry_host = re.sub(r"-[0-9]+(?=\.dstack-)", "-8091", parsed_rpc_url.hostname or "")
        if not telemetry_host:
            raise WorkerConfigurationError("could not derive the control API telemetry URL")
        telemetry_url = urllib.parse.urlunsplit((parsed_rpc_url.scheme or "https", telemetry_host, "", "", ""))
    config = WorkerDeploymentConfig(
        phala_cloud_api_key=required["PHALA_CLOUD_API_KEY"],
        worker_image=required["DYNAMIC_WORKER_IMAGE"],
        rpc_url=required["DYNAMIC_WORKER_RPC_URL"],
        kubo_api_url=required["DYNAMIC_WORKER_KUBO_API_URL"],
        kubo_gateway_url=required["DYNAMIC_WORKER_KUBO_GATEWAY_URL"],
        telemetry_url=telemetry_url,
        expected_device_registry_address=os.environ.get(
            "DYNAMIC_WORKER_EXPECTED_DEVICE_REGISTRY_ADDRESS",
            "0x5FbDB2315678afecb367f032d93F642f64180aa3",
        ),
        expected_aggregator_address=os.environ.get(
            "DYNAMIC_WORKER_EXPECTED_AGGREGATOR_ADDRESS",
            "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512",
        ),
        expected_gm_storage_address=os.environ.get(
            "DYNAMIC_WORKER_EXPECTED_GM_STORAGE_ADDRESS",
            "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0",
        ),
        expected_medical_signer_registry_address=os.environ.get(
            "DYNAMIC_WORKER_EXPECTED_MEDICAL_SIGNER_REGISTRY_ADDRESS",
            "0xCf7Ed3AccA5a467e9e704C703E8D87F634fB0Fc9",
        ),
        expected_chain_id=integer("DYNAMIC_WORKER_EXPECTED_CHAIN_ID", 31337),
        eth_eur_price=os.environ.get("ETH_EUR_PRICE", "3000"),
        eth_usd_price=os.environ.get("ETH_USD_PRICE", ""),
        exchange_rate_source=os.environ.get(
            "EXCHANGE_RATE_SOURCE", "manual_configuration"
        ),
        exchange_rate_timestamp_utc=os.environ.get("EXCHANGE_RATE_TIMESTAMP_UTC", ""),
        reference_mainnet_gas_price_gwei=os.environ.get(
            "REFERENCE_MAINNET_GAS_PRICE_GWEI", ""
        ),
        reference_gas_price_source=os.environ.get("REFERENCE_GAS_PRICE_SOURCE", ""),
        reference_gas_price_timestamp_utc=os.environ.get(
            "REFERENCE_GAS_PRICE_TIMESTAMP_UTC", ""
        ),
        region=os.environ.get("PHALA_REGION", "US-WEST-1"),
        os_image=os.environ.get("PHALA_OS_IMAGE", "dstack-dev-0.5.7"),
        epoch=integer("EPOCH", 1),
        round=integer("ROUND", 5),
        dfl_model_seed=os.environ.get("DFL_MODEL_SEED", "42"),
        dfl_train_seed=os.environ.get("DFL_TRAIN_SEED", "42"),
        dfl_train_optimizer=os.environ.get("DFL_TRAIN_OPTIMIZER", "adamw"),
        dfl_train_learning_rate=os.environ.get("DFL_TRAIN_LEARNING_RATE", "0.003"),
        dfl_train_lr_schedule=os.environ.get("DFL_TRAIN_LR_SCHEDULE", "constant"),
        dfl_train_lr_decay_start_round=os.environ.get(
            "DFL_TRAIN_LR_DECAY_START_ROUND", "20"
        ),
        dfl_train_lr_final_factor=os.environ.get("DFL_TRAIN_LR_FINAL_FACTOR", "0.25"),
        dfl_train_weight_decay=os.environ.get("DFL_TRAIN_WEIGHT_DECAY", "0.0001"),
        dfl_grad_clip_norm=os.environ.get("DFL_GRAD_CLIP_NORM", "5"),
        dfl_pos_weight_cap=os.environ.get("DFL_POS_WEIGHT_CAP", "10"),
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
        sello_required=os.environ.get("DYNAMIC_WORKER_SELLO_REQUIRED", "0").lower()
        in {"1", "true", "yes"},
        sello_scitt_url=os.environ.get("DYNAMIC_WORKER_SELLO_SCITT_URL", ""),
        sello_tee_service_signing_seed=os.environ.get(
            "DYNAMIC_WORKER_SELLO_TEE_SERVICE_SIGNING_SEED",
            "",
        ),
        sello_token_issuer_public_key=os.environ.get(
            "DYNAMIC_WORKER_SELLO_TOKEN_ISSUER_PUBLIC_KEY",
            "",
        ),
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
    return PhalaWorkerController(inventory, runner)

#!/usr/bin/env python3
"""Small control API for contract initialization and training restarts."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response


WORKSPACE_ROOT = Path(os.environ.get("TRAINING_WORKSPACE_ROOT", "/workspace")).resolve()
TRAINING_ENV_FILE = WORKSPACE_ROOT / ".env"
TRAINING_COMPOSE_FILE = WORKSPACE_ROOT / "compose.yml"
TRAINING_CONFIG_KEYS = ("ROUND", "EPOCH", "WORKER_COUNT", "CLIENT_LIMIT")
CONTRACT_TIMEOUT_SECONDS = 600
OBSERVABILITY_VOLUME_NAMES = (
    "grafana-data",
    "prometheus-data",
    "loki-data",
    "tempo-data",
    "promtail-positions",
)
STATIC_CONTAINER_NAMES = {
    "anvil": "anvil",
    "ipfs": "ipfs_local",
    "smart-contracts": "smart-contracts",
    "agent": "agent",
    "zk-inference": "zk-inference",
}

app = FastAPI(title="Master Thesis Control API")
operation_lock = asyncio.Lock()


def resolve_docker_bin() -> str:
    configured = os.environ.get("TRAINING_DOCKER_BIN", "").strip()
    candidates = [
        configured,
        shutil.which("docker") or "",
        "/usr/bin/docker",
        "/usr/local/bin/docker",
        "/bin/docker",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise RuntimeError(
        "Docker CLI was not found in the control-api container. Rebuild control-api or set TRAINING_DOCKER_BIN."
    )


def _env_line_value(line: str) -> tuple[str, str] | None:
    match = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*(?:#.*)?$", line)
    if not match:
        return None
    return match.group(1), match.group(2)


def _safe_int(value: str | None, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _available_worker_services(compose_file: Path = TRAINING_COMPOSE_FILE) -> list[str]:
    if not compose_file.exists():
        return []

    services: list[tuple[int, str]] = []
    for line in compose_file.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s{2}(VM-(\d+)):\s*$", line)
        if match:
            services.append((int(match.group(2)), match.group(1)))
    services.sort(key=lambda item: item[0])
    return [name for _, name in services]


def read_env_values(env_file: Path = TRAINING_ENV_FILE) -> dict[str, str]:
    values: dict[str, str] = {}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            parsed = _env_line_value(line)
            if parsed is None:
                continue
            key, value = parsed
            values[key] = value.strip()
    return values


def read_training_config(
    env_file: Path = TRAINING_ENV_FILE, compose_file: Path = TRAINING_COMPOSE_FILE
) -> dict[str, Any]:
    values = read_env_values(env_file)

    available_workers = _available_worker_services(compose_file)
    max_worker_count = len(available_workers)
    worker_count_default = max_worker_count if max_worker_count > 0 else 1
    worker_count = _safe_int(values.get("WORKER_COUNT"), worker_count_default)
    if max_worker_count > 0:
        worker_count = max(1, min(worker_count, max_worker_count))
    else:
        worker_count = max(1, worker_count)

    return {
        "rounds": max(1, _safe_int(values.get("ROUND"), 5)),
        "epoch": max(1, _safe_int(values.get("EPOCH"), 1)),
        "worker_count": worker_count,
        "client_limit": max(1, _safe_int(values.get("CLIENT_LIMIT"), 1)),
        "max_worker_count": max_worker_count,
        "available_workers": available_workers,
    }


def normalize_training_config(payload: dict[str, Any]) -> dict[str, int]:
    current = read_training_config()
    max_worker_count = max(1, int(current["max_worker_count"]) or 1)

    rounds = max(1, _safe_int(payload.get("rounds"), int(current["rounds"])))
    epoch = max(1, _safe_int(payload.get("epoch"), int(current["epoch"])))
    worker_count = max(
        1, min(_safe_int(payload.get("worker_count"), int(current["worker_count"])), max_worker_count)
    )
    client_limit = max(1, _safe_int(payload.get("client_limit"), int(current["client_limit"])))

    return {
        "rounds": rounds,
        "epoch": epoch,
        "worker_count": worker_count,
        "client_limit": client_limit,
    }


def write_training_config(config: dict[str, int], env_file: Path = TRAINING_ENV_FILE) -> dict[str, Any]:
    existing_lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    updated_lines: list[str] = []
    seen: set[str] = set()

    value_map = {
        "ROUND": str(config["rounds"]),
        "EPOCH": str(config["epoch"]),
        "WORKER_COUNT": str(config["worker_count"]),
        "CLIENT_LIMIT": str(config["client_limit"]),
    }

    for line in existing_lines:
        parsed = _env_line_value(line)
        if parsed is None:
            updated_lines.append(line)
            continue
        key, _ = parsed
        if key in value_map:
            updated_lines.append(f"{key}={value_map[key]}")
            seen.add(key)
        else:
            updated_lines.append(line)

    if updated_lines and updated_lines[-1] != "":
        updated_lines.append("")

    for key in TRAINING_CONFIG_KEYS:
        if key not in seen:
            updated_lines.append(f"{key}={value_map[key]}")

    env_file.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    return read_training_config(env_file)


async def run_subprocess(command: list[str], *, cwd: Path, check: bool = True) -> dict[str, Any]:
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await process.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    result = {
        "command": command,
        "returncode": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
    }
    if check and process.returncode != 0:
        raise RuntimeError(
            f"Command failed ({process.returncode}): {' '.join(command)}\n{stderr.strip() or stdout.strip()}"
        )
    return result


def compose_command(*args: str, include_profile: bool = True) -> list[str]:
    command = [
        os.environ.get("TRAINING_COMPOSE_BIN", "docker-compose"),
        "--project-directory",
        str(WORKSPACE_ROOT),
        "-f",
        str(TRAINING_COMPOSE_FILE),
    ]
    compose_project_name = os.environ.get("TRAINING_COMPOSE_PROJECT_NAME", "").strip()
    if compose_project_name:
        command.extend(["-p", compose_project_name])
    compose_profile = os.environ.get("TRAINING_COMPOSE_PROFILE", "runtime").strip()
    if include_profile and compose_profile:
        command.extend(["--profile", compose_profile])
    command.extend(args)
    return command


def compose_project_name() -> str:
    configured = os.environ.get("TRAINING_COMPOSE_PROJECT_NAME", "").strip()
    if configured:
        return configured
    return WORKSPACE_ROOT.name


def compose_ps_state(service_name: str, output: str, container_id: str = "") -> dict[str, Any]:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    data_lines = [line for line in lines if not line.lower().startswith(("name ", "name\t"))]
    text = "\n".join(data_lines).strip()
    if not text:
        return {
            "service": service_name,
            "exists": False,
            "status": "missing",
            "running": False,
            "exit_code": None,
            "health": None,
            "container_id": container_id or None,
            "container_name": STATIC_CONTAINER_NAMES.get(service_name),
        }

    lowered = text.lower()
    status = "unknown"
    running = False
    exit_code = None

    exited_match = re.search(r"(?:exit|exited)\s*\(?(\d+)\)?", lowered)
    if exited_match:
        status = "exited"
        running = False
        exit_code = int(exited_match.group(1))
    elif "restarting" in lowered:
        status = "restarting"
    elif "up" in lowered or "running" in lowered:
        status = "running"
        running = True
    elif "created" in lowered:
        status = "created"

    return {
        "service": service_name,
        "exists": True,
        "status": status,
        "running": running,
        "exit_code": exit_code,
        "health": None,
        "container_id": container_id or None,
        "container_name": STATIC_CONTAINER_NAMES.get(service_name),
    }


def _post_json(url: str, payload: dict[str, Any], timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def probe_anvil_ready(env_values: dict[str, str]) -> bool:
    rpc_url = env_values.get("RPC_URL", "http://anvil:8545").strip()
    payload = {"jsonrpc": "2.0", "method": "eth_chainId", "params": [], "id": 1}
    response = _post_json(rpc_url, payload)
    return bool(response and response.get("result"))


def probe_ipfs_ready(env_values: dict[str, str]) -> bool:
    kubo_api = env_values.get("KUBO_API", "http://ipfs:5001").strip()
    try:
        request = urllib.request.Request(f"{kubo_api}/api/v0/version", data=b"", method="POST")
        with urllib.request.urlopen(request, timeout=2.0) as response:
            return response.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def probe_contract_deployment(env_values: dict[str, str]) -> bool:
    rpc_url = env_values.get("RPC_URL", "http://anvil:8545").strip()
    addresses = [
        env_values.get("REGISTRY_ADDRESS", "").strip(),
        env_values.get("AGGREGATOR_ADDRESS", "").strip(),
        env_values.get("GM_STORAGE_ADDRESS", "").strip(),
    ]
    valid_addresses = [address for address in addresses if re.fullmatch(r"0x[a-fA-F0-9]{40}", address)]
    if not valid_addresses:
        return False

    for index, address in enumerate(valid_addresses, start=1):
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getCode",
            "params": [address, "latest"],
            "id": index,
        }
        response = _post_json(rpc_url, payload)
        code = (response or {}).get("result", "0x")
        if isinstance(code, str) and code not in {"0x", "0X", ""}:
            return True
    return False


def read_chain_round(env_values: dict[str, str]) -> int | None:
    rpc_url = env_values.get("RPC_URL", "http://anvil:8545").strip()
    gm_storage_address = env_values.get("GM_STORAGE_ADDRESS", "").strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", gm_storage_address):
        return None

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": gm_storage_address, "data": "0x9f8743f7"}, "latest"],
        "id": 1,
    }
    response = _post_json(rpc_url, payload)
    result = (response or {}).get("result")
    if not isinstance(result, str) or result in {"", "0x"}:
        return None
    try:
        return int(result, 16)
    except ValueError:
        return None


def _decode_abi_address(hex_data: str) -> str | None:
    if not isinstance(hex_data, str):
        return None
    data = hex_data.removeprefix("0x")
    if len(data) < 64:
        return None
    try:
        return "0x" + bytes.fromhex(data)[12:32].hex()
    except ValueError:
        return None


def _worker_label_for_address(env_values: dict[str, str], address: str | None) -> str | None:
    if not address:
        return None
    normalized = address.lower()
    for index in range(16):
        worker_address = env_values.get(f"W{index}_ACCOUNT_ADDRESS", "").strip().lower()
        if worker_address == normalized:
            return f"VM-{index}"
    return None


def read_current_aggregator(env_values: dict[str, str]) -> dict[str, str | None]:
    rpc_url = env_values.get("RPC_URL", "http://anvil:8545").strip()
    aggregator_address = env_values.get("AGGREGATOR_ADDRESS", "").strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", aggregator_address):
        return {"address": None, "vm": None}

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": aggregator_address, "data": "0xb0fa5f84"}, "latest"],
        "id": 1,
    }
    response = _post_json(rpc_url, payload)
    result = (response or {}).get("result")
    current_address = _decode_abi_address(result)
    return {
        "address": current_address,
        "vm": _worker_label_for_address(env_values, current_address),
    }


async def inspect_service(service_name: str) -> dict[str, Any]:
    ps_details = await run_subprocess(
        compose_command("ps", "-a", service_name, include_profile=False),
        cwd=WORKSPACE_ROOT,
        check=False,
    )
    docker_bin = None
    try:
        docker_bin = resolve_docker_bin()
    except RuntimeError:
        docker_bin = None

    fallback_name = STATIC_CONTAINER_NAMES.get(service_name)
    container_id = ""
    if docker_bin and fallback_name:
        fallback_result = await run_subprocess(
            [docker_bin, "ps", "-aq", "--filter", f"name=^/{fallback_name}$"],
            cwd=WORKSPACE_ROOT,
            check=False,
        )
        container_id = fallback_result["stdout"].strip()
    if not container_id:
        ps_result = await run_subprocess(
            compose_command("ps", "-a", "-q", service_name, include_profile=False),
            cwd=WORKSPACE_ROOT,
            check=False,
        )
        container_id = ps_result["stdout"].strip()
    if not docker_bin or not container_id:
        return compose_ps_state(service_name, ps_details["stdout"], container_id)

    inspect_result = await run_subprocess([docker_bin, "inspect", container_id], cwd=WORKSPACE_ROOT, check=True)
    payload = json.loads(inspect_result["stdout"])[0]
    state = payload.get("State", {})
    health = state.get("Health") or {}
    return {
        "service": service_name,
        "exists": True,
        "status": state.get("Status", "unknown"),
        "running": bool(state.get("Running")),
        "exit_code": state.get("ExitCode"),
        "health": health.get("Status"),
        "container_id": container_id,
        "container_name": payload.get("Name", "").lstrip("/"),
    }


async def wait_for_service_exit_success(service_name: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_state: dict[str, Any] | None = None

    while asyncio.get_running_loop().time() < deadline:
        state = await inspect_service(service_name)
        last_state = state
        if not state["exists"] or state["status"] in {"created", "restarting"}:
            await asyncio.sleep(2)
            continue
        if state["running"]:
            await asyncio.sleep(2)
            continue
        if state["status"] == "exited" and state["exit_code"] == 0:
            return state
        raise RuntimeError(
            f"{service_name} finished unsuccessfully with status={state['status']} exit_code={state['exit_code']}."
        )

    raise RuntimeError(f"Timed out while waiting for {service_name} to finish. Last state: {last_state}")


async def wait_for_service_ready(service_name: str, timeout_seconds: int) -> dict[str, Any]:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    last_state: dict[str, Any] | None = None

    while asyncio.get_running_loop().time() < deadline:
        state = await inspect_service(service_name)
        last_state = state
        if not state["exists"] or state["status"] in {"created", "restarting"}:
            await asyncio.sleep(2)
            continue
        if not state["running"]:
            raise RuntimeError(
                f"{service_name} is not running. status={state['status']} exit_code={state['exit_code']}."
            )
        if state["health"] in {None, "healthy"}:
            return state
        if state["health"] == "unhealthy":
            raise RuntimeError(f"{service_name} became unhealthy.")
        await asyncio.sleep(2)

    raise RuntimeError(f"Timed out while waiting for {service_name} to become ready. Last state: {last_state}")


async def collect_runtime_status() -> dict[str, Any]:
    env_values = read_env_values()
    worker_services = _available_worker_services()
    contract_state = await inspect_service("smart-contracts")
    anvil_state = await inspect_service("anvil")
    ipfs_state = await inspect_service("ipfs")
    agent_state = await inspect_service("agent")
    zk_inference_state = await inspect_service("zk-inference")
    worker_states = [await inspect_service(service) for service in worker_services]
    running_workers = [state["service"] for state in worker_states if state["running"]]

    anvil_ready = probe_anvil_ready(env_values)
    ipfs_ready = probe_ipfs_ready(env_values)
    chain_contracts_ready = probe_contract_deployment(env_values) if anvil_ready else False
    current_round = read_chain_round(env_values) if chain_contracts_ready else None
    current_aggregator = read_current_aggregator(env_values) if chain_contracts_ready else {"address": None, "vm": None}

    contract_completed_successfully = contract_state["status"] == "exited" and contract_state["exit_code"] == 0
    contract_initialized = contract_completed_successfully and chain_contracts_ready
    contract_running = contract_state["running"]
    training_started = contract_initialized and bool(
        running_workers or agent_state["running"] or zk_inference_state["running"]
    )

    return {
        "contract_initialized": contract_initialized,
        "contract_running": contract_running,
        "training_started": training_started,
        "agent_running": agent_state["running"],
        "base_runtime_ready": (anvil_state["running"] or anvil_ready) and (ipfs_state["running"] or ipfs_ready),
        "chain_contracts_ready": chain_contracts_ready,
        "contract_completed_successfully": contract_completed_successfully,
        "current_round": current_round,
        "current_aggregator_address": current_aggregator["address"],
        "current_aggregator_vm": current_aggregator["vm"],
        "running_workers": running_workers,
        "services": {
            "anvil": anvil_state,
            "ipfs": ipfs_state,
            "smart-contracts": contract_state,
            "zk-inference": zk_inference_state,
            "agent": agent_state,
            "workers": worker_states,
        },
    }


async def reset_services(service_names: list[str]) -> list[dict[str, Any]]:
    logs: list[dict[str, Any]] = []
    if not service_names:
        return logs

    for args in (["stop", *service_names], ["rm", "-f", *service_names]):
        logs.append(await run_subprocess(compose_command(*args), cwd=WORKSPACE_ROOT, check=False))
    return logs


async def reset_observability_volumes() -> list[dict[str, Any]]:
    docker_bin = resolve_docker_bin()
    project_name = compose_project_name()
    logs: list[dict[str, Any]] = []

    for volume_name in OBSERVABILITY_VOLUME_NAMES:
        logs.append(
            await run_subprocess(
                [docker_bin, "volume", "rm", "-f", f"{project_name}_{volume_name}"],
                cwd=WORKSPACE_ROOT,
                check=False,
            )
        )
    return logs


async def initialize_contract_stack() -> dict[str, Any]:
    worker_services = _available_worker_services()
    reset_targets = ["agent", "zk-inference", *worker_services, "smart-contracts", "anvil", "ipfs"]
    logs = await reset_services(reset_targets)
    logs.append(
        await run_subprocess(
            compose_command("up", "--build", "-d", "anvil", "ipfs"),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    await wait_for_service_ready("anvil", 120)
    await wait_for_service_ready("ipfs", 120)
    logs.append(
        await run_subprocess(
            compose_command("up", "--build", "-d", "smart-contracts"),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    contract_state = await wait_for_service_exit_success("smart-contracts", CONTRACT_TIMEOUT_SECONDS)
    return {
        "contract_state": contract_state,
        "status": await collect_runtime_status(),
        "logs": logs[-3:],
    }


async def start_training_services(config: dict[str, int]) -> dict[str, Any]:
    available_workers = _available_worker_services()
    if not available_workers:
        raise RuntimeError("No active VM-* worker services are defined in compose.yml.")

    runtime_status = await collect_runtime_status()
    if not runtime_status["contract_initialized"]:
        raise RuntimeError("Smart contracts are not initialized yet. Run contract initialization first.")

    selected_workers = available_workers[: config["worker_count"]]
    inactive_workers = available_workers[config["worker_count"] :]

    logs = await reset_services(["agent", "zk-inference", *available_workers])
    logs.append(
        await run_subprocess(
            compose_command(
                "up",
                "-d",
                "anvil",
                "ipfs",
                "loki",
                "promtail",
                "tempo",
                "otel-collector",
                "ollama",
            ),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    logs.append(
        await run_subprocess(
            compose_command(
                "up",
                "-d",
                "ollama-init",
            ),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    logs.append(
        await run_subprocess(
            compose_command(
                "up",
                "--no-deps",
                "-d",
                "zk-inference",
            ),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    logs.append(
        await run_subprocess(
            compose_command(
                "up",
                "--no-deps",
                "-d",
                *selected_workers,
            ),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    logs.append(
        await run_subprocess(
            compose_command(
                "up",
                "--no-deps",
                "-d",
                "agent",
            ),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    if inactive_workers:
        logs.extend(await reset_services(inactive_workers))

    return {
        "selected_workers": selected_workers,
        "inactive_workers": inactive_workers,
        "status": await collect_runtime_status(),
        "logs": logs[-4:],
    }


async def reset_training_services() -> dict[str, Any]:
    worker_services = _available_worker_services()
    reset_targets = [
        "agent",
        "zk-inference",
        *worker_services,
        "smart-contracts",
        "anvil",
        "ipfs",
        "grafana",
        "prometheus",
        "loki",
        "promtail",
        "tempo",
        "otel-collector",
        "ollama-init",
        "ollama",
    ]
    logs = await reset_services(reset_targets)
    logs.extend(await reset_observability_volumes())
    logs.append(
        await run_subprocess(
            compose_command(
                "up",
                "-d",
                "grafana",
                "prometheus",
                "loki",
                "promtail",
                "tempo",
                "otel-collector",
                "anvil",
                "ipfs",
            ),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    await wait_for_service_ready("anvil", 120)
    await wait_for_service_ready("ipfs", 120)
    logs.append(
        await run_subprocess(
            compose_command("up", "--build", "-d", "smart-contracts"),
            cwd=WORKSPACE_ROOT,
            check=True,
        )
    )
    contract_state = await wait_for_service_exit_success("smart-contracts", CONTRACT_TIMEOUT_SECONDS)
    return {
        "contract_state": contract_state,
        "status": await collect_runtime_status(),
        "logs": logs[-4:],
    }


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics() -> Response:
    env_values = read_env_values()
    current_round = read_chain_round(env_values)
    current_aggregator = read_current_aggregator(env_values)
    round_value = current_round if current_round is not None else 0
    aggregator_vm = current_aggregator["vm"] or "unknown"
    payload_lines = [
        "# HELP dfl_current_round Current DFL round read directly from the GMStorage smart contract.",
        "# TYPE dfl_current_round gauge",
        f"dfl_current_round {round_value}",
        "",
        "# HELP dfl_current_aggregator Current DFL aggregator selected on-chain.",
        "# TYPE dfl_current_aggregator gauge",
        f'dfl_current_aggregator{{aggregator_vm="{aggregator_vm}"}} 1',
        "",
    ]
    payload = "\n".join(payload_lines)
    return Response(content=payload, media_type="text/plain; version=0.0.4")


@app.get("/api/control/status")
async def get_control_status() -> dict[str, Any]:
    try:
        return {
            "config": read_training_config(),
            "runtime": await collect_runtime_status(),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/control/contracts/initialize")
async def initialize_contracts() -> dict[str, Any]:
    if operation_lock.locked():
        raise HTTPException(status_code=409, detail="Another control operation is already running.")

    async with operation_lock:
        try:
            return {"ok": True, **await initialize_contract_stack()}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/training/config")
async def get_training_config() -> dict[str, Any]:
    try:
        return read_training_config()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/training/config")
async def update_training_config(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        normalized = normalize_training_config(payload)
        config = write_training_config(normalized)
        return {"ok": True, "config": config}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/training/start")
async def start_training(payload: dict[str, Any]) -> dict[str, Any]:
    if operation_lock.locked():
        raise HTTPException(status_code=409, detail="Another control operation is already running.")

    async with operation_lock:
        try:
            normalized = normalize_training_config(payload)
            saved_config = write_training_config(normalized)
            result = await start_training_services(normalized)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "config": saved_config, **result}


@app.post("/api/training/reset")
async def reset_training() -> dict[str, Any]:
    if operation_lock.locked():
        raise HTTPException(status_code=409, detail="Another control operation is already running.")

    async with operation_lock:
        try:
            result = await reset_training_services()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"ok": True, "config": read_training_config(), **result}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("CONTROL_API_PORT", "8091")), log_level="warning")

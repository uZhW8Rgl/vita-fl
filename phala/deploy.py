#!/usr/bin/env python3
"""Reconcile the disposable Phala demo, including workers outside root state.

Uses the versioned Phala SDK API: GET /cvms/paginated, DELETE /cvms/{id}.
Terraform creates resources and deletes applications recorded in root state.
The reserved app names belong to one demo in a dedicated Phala workspace.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

if __package__:
    from .github_state import GitHubState, StateError
else:
    from github_state import GitHubState, StateError

SCRIPT_DIR = Path(__file__).resolve().parent
API_PREFIX = "https://cloud-api.phala.com/api/v1"
API_VERSION = "2026-01-21"
RUNTIME_ADDRESS = "phala_app.contract_runtime"
WORKER_NAMES = {
    "master-thesis-dfl-worker",
    "master-thesis-dfl-worker-phala",
    *(f"master-thesis-dfl-worker-{number}" for number in range(500)),
}
RESERVED_NAMES = WORKER_NAMES | {
    "master-thesis-contract-runtime-phala",
    "master-thesis-ollama",
    "master-thesis-zk-inference",
    "master-thesis-tee-inference",
}
RUNTIME_VARIABLES = (
    "runtime_endpoint_override",
    "runtime_rpc_url_override",
    "runtime_kubo_api_url_override",
    "runtime_kubo_gateway_url_override",
)


class DeploymentError(RuntimeError):
    """A preflight, Terraform or cloud operation failed safely."""


def read_environment(*paths: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    return values


def app_id(value: Any) -> str:
    return str(value or "").removeprefix("app_")


def resources(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Read Terraform show/plan JSON resources, including nested modules."""
    root = document.get("values", document.get("planned_values", {})).get("root_module", {})

    def visit(module: dict[str, Any]) -> list[dict[str, Any]]:
        result = list(module.get("resources", []))
        for child in module.get("child_modules", []):
            result.extend(visit(child))
        return result

    return visit(root)


def managed_apps(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [resource for resource in resources(document) if resource.get("type") == "phala_app"]


def service_url(endpoint: str, port: str | int) -> str:
    """Replace the embedded gateway port, or a regular HTTP(S) origin port."""
    parsed = urllib.parse.urlsplit(endpoint.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise DeploymentError("Terraform returned an invalid runtime endpoint")
    if parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise DeploymentError("The runtime endpoint must be an HTTP(S) origin without path or query")
    hostname = parsed.hostname
    if re.search(r"-\d+s?\.", hostname):
        host = re.sub(r"-\d+s?\.", f"-{port}.", hostname, count=1)
    else:
        host = f"[{hostname}]" if ":" in hostname else hostname
        host = f"{host}:{str(port).removesuffix('s')}"
    return urllib.parse.urlunsplit((parsed.scheme, host, "", "", ""))


@dataclass(frozen=True)
class Cvm:
    identifier: str
    name: str
    app_id: str
    app_name: str = ""

    @property
    def deployment_name(self) -> str:
        return self.app_name or self.name

    @classmethod
    def from_json(cls, item: Any) -> Cvm:
        if not isinstance(item, dict):
            raise DeploymentError("Phala returned an invalid CVM record")
        identifier, name = item.get("id"), item.get("name")
        if not isinstance(identifier, str) or not identifier or not isinstance(name, str) or not name:
            raise DeploymentError("Phala returned a CVM without a valid ID/name; refusing partial cleanup")
        return cls(identifier, name, app_id(item.get("app_id")))


class PhalaClient:
    def __init__(self, api_key: str, api_prefix: str = API_PREFIX) -> None:
        if not api_key:
            raise DeploymentError("PHALA_CLOUD_API_KEY is required")
        parsed = urllib.parse.urlsplit(api_prefix)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise DeploymentError("PHALA_CLOUD_API_PREFIX must be an HTTPS URL")
        self.api_key = api_key
        self.api_prefix = api_prefix.rstrip("/")

    def request(self, method: str, path: str) -> Any:
        request = urllib.request.Request(
            self.api_prefix + path,
            method=method,
            headers={
                "X-API-Key": self.api_key,
                "X-Phala-Version": API_VERSION,
                "Accept": "application/json",
                "User-Agent": "vita-fl-deploy/1",
            },
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    raw = response.read()
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                if error.code == 404 and (
                    method == "DELETE" or (method == "GET" and path.startswith("/apps/") and path.endswith("/cvms"))
                ):
                    return None
                if error.code in {429, 500, 502, 503, 504} and attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise DeploymentError(f"Phala {method} failed with HTTP {error.code}") from error
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                if attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise DeploymentError(f"Phala {method} failed; check network access and retry") from error
            except (ValueError, UnicodeDecodeError) as error:
                raise DeploymentError("Phala returned invalid JSON") from error
        raise DeploymentError("Phala request retry limit exceeded")

    def list_cvms(self) -> list[Cvm]:
        result: dict[str, Cvm] = {}
        for page in range(1, 10001):
            payload = self.request("GET", f"/cvms/paginated?page={page}&page_size=100")
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                raise DeploymentError("Phala returned an invalid paginated CVM inventory")
            pages = payload.get("pages")
            if not isinstance(pages, int) or isinstance(pages, bool) or pages < 0 or payload.get("page") != page:
                raise DeploymentError("Phala returned invalid pagination; refusing incomplete cleanup")
            for item in payload["items"]:
                cvm = Cvm.from_json(item)
                result[cvm.identifier] = cvm
            if page >= pages:
                return list(result.values())
            if not payload["items"]:
                raise DeploymentError("Phala inventory ended before its last page; retry")
        raise DeploymentError("Phala inventory pagination exceeded the safety limit")

    def validate_os_images(self, plan: dict[str, Any]) -> None:
        """Check planned placements against the current read-only node catalog."""
        requested = [
            (resource.get("address", "phala_app"), resource.get("values", {})) for resource in managed_apps(plan)
        ]
        variables = plan.get("variables", {})
        if variables.get("enable_phala_control_api", {}).get("value") is True:
            # UI workers do not exist in root state until the user starts
            # training, but their pinned OS must already be deployable.
            requested.append(
                (
                    "dynamic UI workers",
                    {
                        "image": variables.get("dynamic_worker_os_image", {}).get("value"),
                        "node_id": variables.get("dynamic_worker_node_id", {}).get("value"),
                        "region": variables.get("region", {}).get("value"),
                    },
                )
            )
        if not requested:
            return
        for address, values in requested:
            node_id = values.get("node_id")
            if (
                not isinstance(values.get("image"), str)
                or not values["image"]
                or not isinstance(values.get("region"), str)
                or not values["region"]
                or (node_id is not None and (type(node_id) is not int or node_id <= 0))
            ):
                raise DeploymentError(f"Cannot validate OS image placement for {address}; check the Terraform plan")

        payload = self.request("GET", "/teepods/available")
        if not isinstance(payload, dict) or not isinstance(payload.get("nodes"), list):
            raise DeploymentError("Phala returned an invalid node/OS image catalog; refusing deployment")
        nodes = payload["nodes"]
        for node in nodes:
            if (
                not isinstance(node, dict)
                or type(node.get("teepod_id")) is not int
                or node["teepod_id"] <= 0
                or not isinstance(node.get("region_identifier"), str)
                or not node["region_identifier"]
                or not isinstance(node.get("images"), list)
            ):
                raise DeploymentError("Phala returned an invalid node/OS image catalog; refusing deployment")
            for image in node["images"]:
                if (
                    not isinstance(image, dict)
                    or not isinstance(image.get("name"), str)
                    or not image["name"]
                    or (image.get("slug") is not None and not isinstance(image["slug"], str))
                    or type(image.get("enabled")) is not bool
                ):
                    raise DeploymentError("Phala returned an invalid node/OS image catalog; refusing deployment")
        for address, values in requested:
            compatible = any(
                node["region_identifier"] == values["region"]
                and (values.get("node_id") is None or node["teepod_id"] == values["node_id"])
                and any(
                    image["enabled"] and values["image"] in {image["name"], image.get("slug")}
                    for image in node["images"]
                )
                for node in nodes
            )
            if not compatible:
                placement = f"node {values['node_id']} in " if values.get("node_id") is not None else ""
                raise DeploymentError(
                    f"OS image {values['image']} for {address} is unavailable or disabled on "
                    f"{placement}region {values['region']}; choose an enabled image before retrying"
                )

    def delete(self, cvms: list[Cvm], timeout: float = 300) -> None:
        """Wait until concrete CVM IDs disappear before reusing their names."""
        pending = {cvm.identifier: cvm for cvm in cvms}
        for cvm in pending.values():
            print(f"Deleting orphan CVM {cvm.name} ({cvm.identifier})", flush=True)
            self.request("DELETE", "/cvms/" + urllib.parse.quote(cvm.identifier, safe=""))
        deadline = time.monotonic() + timeout
        while pending:
            present = {
                cvm.identifier for cvm in self.inventory(set(), {cvm.app_id for cvm in pending.values() if cvm.app_id})
            }
            pending = {identifier: cvm for identifier, cvm in pending.items() if identifier in present}
            if not pending:
                break
            if time.monotonic() >= deadline:
                names = ", ".join(cvm.name for cvm in pending.values())
                raise DeploymentError(f"Deletion not confirmed for {names}; rerun the same command")
            time.sleep(3)

    def inventory(self, names: set[str], ids: set[str]) -> list[Cvm]:
        """Expand known applications explicitly; paginated views may hide replicas."""
        cvms = self.list_cvms()
        result = {cvm.identifier: cvm for cvm in cvms}
        app_names = self.list_app_names() if names else {}
        selected_ids = (
            ids
            | {cvm.app_id for cvm in cvms if cvm.name in names and cvm.app_id}
            | {identifier for identifier, name in app_names.items() if name in names}
        )
        for identifier in sorted(selected_ids):
            payload = self.request("GET", f"/apps/{urllib.parse.quote(identifier, safe='')}/cvms")
            if payload is None:
                continue  # An already deleted app is absent.
            if not isinstance(payload, list):
                raise DeploymentError("Phala returned an invalid app replica inventory")
            for item in payload:
                cvm = Cvm.from_json(item)
                if cvm.app_id != identifier:
                    raise DeploymentError("Phala returned a replica from a different app; refusing cleanup")
                result[cvm.identifier] = Cvm(cvm.identifier, cvm.name, cvm.app_id, app_names.get(cvm.app_id, ""))
        return list(result.values())

    def list_app_names(self) -> dict[str, str]:
        result = {}
        for page in range(1, 10001):
            payload = self.request("GET", f"/apps?page={page}&page_size=100")
            if not isinstance(payload, dict) or not isinstance(payload.get("dstack_apps"), list):
                raise DeploymentError("Phala returned an invalid application inventory")
            pages = payload.get("total_pages")
            if not isinstance(pages, int) or isinstance(pages, bool) or pages < 0 or payload.get("page") != page:
                raise DeploymentError("Phala returned invalid application pagination")
            for item in payload["dstack_apps"]:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item.get("app_id"):
                    raise DeploymentError("Phala returned an application without a valid name/ID")
                result[app_id(item["app_id"])] = item["name"]
            if page >= pages:
                return result
            if not payload["dstack_apps"]:
                raise DeploymentError("Phala application inventory ended before its last page")
        raise DeploymentError("Phala application pagination exceeded the safety limit")


@dataclass(frozen=True)
class Decision:
    reset: bool
    reason: str
    scoped: list[Cvm]
    names: set[str]
    app_ids: set[str]


def decide(
    state: dict[str, Any], plan: dict[str, Any], cvms: list[Cvm], *, recreate: bool = False, destroy: bool = False
) -> Decision:
    apps = managed_apps(state)
    configured = managed_apps(plan)
    names = RESERVED_NAMES | {
        resource.get("values", {}).get("name")
        for resource in apps + configured
        if resource.get("values", {}).get("name")
    }
    managed_ids = {app_id(resource.get("values", {}).get("app_id")) for resource in apps} - {""}
    ids = managed_ids | {
        cvm.app_id for cvm in cvms if (cvm.name in names or cvm.deployment_name in names) and cvm.app_id
    }
    scoped = [
        cvm for cvm in cvms if cvm.name in names or cvm.deployment_name in names or (cvm.app_id and cvm.app_id in ids)
    ]
    orphans = [cvm for cvm in scoped if cvm.app_id not in managed_ids]
    worker_ids = {cvm.app_id for cvm in cvms if cvm.deployment_name in WORKER_NAMES}
    changes = [
        resource
        for resource in plan.get("resource_changes", [])
        if resource.get("type") == "phala_app" and resource.get("change", {}).get("actions", ["no-op"]) != ["no-op"]
    ]
    runtime_changed = any(
        resource.get("address") == RUNTIME_ADDRESS or "dfl_worker" in resource.get("address", "")
        for resource in changes
    )
    if destroy:
        return Decision(True, "explicit --destroy", scoped, names, ids)
    if recreate:
        return Decision(True, "explicit --recreate", scoped, names, ids)
    if any(cvm.deployment_name not in WORKER_NAMES and cvm.app_id not in worker_ids for cvm in orphans):
        return Decision(True, "existing application outside this Terraform state", scoped, names, ids)
    if runtime_changed and (scoped or apps):
        return Decision(
            True,
            "runtime/worker configuration changed; the immutable training roster needs a fresh run",
            scoped,
            names,
            ids,
        )
    return Decision(False, "new deployment" if not apps else "reconcile existing deployment", scoped, names, ids)


class Terraform:
    def __init__(self, directory: Path, environment: dict[str, str]) -> None:
        self.directory = directory
        self.environment = environment
        self.runtime_vars: dict[str, Any] = {}
        self.state_store: GitHubState | None = None

    def run(self, *args: str, capture: bool = False) -> str:
        arguments = list(args)
        if args[0] in {"plan", "apply", "destroy"}:
            # CLI values take precedence over .tfvars, inherited TF_VARs and
            # image vars. Null stays a Terraform null via the JSON var file.
            if self.runtime_vars:
                with tempfile.NamedTemporaryFile(mode="w", suffix=".tfvars.json") as variables:
                    json.dump(self.runtime_vars, variables)
                    variables.flush()
                    return self._run([*arguments, f"-var-file={variables.name}"], capture=capture)
        return self._run(arguments, capture=capture)

    def _run(self, arguments: list[str], *, capture: bool) -> str:
        result = subprocess.run(
            ["bash", str(self.directory / "tf-env.sh"), *arguments],
            env=self.environment,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            check=False,
        )
        if result.returncode < 0 or result.returncode >= 128:
            # Shell signal exit codes must retain the shared lock: descendant
            # provider processes may still be finishing after cancellation.
            raise KeyboardInterrupt
        # Terraform also writes recovery state when an apply fails. Persist
        # that snapshot before propagating the failure or changing more CVMs.
        if self.state_store is not None and arguments[0] in {"apply", "destroy", "import", "refresh", "state"}:
            self.state_store.sync()
        if result.returncode:
            raise DeploymentError(
                f"Terraform {arguments[0]} failed (exit {result.returncode}); fix the error and rerun"
            )
        return result.stdout or ""

    def state(self) -> dict[str, Any]:
        return json.loads(self.run("show", "-json", capture=True) or "{}")

    def plan(self, path: Path, *, destroy: bool = False) -> dict[str, Any]:
        args = ["plan", "-input=false", "-no-color", "-lock-timeout=5m", f"-out={path}"]
        if destroy:
            args.append("-destroy")
        self.run(*args)
        path.chmod(0o600)
        return json.loads(self.run("show", "-json", str(path), capture=True))

    def wire_runtime(self, endpoint: str | None, *, sello: bool = False) -> None:
        self.runtime_vars = dict.fromkeys(RUNTIME_VARIABLES)
        if endpoint:
            endpoint = endpoint.rstrip("/")
            self.runtime_vars.update(
                runtime_endpoint_override=endpoint,
                runtime_rpc_url_override=service_url(endpoint, 8545),
                runtime_kubo_api_url_override=service_url(endpoint, 5001),
                runtime_kubo_gateway_url_override=service_url(endpoint, 8080),
            )
        if sello:
            self.runtime_vars["sello_scitt_url"] = (
                service_url(endpoint, "8000s") if endpoint else "https://runtime-endpoint-not-configured.invalid"
            )


def runtime_endpoint(state: dict[str, Any], cvms: list[Cvm] | None = None) -> str | None:
    runtime = next((resource for resource in managed_apps(state) if resource.get("address") == RUNTIME_ADDRESS), None)
    if not runtime:
        return None
    values = runtime.get("values", {})
    if cvms is not None and app_id(values.get("app_id")) not in {cvm.app_id for cvm in cvms}:
        return None
    endpoint = values.get("endpoint")
    if isinstance(endpoint, str) and endpoint:
        service_url(endpoint, 8545)
        return endpoint.rstrip("/")
    return None


def execute(args: argparse.Namespace, directory: Path = SCRIPT_DIR) -> None:
    root = directory.parent
    env_file = Path(os.environ.get("PHALA_ENV_FILE", root / ".env.phala.anvil")).resolve()
    if not env_file.is_file():
        raise DeploymentError(f"Missing {env_file}; copy .env.phala.anvil.example and replace its placeholders")
    config = read_environment(env_file)
    environment = dict(os.environ, PHALA_ENV_FILE=str(env_file), TF_IN_AUTOMATION="1")
    for key in ("PHALA_STATE_REPOSITORY", "PHALA_STATE_BRANCH", "PHALA_STATE_PASSPHRASE"):
        if key not in environment and config.get(key):
            environment[key] = config[key]
    shared = bool(environment.get("PHALA_STATE_REPOSITORY"))
    initialize = getattr(args, "init_github_state", False)
    unlock_id = getattr(args, "unlock_github_state", None)
    recovery = getattr(args, "recover_github_state", None)
    if (initialize or unlock_id or recovery) and not shared:
        raise DeploymentError("Set PHALA_STATE_REPOSITORY and PHALA_STATE_PASSPHRASE before using shared state")
    if shared and (directory / "deployment-backend_override.tf").exists():
        raise DeploymentError(
            "An external Terraform backend is still configured. Recover its state and remove its override "
            "before initializing GitHub state; no backend is changed automatically."
        )
    state_store = GitHubState.from_environment(directory, environment) if shared else None
    if recovery:
        state_store.recover(Path(recovery), args.github_state_lock_id)
        print("Recovered Terraform state saved to GitHub; deployment lock released.")
        return
    if unlock_id:
        state_store.unlock(unlock_id)
        print("GitHub deployment lock removed.")
        return
    session = state_store.session(initialize=initialize) if state_store else contextlib.nullcontext()
    with session:
        execute_deployment(args, directory, env_file, config, environment, state_store)


def execute_deployment(
    args: argparse.Namespace,
    directory: Path,
    env_file: Path,
    config: dict[str, str],
    environment: dict[str, str],
    state_store: GitHubState | None = None,
) -> None:
    terraform = Terraform(directory, environment)
    terraform.state_store = state_store
    terraform.run("init", "-input=false", "-no-color")
    if args.init_only:
        return
    with tempfile.TemporaryDirectory(prefix="vita-fl-deploy-") as temporary:
        temporary_path = Path(temporary)
        if not args.destroy and not environment.get("PHALA_IMAGE_VARS_FILE"):
            image_vars = temporary_path / "images.tfvars.json"
            command = [
                sys.executable,
                str(directory / "resolve_images.py"),
                "--env-file",
                str(env_file),
                "--output",
                str(image_vars),
            ]
            if args.pinned:
                command.append("--pinned")
            subprocess.run(command, check=True, env=environment)
            environment["PHALA_IMAGE_VARS_FILE"] = str(image_vars)
        terraform.run("validate", "-no-color")
        client = PhalaClient(
            config.get("PHALA_CLOUD_API_KEY", ""),
            environment.get("PHALA_CLOUD_API_PREFIX") or config.get("PHALA_CLOUD_API_PREFIX", API_PREFIX),
        )
        cvms = client.list_cvms()
        state = terraform.state()
        # An old endpoint in .env must never reconnect new workers to the
        # previous chain. A deliberate external SCITT endpoint is preserved.
        endpoint = runtime_endpoint(state, cvms)
        scitt_host = urllib.parse.urlsplit(config.get("SELLO_SCITT_URL", "")).hostname or ""
        sello = config.get("ENABLE_SELLO_RECEIPTS", "false").lower() in {"true", "1"} and (
            not scitt_host or scitt_host.endswith(".phala.network")
        )
        terraform.wire_runtime(endpoint, sello=sello)
        plan = terraform.plan(temporary_path / "preflight.tfplan", destroy=args.destroy)
        if not args.destroy:
            client.validate_os_images(plan)
        decision = decide(state, plan, cvms, recreate=args.recreate, destroy=args.destroy)
        cvms = client.inventory(decision.names, decision.app_ids)
        decision = decide(state, plan, cvms, recreate=args.recreate, destroy=args.destroy)
        if decision.reset and not args.destroy:
            # Validate the first creation phase before removing a working
            # runtime. Its new endpoints are unknown, even when the initial
            # plan could use the existing runtime's healthy endpoints.
            print("Validating runtime bootstrap before resetting existing apps.", flush=True)
            terraform.wire_runtime(None, sello=sello)
            try:
                terraform.plan(temporary_path / "bootstrap-preflight.tfplan")
            finally:
                terraform.wire_runtime(endpoint, sello=sello)
        print(f"Deployment decision: {decision.reason}", flush=True)
        if decision.reset:
            print(
                "Reset clears the demo chain, IPFS data, training history and worker identities for this run.",
                flush=True,
            )
            for cvm in decision.scoped:
                print(f"  {cvm.name} ({cvm.identifier})", flush=True)
        if args.dry_run:
            print("Dry run complete: no cloud resources or Terraform state were changed.", flush=True)
            return
        if not args.destroy:
            # Persist the selected release independently of CVMs so a failed
            # reset can resume with exactly the same digests in CI.
            terraform.run(
                "apply",
                "-input=false",
                "-auto-approve",
                "-no-color",
                "-lock-timeout=5m",
                "-target=terraform_data.deployment_manifest",
            )
        if decision.reset:
            targets = [
                f"-target={resource['address']}"
                for resource in resources(state)
                if resource.get("mode", "managed") == "managed" and resource.get("type", "").startswith("phala_")
            ]
            if args.destroy or targets:
                terraform.run(
                    "destroy",
                    "-input=false",
                    "-auto-approve",
                    "-no-color",
                    "-lock-timeout=5m",
                    *([] if args.destroy else targets),
                )
            # Stop old control APIs before the final worker inventory so they
            # cannot create workers after cleanup has already finished.
            remaining = client.inventory(decision.names, decision.app_ids)
            scoped = [
                cvm
                for cvm in remaining
                if cvm.name in decision.names or (cvm.app_id and cvm.app_id in decision.app_ids)
            ]
            client.delete([cvm for cvm in scoped if cvm.deployment_name not in WORKER_NAMES])
            remaining = client.inventory(decision.names, decision.app_ids)
            client.delete(
                [
                    cvm
                    for cvm in remaining
                    if cvm.name in decision.names or (cvm.app_id and cvm.app_id in decision.app_ids)
                ]
            )
            endpoint = None
            terraform.wire_runtime(None, sello=sello)
        if args.destroy:
            print("Phala demo resources deleted.", flush=True)
            return
        if endpoint is None:
            print("Creating the runtime before wiring worker endpoints.", flush=True)
            terraform.run(
                "apply",
                "-input=false",
                "-auto-approve",
                "-no-color",
                "-lock-timeout=5m",
                f"-target={RUNTIME_ADDRESS}",
            )
            endpoint = runtime_endpoint(terraform.state())
            if endpoint is None:
                raise DeploymentError("Runtime has no public endpoint; enable its gateway and rerun")
        for attempt in range(3):
            terraform.wire_runtime(endpoint, sello=sello)
            print("Applying the deployment with the current runtime endpoints.", flush=True)
            terraform.run("apply", "-input=false", "-auto-approve", "-no-color", "-lock-timeout=5m")
            final_state = terraform.state()
            current_endpoint = runtime_endpoint(final_state)
            if current_endpoint == endpoint:
                break
            if current_endpoint is None or attempt == 2:
                raise DeploymentError("Runtime endpoint did not stabilize; inspect the Phala app and rerun")
            endpoint = current_endpoint
        print(f"Phala resources applied. Runtime: {endpoint}", flush=True)
        ui_endpoint = final_state.get("values", {}).get("outputs", {}).get("ui_endpoint", {}).get("value")
        if ui_endpoint:
            print(f"UI: {ui_endpoint}", flush=True)
        if config.get("ENABLE_PHALA_CONTROL_API", "false").lower() in {"true", "1"}:
            print("Start Training in the UI commits the participant roster and creates its workers.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Validate and list changes without mutating cloud/state")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--recreate", action="store_true", help="Create a fresh runtime; training remains controlled by the UI"
    )
    mode.add_argument("--destroy", action="store_true", help="Delete this demo, including workers outside root state")
    parser.add_argument(
        "--pinned", action="store_true", help="Use pinned image references from the env file for rollback"
    )
    parser.add_argument(
        "--init-only", action="store_true", help="Initialize Terraform without changing Phala resources"
    )
    parser.add_argument(
        "--init-github-state",
        action="store_true",
        help="Initialize the encrypted GitHub state branch from existing local state (requires --init-only)",
    )
    parser.add_argument(
        "--unlock-github-state",
        metavar="LOCK_ID",
        help="Explicitly remove an abandoned deployment lock after verifying no deployment is running",
    )
    parser.add_argument(
        "--recover-github-state",
        metavar="FILE",
        help="Save an interrupted deployment's encrypted snapshot or raw Terraform state, then release its lock",
    )
    parser.add_argument("--github-state-lock-id", metavar="LOCK_ID", help="Lock ID for explicit state recovery")
    args = parser.parse_args()
    if args.init_github_state and (not args.init_only or args.dry_run or args.destroy or args.recreate):
        parser.error("--init-github-state requires --init-only and cannot be combined with deployment modes")
    deployment_mode = any((args.init_github_state, args.init_only, args.dry_run, args.destroy, args.recreate))
    if args.unlock_github_state and (deployment_mode or args.recover_github_state):
        parser.error("--unlock-github-state must be used on its own")
    if bool(args.recover_github_state) != bool(args.github_state_lock_id):
        parser.error("--recover-github-state and --github-state-lock-id must be used together")
    if args.recover_github_state and deployment_mode:
        parser.error("State recovery cannot be combined with deployment modes")
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        # Protect this checkout while the shared GitHub lock coordinates
        # complete deployment sequences across CI runners and local machines.
        with (SCRIPT_DIR / ".deployment.lock").open("a", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise DeploymentError("Another deployment is already running in this checkout") from error
            execute(args)
    except (DeploymentError, StateError, subprocess.CalledProcessError, OSError, ValueError) as error:
        print(f"Deployment failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Deployment interrupted. Rerun the same command to reconcile remaining resources.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Job-owned ZK inference pipeline for the separate Phala service."""

from __future__ import annotations

import hashlib
import secrets
import shutil
import threading
from pathlib import Path
from typing import Any, Callable

import cbor2


class ZkJobError(RuntimeError):
    """The requested ZK model or job operation is invalid."""


class ZkJobRuntime:
    def __init__(
        self,
        root: Path,
        model_loader: Callable[[Path], tuple[Path, bytes, dict[str, Any]]],
        export_fn: Callable[..., dict[str, Any]],
        query_fn: Callable[..., dict[str, Any]],
        prove_fn: Callable[..., dict[str, Any]],
    ) -> None:
        self.root = root
        self._model_loader = model_loader
        self._export_fn = export_fn
        self._query_fn = query_fn
        self._prove_fn = prove_fn
        self._model: dict[str, Any] | None = None
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def fetch_model(self) -> dict[str, Any]:
        with self._lock:
            source = self.root / "models" / "current" / "source"
            model_path, manifest, bundle = self._model_loader(source)
            model_id = hashlib.sha256(manifest).hexdigest()
            export_dir = self.root / "models" / model_id / "export"
            export = self._export_fn(str(model_path), str(export_dir))
            if not export.get("ok"):
                raise ZkJobError(f"verified model export failed: {export.get('stderr') or export.get('error')}")
            self._model = {
                "model_id": model_id,
                "manifest_sha256": model_id,
                "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
                "model_cid": bundle.get("model_cid", ""),
                "signature_cid": bundle.get("signature_cid", ""),
                "last_aggregator": bundle.get("last_aggregator", ""),
                "export_dir": export_dir,
            }
            return self.model_metadata()

    def model_metadata(self) -> dict[str, Any]:
        if self._model is None:
            raise ZkJobError("no verified ZK model is loaded")
        return {key: value for key, value in self._model.items() if key != "export_dir"}

    def health(self) -> dict[str, Any]:
        with self._lock:
            result: dict[str, Any] = {"ok": True, "model_loaded": self._model is not None}
            if self._model is not None:
                result.update(self.model_metadata())
            return result

    def create_job(self, index: int | None) -> dict[str, Any]:
        with self._lock:
            if self._model is None:
                raise ZkJobError("fetch and verify the current ZK model before creating a job")
            job_id = secrets.token_hex(16)
            job_dir = self.root / "jobs" / job_id
            workdir = job_dir / "work"
            shutil.copytree(self._model["export_dir"], workdir)
            query_dir = job_dir / "query"
            query = self._query_fn(
                index=index,
                out_dir=str(query_dir),
                input_json=str(workdir / "input.json"),
            )
            if not query.get("ok"):
                raise ZkJobError(f"ChestMNIST query creation failed: {query.get('stderr') or query.get('error')}")
            selection = query.get("selection", {})
            job = {
                "job_id": job_id,
                "model_id": self._model["model_id"],
                "model_sha256": self._model["model_sha256"],
                "model_cid": self._model["model_cid"],
                "signature_cid": self._model["signature_cid"],
                "last_aggregator": self._model["last_aggregator"],
                "source_index": selection.get("source_index"),
                "selection": selection,
                "workdir": workdir,
            }
            self._jobs[job_id] = job
            return self.job_metadata(job_id)

    def job_metadata(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        return {key: value for key, value in job.items() if key not in {"workdir", "selection", "transparency_path"}}

    def run_and_verify(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._job(job_id)
            result = self._prove_fn(
                workdir=str(job["workdir"]),
                model="model_logits.onnx",
                data="input.json",
                skip_calibration=False,
            )
            if not result.get("ok"):
                detail = result.get("stderr") or result.get("stdout") or result.get("error") or "unknown error"
                raise ZkJobError(
                    f"EZKL proof generation or verification failed (returncode={result.get('returncode')}): {detail}"
                )
            artifact_hashes: dict[str, str] = {}
            for name in ("proof.json", "witness.json", "settings.json", "vk.key"):
                path = job["workdir"] / name
                if path.is_file():
                    artifact_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            transparency_bundle = cbor2.dumps(
                {
                    "schema": "master-thesis.zk-inference-proof.v1",
                    "job_id": job_id,
                    "model_id": job["model_id"],
                    "model_sha256": job["model_sha256"],
                    "model_cid": job["model_cid"],
                    "signature_cid": job["signature_cid"],
                    "last_aggregator": job["last_aggregator"],
                    "source_index": job["source_index"],
                    "proof_verified": True,
                    "input_json": (job["workdir"] / "input.json").read_bytes(),
                    "proof_json": (job["workdir"] / "proof.json").read_bytes(),
                    "settings_json": (job["workdir"] / "settings.json").read_bytes(),
                    "verification_key": (job["workdir"] / "vk.key").read_bytes(),
                    "artifact_sha256": artifact_hashes,
                },
                canonical=True,
            )
            transparency_path = job["workdir"] / "transparency-bundle.cbor"
            transparency_path.write_bytes(transparency_bundle)
            job["transparency_path"] = transparency_path
            return {
                "job_id": job_id,
                "model_id": job["model_id"],
                "source_index": job["source_index"],
                "proof_verified": True,
                "artifact_sha256": artifact_hashes,
                "transparency_bundle_sha256": hashlib.sha256(transparency_bundle).hexdigest(),
                "transparency_bundle_bytes": len(transparency_bundle),
            }

    def transparency_bundle(self, job_id: str) -> bytes:
        with self._lock:
            job = self._job(job_id)
            path = job.get("transparency_path")
            if not isinstance(path, Path) or not path.is_file():
                raise ZkJobError("ZK proof must be verified before its transparency bundle is available")
            return path.read_bytes()

    def _job(self, job_id: str) -> dict[str, Any]:
        if len(job_id) != 32 or any(character not in "0123456789abcdef" for character in job_id):
            raise ZkJobError("job_id must contain exactly 32 lowercase hexadecimal characters")
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise ZkJobError("ZK inference job does not exist in this container run") from exc

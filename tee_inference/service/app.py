"""FastAPI transport for canonical-CBOR ChestMNIST inference."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from agent_receipts.environment import RECEIPT_HEADER, bearer_token, receipt_header, receiver_from_environment
from agent_receipts.receiver_log import (
    SCITT_BUNDLE_URL_HEADER,
    SCITT_TRANSACTION_HEADER,
    ReceiverTransparencyPublisher,
)
from agent_receipts.sello_v1 import ReceiptVerificationError, verify_authorization_token
from tee_inference.protocol.v1 import ProtocolError
from tee_inference.service.attestation import AirEvidenceEmitter
from tee_inference.service.engine import ChestMnistTorchEngine, InferenceError
from tee_inference.service.jobs import JobError, TeeJobStore
from tee_inference.service.model_source import provision_latest_model

CBOR_MEDIA_TYPE = "application/cbor"
MAX_REQUEST_BYTES = 2_048


ModelLoader = Callable[[], tuple[ChestMnistTorchEngine, AirEvidenceEmitter | None]]


class InferenceRuntime:
    """Atomically prepare and use the model selected by the latest tool call."""

    def __init__(
        self,
        engine: ChestMnistTorchEngine | None = None,
        emitter: AirEvidenceEmitter | None = None,
        loader: ModelLoader | None = None,
    ) -> None:
        self._engine = engine
        self._emitter = emitter
        self._loader = loader
        self._state_lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._last_error: str | None = None

    def health(self) -> dict[str, str | bool]:
        with self._state_lock:
            result: dict[str, str | bool] = {
                "status": "ok",
                "model_loaded": self._engine is not None,
            }
            if self._engine is not None:
                result.update(
                    {
                        "model_sha256": self._engine.model_artifact_hash.hex(),
                        "manifest_sha256": self._engine.model_manifest_hash.hex(),
                    }
                )
            if self._last_error is not None:
                result["last_prepare_error"] = self._last_error
            return result

    def manifest_hash(self) -> bytes:
        with self._state_lock:
            if self._engine is None:
                raise RuntimeError("model is not prepared; fetch the current model bundle first")
            return self._engine.model_manifest_hash

    def prepare(self) -> dict[str, str | bool]:
        with self._operation_lock:
            if self._loader is not None:
                try:
                    engine, emitter = self._loader()
                except Exception as exc:
                    with self._state_lock:
                        self._last_error = str(exc)
                    raise
                with self._state_lock:
                    self._engine = engine
                    self._emitter = emitter
                    self._last_error = None
            with self._state_lock:
                if self._engine is None:
                    raise RuntimeError("TEE inference model loader is not configured")
            return self.health()

    def infer(self, exact_request: bytes) -> bytes:
        with self._operation_lock:
            with self._state_lock:
                engine = self._engine
                emitter = self._emitter
            if engine is None:
                raise RuntimeError("model is not prepared; call POST /v1/prepare first")
            response = engine.infer(exact_request)
            return emitter.emit(exact_request, response) if emitter is not None else response


def _create_app(runtime: InferenceRuntime, jobs: TeeJobStore | None = None) -> FastAPI:
    app = FastAPI(title="ChestMNIST TEE inference", version="1")
    receipt_receiver = receiver_from_environment("tee-inference")
    receipt_publisher = ReceiverTransparencyPublisher("tee-inference") if receipt_receiver is not None else None

    def authorized_token(request: Request) -> str | None:
        if receipt_receiver is None:
            return None
        token = bearer_token(request.headers)
        verify_authorization_token(token, receipt_receiver.token_issuer_key)
        return token

    async def action_response(
        token: str | None,
        action: str,
        action_input: bytes,
        body: bytes,
        *,
        status_code: int = 200,
        media_type: str = "application/json",
        fields: dict[str, object] | None = None,
    ) -> Response:
        headers: dict[str, str] = {}
        if receipt_receiver is not None and token is not None:
            try:
                claims = verify_authorization_token(token, receipt_receiver.token_issuer_key)
                receipt = receipt_receiver.issue(
                    token,
                    action_type=action,
                    action_input=action_input,
                    action_output=body,
                    result_status="success" if status_code < 400 else "error",
                    service_defined_fields={"http-status": status_code, **(fields or {})},
                )
                publication_id, registration = await run_in_threadpool(
                    receipt_publisher.publish, receipt, claims["sello_logs"][0]
                )
                headers[RECEIPT_HEADER] = receipt_header(receipt)
                headers[SCITT_BUNDLE_URL_HEADER] = f"/v1/sello/receipts/{publication_id}"
                headers[SCITT_TRANSACTION_HEADER] = str(registration["transaction_id"])
            except Exception as exc:
                failure = json.dumps(
                    {"error": "receiver receipt could not be committed to SCITT", "detail": str(exc)},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
                return Response(failure, status_code=503, media_type="application/json")
        return Response(body, status_code=status_code, media_type=media_type, headers=headers)

    async def json_action_response(
        token: str | None,
        action: str,
        action_input: bytes,
        payload: dict[str, object],
        *,
        status_code: int = 200,
        fields: dict[str, object] | None = None,
    ) -> Response:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return await action_response(token, action, action_input, body, status_code=status_code, fields=fields)

    @app.get("/healthz")
    async def health() -> dict[str, str | bool]:
        # Docker probes must never trigger on-chain or IPFS model loading.
        return runtime.health()

    @app.get("/v1/sello/receipts/{publication_id}")
    async def get_sello_publication(publication_id: str) -> Response:
        if receipt_publisher is None:
            return JSONResponse({"error": "receiver receipts are not enabled"}, status_code=404)
        try:
            bundle = await run_in_threadpool(receipt_publisher.read, publication_id)
        except (FileNotFoundError, OSError):
            return JSONResponse({"error": "receiver receipt publication was not found"}, status_code=404)
        return Response(bundle, media_type="application/cbor")

    @app.post("/v1/prepare")
    async def prepare() -> Response:
        try:
            state = await run_in_threadpool(runtime.prepare)
        except Exception as exc:
            return JSONResponse(
                {"error": "TEE inference model is not ready", "detail": str(exc)},
                status_code=503,
            )
        return JSONResponse(state)

    @app.post("/v1/models/fetch")
    async def fetch_model_bundle(request: Request) -> Response:
        action = "fetch_latest_verified_tee_model_bundle"
        action_input = await request.body()
        try:
            token = authorized_token(request)
        except ReceiptVerificationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        try:
            state = await run_in_threadpool(runtime.prepare)
        except Exception as exc:
            return await json_action_response(
                token, action, action_input,
                {"error": "TEE inference model is not ready", "detail": str(exc)}, status_code=503,
            )
        return await json_action_response(token, action, action_input, {"ok": True, **state})

    @app.post("/v1/jobs")
    async def create_job(request: Request) -> Response:
        action = "generate_random_tee_chestmnist_image"
        action_input = await request.body()
        try:
            token = authorized_token(request)
        except ReceiptVerificationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        if jobs is None:
            return await json_action_response(token, action, action_input, {"error": "TEE inference job store is not configured"}, status_code=503)
        try:
            payload = json.loads(action_input or b"{}")
            if not isinstance(payload, dict) or set(payload) - {"index"}:
                raise JobError("job request may contain only the optional index field")
            index = payload.get("index")
            if index is not None and (isinstance(index, bool) or not isinstance(index, int)):
                raise JobError("index must be an integer or null")
            job = await run_in_threadpool(jobs.create, index, runtime.manifest_hash())
        except (JobError, RuntimeError, ValueError) as exc:
            return await json_action_response(token, action, action_input, {"error": str(exc)}, status_code=409)
        except Exception as exc:
            return await json_action_response(token, action, action_input, {"error": f"invalid JSON job request: {exc}"}, status_code=400)
        return await json_action_response(token, action, action_input, {"ok": True, **job.public_metadata()})

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str) -> Response:
        if jobs is None:
            return JSONResponse({"error": "TEE inference job store is not configured"}, status_code=503)
        try:
            job = jobs.get(job_id)
        except JobError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)
        return JSONResponse({"ok": True, **job.public_metadata()})

    @app.post("/v1/jobs/{job_id}/run")
    async def run_job(job_id: str, request: Request) -> Response:
        action = "run_and_verify_tee_inference"
        action_input = json.dumps({"job_id": job_id}, sort_keys=True, separators=(",", ":")).encode()
        try:
            token = authorized_token(request)
        except ReceiptVerificationError as exc:
            return JSONResponse({"error": str(exc)}, status_code=401)
        if jobs is None:
            return await json_action_response(token, action, action_input, {"error": "TEE inference job store is not configured"}, status_code=503)
        try:
            job = jobs.get(job_id)
            if job.manifest_hash != runtime.manifest_hash():
                raise JobError("job is bound to a different prepared model; create a new job")
            output = await run_in_threadpool(runtime.infer, job.request_bytes())
            await run_in_threadpool(jobs.store_evidence, job_id, output)
        except JobError as exc:
            return await json_action_response(token, action, action_input, {"error": str(exc)}, status_code=409)
        except (ProtocolError, InferenceError) as exc:
            return await json_action_response(token, action, action_input, {"error": str(exc)}, status_code=400)
        except RuntimeError as exc:
            return await json_action_response(token, action, action_input, {"error": str(exc)}, status_code=503)
        return await action_response(token, action, action_input, output, media_type=CBOR_MEDIA_TYPE, fields={"job-id": job_id})

    @app.post("/v1/infer")
    async def infer(request: Request) -> Response:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != CBOR_MEDIA_TYPE:
            return JSONResponse({"error": "content-type must be application/cbor"}, status_code=415)
        body = await request.body()
        if not body or len(body) > MAX_REQUEST_BYTES:
            return JSONResponse({"error": "request body size is invalid"}, status_code=413)
        try:
            output = await run_in_threadpool(runtime.infer, body)
        except (ProtocolError, InferenceError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)
        return Response(output, media_type=CBOR_MEDIA_TYPE)

    return app


def create_app(engine: ChestMnistTorchEngine, emitter: AirEvidenceEmitter | None = None) -> FastAPI:
    """Build an app around an already loaded engine (primarily for local use/tests)."""

    return _create_app(InferenceRuntime(engine=engine, emitter=emitter))


def create_lazy_app(loader: ModelLoader) -> FastAPI:
    """Build an app whose model is refreshed only by POST /v1/prepare."""

    return _create_app(InferenceRuntime(loader=loader))


def create_job_app(loader: ModelLoader, jobs: TeeJobStore) -> FastAPI:
    """Build the production job API around an on-demand model loader."""

    return _create_app(InferenceRuntime(loader=loader), jobs)


def from_environment() -> FastAPI:
    target = Path(os.environ.get("TEE_MODEL_DIR", "/tmp/tee-inference/model"))
    jobs = TeeJobStore(
        Path(os.environ.get("TEE_JOB_DIR", "/tmp/tee-inference/jobs")),
        Path(os.environ.get("CHESTMNIST_TEST_DATA", "/app/data/chestmnist/test_data/test-data.npz")),
    )

    def load_current_model() -> tuple[ChestMnistTorchEngine, AirEvidenceEmitter]:
        model_path, manifest, _bundle = provision_latest_model(target)
        engine = ChestMnistTorchEngine.from_manifest(model_path, manifest)
        return engine, AirEvidenceEmitter(manifest)

    return create_job_app(load_current_model, jobs)


app = from_environment() if os.environ.get("TEE_INFERENCE_AUTOSTART") == "1" else FastAPI()

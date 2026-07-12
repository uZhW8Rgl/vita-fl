"""FastAPI transport for canonical-CBOR ChestMNIST inference."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from tee_inference.protocol.v1 import ProtocolError
from tee_inference.service.engine import ChestMnistTorchEngine, InferenceError
from tee_inference.service.attestation import AirEvidenceEmitter
from tee_inference.service.model_source import provision_latest_model

CBOR_MEDIA_TYPE = "application/cbor"
MAX_REQUEST_BYTES = 2_048


def create_app(engine: ChestMnistTorchEngine, emitter: AirEvidenceEmitter | None = None) -> FastAPI:
    app = FastAPI(title="ChestMNIST TEE inference", version="1")

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "model_sha256": engine.model_artifact_hash.hex(),
            "manifest_sha256": engine.model_manifest_hash.hex(),
        }

    @app.post("/v1/infer")
    async def infer(request: Request) -> Response:
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != CBOR_MEDIA_TYPE:
            return JSONResponse({"error": "content-type must be application/cbor"}, status_code=415)
        body = await request.body()
        if not body or len(body) > MAX_REQUEST_BYTES:
            return JSONResponse({"error": "request body size is invalid"}, status_code=413)
        try:
            response = engine.infer(body)
        except (ProtocolError, InferenceError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        output = emitter.emit(body, response) if emitter is not None else response
        return Response(output, media_type=CBOR_MEDIA_TYPE)

    return app


def from_environment() -> FastAPI:
    target = Path(os.environ.get("TEE_MODEL_DIR", "/app/model"))
    model_path, manifest, _bundle = provision_latest_model(target)
    engine = ChestMnistTorchEngine.from_manifest(model_path, manifest)
    emitter = AirEvidenceEmitter(manifest)
    return create_app(engine, emitter)


app = from_environment() if os.environ.get("TEE_INFERENCE_AUTOSTART") == "1" else FastAPI()

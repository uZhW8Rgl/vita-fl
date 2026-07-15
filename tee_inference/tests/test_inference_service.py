from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

import httpx
import numpy as np

from tee_inference.protocol.v1 import ProtocolError, decode_request, decode_response, encode_deterministic
from tee_inference.service.app import create_app, create_job_app, create_lazy_app
from tee_inference.service.engine import ChestMnistTorchEngine, InferenceError
from tee_inference.service.jobs import TeeJobStore

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "agent" / "downloads" / "onchain-28945426c804-aggregated.bin"
VECTOR = ROOT / "tee_inference" / "vectors" / "v1-chestmnist.json"


class _FakeEngine:
    model_artifact_hash = bytes.fromhex("11" * 32)
    model_manifest_hash = bytes.fromhex("22" * 32)

    @staticmethod
    def infer(exact_request: bytes) -> bytes:
        return b"response:" + exact_request


class _FakeEmitter:
    @staticmethod
    def emit(exact_request: bytes, response: bytes) -> bytes:
        return b"evidence:" + exact_request + response


class LazyModelLoadingTests(unittest.TestCase):
    def test_job_api_keeps_dataset_and_evidence_inside_service_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset_path = root / "test-data.npz"
            np.savez(
                dataset_path,
                images=np.zeros((2, 28, 28), dtype=np.uint8),
                labels=np.array([[1] + [0] * 13, [0, 1] + [0] * 12], dtype=np.uint8),
            )
            store = TeeJobStore(root / "jobs", dataset_path)
            app = create_job_app(lambda: (_FakeEngine(), _FakeEmitter()), store)

            async def call() -> tuple[httpx.Response, httpx.Response, httpx.Response, httpx.Response]:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    before_model = await client.post("/v1/jobs", json={"index": 1})
                    fetched = await client.post("/v1/models/fetch", json={})
                    created = await client.post("/v1/jobs", json={"index": 1})
                    run = await client.post(f"/v1/jobs/{created.json()['job_id']}/run")
                    return before_model, fetched, created, run

            before, fetched, created, run = asyncio.run(call())
            self.assertEqual(before.status_code, 409)
            self.assertEqual(fetched.status_code, 200)
            self.assertEqual(created.status_code, 200)
            self.assertEqual(created.json()["source_index"], 1)
            self.assertEqual(created.json()["ground_truth"], ["cardiomegaly"])
            self.assertEqual(run.status_code, 200)
            self.assertTrue(run.content.startswith(b"evidence:"))
            job_dir = root / "jobs" / created.json()["job_id"]
            self.assertTrue((job_dir / "pixels.bin").is_file())
            self.assertEqual((job_dir / "evidence.cbor").read_bytes(), run.content)

    def test_health_does_not_load_model_and_prepare_refreshes_it(self) -> None:
        calls = 0

        def loader() -> tuple[_FakeEngine, None]:
            nonlocal calls
            calls += 1
            return _FakeEngine(), None

        async def call() -> tuple[httpx.Response, httpx.Response, httpx.Response, httpx.Response]:
            transport = httpx.ASGITransport(app=create_lazy_app(loader))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                health_before = await client.get("/healthz")
                infer_before = await client.post(
                    "/v1/infer", content=b"request", headers={"content-type": "application/cbor"}
                )
                first_prepare = await client.post("/v1/prepare")
                second_prepare = await client.post("/v1/prepare")
                return health_before, infer_before, first_prepare, second_prepare

        health, infer, first, second = asyncio.run(call())
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok", "model_loaded": False})
        self.assertEqual(infer.status_code, 503)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["manifest_sha256"], "22" * 32)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(calls, 2)

    def test_failed_prepare_is_retried_by_next_tool_call(self) -> None:
        calls = 0

        def loader() -> tuple[_FakeEngine, None]:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("worker is not authorized")
            return _FakeEngine(), None

        async def call() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
            transport = httpx.ASGITransport(app=create_lazy_app(loader))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                first = await client.post("/v1/prepare")
                health = await client.get("/healthz")
                second = await client.post("/v1/prepare")
                return first, health, second

        first, health, second = asyncio.run(call())
        self.assertEqual(first.status_code, 503)
        self.assertEqual(health.status_code, 200)
        self.assertFalse(health.json()["model_loaded"])
        self.assertEqual(second.status_code, 200)
        self.assertEqual(calls, 2)


@unittest.skipUnless(MODEL.exists(), "local native DFL model is not present")
class ChestMnistInferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        vector = json.loads(VECTOR.read_text(encoding="utf-8"))
        cls.manifest_hash = bytes.fromhex(vector["manifest"]["sha256_hex"])
        manifest_cbor = bytes.fromhex(vector["manifest"]["deterministic_cbor_hex"])
        cls.engine = ChestMnistTorchEngine.from_manifest(MODEL, manifest_cbor)

    def request(self, *, manifest_hash: bytes | None = None) -> bytes:
        return encode_deterministic(
            {
                1: 1,
                2: bytes.fromhex("00112233445566778899aabbccddeeff"),
                3: self.manifest_hash if manifest_hash is None else manifest_hash,
                4: bytes(range(256)) * 3 + bytes(range(16)),
                5: bytes.fromhex("a5" * 32),
                6: 1_750_000_000_123,
            }
        )

    def test_real_native_model_contract_and_inference(self) -> None:
        request = self.request()
        response = decode_response(self.engine.infer(request))
        self.assertEqual(response[2], bytes.fromhex("00112233445566778899aabbccddeeff"))
        self.assertEqual(response[3], hashlib.sha256(request).digest())
        self.assertEqual(response[4], self.manifest_hash)
        self.assertEqual(len(struct.unpack("<14d", response[5])), 14)
        probabilities = struct.unpack("<14d", response[6])
        self.assertTrue(all(0.0 <= value <= 1.0 for value in probabilities))
        self.assertEqual(response[7], bytes(int(value >= 0.5) for value in probabilities))

    def test_model_outputs_are_repeatable(self) -> None:
        first = decode_response(self.engine.infer(self.request()))
        second = decode_response(self.engine.infer(self.request()))
        self.assertEqual(first[5], second[5])
        self.assertEqual(first[6], second[6])
        self.assertEqual(first[7], second[7])

    def test_wrong_manifest_is_rejected_before_inference(self) -> None:
        with self.assertRaisesRegex(InferenceError, "manifest"):
            self.engine.infer(self.request(manifest_hash=bytes.fromhex("ff" * 32)))

    def test_noncanonical_request_is_rejected(self) -> None:
        # The timestamp 1 is deliberately encoded as uint8 instead of its shortest form.
        canonical = encode_deterministic(
            {
                1: 1,
                2: bytes(16),
                3: self.manifest_hash,
                4: bytes(784),
                5: bytes(32),
                6: 1,
            }
        )
        self.assertTrue(canonical.endswith(bytes.fromhex("0601")))
        bad = canonical[:-2] + bytes.fromhex("061801")
        with self.assertRaises(ProtocolError):
            decode_request(bad)

    def test_http_cbor_endpoint(self) -> None:
        async def call() -> httpx.Response:
            transport = httpx.ASGITransport(app=create_app(self.engine))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post(
                    "/v1/infer",
                    content=self.request(),
                    headers={"content-type": "application/cbor"},
                )

        result = asyncio.run(call())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.headers["content-type"], "application/cbor")
        decode_response(result.content)

    def test_http_rejects_json(self) -> None:
        async def call() -> httpx.Response:
            transport = httpx.ASGITransport(app=create_app(self.engine))
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                return await client.post("/v1/infer", json={"pixels": []})

        result = asyncio.run(call())
        self.assertEqual(result.status_code, 415)


if __name__ == "__main__":
    unittest.main()

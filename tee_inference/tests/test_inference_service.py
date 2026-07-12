from __future__ import annotations

import asyncio
import hashlib
import json
import struct
import unittest
from pathlib import Path

import httpx

from tee_inference.protocol.v1 import ProtocolError, decode_request, decode_response, encode_deterministic
from tee_inference.service.app import create_app
from tee_inference.service.engine import ChestMnistOnnxEngine, InferenceError

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "zk_inference" / "out" / "model_logits.onnx"
VECTOR = ROOT / "tee_inference" / "vectors" / "v1-chestmnist.json"


@unittest.skipUnless(MODEL.exists(), "local ONNX export is not present")
class ChestMnistInferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        vector = json.loads(VECTOR.read_text(encoding="utf-8"))
        cls.manifest_hash = bytes.fromhex(vector["manifest"]["sha256_hex"])
        manifest_cbor = bytes.fromhex(vector["manifest"]["deterministic_cbor_hex"])
        cls.engine = ChestMnistOnnxEngine.from_manifest(MODEL, manifest_cbor)

    def request(self, *, manifest_hash: bytes | None = None) -> bytes:
        return encode_deterministic({
            1: 1,
            2: bytes.fromhex("00112233445566778899aabbccddeeff"),
            3: self.manifest_hash if manifest_hash is None else manifest_hash,
            4: bytes(range(256)) * 3 + bytes(range(16)),
            5: bytes.fromhex("a5" * 32),
            6: 1_750_000_000_123,
        })

    def test_real_onnx_model_contract_and_inference(self) -> None:
        request = self.request()
        response = decode_response(self.engine.infer(request))
        self.assertEqual(response[2], bytes.fromhex("00112233445566778899aabbccddeeff"))
        self.assertEqual(response[3], hashlib.sha256(request).digest())
        self.assertEqual(response[4], self.manifest_hash)
        self.assertEqual(len(struct.unpack("<14f", response[5])), 14)
        probabilities = struct.unpack("<14f", response[6])
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
        canonical = encode_deterministic({
            1: 1,
            2: bytes(16),
            3: self.manifest_hash,
            4: bytes(784),
            5: bytes(32),
            6: 1,
        })
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

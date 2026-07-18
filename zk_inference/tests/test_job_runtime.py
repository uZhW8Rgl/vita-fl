from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import cbor2

from zk_inference.job_runtime import ZkJobRuntime


class ZkJobRuntimeTests(unittest.TestCase):
    def test_model_sample_and_verified_proof_are_job_owned(self) -> None:
        proof_call = {}

        def load_model(target: Path):
            target.mkdir(parents=True)
            model = target / "aggregated.bin"
            model.write_bytes(b"verified-model")
            return model, b"manifest", {"model_cid": "cid", "last_aggregator": "0xabc"}

        def export_model(_model: str, output: str):
            path = Path(output)
            path.mkdir(parents=True)
            (path / "model_logits.onnx").write_bytes(b"onnx")
            return {"ok": True}

        def create_query(*, index, out_dir, input_json):
            Path(out_dir).mkdir(parents=True)
            Path(input_json).write_text("{}", encoding="utf-8")
            return {"ok": True, "selection": {"source_index": index}}

        def prove(*, workdir, **kwargs):
            proof_call.update(kwargs)
            path = Path(workdir)
            for name in ("proof.json", "witness.json", "settings.json", "vk.key"):
                (path / name).write_bytes(name.encode())
            return {"ok": True}

        with tempfile.TemporaryDirectory() as directory:
            runtime = ZkJobRuntime(Path(directory), load_model, export_model, create_query, prove)
            self.assertEqual(runtime.health(), {"ok": True, "model_loaded": False})
            model = runtime.fetch_model()
            self.assertTrue(runtime.health()["model_loaded"])
            job = runtime.create_job(4)
            result = runtime.run_and_verify(job["job_id"])
            transparency_bundle = cbor2.loads(runtime.transparency_bundle(job["job_id"]))

        self.assertEqual(model["model_cid"], "cid")
        self.assertEqual(job["source_index"], 4)
        self.assertTrue(result["proof_verified"])
        self.assertFalse(proof_call["skip_calibration"])
        self.assertEqual(set(result["artifact_sha256"]), {"proof.json", "witness.json", "settings.json", "vk.key"})
        self.assertEqual(transparency_bundle["schema"], "master-thesis.zk-inference-proof.v1")
        self.assertEqual(transparency_bundle["job_id"], job["job_id"])
        self.assertTrue(transparency_bundle["proof_verified"])
        self.assertEqual(transparency_bundle["proof_json"], b"proof.json")
        self.assertNotIn("witness_json", transparency_bundle)


if __name__ == "__main__":
    unittest.main()

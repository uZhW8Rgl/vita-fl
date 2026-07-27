from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from dfl.neural_network import cli, service
from dfl.neural_network.cli import FederatedCNN, write_model_bin


class AggregateParticipantKeyTests(unittest.TestCase):
    def test_model_receive_rejects_missing_participant_key_path(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "private_key must identify the materialized participant key",
        ):
            service._receive_model(
                {
                    "device_id": "0x" + "11" * 20,
                    "package_base64": "",
                }
            )

    def test_aggregate_signs_with_explicit_participant_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root / "inputs"
            results = root / "results"
            inputs.mkdir()
            results.mkdir()
            write_model_bin(FederatedCNN().double(), inputs / "worker.bin")
            participant_key = root / "run" / "participant-private.pem"

            with (
                patch.object(cli, "aggregation_inputs_dir", return_value=inputs),
                patch.object(cli, "results_dir", return_value=results),
                patch.object(cli, "sign_file") as sign_file,
                patch.object(cli, "run_test", return_value={}),
            ):
                cli.aggregate(1, private_key=str(participant_key))

            sign_file.assert_called_once_with(
                results / "aggregated.bin",
                participant_key,
            )

    def test_aggregate_rejects_input_count_different_from_closed_round(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            inputs = Path(directory)
            write_model_bin(FederatedCNN().double(), inputs / "worker.bin")

            with patch.object(cli, "aggregation_inputs_dir", return_value=inputs):
                with self.assertRaisesRegex(
                    ValueError,
                    "aggregate input-count mismatch",
                ):
                    cli.aggregate(2)

    def test_aggregate_http_request_forwards_participant_key(self) -> None:
        participant_key = "/run/vita-fl/participant-private.pem"
        server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/aggregate",
                data=json.dumps(
                    {
                        "num_files": 2,
                        "round_id": 3,
                        "source_round": 2,
                        "expected_models": 2,
                        "participant_count": 3,
                        "private_key": participant_key,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with patch.object(
                service,
                "aggregate",
                return_value={"model_path": "/tmp/aggregated.bin", "metrics": {}},
            ) as aggregate:
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))

            self.assertTrue(payload["ok"])
            aggregate.assert_called_once_with(
                2,
                round_id=3,
                source_round=2,
                expected_models=2,
                participant_count=3,
                private_key=participant_key,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class TrainingParticipantKeyTests(unittest.TestCase):
    def test_train_model_passes_explicit_key_to_package_encryption(self) -> None:
        participant_key = "/run/vita-fl/participant-private.pem"
        with tempfile.TemporaryDirectory() as directory:
            data_directory = Path(directory)
            model = FederatedCNN().double()
            with (
                patch.object(cli, "data_dir", return_value=data_directory),
                patch.object(cli, "read_model_bin", return_value=model),
                patch.object(cli, "is_multilabel_dataset", return_value=False),
                patch.object(
                    cli,
                    "load_training_dataset",
                    return_value=(SimpleNamespace(shape=(0,)), object()),
                ),
                patch.object(cli, "write_model_bin"),
                patch.object(cli, "encrypt_model_package") as encrypt_model_package,
            ):
                cli.train_model(
                    0,
                    "0x0102",
                    private_key=participant_key,
                )

            encrypt_model_package.assert_called_once_with(
                data_directory / "lm.bin",
                data_directory / "lm.bin.enc",
                "0x0102",
                private_key=participant_key,
            )

    def test_encrypted_model_package_embeds_explicit_participant_public_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            model_path = root / "lm.bin"
            package_path = root / "lm.bin.enc"
            participant_key_path = root / "participant-private.pem"
            model_path.write_bytes(b"model payload")

            participant_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
            )
            participant_key_path.write_bytes(
                participant_key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            expected_sender_public_key = participant_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            aggregator_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048,
            )
            aggregator_public_key_hex = aggregator_key.public_key().public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            ).hex()

            cli.encrypt_model_package(
                model_path,
                package_path,
                aggregator_public_key_hex,
                private_key=str(participant_key_path),
            )

            package = package_path.read_bytes()
            sender_key_length = int.from_bytes(package[:4], "little")
            self.assertEqual(
                package[4 : 4 + sender_key_length],
                expected_sender_public_key,
            )

    def test_train_http_request_forwards_participant_key(self) -> None:
        participant_key = "/run/vita-fl/participant-private.pem"
        server = ThreadingHTTPServer(("127.0.0.1", 0), service.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/train",
                data=json.dumps(
                    {
                        "epochs": 2,
                        "aggregator_public_key_der_hex": "0x0102",
                        "medical_signer_snapshot": {"version": 1},
                        "private_key": participant_key,
                    }
                ).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with patch.object(service, "train_model") as train_model:
                with urllib.request.urlopen(request, timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))

            self.assertTrue(payload["ok"])
            train_model.assert_called_once_with(
                2,
                "0x0102",
                {"version": 1},
                private_key=participant_key,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

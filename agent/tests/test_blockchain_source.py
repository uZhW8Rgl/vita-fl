from __future__ import annotations

import unittest
from unittest.mock import patch

from agent.blockchain_source import (
    decode_device_record,
    decode_finalized_model_bundle,
    read_device_record,
    read_current_bundle_from_contract,
    verify_download_with_registry,
)


def _word(value: int) -> bytes:
    return value.to_bytes(32, "big")


def _dynamic_string(value: str) -> bytes:
    raw = value.encode("utf-8")
    padding = b"\x00" * ((32 - len(raw) % 32) % 32)
    return _word(len(raw)) + raw + padding


def _encoded_finalized_bundle(
    model: str = "bafy-model",
    signature: str = "bafy-signature",
    key_bundle: str = "bafy-key-bundle",
    aggregator: str = "0x" + "44" * 20,
    model_round: int = 7,
    publisher_public_key: bytes = b"publisher-public-key",
) -> str:
    dynamic_values = [
        _dynamic_string(model),
        _dynamic_string(signature),
        _dynamic_string(key_bundle),
        _word(len(publisher_public_key))
        + publisher_public_key
        + b"\x00" * ((32 - len(publisher_public_key) % 32) % 32),
    ]
    next_offset = 6 * 32
    offsets = []
    for value in dynamic_values:
        offsets.append(next_offset)
        next_offset += len(value)
    address_word = bytes.fromhex(aggregator[2:]).rjust(32, b"\x00")
    encoded = (
        b"".join(_word(offset) for offset in offsets[:3])
        + address_word
        + _word(model_round)
        + _word(offsets[3])
        + b"".join(dynamic_values)
    )
    return "0x" + encoded.hex()


def _encoded_device(
    *,
    authorized: bool = True,
    public_ip: str = "https://worker0-8080.example",
    broker_ip: str = "127.0.0.1",
    public_key: bytes = b"participant-public-key",
) -> str:
    dynamic_values = [
        _dynamic_string(public_ip),
        _dynamic_string(broker_ip),
        _word(len(public_key))
        + public_key
        + b"\x00" * ((32 - len(public_key) % 32) % 32),
    ]
    next_offset = 4 * 32
    offsets = []
    for value in dynamic_values:
        offsets.append(next_offset)
        next_offset += len(value)
    encoded = (
        _word(1 if authorized else 0)
        + b"".join(_word(offset) for offset in offsets)
        + b"".join(dynamic_values)
    )
    return "0x" + encoded.hex()


class FinalizedModelBundleTests(unittest.TestCase):
    def test_decodes_atomic_finalized_model_tuple(self) -> None:
        self.assertEqual(
            decode_finalized_model_bundle(_encoded_finalized_bundle()),
            (
                "bafy-model",
                "bafy-signature",
                "bafy-key-bundle",
                "0x" + "44" * 20,
                7,
                b"publisher-public-key",
            ),
        )

    def test_rejects_out_of_bounds_dynamic_value(self) -> None:
        malformed = bytearray.fromhex(_encoded_finalized_bundle().removeprefix("0x"))
        malformed[:32] = _word(len(malformed) + 32)
        with self.assertRaisesRegex(RuntimeError, "requested 32-byte word"):
            decode_finalized_model_bundle("0x" + malformed.hex())

    def test_contract_bundle_resolution_uses_one_atomic_eth_call(self) -> None:
        gm_storage = "0x" + "11" * 20
        registry = "0x" + "22" * 20
        with patch(
            "agent.blockchain_source.rpc_call",
            return_value=_encoded_finalized_bundle(),
        ) as rpc:
            bundle = read_current_bundle_from_contract("http://rpc", gm_storage, registry)

        self.assertEqual(bundle["finalized_model_round"], 7)
        self.assertEqual(bundle["last_aggregator"], "0x" + "44" * 20)
        self.assertEqual(bundle["publisher_public_key_der_hex"], b"publisher-public-key".hex())
        rpc.assert_called_once_with(
            "http://rpc",
            "eth_call",
            [{"to": gm_storage, "data": "0xfd419631"}, "latest"],
        )

    def test_signature_verification_uses_finalization_time_key_snapshot(self) -> None:
        bundle = {
            "gm_storage_address": "0x" + "11" * 20,
            "last_aggregator": "0x" + "44" * 20,
            "publisher_public_key_der_hex": b"publisher-public-key".hex(),
        }
        download = {
            "encrypted_bundle": False,
            "model_path": "/tmp/model.bin",
            "signature_path": "/tmp/model.bin.sig",
        }
        with (
            patch(
                "agent.blockchain_source.verify_model_signature",
                return_value={"ok": True},
            ) as verify,
            patch(
                "agent.blockchain_source.read_device_public_key_der",
            ) as current_registry_read,
        ):
            result = verify_download_with_registry(
                bundle,
                download,
                "http://rpc",
                "0x" + "22" * 20,
            )

        self.assertTrue(result["ok"])
        verify.assert_called_once()
        self.assertEqual(verify.call_args.args[2], b"publisher-public-key")
        current_registry_read.assert_not_called()

    def test_decodes_registered_device_endpoint_and_public_key(self) -> None:
        record = decode_device_record(_encoded_device())

        self.assertTrue(record["authorized"])
        self.assertEqual(record["public_ip"], "https://worker0-8080.example")
        self.assertEqual(record["message_broker_ip"], "127.0.0.1")
        self.assertEqual(record["public_key_der"], b"participant-public-key")

    def test_reads_device_record_with_one_atomic_call(self) -> None:
        registry = "0x" + "22" * 20
        participant = "0x" + "44" * 20
        with patch(
            "agent.blockchain_source.rpc_call",
            return_value=_encoded_device(),
        ) as rpc:
            record = read_device_record("https://rpc.example", registry, participant)

        self.assertEqual(record["public_ip"], "https://worker0-8080.example")
        rpc.assert_called_once_with(
            "https://rpc.example",
            "eth_call",
            [
                {
                    "to": registry,
                    "data": "0x00d55318" + participant[2:].lower().rjust(64, "0"),
                },
                "latest",
            ],
        )

    def test_device_record_read_rejects_invalid_contract_or_participant_address(self) -> None:
        valid = "0x" + "22" * 20
        with self.assertRaisesRegex(RuntimeError, "Invalid REGISTRY_ADDRESS"):
            read_device_record("https://rpc.example", "registry", valid)
        with self.assertRaisesRegex(RuntimeError, "Invalid device address"):
            read_device_record("https://rpc.example", valid, "worker0")

    def test_rejects_unauthorized_device_endpoint(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not authorized"):
            decode_device_record(_encoded_device(authorized=False))


if __name__ == "__main__":
    unittest.main()

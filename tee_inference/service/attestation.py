"""dstack-bound AIR receipt and evidence-bundle emission."""

from __future__ import annotations

import hashlib
import http.client
import json
import socket
import time
from pathlib import Path

from nacl.signing import SigningKey

from tee_inference.air.v1 import AirClaims, emit_receipt
from tee_inference.protocol.v1 import decode_manifest, decode_request, decode_response, encode_deterministic


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, socket_path: str) -> None:
        super().__init__("localhost", timeout=30)
        self.socket_path = socket_path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.socket_path)


class DstackClient:
    def __init__(self, socket_path: str = "/var/run/dstack.sock") -> None:
        if not Path(socket_path).is_socket():
            raise RuntimeError(f"dstack socket is unavailable: {socket_path}")
        self.socket_path = socket_path

    def call(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        connection = _UnixConnection(self.socket_path)
        body = json.dumps(payload, separators=(",", ":")).encode()
        connection.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read()
        connection.close()
        if response.status != 200:
            raise RuntimeError(f"dstack {path} returned HTTP {response.status}")
        value = json.loads(raw)
        if isinstance(value, dict) and value.get("error"):
            raise RuntimeError(f"dstack {path} failed: {value['error']}")
        return value


class AirEvidenceEmitter:
    def __init__(self, manifest_bytes: bytes, socket_path: str = "/var/run/dstack.sock") -> None:
        self.manifest_bytes = manifest_bytes
        self.manifest = decode_manifest(manifest_bytes)
        self.manifest_hash = hashlib.sha256(manifest_bytes).digest()
        self.client = DstackClient(socket_path)
        key = self.client.call("/GetKey", {"path": "master-thesis/air-v1", "purpose": "air-ed25519-signing"})
        seed = bytes.fromhex(str(key["key"]))
        if len(seed) < 32:
            raise RuntimeError("dstack-derived AIR key is shorter than 32 bytes")
        self.signing_key = SigningKey(seed[:32])
        self.sequence = 0

    def emit(self, request_bytes: bytes, response_bytes: bytes) -> bytes:
        request = decode_request(request_bytes)
        response = decode_response(response_bytes)
        request_hash = hashlib.sha256(request_bytes).digest()
        response_hash = hashlib.sha256(response_bytes).digest()
        public_key = bytes(self.signing_key.verify_key)
        identity_hash = hashlib.sha256(b"MasterThesis.AIR.key.v1" + public_key + self.manifest_hash).digest()
        report_data = identity_hash + request_hash
        quote_result = self.client.call("/GetQuote", {"report_data": report_data.hex()})
        info = self.client.call("/Info", {})
        quote = bytes.fromhex(str(quote_result["quote"]).removeprefix("0x"))
        tcb_raw = info.get("tcb_info", {})
        tcb = json.loads(tcb_raw) if isinstance(tcb_raw, str) else tcb_raw
        self.sequence += 1
        receipt = emit_receipt(
            AirClaims(
                issuer="master-thesis-tee-inference",
                issued_at=int(time.time()),
                cti=request[2],
                nonce=request[5],
                model_id=self.manifest[2],
                model_version=self.manifest[3],
                model_hash=self.manifest_hash,
                request_hash=request_hash,
                response_hash=response_hash,
                attestation_doc_hash=hashlib.sha256(quote).digest(),
                enclave_measurements={
                    "measurement_type": "tdx-mrtd-rtmr",
                    "pcr0": bytes.fromhex(tcb["mrtd"]),
                    "pcr1": bytes.fromhex(tcb["rtmr0"]),
                    "pcr2": bytes.fromhex(tcb["rtmr1"]),
                    "pcr3": bytes.fromhex(tcb["rtmr2"]),
                    "pcr4": bytes.fromhex(tcb["rtmr3"]),
                },
                policy_version="master-thesis-air-v1",
                sequence_number=self.sequence,
                execution_time_ms=max(1, response[8] // 1_000),
                memory_peak_mb=0,
                security_mode="production",
            ),
            self.signing_key,
        )
        event_log = quote_result.get("event_log", "")
        app_compose = tcb.get("app_compose", "")
        return encode_deterministic({
            1: 1,
            2: request_bytes,
            3: response_bytes,
            4: receipt,
            5: public_key,
            6: quote,
            7: event_log if isinstance(event_log, str) else json.dumps(event_log, separators=(",", ":")),
            8: app_compose if isinstance(app_compose, str) else json.dumps(app_compose, separators=(",", ":")),
            9: self.manifest_bytes,
            10: report_data,
        })


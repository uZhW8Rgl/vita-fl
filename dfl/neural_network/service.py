from __future__ import annotations

import json
import base64
import hashlib
import os
import tempfile
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .cli import (
    aggregate,
    decrypt_model_package,
    model_from_bytes,
    private_key_path,
    received_models_dir,
    save_random,
    start_client,
    start_server,
    train_model,
)


server_thread: threading.Thread | None = None
server_stop_event: threading.Event | None = None
server_lock = threading.Lock()
model_receive_lock = threading.Lock()


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    return json.loads(handler.rfile.read(length).decode("utf-8"))


def _required_private_key(payload: dict[str, Any]) -> str:
    value = payload.get("private_key")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("private_key must identify the materialized participant key")
    return value


def _receive_model(payload: dict[str, Any]) -> dict[str, Any]:
    with model_receive_lock:
        device_id = str(payload["device_id"]).lower()
        if not device_id.startswith("0x") or len(device_id) != 42:
            raise ValueError("invalid Ethereum device address")
        int(device_id[2:], 16)
        package = base64.b64decode(str(payload["package_base64"]), validate=True)
        plain = decrypt_model_package(
            package,
            private_key_path(_required_private_key(payload)),
        )
        model_from_bytes(plain, f"worker model from {device_id}")
        model_sha256 = hashlib.sha256(plain).hexdigest()
        expected_model_sha256 = payload.get("expected_model_sha256")
        if expected_model_sha256:
            normalized_expected = str(expected_model_sha256).lower()
            if normalized_expected.startswith("0x"):
                normalized_expected = normalized_expected[2:]
            if len(normalized_expected) != 64 or any(
                character not in "0123456789abcdef" for character in normalized_expected
            ):
                raise ValueError("expected_model_sha256 must be a 32-byte hexadecimal digest")
            if model_sha256 != normalized_expected:
                raise ValueError("decrypted model does not match the already accepted on-chain hash")

        destination = received_models_dir() / f"wb_client_{device_id}.bin"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(plain)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = temporary.name
            os.replace(temporary_path, destination)
            temporary_path = None
        finally:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass
        return {
            "ok": True,
            "bytes": len(plain),
            "model_sha256": model_sha256,
        }


def _start_zmq_server(client_limit: int, private_key: str | None = None) -> dict[str, Any]:
    global server_thread, server_stop_event
    with server_lock:
        if server_thread and server_thread.is_alive():
            return {"running": True, "already_running": True}

        server_stop_event = threading.Event()
        server_thread = threading.Thread(
            target=start_server,
            kwargs={
                "client_limit": client_limit,
                "key_path": private_key,
                "stop_event": server_stop_event,
            },
            daemon=True,
        )
        server_thread.start()
        return {"running": True, "already_running": False}


def _stop_zmq_server() -> dict[str, Any]:
    global server_thread, server_stop_event
    with server_lock:
        if not server_thread or not server_thread.is_alive():
            server_thread = None
            server_stop_event = None
            return {"running": False}
        assert server_stop_event is not None
        server_stop_event.set()
        thread = server_thread

    thread.join(timeout=5)

    with server_lock:
        running = bool(server_thread and server_thread.is_alive())
        if not running:
            server_thread = None
            server_stop_event = None
        return {"running": running}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            _json_response(self, 200, {"ok": True})
            return
        if self.path == "/server/status":
            running = bool(server_thread and server_thread.is_alive())
            _json_response(self, 200, {"running": running})
            return
        _json_response(self, 404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        try:
            payload = _read_json(self)
            if self.path == "/train":
                train_model(
                    int(payload["epochs"]),
                    str(payload.get("aggregator_public_key_der_hex", "")),
                    payload.get("medical_signer_snapshot"),
                    private_key=_required_private_key(payload),
                )
                _json_response(self, 200, {"ok": True})
                return
            if self.path == "/client":
                start_client(
                    str(payload["server_ip"]),
                    str(payload["device_id"]),
                    int(payload.get("timeout_ms") or os.environ.get("MODEL_TRANSFER_TIMEOUT_MS", "20000")),
                )
                _json_response(self, 200, {"ok": True})
                return
            if self.path == "/model/receive":
                _json_response(self, 200, _receive_model(payload))
                return
            if self.path == "/aggregate":
                result = aggregate(
                    int(payload["num_files"]),
                    round_id=payload.get("round_id"),
                    source_round=payload.get("source_round"),
                    expected_models=payload.get("expected_models"),
                    participant_count=payload.get("participant_count"),
                    private_key=_required_private_key(payload),
                )
                _json_response(self, 200, {"ok": True, **result})
                return
            if self.path == "/random":
                save_random()
                _json_response(self, 200, {"ok": True})
                return
            if self.path == "/server/start":
                result = _start_zmq_server(
                    int(payload["client_limit"]),
                    payload.get("private_key"),
                )
                _json_response(self, 200, {"ok": True, **result})
                return
            if self.path == "/server/stop":
                result = _stop_zmq_server()
                _json_response(self, 200, {"ok": True, **result})
                return
            _json_response(self, 404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            _json_response(self, 500, {"ok": False, "error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        if self.path == "/health":
            return
        print(f"{self.address_string()} - {fmt % args}")


def main() -> int:
    host = os.environ.get("PYTHON_SERVICE_HOST", "127.0.0.1")
    port = int(os.environ.get("PYTHON_SERVICE_PORT", "8000"))
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Python ML service listening on http://{host}:{port}")
    httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

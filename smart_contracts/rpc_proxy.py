#!/usr/bin/env python3
"""Small fail-closed JSON-RPC proxy for the Phala contract-runtime Anvil.

Only read-only Ethereum RPC methods and signed raw transaction submission are
forwarded. Anvil/Hardhat admin methods and unlocked-account signing APIs never
reach the upstream node.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


UPSTREAM_URL = os.environ.get("RPC_PROXY_UPSTREAM", "http://anvil:8545")
LISTEN_HOST = os.environ.get("RPC_PROXY_HOST", "0.0.0.0")
LISTEN_PORT = int(os.environ.get("RPC_PROXY_PORT", "8545"))
MAX_BODY_BYTES = int(os.environ.get("RPC_PROXY_MAX_BODY_BYTES", str(1024 * 1024)))
UPSTREAM_TIMEOUT_SECONDS = float(os.environ.get("RPC_PROXY_TIMEOUT_SECONDS", "30"))

ALLOWED_METHODS = frozenset(
    {
        "eth_blockNumber",
        "eth_call",
        "eth_chainId",
        "eth_createAccessList",
        "eth_estimateGas",
        "eth_feeHistory",
        "eth_gasPrice",
        "eth_getBalance",
        "eth_getBlockByHash",
        "eth_getBlockByNumber",
        "eth_getBlockReceipts",
        "eth_getBlockTransactionCountByHash",
        "eth_getBlockTransactionCountByNumber",
        "eth_getCode",
        "eth_getFilterChanges",
        "eth_getFilterLogs",
        "eth_getLogs",
        "eth_getProof",
        "eth_getStorageAt",
        "eth_getTransactionByBlockHashAndIndex",
        "eth_getTransactionByBlockNumberAndIndex",
        "eth_getTransactionByHash",
        "eth_getTransactionCount",
        "eth_getTransactionReceipt",
        "eth_maxPriorityFeePerGas",
        "eth_newBlockFilter",
        "eth_newFilter",
        "eth_newPendingTransactionFilter",
        "eth_sendRawTransaction",
        "eth_syncing",
        "eth_uninstallFilter",
        "net_listening",
        "net_version",
        "web3_clientVersion",
    }
)


def error_response(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def forward_request(request_value: Any) -> dict[str, Any] | None:
    if not isinstance(request_value, dict):
        return error_response(None, -32600, "Invalid Request")

    request_id = request_value.get("id")
    method = request_value.get("method")
    if not isinstance(method, str) or method not in ALLOWED_METHODS:
        return error_response(request_id, -32601, "Method not allowed")

    upstream_request = urllib.request.Request(
        UPSTREAM_URL,
        data=json.dumps(request_value, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(upstream_request, timeout=UPSTREAM_TIMEOUT_SECONDS) as response:
            response_value = json.loads(response.read(MAX_BODY_BYTES + 1))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return error_response(request_id, -32000, "Upstream RPC unavailable")

    if request_id is None:
        return None
    if not isinstance(response_value, dict):
        return error_response(request_id, -32000, "Invalid upstream response")
    return response_value


class RpcProxyHandler(BaseHTTPRequestHandler):
    server_version = "DflRpcProxy/1.0"

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler name
        if self.path != "/health":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "3")
        self.end_headers()
        self.wfile.write(b"ok\n")

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler name
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if content_length <= 0 or content_length > MAX_BODY_BYTES:
            self._write_json(error_response(None, -32600, "Invalid request size"))
            return

        try:
            value = json.loads(self.rfile.read(content_length))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write_json(error_response(None, -32700, "Parse error"))
            return

        if isinstance(value, list):
            if not value:
                result: Any = error_response(None, -32600, "Invalid Request")
            else:
                result = [response for item in value if (response := forward_request(item)) is not None]
                if not result:
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self.end_headers()
                    return
        else:
            result = forward_request(value)
            if result is None:
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
        self._write_json(result)

    def _write_json(self, value: Any) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format_string: str, *args: Any) -> None:
        # Avoid logging raw JSON-RPC payloads or signed transaction bodies.
        if os.environ.get("RPC_PROXY_ACCESS_LOG") == "1":
            super().log_message(format_string, *args)


def main() -> None:
    server = ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), RpcProxyHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()

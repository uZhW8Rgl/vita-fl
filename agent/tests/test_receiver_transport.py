"""Regression checks for mandatory authorization on every receiver HTTP path."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent import mcp_server, tee_inference_client
from agent.sello_client import validate_receiver_security


class JsonResponse:
    status = 200
    headers = {}

    def __init__(self, value):
        self.body = json.dumps(value).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, limit=None):
        return self.body if limit is None else self.body[:limit]


class ReceiverTransportTests(unittest.TestCase):
    def test_metadata_reads_require_scoped_token_and_mtls(self):
        call = SimpleNamespace(headers={"Authorization": "Bearer scoped-token"}, client_identity=object())
        with (
            patch("agent.tee_inference_client.begin_receiver_call", return_value=call) as begin,
            patch("agent.tee_inference_client.open_receiver", return_value=JsonResponse({"ok": True})) as send,
            patch("agent.tee_inference_client.complete_receiver_call") as complete,
        ):
            tee_inference_client._json_request("https://worker.example", "/v1/jobs/abc", 30)
        begin.assert_called_once_with("jobs:read", "tee-inference", b"", receiver_base_url="https://worker.example")
        self.assertEqual(send.call_args.args[0].get_header("Authorization"), "Bearer scoped-token")
        self.assertIs(send.call_args.kwargs["identity"], call.client_identity)
        self.assertEqual(send.call_args.args[0].get_method(), "GET")
        complete.assert_not_called()  # Metadata is not itself a tool execution receipt.

    def test_tool_response_without_receipt_fails_closed(self):
        call = SimpleNamespace(headers={"Authorization": "Bearer scoped-token"}, client_identity=object())
        with (
            patch("agent.tee_inference_client.begin_receiver_call", return_value=call),
            patch("agent.tee_inference_client.open_receiver", return_value=JsonResponse({"ok": True})),
            patch("agent.tee_inference_client.complete_receiver_call", side_effect=RuntimeError("missing receipt")),
            self.assertRaisesRegex(RuntimeError, "missing receipt"),
        ):
            tee_inference_client.fetch_latest_verified_tee_model_bundle(endpoint="https://worker.example")

    def test_missing_authorization_stops_request_before_transport(self):
        with (
            patch("agent.tee_inference_client.begin_receiver_call", side_effect=RuntimeError("owner absent")),
            patch("agent.tee_inference_client.open_receiver") as send,
            self.assertRaisesRegex(RuntimeError, "owner absent"),
        ):
            tee_inference_client._json_request("https://worker.example", "/v1/jobs/abc", 30)
        send.assert_not_called()

    def test_zk_tool_requires_receipt_and_scoped_mtls_call(self):
        call = SimpleNamespace(headers={"Authorization": "Bearer zk-token"}, client_identity=object())
        with (
            patch("agent.mcp_server.zk_inference_enabled", return_value=True),
            patch("agent.mcp_server.ZK_INFERENCE_URL", "https://zk.example"),
            patch("agent.mcp_server.begin_receiver_call", return_value=call) as begin,
            patch("agent.mcp_server.open_receiver", return_value=JsonResponse({"ok": True})) as send,
            patch("agent.mcp_server.complete_receiver_call", return_value={"verified": True}) as complete,
        ):
            result = mcp_server._remote_call(
                "/v1/models/fetch", {}, receipt_action="fetch_latest_verified_zk_model_bundle"
            )
        begin.assert_called_once_with(
            "fetch_latest_verified_zk_model_bundle",
            "zk-inference",
            b"{}",
            receiver_base_url="https://zk.example",
        )
        self.assertEqual(send.call_args.args[0].get_header("Authorization"), "Bearer zk-token")
        self.assertIs(send.call_args.kwargs["identity"], call.client_identity)
        self.assertTrue(result["tool_receipt"]["verified"])
        complete.assert_called_once()

    def test_zk_bundle_read_is_authenticated(self):
        response = JsonResponse({})
        response.body = b"proof-bundle"
        response.read = lambda: response.body
        response.headers = SimpleNamespace(get_content_type=lambda: "application/cbor")
        call = SimpleNamespace(headers={"Authorization": "Bearer zk-token"}, client_identity=object())
        with (
            patch("agent.mcp_server.zk_inference_enabled", return_value=True),
            patch("agent.mcp_server.ZK_INFERENCE_URL", "https://zk.example"),
            patch("agent.mcp_server.begin_receiver_call", return_value=call) as begin,
            patch("agent.mcp_server.open_receiver", return_value=response) as send,
        ):
            self.assertEqual(mcp_server._remote_get_bytes("/v1/jobs/abc/transparency-bundle"), b"proof-bundle")
        begin.assert_called_once_with("receipts:read", "zk-inference", b"", receiver_base_url="https://zk.example")
        self.assertEqual(send.call_args.args[0].get_header("Authorization"), "Bearer zk-token")
        self.assertIs(send.call_args.kwargs["identity"], call.client_identity)

    def test_legacy_zk_receiver_call_cannot_skip_authorization(self):
        with (
            patch("agent.mcp_server.zk_inference_enabled", return_value=True),
            patch("agent.mcp_server.ZK_INFERENCE_URL", "https://zk.example"),
            patch("agent.mcp_server.open_receiver") as send,
            self.assertRaisesRegex(RuntimeError, "Legacy receiver endpoints"),
        ):
            mcp_server._remote_call("export-model", {})
        send.assert_not_called()

    def test_startup_requires_owner_and_tls_configuration(self):
        with (
            patch("agent.sello_client.owner_from_environment", return_value=None),
            patch("agent.sello_client.validate_client_configuration") as tls,
            self.assertRaisesRegex(RuntimeError, "mandatory"),
        ):
            validate_receiver_security()
        tls.assert_not_called()
        with (
            patch("agent.sello_client.owner_from_environment", return_value=object()),
            patch(
                "agent.sello_client.validate_client_configuration", side_effect=RuntimeError("TLS credentials absent")
            ),
            self.assertRaisesRegex(RuntimeError, "TLS credentials absent"),
        ):
            validate_receiver_security()


if __name__ == "__main__":
    unittest.main()

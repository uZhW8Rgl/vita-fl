from __future__ import annotations

import importlib
import io
import os
import unittest
from unittest.mock import Mock, patch


class DisabledZkTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        with patch.dict(
            os.environ,
            {
                "SELLO_SERVICE_SIGNING_SEED": "11" * 32,
                "SELLO_TOKEN_ISSUER_PUBLIC_KEY": "22" * 32,
                "ZK_INFERENCE_ORIGIN": "https://zk.example",
            },
            clear=True,
        ):
            cls.server = importlib.import_module("zk_inference.server")

    def handler(self, path: str):
        handler = object.__new__(self.server.Handler)
        handler.path = path
        handler.headers = {}
        handler.rfile = io.BytesIO(b"{}")
        handler._send_json = Mock()
        return handler

    def test_no_plain_http_startup_without_trusted_proxy_transport(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "transport is disabled"):
            self.server.main()

    def test_legacy_tools_are_not_http_routes(self) -> None:
        for path in ("/export-model", "/create-query", "/run-ezkl"):
            with self.subTest(path=path):
                handler = self.handler(path)
                handler.do_POST()
                self.assertEqual(handler._send_json.call_args.args[0], 404)

    def test_missing_mtls_identity_stops_operations(self) -> None:
        handler = self.handler("/v1/models/fetch")
        with patch.object(self.server.RUNTIME, "fetch_model") as fetch:
            handler.do_POST()
        self.assertEqual(handler._send_json.call_args.args[0], 401)
        fetch.assert_not_called()

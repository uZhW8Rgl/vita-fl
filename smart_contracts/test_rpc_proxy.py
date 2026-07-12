import unittest
from unittest.mock import patch

import rpc_proxy


class RpcProxyPolicyTest(unittest.TestCase):
    def test_rejects_anvil_admin_methods_without_forwarding(self):
        with patch("rpc_proxy.urllib.request.urlopen") as urlopen:
            result = rpc_proxy.forward_request(
                {"jsonrpc": "2.0", "id": 1, "method": "anvil_impersonateAccount", "params": ["0x0"]}
            )
        self.assertEqual(result["error"]["code"], -32601)
        urlopen.assert_not_called()

    def test_rejects_unlocked_account_transactions(self):
        with patch("rpc_proxy.urllib.request.urlopen") as urlopen:
            result = rpc_proxy.forward_request(
                {"jsonrpc": "2.0", "id": 2, "method": "eth_sendTransaction", "params": [{}]}
            )
        self.assertEqual(result["error"]["code"], -32601)
        urlopen.assert_not_called()

    def test_allows_signed_raw_transactions(self):
        self.assertIn("eth_sendRawTransaction", rpc_proxy.ALLOWED_METHODS)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from transparency_log.bootstrap import build_config


class BootstrapConfigTests(unittest.TestCase):
    def test_start_configuration_binds_virtual_scitt_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = root / "installed"
            installed.mkdir()
            for name in ("validate.js", "apply.js", "resolve.js", "actions.js", "scitt.js"):
                (installed / name).write_text(f"// {name}\n", encoding="utf-8")
            original = Path
            with patch("transparency_log.bootstrap.Path") as mocked_path:
                mocked_path.side_effect = lambda value: (
                    installed if value == "/opt/scitt/share/scitt/constitution" else original(value)
                )
                config = build_config(root, "0.0.0.0", 8000, False)
        self.assertEqual(config["command"]["type"], "Start")
        self.assertEqual(config["network"]["rpc_interfaces"]["rpc"]["bind_address"], "0.0.0.0:8000")
        self.assertEqual(len(config["command"]["start"]["constitution_files"]), 5)

    def test_recovery_configuration_uses_previous_service_identity(self) -> None:
        root = Path("/data")
        config = build_config(root, "0.0.0.0", 8000, True)
        self.assertEqual(config["command"]["type"], "Recover")
        self.assertEqual(
            config["command"]["recover"]["previous_service_identity_file"],
            "/data/previous_service_cert.pem",
        )

    def test_public_tls_hostname_is_included_without_removing_internal_names(self) -> None:
        with patch.dict("os.environ", {"SCITT_PUBLIC_URL": "https://runtime-8000s.dstack-test.phala.network"}):
            config = build_config(Path("/data"), "0.0.0.0", 8000, True)
        names = config["node_certificate"]["subject_alt_names"]
        self.assertIn("dNSName:runtime-8000s.dstack-test.phala.network", names)
        self.assertIn("dNSName:transparency-log", names)
        self.assertIn("iPAddress:127.0.0.1", names)
        self.assertEqual(config["output_files"]["node_certificate_file"], "/data/node_cert.pem")

    def test_invalid_public_origin_is_rejected(self) -> None:
        for url in ("http://runtime.example", "https://user:password@runtime.example", "https://runtime.example/path"):
            with self.subTest(url=url), patch.dict("os.environ", {"SCITT_PUBLIC_URL": url}):
                with self.assertRaisesRegex(ValueError, "HTTPS origin"):
                    build_config(Path("/data"), "0.0.0.0", 8000, True)


if __name__ == "__main__":
    unittest.main()

"""Exercise DNS ownership, instance routing, secret handling and TLS readiness."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from phala import ci_config, ui_domain

DOMAIN = "vita-fl.duckdns.org"
GATEWAY = "gateway.dstack-pha-prod9.phala.network"
TOKEN = "00000000-0000-0000-0000-000000000000"
APP = "a" * 40
INSTANCE = "b" * 40
METADATA = {
    "name": "master-thesis-contract-runtime-phala",
    "app_id": APP,
    "instance_id": INSTANCE,
    "gateway": {"cname": GATEWAY},
}


class DomainRoutingTests(unittest.TestCase):
    def test_gateway_follows_managed_endpoint_across_nodes(self):
        for suffix in ("dstack-pha-prod9.phala.network", "dstack-pha-prod5.phala.network"):
            self.assertEqual(ui_domain.gateway_for_endpoint(f"https://{APP}-443.{suffix}"), "gateway." + suffix)
        for endpoint in (
            "http://" + APP + "-443.dstack-pha-prod9.phala.network",
            "https://unrelated.example.org",
            "https://" + APP + "-443.dstack-pha-prod9.phala.network/redirect",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                ui_domain.gateway_for_endpoint(endpoint)

    def test_only_verified_runtime_identity_is_used(self):
        self.assertEqual(ui_domain.routing_target(METADATA, APP, GATEWAY), INSTANCE + ":443")
        for field, wrong in (
            ("app_id", "c" * 40),
            ("instance_id", "cvm-internal-id"),
            ("name", "unrelated-app"),
            ("gateway", {"cname": "gateway.other.phala.network"}),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "does not match"):
                ui_domain.routing_target({**METADATA, field: wrong}, APP, GATEWAY)

    def test_settings_reject_unrelated_domains_and_injection(self):
        ui_domain.validate_settings(DOMAIN, GATEWAY, TOKEN)
        for domain in ("other.example.org", "nested.vita-fl.duckdns.org", DOMAIN + "\nINJECTED=yes"):
            with self.subTest(domain=domain), self.assertRaises(ValueError):
                ui_domain.validate_settings(domain, GATEWAY, TOKEN)
        with self.assertRaises(ValueError):
            ui_domain.validate_settings(DOMAIN, "gateway.attacker.test", TOKEN)

    def test_dns_points_at_instance_instead_of_app_or_host_machine(self):
        client = Mock()
        client.request.return_value = METADATA
        with (
            patch.object(ui_domain, "public_dns", side_effect=[["8.8.8.8"], [], []]),
            patch.object(ui_domain, "DuckDNS") as dns,
        ):
            result = ui_domain.configure_dns(
                client, cvm_id="cvm-target", app_id=APP, domain=DOMAIN, gateway=GATEWAY, token=TOKEN
            )
        self.assertEqual(result, "https://" + DOMAIN)
        self.assertEqual(dns.return_value.update.call_args_list[0].kwargs, {"ip": "8.8.8.8"})
        self.assertEqual(dns.return_value.update.call_args_list[1].kwargs, {"txt": INSTANCE + ":443"})

    def test_stale_ipv6_record_is_removed(self):
        client = Mock()
        client.request.return_value = METADATA
        with (
            patch.object(ui_domain, "public_dns", side_effect=[["8.8.8.8"], [], ["2001:4860::1"]]),
            patch.object(ui_domain, "DuckDNS") as dns,
        ):
            ui_domain.configure_dns(client, cvm_id="x", app_id=APP, domain=DOMAIN, gateway=GATEWAY, token=TOKEN)
        self.assertEqual(dns.return_value.update.call_args_list[0].kwargs, {"clear": "true"})

    def test_missing_public_gateway_does_not_modify_dns(self):
        client = Mock()
        client.request.return_value = METADATA
        with (
            patch.object(ui_domain, "public_dns", side_effect=[["127.0.0.1"], []]),
            patch.object(ui_domain, "DuckDNS") as dns,
        ):
            with self.assertRaisesRegex(ValueError, "no public IPv4"):
                ui_domain.configure_dns(client, cvm_id="x", app_id=APP, domain=DOMAIN, gateway=GATEWAY, token=TOKEN)
        dns.assert_not_called()

    def test_wrong_runtime_does_not_modify_dns(self):
        client = Mock()
        client.request.return_value = {**METADATA, "app_id": "c" * 40}
        with patch.object(ui_domain, "DuckDNS") as dns, self.assertRaises(ValueError):
            ui_domain.configure_dns(client, cvm_id="x", app_id=APP, domain=DOMAIN, gateway=GATEWAY, token=TOKEN)
        dns.assert_not_called()


class DomainHttpTests(unittest.TestCase):
    def test_redirect_never_forwards_dns_credentials(self):
        handler = ui_domain.NoRedirect()
        self.assertIsNone(handler.redirect_request(None, None, 302, "", {}, "https://other.example.org"))

    def test_dns_http_error_does_not_expose_token_url(self):
        dns = ui_domain.DuckDNS(DOMAIN, TOKEN)
        error = urllib.error.HTTPError("https://www.duckdns.org/update?token=" + TOKEN, 403, TOKEN, {}, None)
        with patch.object(dns.opener, "open", side_effect=error), self.assertRaises(ValueError) as caught:
            dns.update(ip="8.8.8.8")
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertNotIn("token=", str(caught.exception))

    def test_rejected_dns_update_is_not_success(self):
        dns = ui_domain.DuckDNS(DOMAIN, TOKEN)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"KO"
        with patch.object(dns.opener, "open", return_value=response), self.assertRaisesRegex(ValueError, "rejected"):
            dns.update(ip="8.8.8.8")

    def test_https_requires_the_uis_authentication_gate(self):
        error = urllib.error.HTTPError(
            "https://" + DOMAIN, 401, "", {"WWW-Authenticate": 'Basic realm="Master Thesis DFL"'}, None
        )
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(ui_domain.urllib.request, "build_opener", return_value=opener):
            ui_domain.wait_for_https(DOMAIN, timeout=0)

    def test_wrong_origin_or_untrusted_certificate_cannot_pass_readiness(self):
        for error in (
            OSError("certificate verify failed"),
            urllib.error.HTTPError("https://" + DOMAIN, 404, "", {}, None),
        ):
            with self.subTest(error=type(error)):
                opener = Mock()
                opener.open.side_effect = error
                with (
                    patch.object(ui_domain.urllib.request, "build_opener", return_value=opener),
                    self.assertRaisesRegex(ValueError, "not ready"),
                ):
                    ui_domain.wait_for_https(DOMAIN, timeout=0)


class PrivateDomainConfigurationTests(unittest.TestCase):
    def test_optional_settings_are_repository_bound_and_decrypted_privately(self):
        document = {"format": 1, "repository": "example/repo", "domain": DOMAIN, "gateway": GATEWAY, "token": TOKEN}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "phala").mkdir()
            (root / "phala/ui-domain.env.gpg").write_bytes(b"encrypted")
            environ = {"PHALA_STATE_REPOSITORY": "example/repo", "PHALA_STATE_PASSPHRASE": "secret"}
            with patch.object(ci_config, "crypt", return_value=json.dumps(document).encode()):
                result = ci_config.ui_domain_settings(environ, root)
                self.assertEqual(result["DUCKDNS_TOKEN"], TOKEN)
                with self.assertRaisesRegex(ValueError, "private UI domain"):
                    ci_config.ui_domain_settings({**environ, "PHALA_STATE_REPOSITORY": "other/repo"}, root)
            with patch.object(ci_config, "crypt", side_effect=ValueError(TOKEN)):
                with self.assertRaises(ValueError) as caught:
                    ci_config.ui_domain_settings(environ, root)
                self.assertNotIn(TOKEN, str(caught.exception))


class IngressRenderingTests(unittest.TestCase):
    def test_shipped_runtime_renders_optional_pinned_ingress(self):
        root = Path(__file__).resolve().parent
        template = (root / "dstack-compose.contracts.phala.tftpl").read_text()
        names = set(re.findall(r"(?<!\$)\$\{(?:jsonencode\()?([a-zA-Z_]\w*)", template))
        values = dict.fromkeys(names, "test")
        values.update(
            enable_control_api=True,
            enable_ui=True,
            enable_agent=False,
            enable_zk_inference=False,
            agent_pop_registry={},
            ui_domain=DOMAIN,
            ui_gateway_domain=GATEWAY,
            dynamic_worker_inventory_environment="",
        )
        environment = {
            key: value for key, value in os.environ.items() if not key.startswith(("TF_VAR_", "TF_CLI_ARGS"))
        }
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "runtime.tftpl").write_text(template)
            (directory / "main.tf").write_text(
                'locals { values = jsondecode(file("${path.module}/values.json")) }\n'
                'output "compose" { value = templatefile("${path.module}/runtime.tftpl", local.values) }\n'
            )

            def run(*args):
                result = subprocess.run(
                    [str(root / "bin/terraform"), f"-chdir={directory}", *args],
                    env=environment,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result.stdout

            run("init", "-input=false", "-no-color")
            for domain in (DOMAIN, ""):
                (directory / "values.json").write_text(json.dumps({**values, "ui_domain": domain}))
                run("apply", "-input=false", "-auto-approve", "-no-color")
                compose = json.loads(run("output", "-json"))["compose"]["value"]
                if domain:
                    self.assertIn(ui_domain.INGRESS_IMAGE, compose)
                    self.assertIn("CHALLENGE_TYPE: tls-alpn-01", compose)
                    self.assertIn("TARGET_ENDPOINT: ui:8080", compose)
                    self.assertNotIn("DUCKDNS_TOKEN", compose)
                    Path("/tmp/vita-ui-ingress-rendered.yml").write_text(compose)
                else:
                    self.assertNotIn("ui-ingress", compose)

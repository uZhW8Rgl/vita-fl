"""Check deployment ordering and isolation of the public redirect artifact."""

import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import Mock, patch

from ui_redirect import pages as ui_pages

FIRST = "https://" + "a" * 40 + "-8088.dstack-pha-prod9.phala.network"
SECOND = "https://" + "b" * 40 + "-8088.dstack-pha-prod9.phala.network"


class PagesTests(unittest.TestCase):
    def test_actual_completion_order_wins_over_build_creation_order(self):
        github = Mock()
        runs = [
            {"id": 20, "name": "Publish Phala Agent", "head_branch": "phala_app_key", "conclusion": "success"},
            {"id": 10, "name": "Publish DFL Worker Image", "head_branch": "phala_app_key", "conclusion": "success"},
            {"id": 30, "name": "Publish Phala UI", "head_branch": "phala_app_key", "conclusion": "failure"},
        ]
        github.document.side_effect = [
            {"workflow_runs": runs},
            {"jobs": [self.job(20, "2026-10-12T16:30:00Z")]},
            {"jobs": [self.job(10, "2026-10-12T16:45:00Z")]},
        ]
        github.read.return_value = f"2026-10-12T16:45:00Z UI: {SECOND}\n".encode()
        self.assertEqual(ui_pages.latest_endpoint(github, "phala_app_key"), (SECOND, 10))
        github.read.assert_called_once_with("actions/jobs/10/logs")

    def test_stale_build_skip_does_not_replace_real_deployment(self):
        github = Mock()
        github.document.side_effect = [
            {
                "workflow_runs": [
                    {"id": 1, "name": "Publish Phala Agent", "head_branch": "phala_app_key", "conclusion": "success"}
                ]
            },
            {"jobs": [self.job(1, "2026-10-12T16:30:00Z"), self.job(2, "2026-10-12T16:45:00Z")]},
        ]
        github.read.side_effect = [b"Skipping stale build\n", f"UI: {FIRST}\nUI: {SECOND}\n".encode()]
        self.assertEqual(ui_pages.latest_endpoint(github, "phala_app_key"), (SECOND, 1))

    def test_other_branches_and_workflows_cannot_supply_redirect(self):
        github = Mock()
        github.document.return_value = {
            "workflow_runs": [
                {"id": 1, "name": "CI", "head_branch": "phala_app_key", "conclusion": "success"},
                {"id": 2, "name": "Publish Phala Agent", "head_branch": "untrusted", "conclusion": "success"},
            ]
        }
        with self.assertRaisesRegex(ValueError, "No successful"):
            ui_pages.latest_endpoint(github, "phala_app_key")
        github.read.assert_not_called()

    def test_artifact_contains_only_public_redirect_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            ui_pages.render_site(directory, FIRST)
            self.assertEqual({p.name for p in directory.iterdir()}, {"index.html", "endpoint.json", ".nojekyll"})
            self.assertEqual(json.loads((directory / "endpoint.json").read_text()), {"url": FIRST})
            page = (directory / "index.html").read_text()
            self.assertIn(FIRST, page)
            self.assertIn("no-referrer", page)

    def test_invalid_destinations_are_rejected(self):
        for endpoint in (
            "http://" + FIRST.removeprefix("https://"),
            "https://example.com",
            FIRST + "/?token=secret",
            FIRST + "#fragment",
            FIRST + "/unexpected",
            FIRST.replace("https://", "https://user:password@"),
            FIRST + ".evil.example",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                ui_pages.validate_endpoint(endpoint)

    def test_github_token_is_not_forwarded_to_log_storage(self):
        github = ui_pages.GitHub("example/repo", "private-test-token")
        storage_url = "https://logs.example.invalid/job.txt?signature=test"
        redirect = urllib.error.HTTPError("https://api.github.com", 302, "Found", {"Location": storage_url}, None)
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"public log"
        github.opener.open = Mock(side_effect=[redirect, response])
        self.assertEqual(github.read("actions/jobs/1/logs"), b"public log")
        initial, download = [call.args[0] for call in github.opener.open.call_args_list]
        self.assertEqual(initial.get_header("Authorization"), "Bearer private-test-token")
        self.assertIsNone(download.get_header("Authorization"))
        self.assertEqual(download.full_url, storage_url)

    def test_failed_ui_check_is_not_reported_as_ready(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status = 200
        response.read.return_value = b"not ready"
        opener = Mock()
        opener.open.return_value = response
        with patch.object(urllib.request, "build_opener", return_value=opener):
            with self.assertRaisesRegex(ValueError, "health check"):
                ui_pages.check_ui(FIRST)

    @staticmethod
    def job(identifier, completed_at):
        return {
            "id": identifier,
            "run_id": identifier,
            "name": "Deploy published image to Phala / deploy",
            "conclusion": "success",
            "completed_at": completed_at,
        }

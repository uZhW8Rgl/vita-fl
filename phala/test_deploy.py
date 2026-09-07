"""Lifecycle regression tests use simulated Terraform and HTTP only."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from phala import deploy

RUNTIME_NAME = "master-thesis-contract-runtime-phala"
OLD_ENDPOINT = "https://old-5001.dstack-test.phala.network"
NEW_ENDPOINT = "https://new-5001.dstack-test.phala.network"


def resource(address=deploy.RUNTIME_ADDRESS, name=RUNTIME_NAME, identifier="runtime", endpoint=OLD_ENDPOINT):
    return {
        "address": address,
        "mode": "managed",
        "type": "phala_app",
        "values": {
            "name": name,
            "app_id": identifier,
            "endpoint": endpoint,
            "image": "dstack-dev-0.5.9",
            "region": "US-WEST-1",
            "node_id": None,
        },
    }


def state(*items):
    return {"values": {"root_module": {"resources": list(items)}}}


def plan(actions="no-op", address=deploy.RUNTIME_ADDRESS):
    return {
        "planned_values": {"root_module": {"resources": [resource()]}},
        "resource_changes": [{"address": address, "type": "phala_app", "change": {"actions": [actions]}}],
    }


def node_catalog():
    return {
        "nodes": [
            {
                "teepod_id": 18,
                "region_identifier": "US-WEST-1",
                "images": [{"name": "dstack-dev-0.5.9", "slug": "dstack-dev-0.5.9-hash", "enabled": True}],
            }
        ]
    }


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.runtime = deploy.Cvm("runtime-cvm", RUNTIME_NAME, "runtime")
        self.worker = deploy.Cvm("worker-cvm", "master-thesis-dfl-worker-0", "worker")

    def test_unchanged_runtime_preserves_ui_workers_outside_state(self):
        decision = deploy.decide(state(resource()), plan(), [self.runtime, self.worker])
        self.assertFalse(decision.reset)

    def test_material_runtime_change_resets_existing_dynamic_workers(self):
        decision = deploy.decide(state(resource()), plan("update"), [self.runtime, self.worker])
        self.assertTrue(decision.reset)
        self.assertEqual({cvm.identifier for cvm in decision.scoped}, {"runtime-cvm", "worker-cvm"})

    def test_worker_policy_change_resets_immutable_roster(self):
        decision = deploy.decide(state(resource()), plan("update", "phala_app.dfl_worker[0]"), [self.runtime])
        self.assertTrue(decision.reset)

    def test_first_start_without_cloud_resources_does_not_destroy(self):
        self.assertFalse(deploy.decide({}, plan("create"), []).reset)

    def test_missing_state_with_existing_runtime_recovers(self):
        self.assertTrue(deploy.decide({}, plan("create"), [self.runtime]).reset)

    def test_missing_state_with_only_old_workers_recovers(self):
        self.assertTrue(deploy.decide({}, plan("create"), [self.worker]).reset)

    def test_cloud_deleted_runtime_with_stale_state_resets_workers(self):
        self.assertTrue(deploy.decide(state(resource()), plan("create"), [self.worker]).reset)
        self.assertIsNone(deploy.runtime_endpoint(state(resource()), [self.worker]))

    def test_scope_uses_exact_names_and_own_app_ids(self):
        unrelated = deploy.Cvm("other", "master-thesis-dfl-worker-0-production", "other-app")
        out_of_range = deploy.Cvm("500", "master-thesis-dfl-worker-500", "other-app-2")
        replica = deploy.Cvm("replica", "renamed-member", "runtime")
        decision = deploy.decide(
            state(resource()), plan(), [self.runtime, self.worker, unrelated, out_of_range, replica], recreate=True
        )
        self.assertEqual({cvm.identifier for cvm in decision.scoped}, {"runtime-cvm", "worker-cvm", "replica"})

    def test_custom_name_from_plan_is_recovered_even_without_state(self):
        desired = plan("create")
        desired["planned_values"]["root_module"]["resources"] = [resource(name="my-demo-runtime")]
        decision = deploy.decide({}, desired, [deploy.Cvm("x", "replica-two", "custom", "my-demo-runtime")])
        self.assertTrue(decision.reset)
        self.assertEqual(decision.app_ids, {"custom"})

    def test_worker_replica_does_not_look_like_orphan_runtime(self):
        replica = deploy.Cvm("replica", "replica-two", "worker", self.worker.name)
        self.assertFalse(deploy.decide(state(resource()), plan(), [self.runtime, replica]).reset)

    def test_explicit_recreate_and_destroy_reset_unchanged_deployment(self):
        self.assertTrue(deploy.decide(state(resource()), plan(), [self.runtime], recreate=True).reset)
        self.assertTrue(deploy.decide(state(resource()), plan(), [self.runtime], destroy=True).reset)


class RuntimeAndStateTests(unittest.TestCase):
    def test_gateway_ports_and_tls_passthrough(self):
        self.assertEqual(deploy.service_url(OLD_ENDPOINT + "/", 8545), OLD_ENDPOINT.replace("5001", "8545"))
        self.assertEqual(deploy.service_url(OLD_ENDPOINT, "8000s"), OLD_ENDPOINT.replace("5001", "8000s"))
        self.assertEqual(deploy.service_url(OLD_ENDPOINT.replace("5001", "8000s"), 5001), OLD_ENDPOINT)

    def test_regular_origins_replace_existing_ports(self):
        self.assertEqual(deploy.service_url("http://localhost:5001", 8545), "http://localhost:8545")
        self.assertEqual(deploy.service_url("https://example.org", 8080), "https://example.org:8080")
        self.assertEqual(deploy.service_url("http://[::1]:5001", 8545), "http://[::1]:8545")

    def test_invalid_endpoints_fail_before_mutation(self):
        for endpoint in (
            "file:///tmp/x",
            "https://secret@example.org",
            "https://example.org/path",
            "https://x/?token=y",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(deploy.DeploymentError):
                deploy.service_url(endpoint, 8545)

    def test_runtime_var_file_overrides_stale_tfvars_and_uses_real_null(self):
        terraform = deploy.Terraform(Path("/unused"), {"TF_VAR_runtime_endpoint_override": "stale"})
        captured = []

        def run(arguments, *, capture):
            captured.append((arguments, json.loads(Path(arguments[-1].split("=", 1)[1]).read_text())))
            return ""

        terraform._run = run
        terraform.wire_runtime(None)
        terraform.run("apply", "-input=false")
        self.assertIsNone(captured[0][1]["runtime_endpoint_override"])
        terraform.wire_runtime(NEW_ENDPOINT, sello=True)
        terraform.run("plan")
        self.assertEqual(captured[1][1]["runtime_rpc_url_override"], NEW_ENDPOINT.replace("5001", "8545"))
        self.assertEqual(captured[1][1]["sello_scitt_url"], NEW_ENDPOINT.replace("5001", "8000s"))

    def test_failed_apply_saves_partial_state_before_reporting_failure(self):
        terraform = deploy.Terraform(Path("/unused"), {})
        terraform.state_store = Mock()
        with patch.object(deploy.subprocess, "run", return_value=Mock(returncode=1)):
            with self.assertRaises(deploy.DeploymentError):
                terraform.run("apply")
        terraform.state_store.sync.assert_called_once_with()

    def test_read_only_terraform_commands_do_not_upload_state(self):
        terraform = deploy.Terraform(Path("/unused"), {})
        terraform.state_store = Mock()
        with patch.object(deploy.subprocess, "run", return_value=Mock(returncode=0, stdout="{}")):
            terraform.run("show", "-json", capture=True)
        terraform.state_store.sync.assert_not_called()

    def test_terminated_terraform_is_an_interruption(self):
        terraform = deploy.Terraform(Path("/unused"), {})
        with patch.object(deploy.subprocess, "run", return_value=Mock(returncode=-15)):
            with self.assertRaises(KeyboardInterrupt):
                terraform.run("apply")


class PhalaClientTests(unittest.TestCase):
    def setUp(self):
        self.client = deploy.PhalaClient("secret")

    def test_reads_all_cvm_pages(self):
        self.client.request = Mock(
            side_effect=[
                {"page": 1, "pages": 2, "items": [{"id": "a", "name": "a", "app_id": "app_one"}]},
                {"page": 2, "pages": 2, "items": [{"id": "b", "name": "b", "app_id": "two"}]},
            ]
        )
        cvms = self.client.list_cvms()
        self.assertEqual([cvm.identifier for cvm in cvms], ["a", "b"])
        self.assertEqual(cvms[0].app_id, "one")

    def test_invalid_pagination_fails_closed(self):
        self.client.request = Mock(return_value={"page": 1, "pages": 2, "items": []})
        with self.assertRaises(deploy.DeploymentError):
            self.client.list_cvms()

    def test_expands_replica_even_if_primary_missing_from_paginated_view(self):
        self.client.list_cvms = Mock(return_value=[])
        self.client.list_app_names = Mock(return_value={"app1": "master-thesis-dfl-worker-0", "other": "someone-else"})
        self.client.request = Mock(return_value=[{"id": "replica", "name": "renamed", "app_id": "app1"}])
        result = self.client.inventory(deploy.RESERVED_NAMES, set())
        self.assertEqual(result, [deploy.Cvm("replica", "renamed", "app1", "master-thesis-dfl-worker-0")])
        self.client.request.assert_called_once_with("GET", "/apps/app1/cvms")

    def test_rejects_app_replica_from_other_application(self):
        self.client.list_cvms = Mock(return_value=[])
        self.client.request = Mock(return_value=[{"id": "x", "name": "x", "app_id": "other"}])
        with self.assertRaises(deploy.DeploymentError):
            self.client.inventory(set(), {"owned"})

    def test_delete_waits_for_absence(self):
        cvm = deploy.Cvm("123", "worker", "app")
        self.client.request = Mock()
        self.client.inventory = Mock(side_effect=[[cvm], []])
        with patch.object(deploy.time, "sleep") as sleep:
            self.client.delete([cvm])
        self.client.request.assert_called_once_with("DELETE", "/cvms/123")
        sleep.assert_called_once_with(3)

    def test_delete_timeout_prevents_recreation(self):
        cvm = deploy.Cvm("123", "worker", "app")
        self.client.request = Mock()
        self.client.inventory = Mock(return_value=[cvm])
        with self.assertRaisesRegex(deploy.DeploymentError, "Deletion not confirmed"):
            self.client.delete([cvm], timeout=0)

    def test_http_auth_error_is_not_ignored_and_does_not_expose_key(self):
        error = urllib.error.HTTPError("https://example.org", 401, "secret detail", {}, None)
        with patch.object(deploy.urllib.request, "urlopen", side_effect=error):
            with self.assertRaisesRegex(deploy.DeploymentError, "HTTP 401") as result:
                self.client.request("DELETE", "/cvms/123")
        self.assertNotIn("secret", str(result.exception))

    def test_already_deleted_cvm_is_idempotent(self):
        error = urllib.error.HTTPError("https://example.org", 404, "Not found", {}, None)
        with patch.object(deploy.urllib.request, "urlopen", side_effect=error):
            self.assertIsNone(self.client.request("DELETE", "/cvms/123"))


class OsImagePreflightTests(unittest.TestCase):
    def setUp(self):
        self.client = deploy.PhalaClient("secret")
        self.catalog = node_catalog()
        self.client.request = Mock(return_value=self.catalog)
        self.plan = plan("create")
        self.values = self.plan["planned_values"]["root_module"]["resources"][0]["values"]

    def test_accepts_enabled_image_name_or_slug(self):
        for image in ("dstack-dev-0.5.9", "dstack-dev-0.5.9-hash"):
            with self.subTest(image=image):
                self.values["image"] = image
                self.values["node_id"] = 18
                self.client.validate_os_images(self.plan)
        self.client.request.assert_called_with("GET", "/teepods/available")

    def test_rejects_disabled_image(self):
        self.catalog["nodes"][0]["images"][0]["enabled"] = False
        with self.assertRaisesRegex(deploy.DeploymentError, "unavailable or disabled"):
            self.client.validate_os_images(self.plan)

    def test_rejects_image_missing_from_node(self):
        self.values["image"] = "dstack-dev-0.5.7"
        with self.assertRaisesRegex(deploy.DeploymentError, "dstack-dev-0.5.7"):
            self.client.validate_os_images(self.plan)

    def test_enabled_image_in_another_region_does_not_satisfy_placement(self):
        self.catalog["nodes"][0]["region_identifier"] = "EU-WEST-1"
        with self.assertRaisesRegex(deploy.DeploymentError, "region US-WEST-1"):
            self.client.validate_os_images(self.plan)

    def test_enabled_image_on_another_node_does_not_satisfy_pinning(self):
        self.values["node_id"] = 26
        with self.assertRaisesRegex(deploy.DeploymentError, "node 26"):
            self.client.validate_os_images(self.plan)

    def test_missing_and_malformed_catalogs_fail_closed(self):
        for payload in (
            None,
            {},
            {"nodes": None},
            {"nodes": [{}]},
            {"nodes": [{"teepod_id": 18, "region_identifier": "US-WEST-1", "images": [{}]}]},
        ):
            with self.subTest(payload=payload):
                self.client.request.return_value = payload
                with self.assertRaisesRegex(deploy.DeploymentError, "invalid node/OS image catalog"):
                    self.client.validate_os_images(self.plan)

    def test_image_without_explicit_enabled_status_fails_closed(self):
        for enabled in (None, "true", 1):
            with self.subTest(enabled=enabled):
                self.catalog["nodes"][0]["images"][0]["enabled"] = enabled
                with self.assertRaisesRegex(deploy.DeploymentError, "invalid node/OS image catalog"):
                    self.client.validate_os_images(self.plan)

    def test_missing_planned_image_fails_before_cloud_request(self):
        del self.values["image"]
        with self.assertRaisesRegex(deploy.DeploymentError, "Cannot validate OS image placement"):
            self.client.validate_os_images(self.plan)
        self.client.request.assert_not_called()

    def test_dynamic_workers_are_checked_before_ui_creates_them(self):
        self.plan["variables"] = {
            "enable_phala_control_api": {"value": True},
            "dynamic_worker_os_image": {"value": "dstack-dev-0.5.7"},
            "dynamic_worker_node_id": {"value": 18},
            "region": {"value": "US-WEST-1"},
        }
        with self.assertRaisesRegex(deploy.DeploymentError, "dynamic UI workers"):
            self.client.validate_os_images(self.plan)
        self.plan["variables"]["dynamic_worker_os_image"]["value"] = "dstack-dev-0.5.9"
        self.client.validate_os_images(self.plan)

    def test_disabled_control_api_does_not_require_dynamic_worker_image(self):
        self.plan["variables"] = {
            "enable_phala_control_api": {"value": False},
            "dynamic_worker_os_image": {"value": "dstack-dev-0.5.7"},
        }
        self.client.validate_os_images(self.plan)


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.directory = self.root / "phala"
        self.directory.mkdir()
        self.env_file = self.root / ".env.phala.anvil"
        self.env_file.write_text(
            "PHALA_CLOUD_API_KEY=key\nENABLE_PHALA_CONTROL_API=true\n"
            "PHALA_RUNTIME_ENDPOINT_OVERRIDE=https://stale-5001.dstack-test.phala.network\n"
        )
        self.events = []
        self.initial_state = state(resource())
        self.current_state = self.initial_state
        self.current_plan = plan()
        self.catalog = node_catalog()
        self.cloud_cvms = [
            deploy.Cvm("runtime-cvm", RUNTIME_NAME, "runtime"),
            deploy.Cvm("worker-cvm", "master-thesis-dfl-worker-0", "worker"),
            deploy.Cvm("unrelated", "other-application", "other"),
        ]
        self.fail_command = None
        self.fail_bootstrap_plan = False
        scenario = self

        class FakeTerraform(deploy.Terraform):
            def run(self, *args, capture=False):
                scenario.events.append(("tf", args, dict(self.runtime_vars)))
                if args[0] == scenario.fail_command:
                    raise deploy.DeploymentError("simulated failure")
                if args[0] == "destroy":
                    scenario.current_state = {}
                    scenario.cloud_cvms = [cvm for cvm in scenario.cloud_cvms if cvm.identifier != "runtime-cvm"]
                if args[0] == "apply" and f"-target={deploy.RUNTIME_ADDRESS}" in args:
                    scenario.current_state = state(resource(identifier="new-runtime", endpoint=NEW_ENDPOINT))
                    scenario.cloud_cvms.append(deploy.Cvm("new-runtime-cvm", RUNTIME_NAME, "new-runtime"))
                return ""

            def state(self):
                return scenario.current_state

            def plan(self, path, *, destroy=False):
                scenario.events.append(("plan", destroy, dict(self.runtime_vars)))
                if scenario.fail_command == "plan":
                    raise deploy.DeploymentError("simulated invalid configuration")
                if scenario.fail_bootstrap_plan and self.runtime_vars.get("runtime_endpoint_override") is None:
                    raise deploy.DeploymentError("simulated runtime bootstrap failure")
                return scenario.current_plan

        class FakeClient(deploy.PhalaClient):
            def __init__(self, *_args):
                pass

            def list_cvms(self):
                return list(scenario.cloud_cvms)

            def inventory(self, _names, _ids):
                return list(scenario.cloud_cvms)

            def request(self, method, path):
                scenario.events.append(("request", method, path))
                if (method, path) != ("GET", "/teepods/available"):
                    raise AssertionError(f"Unexpected Phala request: {method} {path}")
                return scenario.catalog

            def delete(self, cvms):
                for cvm in cvms:
                    scenario.events.append(("delete", cvm.identifier))
                    if scenario.fail_command == "delete":
                        raise deploy.DeploymentError("simulated cloud failure")
                    scenario.cloud_cvms.remove(cvm)

        self.addCleanup(patch.stopall)
        patch.object(deploy, "Terraform", FakeTerraform).start()
        patch.object(deploy, "PhalaClient", FakeClient).start()
        patch.dict(
            os.environ,
            {
                "PHALA_ENV_FILE": str(self.env_file),
                "PHALA_IMAGE_VARS_FILE": str(self.root / "pinned.tfvars.json"),
            },
            clear=True,
        ).start()

    def run_deployment(self, **options):
        args = argparse.Namespace(
            destroy=False,
            recreate=False,
            dry_run=False,
            init_only=False,
            init_github_state=False,
            unlock_github_state=None,
            recover_github_state=None,
            github_state_lock_id=None,
            pinned=False,
        )
        for key, value in options.items():
            setattr(args, key, value)
        with contextlib.redirect_stdout(io.StringIO()):
            deploy.execute(args, self.directory)

    def mutations(self):
        return [
            event
            for event in self.events
            if event[0] == "delete" or (event[0] == "tf" and event[1][0] in {"apply", "destroy"})
        ]

    def test_dry_run_reports_reset_without_any_apply_destroy_delete(self):
        self.current_plan = plan("update")
        self.run_deployment(dry_run=True)
        self.assertFalse(self.mutations())

    def test_bootstrap_plan_failure_preserves_existing_apps_and_workers(self):
        self.current_plan = plan("update")
        self.fail_bootstrap_plan = True
        original_cvms = list(self.cloud_cvms)
        with self.assertRaisesRegex(deploy.DeploymentError, "runtime bootstrap failure"):
            self.run_deployment()
        self.assertFalse(self.mutations())
        self.assertEqual(self.cloud_cvms, original_cvms)
        plans = [event for event in self.events if event[0] == "plan"]
        self.assertEqual(len(plans), 2)
        self.assertEqual(plans[0][2]["runtime_endpoint_override"], OLD_ENDPOINT)
        self.assertIsNone(plans[1][2]["runtime_endpoint_override"])

    def test_dry_run_also_validates_the_bootstrap_phase(self):
        self.current_plan = plan("update")
        self.fail_bootstrap_plan = True
        with self.assertRaisesRegex(deploy.DeploymentError, "runtime bootstrap failure"):
            self.run_deployment(dry_run=True)
        self.assertFalse(self.mutations())

    def test_unavailable_os_preserves_existing_apps_and_workers(self):
        self.current_plan = plan("update")
        self.catalog["nodes"][0]["images"][0]["enabled"] = False
        original_cvms = list(self.cloud_cvms)
        with self.assertRaisesRegex(deploy.DeploymentError, "unavailable or disabled"):
            self.run_deployment(recreate=True)
        self.assertFalse(self.mutations())
        self.assertEqual(self.cloud_cvms, original_cvms)

    def test_dry_run_also_rejects_unavailable_os(self):
        self.catalog["nodes"] = []
        with self.assertRaisesRegex(deploy.DeploymentError, "unavailable or disabled"):
            self.run_deployment(dry_run=True)
        self.assertFalse(self.mutations())

    def test_destroy_does_not_require_available_os_images(self):
        self.catalog = None
        self.run_deployment(destroy=True)
        self.assertFalse(any(event[0] == "request" for event in self.events))
        self.assertTrue(any(event[0] == "tf" and event[1][0] == "destroy" for event in self.mutations()))

    def test_destroy_does_not_require_a_valid_bootstrap(self):
        self.fail_bootstrap_plan = True
        self.run_deployment(destroy=True)
        self.assertTrue(any(event[0] == "tf" and event[1][0] == "destroy" for event in self.mutations()))

    def test_init_only_never_resolves_images_or_reads_cloud(self):
        self.run_deployment(init_only=True)
        self.assertEqual([event[1][0] for event in self.events], ["init"])

    def test_phala_profile_does_not_read_shared_configuration(self):
        # A stale/unreadable local Compose profile must not break Phala starts.
        (self.root / ".env.shared").write_bytes(b"\xff")
        self.run_deployment(init_only=True)
        self.assertEqual([event[1][0] for event in self.events], ["init"])

    def test_missing_api_key_is_not_loaded_from_shared_configuration(self):
        self.env_file.write_text("ENABLE_PHALA_CONTROL_API=true\n")
        (self.root / ".env.shared").write_text("PHALA_CLOUD_API_KEY=shared-only-key\n")
        with patch.object(deploy, "PhalaClient") as client:
            client.side_effect = deploy.DeploymentError("PHALA_CLOUD_API_KEY is required")
            with self.assertRaises(deploy.DeploymentError):
                self.run_deployment(dry_run=True)
        self.assertEqual(client.call_args.args[0], "")
        self.assertFalse(self.mutations())

    def test_shared_state_is_restored_before_terraform_starts(self):
        store = Mock()

        @contextlib.contextmanager
        def session(*, initialize):
            self.events.append(("restore", initialize))
            yield store
            self.events.append(("save_and_unlock",))

        store.session.side_effect = session
        with patch.dict(os.environ, PHALA_STATE_REPOSITORY="owner/repository"):
            with patch.object(deploy.GitHubState, "from_environment", return_value=store):
                self.run_deployment(init_only=True, init_github_state=True)
        self.assertEqual([event[0] for event in self.events], ["restore", "tf", "save_and_unlock"])
        self.assertEqual(self.events[0], ("restore", True))

    def test_failed_shared_state_restore_prevents_all_deployment_commands(self):
        store = Mock()
        store.session.side_effect = deploy.StateError("conflicting lock")
        with patch.dict(os.environ, PHALA_STATE_REPOSITORY="owner/repository"):
            with patch.object(deploy.GitHubState, "from_environment", return_value=store):
                with self.assertRaises(deploy.StateError):
                    self.run_deployment()
        self.assertEqual(self.events, [])

    def test_explicit_recovery_never_starts_or_deletes_apps(self):
        store = Mock()
        with patch.dict(os.environ, PHALA_STATE_REPOSITORY="owner/repository"):
            with patch.object(deploy.GitHubState, "from_environment", return_value=store):
                self.run_deployment(recover_github_state="recovery.gpg", github_state_lock_id="lock-id")
        store.recover.assert_called_once_with(Path("recovery.gpg"), "lock-id")
        store.session.assert_not_called()
        self.assertEqual(self.events, [])

    def test_external_backend_requires_explicit_migration_before_shared_state(self):
        (self.directory / "deployment-backend_override.tf").write_text('terraform { backend "http" {} }')
        with patch.dict(os.environ, PHALA_STATE_REPOSITORY="owner/repository"):
            with self.assertRaises(deploy.DeploymentError):
                self.run_deployment(init_only=True, init_github_state=True)
        self.assertEqual(self.events, [])

    def test_unchanged_deployment_preserves_existing_training_workers(self):
        self.run_deployment()
        self.assertFalse(any(event[0] == "delete" for event in self.events))
        self.assertFalse(any(event[0] == "tf" and event[1][0] == "destroy" for event in self.events))
        final_apply = self.mutations()[-1]
        self.assertEqual(final_apply[2]["runtime_endpoint_override"], OLD_ENDPOINT)

    def test_reset_persists_manifest_then_destroys_before_worker_cleanup_and_bootstrap(self):
        self.current_plan = plan("update")
        self.run_deployment()
        mutations = self.mutations()
        self.assertIn("-target=terraform_data.deployment_manifest", mutations[0][1])
        self.assertEqual(mutations[1][1][0], "destroy")
        self.assertIn(f"-target={deploy.RUNTIME_ADDRESS}", mutations[1][1])
        self.assertEqual(mutations[2], ("delete", "worker-cvm"))
        self.assertIn(f"-target={deploy.RUNTIME_ADDRESS}", mutations[3][1])
        self.assertIsNone(mutations[3][2]["runtime_endpoint_override"])
        self.assertEqual(mutations[-1][2]["runtime_endpoint_override"], NEW_ENDPOINT)
        self.assertIn("unrelated", {cvm.identifier for cvm in self.cloud_cvms})

    def test_first_deployment_only_creates_runtime_before_final_apply(self):
        self.current_state = {}
        self.cloud_cvms = []
        self.current_plan = plan("create")
        self.run_deployment()
        mutations = self.mutations()
        self.assertEqual(len(mutations), 3)
        self.assertIn(f"-target={deploy.RUNTIME_ADDRESS}", mutations[1][1])
        self.assertEqual(mutations[2][2]["runtime_rpc_url_override"], NEW_ENDPOINT.replace("5001", "8545"))

    def test_missing_state_recovers_orphans_without_destroying_release_manifest(self):
        self.current_state = {}
        self.current_plan = plan("create")
        self.run_deployment()
        mutations = self.mutations()
        self.assertFalse(any(event[0] == "tf" and event[1][0] == "destroy" for event in mutations))
        deleted = [event[1] for event in mutations if event[0] == "delete"]
        self.assertEqual(deleted, ["runtime-cvm", "worker-cvm"])

    def test_preflight_failure_keeps_everything_intact(self):
        self.fail_command = "plan"
        with self.assertRaises(deploy.DeploymentError):
            self.run_deployment()
        self.assertFalse(self.mutations())

    def test_failed_destroy_does_not_cleanup_or_create(self):
        self.fail_command = "destroy"
        with self.assertRaises(deploy.DeploymentError):
            self.run_deployment(recreate=True)
        self.assertEqual(len(self.mutations()), 2)  # release snapshot then failed destroy

    def test_failed_cloud_cleanup_does_not_create_new_runtime(self):
        self.fail_command = "delete"
        with self.assertRaises(deploy.DeploymentError):
            self.run_deployment(recreate=True)
        self.assertFalse(
            any(
                event[0] == "tf" and event[1][0] == "apply" and f"-target={deploy.RUNTIME_ADDRESS}" in event[1]
                for event in self.events
            )
        )

    def test_destroy_cleans_workers_and_does_not_bootstrap(self):
        self.run_deployment(destroy=True)
        self.assertFalse(any(event[0] == "tf" and event[1][0] == "apply" for event in self.events))
        self.assertEqual([cvm.identifier for cvm in self.cloud_cvms], ["unrelated"])


if __name__ == "__main__":
    unittest.main()

import hashlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from phala.resolve_images import (
    IMAGE_KEYS,
    assignments,
    image_values,
    main,
    resolve_reference,
    write_values,
)


def pin(key, digit="a"):
    return f"ghcr.io/test/{key}@sha256:{digit * 64}"


class ImageResolutionTests(unittest.TestCase):
    def setUp(self):
        self.sources = {key: f"ghcr.io/test/{key}:release" for key in IMAGE_KEYS}
        self.pins = {key: pin(key) for key in IMAGE_KEYS}

    def test_ci_update_preserves_other_components_and_revisions(self):
        state = {
            "deployment_images": {"value": self.pins},
            "deployment_image_revisions": {"value": {"worker_image": "a" * 40, "ui_image": "b" * 40}},
        }
        result = image_values(
            self.sources, state, {"worker_image": pin("worker_image", "b")}, {"worker_image": "c" * 40}, {}
        )
        self.assertEqual(result["worker_image"], pin("worker_image", "b"))
        for key in IMAGE_KEYS[1:]:
            self.assertEqual(result[key], self.pins[key])
        self.assertEqual(result["deployment_image_revisions"], {"worker_image": "c" * 40, "ui_image": "b" * 40})

    def test_fresh_launch_resolves_tags_despite_old_env_pins(self):
        seen = []

        def resolver(value):
            seen.append(value)
            return pin("resolved")

        image_values(
            self.sources, {}, {}, {}, {key.upper(): value for key, value in self.pins.items()}, resolver=resolver
        )
        self.assertEqual(set(seen), set(self.sources.values()))

    def test_pinned_launch_is_offline_and_requires_complete_pins(self):
        result = image_values(
            self.sources, {}, {}, {}, {key.upper(): value for key, value in self.pins.items()}, pinned=True
        )
        self.assertEqual(result["worker_image"], self.pins["worker_image"])
        with self.assertRaisesRegex(ValueError, "--pinned requires"):
            image_values(self.sources, {}, {}, {}, {}, pinned=True)

    def test_replaced_digest_drops_old_revision(self):
        state = {
            "deployment_images": {"value": self.pins},
            "deployment_image_revisions": {"value": {"worker_image": "a" * 40}},
        }
        result = image_values(self.sources, state, {"worker_image": pin("worker_image", "b")}, {}, {})
        self.assertEqual(result["deployment_image_revisions"], {})

    def test_invalid_overrides_and_state_fail_before_resolution(self):
        for override in ["unknown=x", "worker_image=ghcr.io/a/b:latest", "worker_image=x"]:
            with self.assertRaises(ValueError):
                assignments([override])
        with self.assertRaises(ValueError):
            assignments(["worker_image=1234"], revision=True)
        with self.assertRaises(ValueError):
            image_values(self.sources, {"deployment_images": {"value": {"ui_image": "latest"}}}, {}, {}, {})
        with self.assertRaises(ValueError):
            image_values(self.sources, {}, {}, {"worker_image": "a" * 40}, {})

    def test_manifest_digest_is_computed_and_checked(self):
        payload = b'{"schemaVersion":2,"manifests":[]}'
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        auth = io.BytesIO(b'{"token":"registry-token"}')
        manifest = io.BytesIO(payload)
        manifest.headers = {"Docker-Content-Digest": digest}
        with patch("urllib.request.urlopen", side_effect=[auth, manifest]) as request:
            self.assertEqual(resolve_reference("ghcr.io/test/image:release"), f"ghcr.io/test/image@{digest}")
        self.assertIn("application/vnd.oci.image.index.v1+json", request.call_args.args[0].headers["Accept"])
        auth = io.BytesIO(b'{"token":"registry-token"}')
        manifest = io.BytesIO(payload)
        manifest.headers = {"Docker-Content-Digest": "sha256:" + "0" * 64}
        with patch("urllib.request.urlopen", side_effect=[auth, manifest]):
            with self.assertRaisesRegex(ValueError, "digest mismatch"):
                resolve_reference("ghcr.io/test/image:release")

    def test_complete_output_written_privately_without_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "images.tfvars.json"
            write_values(target, self.pins)
            self.assertEqual(json.loads(target.read_text()), self.pins)
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).iterdir()), [target])

    def invoke_pinned_cli(self, *, missing_key=None, unreadable_shared=False, selected_by="argument"):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            script_directory = directory / "phala"
            script_directory.mkdir()
            (script_directory / "image-sources.json").write_text(json.dumps(self.sources))
            shared = directory / ".env.shared"
            shared.write_text("\n".join(f"{key.upper()}={pin(key, 'b')}" for key in IMAGE_KEYS))
            env_path = directory / (".env.phala.anvil" if selected_by == "default" else "selected.env")
            env_path.write_text(
                "\n".join(f"{key.upper()}={value}" for key, value in self.pins.items() if key != missing_key)
            )
            output = directory / "images.json"
            arguments = ["resolve_images.py", "--pinned", "--output", str(output)]
            if selected_by == "argument":
                arguments.extend(["--env-file", str(env_path)])
            environment = {"PHALA_ENV_FILE": str(env_path)} if selected_by == "environment" else {}
            read_text = Path.read_text

            def guarded_read(path, *args, **kwargs):
                if unreadable_shared and path == shared:
                    raise PermissionError("shared configuration must never be read")
                return read_text(path, *args, **kwargs)

            with (
                patch("phala.resolve_images.SCRIPT_DIR", script_directory),
                patch.dict(os.environ, environment, clear=True),
                patch("sys.argv", arguments),
                patch.object(Path, "read_text", guarded_read),
                patch("urllib.request.urlopen", side_effect=AssertionError("pinned resolution must stay offline")),
                patch("sys.stdout", new_callable=io.StringIO),
                patch("sys.stderr", new_callable=io.StringIO) as errors,
            ):
                status = main()
            return status, json.loads(output.read_text()) if output.exists() else None, errors.getvalue()

    def test_cli_uses_only_selected_configuration_file(self):
        for selected_by in ("argument", "environment", "default"):
            with self.subTest(selected_by=selected_by):
                status, values, errors = self.invoke_pinned_cli(selected_by=selected_by)
                self.assertEqual(status, 0, errors)
                self.assertEqual({key: values[key] for key in IMAGE_KEYS}, self.pins)

    def test_shared_file_cannot_supply_missing_image_pin(self):
        status, values, errors = self.invoke_pinned_cli(missing_key="worker_image")
        self.assertEqual(status, 1)
        self.assertIsNone(values)
        self.assertIn("--pinned requires WORKER_IMAGE", errors)

    def test_unreadable_shared_file_does_not_affect_resolution(self):
        status, values, errors = self.invoke_pinned_cli(unreadable_shared=True)
        self.assertEqual(status, 0, errors)
        self.assertEqual(values["worker_image"], self.pins["worker_image"])


if __name__ == "__main__":
    unittest.main()

"""Materialize image contexts and exercise Docker's real ignore/COPY behavior.

The scratch probes use tiny stand-ins at the real Dockerfile source paths. They
need no base images, package installs, running containers, or registry access.
"""

from __future__ import annotations

import io
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import uuid
from pathlib import Path

from scripts.prepare_tee_build_context import prepare_context

ROOT = Path(__file__).resolve().parents[2]
DOCKERFILES = sorted(ROOT.glob("*/Dockerfile"))


def local_copy_sources(dockerfile: Path) -> list[str]:
    """Read the single-line local COPY instructions used by this repository."""
    result = []
    for line in dockerfile.read_text().splitlines():
        match = re.match(r"^COPY\s+(.+)$", line)
        if not match:
            continue
        value = match.group(1)
        if value.startswith("--from="):
            continue
        sources = json.loads(value) if value.startswith("[") else shlex.split(value)
        if any(source.startswith("--") for source in sources):
            raise AssertionError(f"Extend the COPY probe for this instruction: {line}")
        result.extend(sources[:-1])
    return result


def source_context(dockerfile: Path) -> Path:
    return dockerfile.parent if dockerfile.parent.name == "transparency_log" else ROOT


def effective_ignore(dockerfile: Path) -> Path:
    specific = dockerfile.with_name(dockerfile.name + ".dockerignore")
    return specific if specific.exists() else source_context(dockerfile) / ".dockerignore"


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except subprocess.TimeoutExpired:
        return False


class TeeContextStagingTests(unittest.TestCase):
    def test_real_tracked_context_contains_every_local_copy_source(self):
        with tempfile.TemporaryDirectory(prefix="vita-tee-context-") as temporary:
            context = Path(temporary) / "context"
            self.assertGreater(prepare_context(ROOT, context), 0)
            for source in local_copy_sources(ROOT / "tee_inference/Dockerfile"):
                with self.subTest(source=source):
                    self.assertTrue(list(context.glob(source)), f"Missing staged Docker COPY input: {source}")
            for source in (
                "pki/runtime.py",
                "pki/ratls_runtime.py",
                "pki/nginx-ratls.conf.template",
                "transport_security/ratls_client.py",
                "transport_security/attestation.py",
            ):
                self.assertEqual((context / source).read_bytes(), (ROOT / source).read_bytes())
            self.assertGreater((context / "data/chestmnist/test_data/test-data.npz").stat().st_size, 0)
            self.assertFalse(list(context.rglob("__pycache__")))
            self.assertFalse((context / "pki/state").exists())
            self.assertFalse((context / "pki/secrets").exists())

    def test_existing_output_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix="vita-existing-context-") as temporary:
            context = Path(temporary)
            sentinel = context / "keep.txt"
            sentinel.write_text("existing user data")
            with self.assertRaises(FileExistsError):
                prepare_context(ROOT, context)
            self.assertEqual(sentinel.read_text(), "existing user data")


@unittest.skipUnless(docker_available(), "Docker daemon is needed for real ignore/COPY context probes")
class DockerContextTests(unittest.TestCase):
    def probe(self, dockerfile: Path, *, inject_private_files=False, real_staged_tee=False) -> set[str]:
        with tempfile.TemporaryDirectory(prefix="vita-docker-context-") as temporary:
            context = Path(temporary) / "context"
            if real_staged_tee:
                prepare_context(ROOT, context)
            else:
                context.mkdir()
            sources = local_copy_sources(dockerfile)
            for source in () if real_staged_tee else sources:
                matches = list(source_context(dockerfile).glob(source))
                self.assertTrue(matches, f"Missing checkout COPY input for {dockerfile}: {source}")
                for actual in matches:
                    relative = actual.relative_to(source_context(dockerfile))
                    target = context / relative
                    if actual.is_dir():
                        target /= "context-probe.txt"
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(f"Public context fixture: {relative}\n")
            # Apply the effective policy to the scratch probe's context. Dockerfile-
            # specific policies replace, rather than extend, root .dockerignore.
            shutil.copyfile(effective_ignore(dockerfile), context / ".dockerignore")
            if inject_private_files:
                for source in (
                    "pki/secrets/secret-marker.key",
                    "pki/state/secret-marker.pem",
                    "pki/private_key-secret-marker.pem",
                    "pki/.env",
                    "transport_security/__pycache__/secret-marker.pyc",
                ):
                    target = context / source
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text("SECRET-MARKER: must not reach image")
            probe = context / "Dockerfile.context-probe"
            probe.write_text(
                "FROM scratch\n"
                + "\n".join(f"COPY {json.dumps([source, f'/probe/{i}/'])}" for i, source in enumerate(sources))
                + "\n"
            )
            tag = "vita-context-test:" + uuid.uuid4().hex
            env = os.environ.copy()
            # Current CI has Buildx. Older local engines can still exercise the
            # same Docker ignore matcher and COPY rules with the legacy builder.
            buildx = subprocess.run(["docker", "buildx", "version"], capture_output=True).returncode == 0
            env["DOCKER_BUILDKIT"] = "1" if buildx else "0"
            builder = ["docker", "buildx", "build", "--load"] if buildx else ["docker", "build"]
            result = subprocess.run(
                [*builder, "--network=none", "--tag", tag, "--file", str(probe), str(context)],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            try:
                self.assertEqual(result.returncode, 0, f"{dockerfile}:\n{result.stdout}\n{result.stderr}")
                if not inject_private_files:
                    return set()
                archive = context / "image.tar"
                subprocess.run(["docker", "image", "save", "--output", str(archive), tag], check=True, timeout=30)
                members = set()
                with tarfile.open(archive) as image:
                    manifest = json.load(image.extractfile("manifest.json"))
                    for layer in manifest[0]["Layers"]:
                        with tarfile.open(fileobj=io.BytesIO(image.extractfile(layer).read())) as content:
                            for member in content:
                                members.add(member.name)
                                if member.isfile():
                                    self.assertNotIn(b"SECRET-MARKER", content.extractfile(member).read())
                return members
            finally:
                subprocess.run(["docker", "image", "rm", tag], capture_output=True, timeout=30)

    def test_all_image_copy_inputs_survive_their_effective_ignore_policy(self):
        for dockerfile in DOCKERFILES:
            with self.subTest(image=dockerfile.parent.name):
                self.probe(dockerfile)

    def test_real_staged_tee_copy_inputs_survive_effective_ignore_policy(self):
        self.probe(ROOT / "tee_inference/Dockerfile", real_staged_tee=True)

    def test_tee_package_inclusions_do_not_include_private_keys_or_caches(self):
        members = self.probe(ROOT / "tee_inference/Dockerfile", inject_private_files=True)
        self.assertTrue(any(name.endswith("context-probe.txt") for name in members))
        self.assertFalse(any("secret-marker" in name or name.endswith("/.env") for name in members))


if __name__ == "__main__":
    unittest.main()

"""Exercise the real bundler in synthetic Git checkouts; no private inputs."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build_bundle.py"
GIT = shutil.which("git")


@unittest.skipUnless(GIT, "Git required for synthetic repositories")
class BundlePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        shutil.copyfile(SCRIPT, self.repo / "scripts/build_bundle.py")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, PYTHONDONTWRITEBYTECODE="1"
        )
        self.git("init", "-q")
        self.put("normal file.txt", "synthetic tracked\n", tracked=True)
        self.git("add", "scripts/build_bundle.py")
        self.git(
            "-c",
            "user.name=Synthetic",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "fixture",
        )

    def git(self, *args):
        return subprocess.run(  # noqa: S603 - fixed executable and synthetic test paths
            [GIT, "-C", str(self.repo), *args],
            env=self.env,
            capture_output=True,
            check=True,
            timeout=20,
        )

    def put(self, name, content, tracked=False):
        dest = self.repo / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")
        if tracked:
            self.git("add", "-f", "--", name)

    def build(self, **env):
        return subprocess.run(  # noqa: S603 - fixed executable and synthetic test paths
            [sys.executable, str(self.repo / "scripts/build_bundle.py")],
            env={**self.env, **env},
            capture_output=True,
            timeout=30,
        )

    def assert_refused(self, result):
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"REFUSING TO PACKAGE", result.stderr)
        self.assertFalse((self.repo / "dist/knowledge-hub-source.zip").exists())

    def test_members_and_manifest(self):
        import zipfile

        self.put("private.json", '{"synthetic": true}')
        self.put("notes.txt", "synthetic untracked")
        external = self.base / "external.txt"
        external.write_text("synthetic external", encoding="utf-8")
        (self.repo / "external-link").symlink_to(external)
        for name in [".env", "var/state.json", "node_modules/example.txt", "private.key"]:
            self.put(name, "synthetic excluded", tracked=True)
        self.assertEqual(self.build().returncode, 0)
        bundle = self.repo / "dist/knowledge-hub-source.zip"
        with zipfile.ZipFile(bundle) as archive:
            names = archive.namelist()
            self.assertEqual(
                set(names),
                {"knowledge-hub/scripts/build_bundle.py", "knowledge-hub/normal file.txt"},
            )
            self.assertEqual(archive.read("knowledge-hub/normal file.txt"), b"synthetic tracked\n")
            digest = hashlib.sha256()
            for name in sorted(names):
                digest.update(name.removeprefix("knowledge-hub/").encode())
                digest.update(hashlib.sha256(archive.read(name)).digest())
        manifest = json.loads((self.repo / "dist/source-manifest.json").read_text())
        sha = hashlib.sha256(bundle.read_bytes()).hexdigest()
        self.assertEqual(manifest["file_count"], 2)
        self.assertEqual(manifest["bytes"], bundle.stat().st_size)
        self.assertEqual(manifest["sha256"], sha)
        self.assertEqual(manifest["source_manifest_sha256"], digest.hexdigest())
        self.assertEqual(manifest["git_sha"], self.git("rev-parse", "HEAD").stdout.decode().strip())
        self.assertEqual(
            (self.repo / "dist/SHA256SUMS").read_text(), f"{sha}  knowledge-hub-source.zip\n"
        )

    def test_tracked_symlink_refused(self):
        (self.repo / "link").symlink_to(self.base / "outside")
        self.git("add", "link")
        self.assert_refused(self.build())

    def test_symlink_parent_refused(self):
        self.put("directory/item.txt", "synthetic", tracked=True)
        shutil.move(self.repo / "directory", self.base / "moved")
        (self.repo / "directory").symlink_to(self.base / "moved", target_is_directory=True)
        self.assert_refused(self.build())

    def test_missing_tracked_file_refused(self):
        (self.repo / "normal file.txt").unlink()
        self.assert_refused(self.build())

    def test_missing_git_refused(self):
        empty = self.base / "empty"
        empty.mkdir()
        self.assert_refused(self.build(PATH=str(empty)))

    def test_source_without_git_refused(self):
        shutil.rmtree(self.repo / ".git")
        self.assert_refused(self.build())

    def test_parent_repository_refused(self):
        shutil.rmtree(self.repo / ".git")
        subprocess.run(  # noqa: S603 - fixed executable and synthetic test paths
            [GIT, "-C", str(self.base), "init", "-q"],
            env=self.env,
            capture_output=True,
            check=True,
            timeout=20,
        )
        self.assert_refused(self.build())

    def test_invalid_portable_path_refused(self):
        self.put("bad\\name.txt", "synthetic", tracked=True)
        self.assert_refused(self.build())

    def test_git_environment_cannot_redirect_index(self):
        self.assertEqual(self.build(GIT_INDEX_FILE=str(self.base / "absent-index")).returncode, 0)
        manifest = json.loads((self.repo / "dist/source-manifest.json").read_text())
        self.assertEqual(manifest["file_count"], 2)


if __name__ == "__main__":
    unittest.main()

"""Offline tests for isolated, non-secret GitOps candidate preparation."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[1] / "helpers/prepare_candidate.py"
SPEC = importlib.util.spec_from_file_location("prepare_candidate", HELPER)
CANDIDATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CANDIDATE)


PREPARE = '''import argparse, json
from pathlib import Path
p = argparse.ArgumentParser()
p.add_argument("--terraform-output", required=True)
p.add_argument("--activate", action="store_true")
a = p.parse_args()
root = Path(__file__).resolve().parents[1]
values = json.loads(Path(a.terraform_output).read_text(encoding="utf-8-sig"))
assert "account_id" in values, "missing output"
(root / "config.yaml").write_text(json.dumps({"accountId": values["account_id"],
    "deploymentProfile": values.get("deployment_profile", "architecture"),
    "deployment": {"enabled": a.activate}}))
'''
CHECK_RELEASE = '''import json, os
from pathlib import Path
root = Path(__file__).resolve().parents[1]
assert (root / "config.yaml").is_file()
assert "GITHUB_OUTPUT" not in os.environ
assert json.loads((root / "environments/dev/images.yaml").read_text())["verified"]
'''


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="retail-candidate-test-")
        self.base = Path(self.temporary.name).resolve()
        self.source = self.base / "source"
        self.gitops = self.base / "gitops"
        self.source.mkdir()
        self.gitops.mkdir()
        self.git(self.source, "init", "--quiet")
        (self.source / ".gitignore").write_text(".ci-tools/\n", encoding="utf-8")
        self.commit(self.source)
        self.git(self.gitops, "init", "--quiet")
        self.write("config.yaml", json.dumps({"deployment": {"enabled": False}}))
        self.write("README.md", "Public GitOps configuration\n")
        self.write("scripts/prepare.py", PREPARE)
        self.write("scripts/check-release.py", CHECK_RELEASE)
        self.write("environments/dev/images.yaml", '{"verified": true}')
        self.write("applications/files/rds-ca.pem", "-----BEGIN CERTIFICATE-----\nPUBLIC\n-----END CERTIFICATE-----\n")
        self.write("applications/files/rds-truststore.p12", "PUBLIC TRUSTSTORE FIXTURE")
        self.write(".env", "TOKEN=must-not-copy")
        self.write("docker-token.json", '{"value": "must-not-copy"}')
        self.write(".aws/credentials", "must-not-copy")
        self.write(".local/notes.txt", "must-not-copy")
        self.write("terraform-output.json", '{"sensitive": "must-not-copy"}')
        self.git(self.gitops, "add", "--force", ".env", "docker-token.json", ".aws/credentials",
                 ".local/notes.txt", "terraform-output.json")
        self.commit(self.gitops)
        self.local = self.source / ".ci-tools/ansible"
        self.local.mkdir(parents=True)
        self.target = self.local / "gitops-candidate"
        self.outputs = self.local / "terraform-outputs.json"
        self.outputs.write_text('{"account_id": "111122223333"}', encoding="utf-8")

    def tearDown(self):
        # TemporaryDirectory's recursive cleanup is bounded to this test fixture.
        self.assertTrue(self.base.name.startswith("retail-candidate-test-"))
        self.assertNotEqual(self.base, self.base.parent)
        self.temporary.cleanup()

    def git(self, root, *arguments):
        return subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, check=True).stdout

    def commit(self, root):
        self.git(root, "add", ".")
        self.git(root, "-c", "user.name=Candidate Test", "-c", "user.email=candidate@example.invalid",
                 "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "fixture")

    def write(self, relative, contents):
        path = self.gitops / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents, encoding="utf-8")

    def prepare(self, **overrides):
        arguments = {"source_dir": self.source, "gitops_dir": self.gitops, "local_dir": self.local,
                     "candidate_dir": self.target, "outputs": self.outputs}
        arguments.update(overrides)
        return CANDIDATE.prepare_candidate(**arguments)

    def test_preparation_is_isolated_and_reports_actual_file_changes(self):
        before = (self.gitops / "config.yaml").read_bytes()
        first = self.prepare()
        self.assertTrue(first["changed"])
        self.assertFalse(first["deployment_enabled"])
        self.assertEqual(Path(first["candidate_dir"]), self.target)
        self.assertEqual((self.gitops / "config.yaml").read_bytes(), before)
        config = json.loads((self.target / "config.yaml").read_text())
        self.assertEqual(config["accountId"], "111122223333")
        self.assertFalse(config["deployment"]["enabled"])
        second = self.prepare()
        self.assertFalse(second["changed"])
        self.assertEqual(first["file_set_sha256"], second["file_set_sha256"])
        self.assertEqual(list(self.local.glob("candidate-build-*")), [])

    def test_tracked_credentials_outputs_and_git_metadata_are_excluded(self):
        self.prepare()
        for relative in (".git", ".env", "docker-token.json", ".aws", ".local", "terraform-output.json"):
            self.assertFalse((self.target / relative).exists(), relative)
        self.assertTrue((self.target / "applications/files/rds-ca.pem").is_file())
        self.assertTrue((self.target / "applications/files/rds-truststore.p12").is_file())
        self.assertNotIn("must-not-copy", "\n".join(path.read_text() for path in self.target.rglob("*") if path.is_file()))

    def test_untracked_token_is_never_copied(self):
        self.write("access-token.txt", "private-token")
        self.write("untracked-config.yaml", "private: should-not-copy")
        self.prepare()
        self.assertFalse((self.target / "access-token.txt").exists())
        self.assertFalse((self.target / "untracked-config.yaml").exists())

    def test_activation_is_explicit_and_only_changes_candidate(self):
        self.prepare()
        active = self.prepare(activate=True)
        self.assertTrue(active["changed"])
        self.assertTrue(active["deployment_enabled"])
        self.assertTrue(json.loads((self.target / "config.yaml").read_text())["deployment"]["enabled"])
        self.assertFalse(json.loads((self.gitops / "config.yaml").read_text())["deployment"]["enabled"])
        self.assertTrue(self.prepare()["changed"])

    def test_new_terraform_values_update_clean_owned_candidate(self):
        self.prepare()
        self.outputs.write_text('{"account_id": "444455556666"}')
        self.assertTrue(self.prepare()["changed"])
        self.assertEqual(json.loads((self.target / "config.yaml").read_text())["accountId"], "444455556666")
        self.assertFalse(self.prepare()["changed"])

    def test_profile_metadata_and_tracked_lab_overlays_reach_the_candidate(self):
        self.write("profiles/lab/argo-cd.yaml", "applicationSet: {replicas: 0}\n")
        self.commit(self.gitops)
        self.outputs.write_text('{"account_id":"111122223333","deployment_profile":"lab"}', encoding="utf-8")
        self.prepare()
        config = json.loads((self.target / "config.yaml").read_text())
        self.assertEqual(config["deploymentProfile"], "lab")
        self.assertTrue((self.target / "profiles/lab/argo-cd.yaml").is_file())

    def test_existing_unowned_directory_is_refused(self):
        self.target.mkdir()
        sentinel = self.target / "my-work.yaml"
        sentinel.write_text("keep")
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "not owned"):
            self.prepare()
        self.assertEqual(sentinel.read_text(), "keep")

    def test_dirty_owned_candidate_is_preserved(self):
        self.prepare()
        config = self.target / "config.yaml"
        config.write_text("my local edit")
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "local edits"):
            self.prepare()
        self.assertEqual(config.read_text(), "my local edit")

    def test_extra_unowned_file_is_preserved(self):
        self.prepare()
        (self.target / "notes.txt").write_text("review notes")
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "unowned files"):
            self.prepare()

    def test_existing_manifest_cannot_claim_another_directory(self):
        self.prepare()
        marker = self.target / CANDIDATE.MANIFEST
        owner = json.loads(marker.read_text())
        owner["candidate_dir"] = str(self.local)
        marker.write_text(json.dumps(owner))
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "ownership"):
            self.prepare()

    def test_outside_ignored_local_directory_is_refused(self):
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "below the ignored"):
            self.prepare(candidate_dir=self.source / "gitops-candidate")
        self.assertFalse((self.source / "gitops-candidate").exists())
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "below the ignored"):
            self.prepare(candidate_dir=self.local)

    def test_unignored_local_directory_is_refused(self):
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "must be ignored"):
            self.prepare(local_dir=self.source / "not-ignored", candidate_dir=self.source / "not-ignored/candidate")

    def test_candidate_cannot_alias_source_gitops(self):
        self.target.mkdir()
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "active GitOps checkout"):
            self.prepare(gitops_dir=self.target)

    def test_tracked_private_key_material_is_refused(self):
        self.write("cert.pem", "-----BEGIN PRIVATE KEY-----\nDO NOT COPY\n-----END PRIVATE KEY-----")
        self.commit(self.gitops)
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "Private key"):
            self.prepare()
        self.assertFalse(self.target.exists())

    def test_canonical_failure_never_publishes_a_partial_candidate(self):
        self.outputs.write_text('{"wrong": true}')
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "Canonical GitOps preparation failed"):
            self.prepare()
        self.assertFalse(self.target.exists())
        self.assertEqual(list(self.local.glob("candidate-build-*")), [])

    def test_release_failure_never_overwrites_existing_clean_candidate(self):
        self.prepare()
        original = (self.target / "config.yaml").read_bytes()
        self.write("environments/dev/images.yaml", '{"verified": false}')
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "Five-image release verification failed"):
            self.prepare()
        self.assertEqual((self.target / "config.yaml").read_bytes(), original)

    def test_candidate_environment_does_not_write_github_outputs(self):
        previous = os.environ.get("GITHUB_OUTPUT")
        output = self.local / "github-output.txt"
        os.environ["GITHUB_OUTPUT"] = str(output)
        try:
            self.prepare()
        finally:
            if previous is None:
                os.environ.pop("GITHUB_OUTPUT", None)
            else:
                os.environ["GITHUB_OUTPUT"] = previous
        self.assertFalse(output.exists())

    def test_symlink_source_is_refused(self):
        target = self.gitops / "README.md"
        destination = self.base / "public.md"
        destination.write_text("public")
        target.unlink()
        try:
            target.symlink_to(destination)
        except OSError:
            self.skipTest("Symlinks require privileges on this Windows installation")
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "Symlinks"):
            self.prepare()
        self.assertFalse(self.target.exists())

    def test_symlink_candidate_directory_is_refused(self):
        destination = self.base / "other-work"
        destination.mkdir()
        try:
            self.target.symlink_to(destination, target_is_directory=True)
        except OSError:
            self.skipTest("Symlinks require privileges on this Windows installation")
        with self.assertRaisesRegex(CANDIDATE.CandidateError, "Symlinks"):
            self.prepare()
        self.assertEqual(list(destination.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

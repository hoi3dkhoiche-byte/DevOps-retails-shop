"""Offline checks for bootstrap target/source and ownership prerequisites."""

import copy
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import yaml

HELPER = Path(__file__).resolve().parents[1] / "helpers" / "bootstrap_guard.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_guard", HELPER)
guard = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(guard)


def fixture():
    config = {
        "deployment": {"enabled": True}, "accountId": "111122223333", "region": "ap-southeast-1",
        "clusterName": "retail-lab-eks", "gitopsRepo": guard.GITOPS_REPO,
        "gitopsRevision": "main", "sourceRepo": guard.SOURCE_REPO,
        "repositoryCredentials": {"enabled": False},
    }
    root = {
        "apiVersion": "argoproj.io/v1alpha1", "kind": "Application",
        "metadata": {"name": "retail-root", "namespace": "argocd"},
        "spec": {
            "project": "default",
            "source": {"repoURL": guard.GITOPS_REPO, "targetRevision": "main", "path": "bootstrap",
                       "helm": {"valueFiles": ["../config.yaml", "../versions.yaml"]}},
            "destination": {"server": "https://kubernetes.default.svc", "namespace": "argocd"},
            "syncPolicy": {"automated": {"selfHeal": True, "prune": False}},
        },
    }
    return config, root


class BootstrapGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.config, self.root = fixture()
        (self.repo / "argocd").mkdir()
        (self.repo / "platform").mkdir()
        self.versions = {
            chart: {"repository": f"https://charts.example/{chart}", "version": version, "appVersion": "v1.2.3"}
            for chart, version in (("external-secrets", "2.14.0"), ("argo-cd", "10.11.6"))
        }
        for chart in self.versions:
            (self.repo / "platform" / f"{chart}.yaml").write_text("replicaCount: 1\n", encoding="utf-8")
        self.write()

    def write(self):
        for relative, value in (("config.yaml", self.config), ("versions.yaml", self.versions),
                                ("argocd/retail-root.yaml", self.root)):
            (self.repo / relative).write_text(yaml.safe_dump(value), encoding="utf-8")

    def contract(self):
        return guard.build_contract(self.repo, "111122223333", "ap-southeast-1", "retail-lab-eks")

    def test_pins_and_optional_repository_secret_are_loaded_from_gitops(self):
        self.config["repositoryCredentials"]["enabled"] = True
        self.versions["external-secrets"]["version"] = "2.15.1"
        self.write()
        result = self.contract()
        self.assertEqual(result["eso_sources"][0]["targetRevision"], "2.15.1")
        self.assertEqual(result["charts"]["external-secrets"]["version"], "2.15.1")
        self.assertEqual(result["expected_external_secrets"], ["argocd-admin", "argocd-redis", "argocd-repositories"])

    def test_disabled_deployment_is_rejected(self):
        self.config["deployment"]["enabled"] = False
        self.write()
        with self.assertRaisesRegex(ValueError, "disabled"):
            self.contract()

    def test_legacy_profile_keeps_full_platform_and_one_values_file(self):
        result = self.contract()
        self.assertEqual(result["deployment_profile"], "architecture")
        self.assertEqual(len(result["platform_applications"]), 9)
        self.assertEqual(result["charts"]["argo-cd"]["values_files"], [str(self.repo / "platform/argo-cd.yaml")])

    def test_lab_bootstrap_and_argo_handoff_use_the_same_overlays(self):
        self.config["deploymentProfile"] = "lab"
        for chart in self.versions:
            overlay = self.repo / "profiles/lab" / f"{chart}.yaml"
            overlay.parent.mkdir(parents=True, exist_ok=True)
            overlay.write_text("resources: {}\n", encoding="utf-8")
        self.write()
        result = guard.build_contract(self.repo, "111122223333", "ap-southeast-1", "retail-lab-eks", "lab")
        self.assertEqual(len(result["charts"]["argo-cd"]["values_files"]), 2)
        self.assertEqual(result["eso_sources"][0]["helm"]["valueFiles"],
                         ["$values/platform/external-secrets.yaml", "$values/profiles/lab/external-secrets.yaml"])
        self.assertFalse({"loki", "alloy", "velero", "cluster-autoscaler"} & set(result["platform_applications"]))

    def test_profile_mismatch_or_missing_lab_overlay_stops_bootstrap(self):
        with self.assertRaisesRegex(ValueError, "differs"):
            guard.build_contract(self.repo, "111122223333", "ap-southeast-1", "retail-lab-eks", "lab")
        self.config["deploymentProfile"] = "lab"
        self.write()
        with self.assertRaisesRegex(ValueError, "Missing lab"):
            self.contract()

    def test_invalid_profile_is_rejected_before_chart_selection(self):
        for profile in (True, "lean", None):
            self.config["deploymentProfile"] = profile
            self.write()
            with self.subTest(profile=profile), self.assertRaisesRegex(ValueError, "profile"):
                self.contract()

    def test_wrong_account_is_rejected(self):
        self.config["accountId"] = "999988887777"
        self.write()
        with self.assertRaisesRegex(ValueError, "account"):
            self.contract()

    def test_root_cannot_override_local_reviewed_values(self):
        for key, value in (("values", "deployment:\n  enabled: true"), ("valuesObject", {"deployment": {"enabled": True}}),
                           ("parameters", [{"name": "deployment.enabled", "value": "true"}])):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.root)
                changed["spec"]["source"]["helm"][key] = value
                with self.assertRaisesRegex(ValueError, "inline overrides"):
                    guard.validate_root(changed, self.config)

    def test_root_repo_ref_path_and_value_files_are_exact(self):
        for key, value in (("repoURL", "https://github.com/other/repo.git"), ("targetRevision", "feature/test"),
                           ("path", "applications"), ("helm", {"valueFiles": ["../config.yaml"]})):
            with self.subTest(key=key):
                changed = copy.deepcopy(self.root)
                changed["spec"]["source"][key] = value
                with self.assertRaises(ValueError):
                    guard.validate_root(changed, self.config)

    def test_multi_source_root_is_rejected(self):
        self.root["spec"]["sources"] = [self.root["spec"]["source"]]
        with self.assertRaisesRegex(ValueError, "one source"):
            guard.validate_root(self.root, self.config)

    def git(self, *arguments):
        result = subprocess.run(["git", "-C", str(self.repo), *arguments], text=True,
                                capture_output=True, check=True, timeout=30)
        return result.stdout.strip()

    def initialize_git(self):
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Offline test")
        self.git("add", ".")
        self.git("commit", "-m", "fixture")
        self.git("remote", "add", "origin", guard.GITOPS_REPO)
        self.git("update-ref", "refs/remotes/origin/main", self.git("rev-parse", "HEAD"))

    def check_checkout(self, remote_head=None, remote_error=False):
        actual_run = subprocess.run
        head = remote_head or self.git("rev-parse", "refs/remotes/origin/main")

        def run(arguments, **kwargs):
            if "ls-remote" in arguments:
                self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
                self.assertEqual(kwargs["timeout"], 30)
                self.assertIn("credential.interactive=never", arguments)
                return subprocess.CompletedProcess(arguments, 128 if remote_error else 0,
                                                   "" if remote_error else f"{head}\trefs/heads/main\n",
                                                   "Authentication required" if remote_error else "")
            return actual_run(arguments, **kwargs)

        with mock.patch.object(guard.subprocess, "run", side_effect=run):
            guard.validate_checkout(self.repo)

    def test_clean_committed_canonical_checkout_passes(self):
        self.initialize_git()
        self.check_checkout()

    def test_dirty_candidate_is_rejected(self):
        self.initialize_git()
        (self.repo / "config.yaml").write_text("candidate: true\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "clean, committed"):
            self.check_checkout()

    def test_unmerged_local_commit_is_rejected(self):
        self.initialize_git()
        (self.repo / "candidate.yaml").write_text("candidate: true\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-m", "unmerged candidate")
        with self.assertRaisesRegex(ValueError, "differs from origin/main"):
            self.check_checkout()

    def test_wrong_origin_is_rejected(self):
        self.initialize_git()
        self.git("remote", "set-url", "origin", "https://github.com/other/repo.git")
        with self.assertRaisesRegex(ValueError, "origin URL"):
            self.check_checkout()

    def test_stale_remote_tracking_branch_is_rejected(self):
        self.initialize_git()
        with self.assertRaisesRegex(ValueError, "current remote main"):
            self.check_checkout(remote_head="0" * 40)

    def test_inaccessible_remote_fails_closed_without_prompt(self):
        self.initialize_git()
        with self.assertRaisesRegex(ValueError, "Cannot inspect"):
            self.check_checkout(remote_error=True)


if __name__ == "__main__":
    unittest.main()

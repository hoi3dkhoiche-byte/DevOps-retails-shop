"""Exercise account/receipt/endpoint guards without AWS or Kubernetes access."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_target", ROOT / "ansible/helpers/check_target.py")
target = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(target)
ACCOUNT = "111122223333"
COMMIT = "a" * 40
ENDPOINT = "https://ABCDEF.ap-southeast-1.eks.amazonaws.com"


class TargetTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.gitops = self.directory / "gitops-checkout"
        self.gitops.mkdir()
        for name in ("scripts/prepare.py", "scripts/check-release.py", "argocd/retail-root.yaml",
                     "bootstrap/Chart.yaml", "argocd-secrets/Chart.yaml"):
            path = self.gitops / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# fixture\n", encoding="utf-8")
        self.config = {
            "sourceRepo": "https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop.git",
            "gitopsRepo": "https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop-gitops.git",
            "gitopsRevision": "main", "region": "ap-southeast-1", "clusterName": "retail-lab-eks",
            "deployment": {"enabled": False}, "repositoryCredentials": {"enabled": False},
        }
        self.write_config()
        versions = {name: {"repository": "https://charts.example.test", "version": version, "appVersion": "v3.5.6"}
                    for name, version in (("argo-cd", "10.11.6"), ("external-secrets", "2.14.0"))}
        (self.gitops / "versions.yaml").write_text(yaml.safe_dump(versions), encoding="utf-8")
        self.output = self.directory / "terraform-outputs.json"
        self.metadata = self.directory / "metadata.json"
        secret = f"arn:aws:secretsmanager:ap-southeast-1:{ACCOUNT}:secret:retail/example-abc123"
        self.outputs = {
            "account_id": ACCOUNT, "cluster_name": "retail-lab-eks", "cluster_endpoint": ENDPOINT,
            "workload_roles": {"bootstrap": f"arn:aws:iam::{ACCOUNT}:role/retail-lab-bootstrap"},
            "mysql_endpoint": "retail.abc.ap-southeast-1.rds.amazonaws.com",
            "mysql_master_secret_arn": secret,
            "backend_endpoints": {"RabbitMQEndpoint": "amqps://b-abc.mq.ap-southeast-1.amazonaws.com:5671",
                                  "RedisEndpoint": "retail.abc.cache.amazonaws.com",
                                  "SearchEndpoint": "vpc-retail.abc.ap-southeast-1.es.amazonaws.com"},
            "platform_secret_arns": {name: secret for name in ("mq_admin", "search_admin", "redis_dev", "redis_prod")},
        }
        self.write_outputs()
        self.receipt = {"schema_version": 1, "repository": target.SOURCE_REPOSITORY,
                        "source_commit": COMMIT, "run_id": "1234", "run_attempt": "2",
                        "aws_account_id": ACCOUNT, "outputs_sha256": self.checksum()}
        self.write_receipt()
        self.commands = []
        self.identity = {"Account": ACCOUNT}
        self.cluster = {"arn": f"arn:aws:eks:ap-southeast-1:{ACCOUNT}:cluster/retail-lab-eks",
                        "name": "retail-lab-eks", "status": "ACTIVE", "version": "1.36", "endpoint": ENDPOINT}

    def write_config(self):
        (self.gitops / "config.yaml").write_text(yaml.safe_dump(self.config), encoding="utf-8")

    def write_outputs(self, native=False):
        values = {key: {"value": value, "type": "fixture", "sensitive": False}
                  for key, value in self.outputs.items()} if native else self.outputs
        self.output.write_text(json.dumps(values, sort_keys=True) + "\n", encoding="utf-8")

    def write_receipt(self):
        self.metadata.write_text(json.dumps(self.receipt), encoding="utf-8")

    def checksum(self):
        return hashlib.sha256(self.output.read_bytes()).hexdigest()

    def args(self, phase="offline", *extra):
        return target.parser().parse_args([
            "--phase", phase, "--source-dir", str(ROOT), "--gitops-dir", str(self.gitops),
            "--account-id", ACCOUNT, "--terraform-output", str(self.output),
            "--terraform-metadata", str(self.metadata), "--kube-context", "retail-lab-eks",
            "--kubeconfig", str(self.directory / "private/kubeconfig.yaml"), *extra])

    def invoke(self, command):
        self.commands.append(command)
        if command[:1] == ["git"] and command[-2:] == ["rev-parse", "HEAD"]:
            return COMMIT + "\n"
        if command[:3] == ["aws", "sts", "get-caller-identity"]:
            return json.dumps(self.identity)
        if command[:3] == ["aws", "eks", "describe-cluster"]:
            return json.dumps({"cluster": self.cluster})
        raise AssertionError("A preflight attempted an unexpected command")

    def run_check(self, phase="cluster", *extra):
        with patch.object(target, "invoke", side_effect=self.invoke):
            return target.check(self.args(phase, *extra))

    def assert_error(self, code, phase="cluster", *extra):
        with self.assertRaises(target.TargetError) as raised:
            self.run_check(phase, *extra)
        self.assertEqual(raised.exception.code, code)

    def test_offline_needs_neither_credentials_nor_aws_or_kubectl(self):
        self.output.unlink()
        self.metadata.unlink()
        args = self.args()
        args.account_id = ""
        with patch.object(target, "invoke", side_effect=AssertionError("Offline invoked an external tool")):
            result = target.check(args)
        self.assertEqual(result["status"], "success")
        self.assertNotIn("account_id", result)

    def test_offline_rejects_a_string_boolean(self):
        self.config["deployment"]["enabled"] = "false"
        self.write_config()
        self.assert_error("invalid_metadata", "offline")
        self.assertEqual(self.commands, [])

    def test_offline_rejects_wrong_or_credential_bearing_repository(self):
        self.config["sourceRepo"] = "https://user:private-token@github.com/hoi3dkhoiche-byte/DevOps-retails-shop.git"
        self.write_config()
        self.assert_error("wrong_repository", "offline")

    def test_output_chart_metadata_is_allowlisted(self):
        path = self.gitops / "versions.yaml"
        versions = yaml.safe_load(path.read_text(encoding="utf-8"))
        versions["argo-cd"]["privateKey"] = "must-not-be-logged"
        path.write_text(yaml.safe_dump(versions), encoding="utf-8")
        result = self.run_check("offline")
        self.assertEqual(set(result["versions"]["argo-cd"]), {"repository", "version", "appVersion"})
        self.assertNotIn("must-not-be-logged", json.dumps(result))

    def test_account_rejects_placeholder_before_network_access(self):
        args = self.args("account")
        args.account_id = "123456789012"
        with patch.object(target, "invoke", side_effect=AssertionError("Account placeholder reached AWS")):
            with self.assertRaises(target.TargetError) as raised:
                target.check(args)
        self.assertEqual(raised.exception.code, "account_required")

    def test_wrong_sts_account_stops_before_describing_cluster(self):
        self.identity["Account"] = "444455556666"
        self.assert_error("wrong_account")
        self.assertEqual([value[:3] for value in self.commands if value[0] == "aws"],
                         [["aws", "sts", "get-caller-identity"]])

    def test_matching_cluster_is_verified_without_writing_kubeconfig(self):
        result = self.run_check()
        self.assertTrue(result["outputs_receipt_verified"])
        self.assertEqual(result["source_commit"], COMMIT)
        self.assertEqual(result["cluster_endpoint"], ENDPOINT)
        self.assertFalse((self.directory / "private").exists())
        self.assertEqual(len(self.commands), 3)

    def test_native_terraform_output_wrapper_is_supported(self):
        self.write_outputs(native=True)
        self.receipt["outputs_sha256"] = self.checksum()
        self.write_receipt()
        self.assertEqual(self.run_check()["account_id"], ACCOUNT)

    def test_optional_deployment_profile_is_validated_and_handed_to_bootstrap(self):
        self.assertEqual(self.run_check()["deployment_profile"], "architecture")
        for profile in ("lab", "architecture"):
            for entry in (profile, {"sensitive": False, "type": "string", "value": profile}):
                self.outputs["deployment_profile"] = entry
                self.write_outputs()
                self.receipt["outputs_sha256"] = self.checksum()
                self.write_receipt()
                with self.subTest(profile=profile, native=isinstance(entry, dict)):
                    self.assertEqual(self.run_check()["deployment_profile"], profile)

    def test_invalid_or_sensitive_profile_is_refused_before_aws(self):
        for entry in (False, None, "lean", {"sensitive": True, "type": "string", "value": "lab"},
                      {"sensitive": False, "type": "bool", "value": "lab"}):
            self.outputs["deployment_profile"] = entry
            self.write_outputs()
            self.commands.clear()
            with self.subTest(entry=entry):
                self.assert_error("invalid_outputs")
                self.assertEqual(self.commands, [])

    def test_changed_output_checksum_stops_before_aws(self):
        self.output.write_bytes(self.output.read_bytes() + b" ")
        self.assert_error("checksum_mismatch")
        self.assertEqual(self.commands, [])

    def test_receipt_from_another_source_commit_stops_before_aws(self):
        self.receipt["source_commit"] = "b" * 40
        self.write_receipt()
        self.assert_error("source_commit_mismatch")
        self.assertTrue(all(command[0] != "aws" for command in self.commands))

    def test_receipt_required_by_default(self):
        self.metadata.unlink()
        self.assert_error("receipt_required")
        self.assertEqual(self.commands, [])

    def test_manual_outputs_still_require_sts_and_live_eks_identity(self):
        self.metadata.unlink()
        result = self.run_check("cluster", "--allow-manual-outputs")
        self.assertFalse(result["outputs_receipt_verified"])
        self.assertEqual([command[1] for command in self.commands], ["sts", "eks"])

    def test_manual_opt_out_does_not_ignore_a_present_bad_receipt(self):
        self.receipt["outputs_sha256"] = "0" * 64
        self.write_receipt()
        self.assert_error("checksum_mismatch", "cluster", "--allow-manual-outputs")
        self.assertEqual(self.commands, [])

    def test_invalid_or_cross_account_receipt_is_rejected(self):
        for field, value in (("schema_version", True), ("run_attempt", 1),
                             ("aws_account_id", "444455556666"), ("repository", "another/repo")):
            original = self.receipt[field]
            with self.subTest(field=field):
                self.receipt[field] = value
                self.write_receipt()
                self.assert_error("invalid_receipt")
            self.receipt[field] = original
        self.assertEqual(self.commands, [])

    def test_live_cluster_must_match_every_identity_and_readiness_field(self):
        cases = (("endpoint", "https://OTHER.ap-southeast-1.eks.amazonaws.com", "endpoint_mismatch"),
                 ("arn", "arn:aws:eks:ap-southeast-1:444455556666:cluster/retail-lab-eks", "wrong_cluster"),
                 ("status", "UPDATING", "cluster_not_active"), ("version", "1.35", "cluster_version"))
        for field, value, code in cases:
            original = self.cluster[field]
            with self.subTest(field=field):
                self.cluster[field] = value
                self.assert_error(code)
            self.cluster[field] = original

    def test_sensitive_output_is_rejected_before_aws(self):
        self.write_outputs(native=True)
        document = json.loads(self.output.read_text(encoding="utf-8"))
        document["mysql_master_secret_arn"]["sensitive"] = True
        self.output.write_text(json.dumps(document), encoding="utf-8")
        self.assert_error("invalid_outputs")
        self.assertEqual(self.commands, [])

    def test_duplicate_json_keys_cannot_hide_receipt_identity(self):
        value = self.metadata.read_text(encoding="utf-8")
        self.metadata.write_text(value[:-1] + ', "aws_account_id": "444455556666"}', encoding="utf-8")
        self.assert_error("invalid_metadata")
        self.assertEqual(self.commands, [])

    def test_command_failure_does_not_leak_raw_stderr_or_metadata(self):
        raw = subprocess.CompletedProcess(["aws"], returncode=1, stdout="", stderr="secret-access-token")
        with patch.object(target.subprocess, "run", return_value=raw):
            with self.assertRaises(target.TargetError) as raised:
                target.invoke(["aws", "sts", "get-caller-identity"])
        self.assertNotIn("secret-access-token", str(raised.exception))
        args = ["--phase", "account", "--source-dir", str(ROOT), "--gitops-dir", str(self.gitops),
                "--account-id", ACCOUNT]
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), patch.object(target.subprocess, "run", return_value=raw):
            self.assertEqual(target.main(args), 1)
        result = json.loads(stream.getvalue())
        self.assertEqual(result["error_code"], "command_failed")
        self.assertNotIn("secret-access-token", stream.getvalue())


if __name__ == "__main__":
    unittest.main()

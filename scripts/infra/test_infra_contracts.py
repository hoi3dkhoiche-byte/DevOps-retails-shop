import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import plan_contract
import preflight


class SavedPlanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.plan = Path(temporary.name) / "terraform.tfplan"
        self.plan.write_bytes(b"saved binary terraform plan")
        self.now = datetime.now(timezone.utc)
        self.sha = "a" * 40
        self.digest = plan_contract.checksum(self.plan)
        self.role = "arn:aws:iam::123456789012:role/retail-lab-terraform"
        self.metadata = {"schema_version": 1, "repository": plan_contract.REPOSITORY, "source_commit": self.sha,
            "terraform_version": plan_contract.VERSION, "created_at": self.now.isoformat(), "sha256": self.digest,
            "run_id": "123", "run_attempt": "1", "role_arn": self.role, "aws_account_id": "123456789012"}
        self.run = {"id": 123, "run_attempt": 1, "head_sha": self.sha, "head_branch": "main", "event": "workflow_dispatch",
            "conclusion": "success", "path": plan_contract.WORKFLOW}
        self.jobs = [{"name": "validate", "conclusion": "success"}, {"name": "plan", "conclusion": "success"}, {"name": "apply", "conclusion": "skipped"}]

    def verify(self, **changes):
        args = dict(metadata=self.metadata, plan=self.plan, expected_commit=self.sha, approved_hash=self.digest, run=self.run, jobs=self.jobs, expected_role=self.role, now=self.now)
        args.update(changes)
        return plan_contract.verify(**args)

    def test_reviewed_plan_passes_without_mutating_metadata(self):
        original = copy.deepcopy(self.metadata)
        self.verify()
        self.assertEqual(self.metadata, original)

    def test_different_or_tampered_plan_fails(self):
        for change in ({"approved_hash": "b" * 64}, {"expected_commit": "c" * 40}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.verify(**change)
        self.plan.write_bytes(b"different binary plan")
        with self.assertRaises(ValueError): self.verify()

    def test_stale_and_future_plan_fail(self):
        for delta in (timedelta(days=2), -timedelta(minutes=5)):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                self.verify(metadata=dict(self.metadata, created_at=(self.now - delta).isoformat()))

    def test_changed_aws_role_or_account_fails(self):
        with self.assertRaises(ValueError):
            self.verify(expected_role="arn:aws:iam::999999999999:role/retail-lab-terraform")
        with self.assertRaises(ValueError):
            self.verify(metadata=dict(self.metadata, aws_account_id="999999999999"))

    def test_untrusted_runs_attempts_or_failed_jobs_fail(self):
        for field, value in (("run_attempt", 2), ("head_branch", "feature"), ("event", "pull_request"), ("conclusion", "failure"), ("path", ".github/workflows/other.yml")):
            with self.subTest(field=field), self.assertRaises(ValueError): self.verify(run=dict(self.run, **{field: value}))
        for conclusion in ("failure", "cancelled", "skipped"):
            jobs = copy.deepcopy(self.jobs)
            jobs[1]["conclusion"] = conclusion
            with self.subTest(conclusion=conclusion), self.assertRaises(ValueError): self.verify(jobs=jobs)
        with self.assertRaises(ValueError): self.verify(jobs=self.jobs + [self.jobs[0]])


class RegionalPreflightTests(unittest.TestCase):
    def setUp(self):
        self.config = {"addon_versions": {"vpc_cni": "v1.21.1-eksbuild.1", "coredns": "v1.13.0-eksbuild.1", "kube_proxy": "v1.36.0-eksbuild.1", "ebs_csi": "v1.51.0-eksbuild.1"},
            "platform_secret_arns": {"mq_admin": "arn:secret"}}

    def aws(self, service, operation, *args):
        if operation == "describe-cluster-versions": return {"clusterVersions": [{"clusterVersion": "1.36", "status": "STANDARD_SUPPORT"}]}
        if operation == "describe-addon-versions":
            name = args[args.index("--addon-name") + 1]
            key = next(key for key, value in preflight.ADDONS.items() if value == name)
            return {"addons": [{"addonVersions": [{"addonVersion": self.config["addon_versions"][key], "compatibilities": [{"clusterVersion": "1.36"}]}]}]}
        if operation == "describe-db-engine-versions": return {"DBEngineVersions": [{"EngineVersion": "8.4.11", "Status": "available"}]}
        if operation == "describe-orderable-db-instance-options": return {"OrderableDBInstanceOptions": [{}]}
        if operation == "describe-cache-engine-versions": return {"CacheEngineVersions": [{}]}
        if operation == "describe-broker-engine-types": return {"brokerEngineTypes": [{"engineVersions": [{"name": "4.2"}]}]}
        if operation == "describe-broker-instance-options": return {"brokerInstanceOptions": [{"supportedDeploymentModes": ["CLUSTER_MULTI_AZ"]}]}
        if operation == "list-versions": return {"Versions": ["OpenSearch_3.3"]}
        if operation == "describe-secret": return {"VersionIdsToStages": {"version-id": ["AWSCURRENT"]}}
        if operation == "head-bucket": return {}
        raise AssertionError(operation)

    def test_all_regional_prerequisites_are_checked_read_only(self):
        with patch.object(preflight, "aws", side_effect=self.aws) as aws:
            preflight.check(self.config)
            operations = [call.args[1] for call in aws.call_args_list]
            self.assertIn("describe-broker-instance-options", operations)
            self.assertNotIn("get-secret-value", operations)

    def test_unsupported_or_old_addon_fails(self):
        with patch.object(preflight, "aws", side_effect=self.aws):
            self.config["addon_versions"]["vpc_cni"] = "v1.20.0-eksbuild.1"
            with self.assertRaises(ValueError): preflight.check(self.config)

    def test_missing_platform_credential_version_fails(self):
        original = self.aws
        def missing(service, operation, *args):
            return {"VersionIdsToStages": {}} if operation == "describe-secret" else original(service, operation, *args)
        with patch.object(preflight, "aws", side_effect=missing), self.assertRaises(ValueError): preflight.check(self.config)


if __name__ == "__main__":
    unittest.main()

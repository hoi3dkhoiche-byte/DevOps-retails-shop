import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
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
        if operation == "describe-cluster-versions": return {"clusterVersions": [{"clusterVersion": "1.36", "status": "standard-support", "versionStatus": "STANDARD_SUPPORT"}]}
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
        if operation == "describe-instance-type-limits": return search_limits()
        if operation == "get-role": return {"Role": search_role()}
        if operation == "describe-secret": return {"VersionIdsToStages": {"version-id": ["AWSCURRENT"]}}
        if operation == "head-bucket": return {}
        raise AssertionError(operation)

    def test_all_regional_prerequisites_are_checked_read_only(self):
        with patch.object(preflight, "aws", side_effect=self.aws) as aws:
            preflight.check(self.config)
            operations = [call.args[1] for call in aws.call_args_list]
            self.assertIn("describe-broker-instance-options", operations)
            self.assertIn("describe-instance-type-limits", operations)
            aws.assert_any_call("opensearch", "describe-instance-type-limits", "--engine-version", "OpenSearch_3.3",
                                "--instance-type", "r8g.medium.search")
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

    def test_current_and_legacy_eks_api_status_fields_are_supported(self):
        for fields in ({"versionStatus": "STANDARD_SUPPORT"}, {"status": "standard-support"}):
            def response(service, operation, *args):
                if operation == "describe-cluster-versions":
                    return {"clusterVersions": [{"clusterVersion": "1.36", **fields}]}
                return self.aws(service, operation, *args)
            with self.subTest(fields=fields), patch.object(preflight, "aws", side_effect=response):
                preflight.check(self.config)

    def test_extended_unknown_and_conflicting_eks_status_are_rejected(self):
        for fields in ({"versionStatus": "EXTENDED_SUPPORT", "status": "standard-support"},
                       {"versionStatus": "UNSUPPORTED"}, {"status": "extended-support"}, {},
                       {"versionStatus": "NEW_UNKNOWN_STATUS"}):
            with self.subTest(fields=fields), patch.object(preflight, "aws", return_value={
                    "clusterVersions": [{"clusterVersion": "1.36", **fields}]}), self.assertRaises(ValueError):
                preflight.check(self.config)

    def test_instance_limits_api_failure_stops_preflight(self):
        def unsupported(service, operation, *args):
            if operation == "describe-instance-type-limits":
                raise subprocess.CalledProcessError(254, ["aws"], stderr="An error occurred (InvalidTypeException)")
            return self.aws(service, operation, *args)
        with patch.object(preflight, "aws", side_effect=unsupported), self.assertRaisesRegex(ValueError, "DescribeInstanceTypeLimits failed"):
            preflight.check(self.config)


def search_limits():
    # AWS DescribeInstanceTypeLimits shape: counts are numbers, storage bounds strings.
    return {"LimitsByRole": {"data": {
        "AdditionalLimits": [{"LimitName": "MaximumNumberOfDataNodesWithoutMasterNode", "LimitValues": ["10"]}],
        "InstanceLimits": {"InstanceCountLimits": {"MinimumInstanceCount": 1, "MaximumInstanceCount": 80}},
        "StorageTypes": [{"StorageTypeName": "ebs", "StorageSubTypeName": "gp3", "StorageTypeLimits": [
            {"LimitName": "MinimumVolumeSize", "LimitValues": ["10"]},
            {"LimitName": "MaximumVolumeSize", "LimitValues": ["1024"]},
        ]}],
    }}}


class SearchInstanceLimitsTests(unittest.TestCase):
    def test_single_data_node_with_gp3_50_gib_is_supported(self):
        preflight.check_search_instance_limits(search_limits())

    def test_missing_or_wrong_node_role_is_rejected(self):
        for payload in (None, {}, {"LimitsByRole": {"master": search_limits()["LimitsByRole"]["data"]}},
                        {"LimitsByRole": {"data": None}}):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "data-node count limits"):
                preflight.check_search_instance_limits(payload)

    def test_instance_count_must_fit_available_data_node_limits(self):
        for counts in ({}, {"MinimumInstanceCount": 2, "MaximumInstanceCount": 80},
                       {"MinimumInstanceCount": 1, "MaximumInstanceCount": 0},
                       {"MinimumInstanceCount": True, "MaximumInstanceCount": 80},
                       {"MinimumInstanceCount": "1", "MaximumInstanceCount": 80}):
            payload = search_limits()
            payload["LimitsByRole"]["data"]["InstanceLimits"]["InstanceCountLimits"] = counts
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                preflight.check_search_instance_limits(payload)

    def test_unavailable_gp3_or_non_ebs_storage_is_rejected(self):
        for storage in ([], [{"StorageTypeName": "ebs", "StorageSubTypeName": "gp2"}],
                        [{"StorageTypeName": "instance", "StorageSubTypeName": "gp3"}], None):
            payload = search_limits()
            payload["LimitsByRole"]["data"]["StorageTypes"] = storage
            with self.subTest(storage=storage), self.assertRaises(ValueError):
                preflight.check_search_instance_limits(payload)

    def test_standalone_domain_must_fit_additional_no_master_limit(self):
        for additional in (None,
                           [{"LimitName": "MaximumNumberOfDataNodesWithoutMasterNode", "LimitValues": ["0"]}],
                           [{"LimitName": "MaximumNumberOfDataNodesWithoutMasterNode", "LimitValues": ["unknown"]}],
                           search_limits()["LimitsByRole"]["data"]["AdditionalLimits"] * 2):
            payload = search_limits()
            payload["LimitsByRole"]["data"]["AdditionalLimits"] = additional
            with self.subTest(additional=additional), self.assertRaises(ValueError):
                preflight.check_search_instance_limits(payload)

    def test_volume_size_must_fit_gp3_bounds(self):
        for minimum, maximum in (("100", "1024"), ("10", "40"), ("0", "1024"), ("100", "10")):
            payload = search_limits()
            limits = payload["LimitsByRole"]["data"]["StorageTypes"][0]["StorageTypeLimits"]
            limits[0]["LimitValues"] = [minimum]
            limits[1]["LimitValues"] = [maximum]
            with self.subTest(minimum=minimum, maximum=maximum), self.assertRaises(ValueError):
                preflight.check_search_instance_limits(payload)

    def test_missing_malformed_or_ambiguous_storage_metadata_is_rejected(self):
        valid = search_limits()["LimitsByRole"]["data"]["StorageTypes"][0]["StorageTypeLimits"]
        for limits in (None, [], valid[:1], valid + [valid[0]],
                       [{"LimitName": "MinimumVolumeSize", "LimitValues": ["10", "20"]}, valid[1]],
                       [{"LimitName": "MinimumVolumeSize", "LimitValues": ["unknown"]}, valid[1]],
                       [{"LimitName": "MinimumVolumeSize", "LimitValues": [True]}, valid[1]]):
            payload = search_limits()
            payload["LimitsByRole"]["data"]["StorageTypes"][0]["StorageTypeLimits"] = limits
            with self.subTest(limits=limits), self.assertRaises(ValueError):
                preflight.check_search_instance_limits(payload)
        payload = search_limits()
        payload["LimitsByRole"]["data"]["StorageTypes"] *= 2
        with self.assertRaisesRegex(ValueError, "unambiguous"):
            preflight.check_search_instance_limits(payload)


def search_role(name="AWSServiceRoleForAmazonOpenSearchService"):
    service = preflight.SEARCH_SERVICE_ROLES[name]
    path = "/aws-service-role/" + service + "/"
    return {"RoleName": name, "Path": path, "Arn": "arn:aws:iam::111122223333:role" + path + name,
            "AssumeRolePolicyDocument": {"Statement": [{"Effect": "Allow", "Principal": {"Service": service},
                                                         "Action": "sts:AssumeRole"}]}}


class SearchServiceRoleTests(unittest.TestCase):
    @staticmethod
    def absent(*args):
        raise subprocess.CalledProcessError(254, ["aws", "iam", "get-role"], stderr="An error occurred (NoSuchEntity)")

    def test_existing_role_is_reused_in_both_modes(self):
        for create in (False, True):
            with self.subTest(create=create), patch.object(preflight, "aws", return_value={"Role": search_role()}):
                preflight.check_search_service_role({"create_opensearch_service_linked_role": create})

    def test_legacy_service_linked_role_is_reused(self):
        def response(service, operation, *args):
            if args[-1] == "AWSServiceRoleForAmazonOpenSearchService":
                return self.absent()
            return {"Role": search_role("AWSServiceRoleForAmazonElasticsearchService")}
        with patch.object(preflight, "aws", side_effect=response):
            preflight.check_search_service_role({})

    def test_missing_role_requires_explicit_creation_mode(self):
        with patch.object(preflight, "aws", side_effect=self.absent):
            with self.assertRaisesRegex(ValueError, "create_opensearch_service_linked_role=true"):
                preflight.check_search_service_role({})
            preflight.check_search_service_role({"create_opensearch_service_linked_role": True})

    def test_access_denied_is_not_treated_as_missing_role(self):
        failure = subprocess.CalledProcessError(254, ["aws"], stderr="An error occurred (AccessDenied)")
        with patch.object(preflight, "aws", side_effect=failure), self.assertRaises(subprocess.CalledProcessError):
            preflight.check_search_service_role({"create_opensearch_service_linked_role": True})

    def test_wrong_path_trust_and_account_are_rejected(self):
        roles = [dict(search_role(), Path="/"), dict(search_role(), AssumeRolePolicyDocument={}),
                 dict(search_role(), Arn=search_role()["Arn"].replace("111122223333", "999988887777"))]
        for role in roles:
            with self.subTest(role=role), patch.object(preflight, "aws", return_value={"Role": role}), self.assertRaises(ValueError):
                preflight.check_search_service_role({"admin_role_arn": "arn:aws:iam::111122223333:role/admin"})

    def test_creation_mode_requires_a_boolean(self):
        with patch.object(preflight, "aws") as api, self.assertRaises(ValueError):
            preflight.check_search_service_role({"create_opensearch_service_linked_role": "false"})
        api.assert_not_called()


if __name__ == "__main__":
    unittest.main()

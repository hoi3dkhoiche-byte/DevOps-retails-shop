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


def valid_config():
    account = "111122223333"
    return {"admin_cidrs": ["8.8.8.8/32"], "admin_role_arn": "arn:aws:iam::" + account + ":role/retail-admin",
            "github_oidc_provider_arn": "arn:aws:iam::" + account + ":oidc-provider/token.actions.githubusercontent.com",
            "terraform_policy_arn": "arn:aws:iam::" + account + ":policy/retail-infra",
            "terraform_workload_boundary_arn": "arn:aws:iam::" + account + ":policy/retail-lab-workload-boundary",
            "addon_versions": {"vpc_cni": "v1.21.1-eksbuild.1", "coredns": "v1.13.0-eksbuild.1",
                               "kube_proxy": "v1.36.0-eksbuild.1", "ebs_csi": "v1.51.0-eksbuild.1"},
            "platform_secret_arns": {key: "arn:aws:secretsmanager:ap-southeast-1:" + account + ":secret:/retail/platform/" + key + "-ABC123"
                                     for key in preflight.PLATFORM_SECRET_KEYS}}


def state_response(service, operation, *args):
    if operation == "head-bucket": return {"BucketRegion": "ap-southeast-1"}
    if operation == "get-bucket-location": return {"LocationConstraint": "ap-southeast-1"}
    if operation == "get-bucket-versioning": return {"Status": "Enabled"}
    if operation == "get-public-access-block": return {"PublicAccessBlockConfiguration": {
        "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True}}
    if operation == "get-bucket-encryption": return {"ServerSideEncryptionConfiguration": {"Rules": [
        {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}}
    raise AssertionError(operation)


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
        self.config = valid_config()

    def aws(self, service, operation, *args):
        if service == "sts": return {"Account": "111122223333"}
        if service == "s3api": return state_response(service, operation, *args)
        if operation == "describe-instance-type-offerings": return {"InstanceTypeOfferings": [
            {"InstanceType": instance, "LocationType": "availability-zone", "Location": zone}
            for instance, zones in preflight.WORKER_OFFERINGS.items() for zone in zones]}
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
        if operation == "get-role":
            if args[-1] in preflight.delegated_role_names():
                raise subprocess.CalledProcessError(254, ["aws"], stderr="An error occurred (NoSuchEntity)")
            return {"Role": {"Arn": self.config["admin_role_arn"]}} if args[-1] == "retail-admin" else {"Role": search_role()}
        if operation == "get-open-id-connect-provider": return {"Url": "token.actions.githubusercontent.com", "ClientIDList": ["sts.amazonaws.com"]}
        if operation == "get-policy": return {"Policy": {"Arn": args[-1], "IsAttachable": True, "DefaultVersionId": "v1"}}
        if operation == "get-policy-version": return {"PolicyVersion": {"IsDefaultVersion": True,
            "Document": preflight.render_boundary("111122223333", preflight.REGION)}}
        if operation == "describe-secret": return {"VersionIdsToStages": {"version-id": ["AWSCURRENT"]}}
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
            self.assertEqual(aws.call_args_list[0].args, ("sts", "get-caller-identity"))
            aws.assert_any_call("s3api", "head-bucket", "--bucket", "manhnguyen1131", "--expected-bucket-owner", "111122223333")

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
            def response(service, operation, *args):
                if operation == "describe-cluster-versions":
                    return {"clusterVersions": [{"clusterVersion": "1.36", **fields}]}
                return self.aws(service, operation, *args)
            with self.subTest(fields=fields), patch.object(preflight, "aws", side_effect=response), self.assertRaisesRegex(ValueError, "EKS 1.36"):
                preflight.check(self.config)

    def test_instance_limits_api_failure_stops_preflight(self):
        def unsupported(service, operation, *args):
            if operation == "describe-instance-type-limits":
                raise subprocess.CalledProcessError(254, ["aws"], stderr="An error occurred (InvalidTypeException)")
            return self.aws(service, operation, *args)
        with patch.object(preflight, "aws", side_effect=unsupported), self.assertRaisesRegex(ValueError, "DescribeInstanceTypeLimits failed"):
            preflight.check(self.config)

    def test_wrong_live_account_stops_before_other_aws_calls(self):
        with patch.object(preflight, "aws", return_value={"Account": "999988887777"}) as api, self.assertRaisesRegex(ValueError, "caller account"):
            preflight.check(self.config)
        api.assert_called_once_with("sts", "get-caller-identity")

    def test_missing_or_wrong_iam_prerequisite_stops_before_state_and_capacity(self):
        cases = (("get-role", {"Role": {"Arn": "arn:aws:iam::111122223333:role/wrong"}}),
                 ("get-open-id-connect-provider", {"Url": "token.actions.githubusercontent.com", "ClientIDList": []}),
                 ("get-policy", {"Policy": {"Arn": self.config["terraform_policy_arn"], "IsAttachable": False}}))
        for operation, result in cases:
            def response(service, name, *args):
                return result if name == operation else self.aws(service, name, *args)
            with self.subTest(operation=operation), patch.object(preflight, "aws", side_effect=response) as api, self.assertRaises(ValueError):
                preflight.check(self.config)
            self.assertFalse(any(call.args[0] == "s3api" for call in api.call_args_list))

    def test_iam_read_denial_is_a_prerequisite_failure(self):
        error = subprocess.CalledProcessError(254, ["aws"], stderr="AccessDenied")
        with patch.object(preflight, "aws", side_effect=error), self.assertRaisesRegex(ValueError, "Cannot verify"):
            preflight.check_iam_prerequisites(self.config)


class InputPreflightTests(unittest.TestCase):
    def test_missing_unreviewed_or_cross_account_boundary_stops_before_aws(self):
        for arn in (None, "arn:aws:iam::111122223333:policy/other-boundary",
                    "arn:aws:iam::999988887777:policy/retail-lab-workload-boundary"):
            config = valid_config()
            config["terraform_workload_boundary_arn"] = arn
            with self.subTest(arn=arn), patch.object(preflight, "aws") as api, self.assertRaises(ValueError):
                preflight.check(config)
            api.assert_not_called()

    def test_real_shaped_inputs_are_accepted_without_aws_calls(self):
        with patch.object(preflight, "aws") as api:
            self.assertEqual(preflight.validate_inputs(valid_config()), "111122223333")
        api.assert_not_called()

    def test_private_documentation_multicast_or_non_host_admin_ranges_stop_before_aws(self):
        for cidr in ("10.0.0.1/32", "192.168.1.1/32", "100.64.0.1/32", "127.0.0.1/32",
                     "192.0.2.10/32", "198.51.100.10/32", "203.0.113.10/32", "224.0.0.1/32",
                     "240.0.0.1/32", "0.0.0.0/0", "8.8.8.8/24", "8.8.8.8", "2001:4860:4860::8888/128"):
            config = valid_config()
            config["admin_cidrs"] = [cidr]
            with self.subTest(cidr=cidr), patch.object(preflight, "aws") as api, self.assertRaisesRegex(ValueError, "admin_cidrs"):
                preflight.check(config)
            api.assert_not_called()

    def test_placeholder_or_cross_account_inputs_are_rejected(self):
        cases = []
        for key in ("admin_role_arn", "github_oidc_provider_arn", "terraform_policy_arn"):
            config = valid_config()
            config[key] = config[key].replace("111122223333", "123456789012")
            cases.append(config)
        config = valid_config()
        config["admin_role_arn"] += "/REPLACE_ADMIN_ROLE"
        cases.append(config)
        for replacement in ("arn:aws:secretsmanager:us-east-1:111122223333:secret:real-ABC123",
                            "arn:aws:secretsmanager:ap-southeast-1:999988887777:secret:real-ABC123",
                            "actual-password-does-not-belong-here"):
            config = valid_config()
            config["platform_secret_arns"]["mq_admin"] = replacement
            cases.append(config)
        config = valid_config()
        del config["platform_secret_arns"]["redis_prod"]
        cases.append(config)
        for config in cases:
            with self.subTest(config=config), patch.object(preflight, "aws") as api, self.assertRaises(ValueError):
                preflight.check(config)
            api.assert_not_called()

    def test_domain_and_zone_must_form_a_real_target(self):
        for domain, zone in (("shop.example.com", "ZABC123"), ("retail.test", "ZABC123"),
                             ("https://retail.company.vn", "ZABC123"), ("retail.company.vn", ""),
                             ("", "ZABC123"), ("retail.company.vn", "not-a-zone")):
            config = valid_config()
            config.update(domain=domain, hosted_zone_id=zone)
            with self.subTest(domain=domain, zone=zone), self.assertRaises(ValueError):
                preflight.validate_inputs(config)
        config = valid_config()
        config.update(domain="company.vn", hosted_zone_id="ZABC123")
        preflight.validate_inputs(config)

    def test_additional_policies_are_distinct_bounded_and_same_account(self):
        arn = "arn:aws:iam::111122223333:policy/retail-extra"
        for values in ("not-a-list", [arn, arn], [valid_config()["terraform_policy_arn"]],
                       [arn.replace("111122223333", "999988887777")], [arn + "/REPLACE"],
                       [arn + str(index) for index in range(10)]):
            config = valid_config()
            config["terraform_additional_policy_arns"] = values
            with self.subTest(values=values), self.assertRaisesRegex(ValueError, "additional Terraform policy"):
                preflight.validate_inputs(config)
        config = valid_config()
        config["terraform_additional_policy_arns"] = [arn]
        preflight.validate_inputs(config)


class StateBucketPreflightTests(unittest.TestCase):
    def test_bucket_metadata_checks_are_read_only_and_owner_bound(self):
        with patch.object(preflight, "aws", side_effect=state_response) as api:
            preflight.check_state_bucket("111122223333")

        for call in api.call_args_list:
            self.assertEqual(call.args[0], "s3api")
            self.assertIn("--expected-bucket-owner", call.args)
            self.assertEqual(call.args[-1], "111122223333")
            self.assertTrue(call.args[1].startswith(("head-", "get-")))

    def test_old_head_bucket_response_falls_back_to_location(self):
        def response(service, operation, *args):
            return {} if operation == "head-bucket" else state_response(service, operation, *args)
        with patch.object(preflight, "aws", side_effect=response) as api:
            preflight.check_state_bucket("111122223333")
        self.assertIn("get-bucket-location", [call.args[1] for call in api.call_args_list])

    def test_wrong_region_disabled_versioning_public_access_and_missing_encryption_stop(self):
        failures = (("head-bucket", {"BucketRegion": "us-east-1"}),
                    ("get-bucket-versioning", {"Status": "Suspended"}),
                    ("get-public-access-block", {"PublicAccessBlockConfiguration": {"BlockPublicAcls": True}}),
                    ("get-bucket-encryption", {}))
        for operation, value in failures:
            def response(service, action, *args):
                return value if action == operation else state_response(service, action, *args)
            with self.subTest(operation=operation), patch.object(preflight, "aws", side_effect=response), self.assertRaises(ValueError):
                preflight.check_state_bucket("111122223333")

    def test_access_denied_is_not_ignored(self):
        error = subprocess.CalledProcessError(254, ["aws"], stderr="An error occurred (AccessDenied)")
        with patch.object(preflight, "aws", side_effect=error), self.assertRaisesRegex(ValueError, "ownership"):
            preflight.check_state_bucket("111122223333")


class WorkloadBoundaryPreflightTests(unittest.TestCase):
    def test_existing_roles_require_admin_boundary_migration_and_missing_roles_are_allowed(self):
        config = valid_config()
        arn = config["terraform_workload_boundary_arn"]
        names = preflight.delegated_role_names()
        def bounded(service, operation, *args):
            return {"Role": {"Arn": "arn:aws:iam::111122223333:role/" + args[-1],
                             "PermissionsBoundary": {"PermissionsBoundaryArn": arn}}}
        with patch.object(preflight, "aws", side_effect=bounded) as api:
            preflight.check_existing_role_boundaries(config)
        self.assertEqual(len(api.call_args_list), len(names))
        for role in ({"Arn": "arn:aws:iam::111122223333:role/" + names[0]},
                     {"Arn": "arn:aws:iam::111122223333:role/" + names[0],
                      "PermissionsBoundary": {"PermissionsBoundaryArn": arn + "-wrong"}}):
            with patch.object(preflight, "aws", return_value={"Role": role}), self.assertRaisesRegex(ValueError, "admin must reconcile"):
                preflight.check_existing_role_boundaries(config)
        with patch.object(preflight, "aws", side_effect=subprocess.CalledProcessError(254, ["aws"], stderr="(NoSuchEntity)")):
            preflight.check_existing_role_boundaries(config)
        with patch.object(preflight, "aws", side_effect=subprocess.CalledProcessError(254, ["aws"], stderr="(AccessDenied)")), \
                self.assertRaises(subprocess.CalledProcessError):
            preflight.check_existing_role_boundaries(config)

    def test_saved_plan_boundary_recheck_reads_actual_identity_before_the_policy(self):
        import contextlib
        import io
        with patch("sys.argv", ["preflight.py", "--boundary-account-id", "111122223333"]), \
                patch.object(preflight, "aws", return_value={"Account": "111122223333"}) as api, \
                patch.object(preflight, "check_workload_boundary") as boundary, \
                patch.object(preflight, "check_existing_role_boundaries") as roles, contextlib.redirect_stdout(io.StringIO()):
            preflight.main()
        api.assert_called_once_with("sts", "get-caller-identity")
        self.assertEqual(boundary.call_args.args[0]["terraform_workload_boundary_arn"],
                         "arn:aws:iam::111122223333:policy/retail-lab-workload-boundary")
        roles.assert_called_once_with(boundary.call_args.args[0])

    def test_saved_plan_boundary_recheck_fails_before_policy_on_wrong_identity(self):
        with patch("sys.argv", ["preflight.py", "--boundary-account-id", "111122223333"]), \
                patch.object(preflight, "aws", return_value={"Account": "999988887777"}), \
                patch.object(preflight, "check_workload_boundary") as boundary, self.assertRaises(ValueError):
            preflight.main()
        boundary.assert_not_called()

    def test_exact_default_policy_is_checked_with_read_only_apis(self):
        from urllib.parse import quote
        config = valid_config()
        arn = config["terraform_workload_boundary_arn"]
        document = preflight.render_boundary("111122223333", preflight.REGION)
        for serialized in (document, quote(json.dumps(document))):
            with self.subTest(serialized=isinstance(serialized, str)), patch.object(preflight, "aws", side_effect=[
                    {"Policy": {"Arn": arn, "IsAttachable": True, "DefaultVersionId": "v12"}},
                    {"PolicyVersion": {"Document": serialized, "IsDefaultVersion": True}}]) as api:
                preflight.check_workload_boundary(config)
            self.assertEqual([call.args[1] for call in api.call_args_list], ["get-policy", "get-policy-version"])
            self.assertEqual(api.call_args_list[-1].args[-1], "v12")

    def test_tampered_default_policy_and_stale_or_missing_versions_fail(self):
        config = valid_config()
        arn = config["terraform_workload_boundary_arn"]
        document = preflight.render_boundary("111122223333", preflight.REGION)
        for policy in ({"Document": {"Version": "2012-10-17", "Statement": []}, "IsDefaultVersion": True},
                       {"Document": document, "IsDefaultVersion": False}, {"IsDefaultVersion": True}):
            with self.subTest(policy=policy), patch.object(preflight, "aws", side_effect=[
                    {"Policy": {"Arn": arn, "IsAttachable": True, "DefaultVersionId": "v2"}},
                    {"PolicyVersion": policy}]), self.assertRaises(ValueError):
                preflight.check_workload_boundary(config)


class WorkerOfferingTests(unittest.TestCase):
    def test_each_selected_type_must_be_offered_in_each_selected_az(self):
        offers = [{"InstanceType": instance, "LocationType": "availability-zone", "Location": zone}
                  for instance, zones in preflight.WORKER_OFFERINGS.items() for zone in zones]
        with patch.object(preflight, "aws", return_value={"InstanceTypeOfferings": offers}):
            preflight.check_worker_offerings()
        for missing in range(len(offers)):
            with self.subTest(missing=offers[missing]), patch.object(preflight, "aws", return_value={
                    "InstanceTypeOfferings": offers[:missing] + offers[missing + 1:]}), self.assertRaisesRegex(ValueError, "offering is unavailable"):
                preflight.check_worker_offerings()


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

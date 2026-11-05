import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import lab_teardown as teardown
import plan_contract
import render_terraform_policy

ACCOUNT = "111122223333"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/retail-lab-terraform"


def item(address, **values):
    return {"address": address, "type": address.split(".")[0], "mode": "managed", "values": values}


def inventory():
    return [item("aws_vpc.retail", id="vpc-11111111", tags_all={"Project": "retail-lab"}),
            item("aws_iam_role.terraform", id="retail-lab-terraform", name="retail-lab-terraform", arn=ROLE),
            item("aws_s3_bucket_versioning.state", id=teardown.STATE_BUCKET, bucket=teardown.STATE_BUCKET)]


def scope():
    return {"schema_version": 1, "project": "retail-lab", "account_id": ACCOUNT,
            "resources": [{"address": value["address"], "type": value["type"], "id": value["values"]["id"],
                           "retain": teardown.retained(value)} for value in inventory()]}


def binary_json():
    return {"terraform_version": plan_contract.VERSION, "complete": True,
            "resource_changes": [{"address": value["address"], "type": value["type"], "mode": "managed",
                                  "change": {"actions": ["forget" if teardown.retained(value) else "delete"],
                                             "before": value["values"], "after": None}} for value in inventory()]}


class OwnershipTests(unittest.TestCase):
    def test_only_owned_project_resources_and_all_iam_state_are_retained(self):
        teardown.assert_owned(inventory(), ACCOUNT)
        manifest = teardown.retained_manifest(inventory(), ACCOUNT)
        self.assertEqual({value["address"] for value in manifest["resources"]},
                         {"aws_iam_role.terraform", "aws_s3_bucket_versioning.state"})
        self.assertNotIn("aws_vpc.retail", teardown.removed_hcl(manifest))

    def test_unknown_module_unowned_foreign_and_wrong_bucket_are_rejected(self):
        bad = [item("module.other.aws_vpc.retail", id="vpc-other"),
               item("aws_vpc.retail", id="vpc-other", tags_all={"Project": "other"}),
               item("aws_vpc.retail", id="vpc-other", arn="arn:aws:ec2:ap-southeast-1:999988887777:vpc/vpc-other",
                    tags_all={"Project": "retail-lab"}),
               item('aws_s3_bucket.data["logs"]', id="other-customer-bucket", tags_all={"Project": "retail-lab"})]
        for resource in bad:
            with self.subTest(resource=resource["address"]), self.assertRaises(ValueError):
                teardown.assert_owned([resource], ACCOUNT)

    def test_cfn_cannot_hide_deletion_of_iam_resources(self):
        value = item("aws_cloudformation_stack.managed_backends", id="stack-id",
                     tags_all={"Project": "retail-lab"},
                     template_body=json.dumps({"Resources": {"Role": {"Type": "AWS::IAM::Role"}}}))
        with self.assertRaisesRegex(ValueError, "ALL IAM"):
            teardown.assert_owned([value], ACCOUNT)

    def test_attachment_recovery_uses_provider_import_identity_not_random_state_id(self):
        value = item("aws_iam_role_policy_attachment.terraform", id="random-provider-id",
                     role="retail-lab-terraform", policy_arn=f"arn:aws:iam::{ACCOUNT}:policy/retail-lab-terraform")
        self.assertEqual(teardown.import_id(value), f"retail-lab-terraform/arn:aws:iam::{ACCOUNT}:policy/retail-lab-terraform")

    def test_generated_inline_policy_name_is_owned_through_its_project_role(self):
        value = item("aws_iam_role_policy.autoscaler", id="retail-lab-autoscaler:terraform-202611040001",
                     name="terraform-202611040001", role="retail-lab-autoscaler")
        teardown.assert_owned([value], ACCOUNT)
        self.assertEqual(teardown.import_id(value), "retail-lab-autoscaler:terraform-202611040001")
        value["values"]["role"] = "unrelated-role"
        with self.assertRaises(ValueError):
            teardown.assert_owned([value], ACCOUNT)


class PlanTests(unittest.TestCase):
    def test_real_delete_and_forget_actions_are_distinct(self):
        result = teardown.validate_plan(binary_json(), scope())
        self.assertEqual(result["delete"], ["aws_vpc.retail"])
        self.assertEqual(len(result["retain"]), 2)

    def test_iam_delete_create_update_replace_and_forged_scope_are_refused(self):
        for actions in (["delete"], ["create"], ["update"], ["delete", "create"], ["no-op"]):
            plan = binary_json()
            plan["resource_changes"][1]["change"]["actions"] = actions
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                teardown.validate_plan(plan, scope())
        altered = scope()
        altered["resources"][1]["retain"] = False
        plan = binary_json()
        plan["resource_changes"][1]["change"]["actions"] = ["delete"]
        with self.assertRaisesRegex(ValueError, "cannot override"):
            teardown.validate_plan(plan, altered)

    def test_foreign_addresses_different_identity_and_incomplete_plan_fail(self):
        for mutation in ("address", "id", "complete"):
            plan = binary_json()
            if mutation == "address":
                plan["resource_changes"][0]["address"] = "aws_vpc.unrelated"
            elif mutation == "id":
                plan["resource_changes"][0]["change"]["before"]["id"] = "vpc-new"
            else:
                plan["complete"] = False
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                teardown.validate_plan(plan, scope())

    def test_omitted_resource_is_not_silently_assumed_deleted(self):
        plan = binary_json()
        plan["resource_changes"].pop()
        with self.assertRaisesRegex(ValueError, "omits"):
            teardown.validate_plan(plan, scope())


class RecreateTests(unittest.TestCase):
    def test_existing_retained_role_blocks_future_create_before_any_mutation(self):
        plan = {"resource_changes": [{"type": "aws_iam_role", "change": {
            "actions": ["create"], "after": {"name": "retail-lab-eks-nodes"}}}]}
        with patch.object(teardown, "aws", return_value={"Account": ACCOUNT}), \
                patch.object(teardown, "optional_aws", return_value={"Role": {}}), \
                self.assertRaisesRegex(ValueError, "re-import"):
            teardown.guard_create(plan, ACCOUNT)

    def test_first_create_allows_computed_inline_names_for_new_roles(self):
        plan = {"resource_changes": [
            {"type": "aws_iam_role", "change": {"actions": ["create"], "after": {"name": "retail-lab-autoscaler"}}},
            {"type": "aws_iam_role_policy", "change": {"actions": ["create"], "after": {"role": None, "name": None}}},
            {"type": "aws_iam_role_policy_attachment", "change": {"actions": ["create"], "after": {"role": None, "policy_arn": "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"}}}]}
        with patch.object(teardown, "aws", return_value={"Account": ACCOUNT}), \
                patch.object(teardown, "optional_aws", return_value=None):
            teardown.guard_create(plan, ACCOUNT)

    def test_unknown_attachment_owner_without_new_role_is_rejected(self):
        plan = {"resource_changes": [{"type": "aws_iam_role_policy_attachment", "change": {
            "actions": ["create"], "after": {"role": None, "policy_arn": "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy"}}}]}
        with patch.object(teardown, "aws", return_value={"Account": ACCOUNT}), self.assertRaisesRegex(ValueError, "Unknown attachment owner"):
            teardown.guard_create(plan, ACCOUNT)

    def test_shared_service_role_reuse_does_not_emit_import_for_missing_count_instance(self):
        values = inventory() + [item('aws_iam_service_linked_role.opensearch[0]', id="shared-id",
                                    arn=f"arn:aws:iam::{ACCOUNT}:role/aws-service-role/opensearchservice.amazonaws.com/AWSServiceRoleForAmazonOpenSearchService")]
        manifest = teardown.retained_manifest(values, ACCOUNT)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "manifest.json"
            source.write_text(json.dumps(manifest))
            teardown.reimport(source, root / "reuse.tf", ACCOUNT)
            self.assertNotIn("aws_iam_service_linked_role.opensearch", (root / "reuse.tf").read_text())
            self.assertIn("aws_iam_role.terraform", (root / "reuse.tf").read_text())
            teardown.reimport(source, root / "manage.tf", ACCOUNT, include_service_linked_role=True)
            self.assertIn('aws_iam_service_linked_role.opensearch[0]', (root / "manage.tf").read_text())


class ReadinessTests(unittest.TestCase):
    def api(self, service, operation, *args):
        if service == "sts":
            return {"Account": ACCOUNT}
        if operation == "describe-load-balancers":
            return {"LoadBalancers": []}
        if operation == "describe-volumes":
            return {"Volumes": []}
        if operation == "describe-instances":
            return {"Reservations": []}
        raise AssertionError((service, operation))

    def test_free_account_cleanup_never_calls_paid_or_upgrade_gate(self):
        with patch.object(teardown, "optional_aws", return_value=None), patch.object(teardown, "aws", side_effect=self.api) as api:
            self.assertTrue(teardown.readiness(scope())["drained"])
        self.assertTrue(all(call.args[0] != "freetier" for call in api.call_args_list))

    def test_active_cluster_requires_actual_operator_kube_context(self):
        with self.assertRaisesRegex(ValueError, "Cluster still exists"):
            teardown.kube_drained({"endpoint": "https://checked.eks.amazonaws.com"}, None, None)

    def test_explicit_context_alias_is_checked_against_live_cluster_endpoint(self):
        commands = []
        def response(command, operation, **kwargs):
            commands.append(command)
            return json.dumps({"clusters": [{"cluster": {"server": "https://checked.eks.amazonaws.com"}}]}
                              if operation == "kube target" else {"items": []})
        with patch.object(teardown, "invoke", side_effect=response):
            teardown.kube_drained({"endpoint": "https://checked.eks.amazonaws.com"}, Path("dedicated.yaml"), "retail-lab-eks-dev")
        self.assertTrue(all(command[command.index("--context") + 1] == "retail-lab-eks-dev" for command in commands))
        with patch.object(teardown, "invoke", return_value=json.dumps({"clusters": [{"cluster": {"server": "https://wrong.eks.amazonaws.com"}}]})), \
                self.assertRaisesRegex(ValueError, "does not target"):
            teardown.kube_drained({"endpoint": "https://checked.eks.amazonaws.com"}, Path("dedicated.yaml"), "retail-lab-eks-dev")

    def test_alb_or_unattached_csi_volume_blocks_before_delete(self):
        for operation, result in (("describe-load-balancers", {"LoadBalancers": [{"VpcId": "vpc-11111111"}]}),
                                  ("describe-volumes", {"Volumes": [{"VolumeId": "vol-retained", "Attachments": []}]})):
            def api(service, name, *args):
                return result if name == operation else self.api(service, name, *args)
            with self.subTest(operation=operation), patch.object(teardown, "optional_aws", return_value=None), \
                    patch.object(teardown, "aws", side_effect=api), self.assertRaises(ValueError):
                teardown.readiness(scope())

    def test_rds_protection_and_nonempty_bucket_are_refused(self):
        target = scope()
        target["resources"].append({"address": 'aws_s3_bucket.data["logs"]', "type": "aws_s3_bucket",
                                    "id": f"retail-lab-{ACCOUNT}-logs", "retain": False})
        for blocked in ("rds", "bucket"):
            def optional(service, operation, *args, **kwargs):
                if blocked == "rds" and service == "rds":
                    return {"DBInstances": [{"DeletionProtection": True}]}
                if blocked == "bucket" and service == "s3api":
                    return {"Versions": [{"Key": "backup", "VersionId": "version"}]}
                return None
            with self.subTest(blocked=blocked), patch.object(teardown, "optional_aws", side_effect=optional), \
                    patch.object(teardown, "aws", side_effect=self.api), self.assertRaises(ValueError):
                teardown.readiness(target)

    def test_aws_denial_does_not_mean_absent(self):
        with patch.object(teardown, "aws", side_effect=teardown.CommandFailure("describe", "(AccessDenied)")):
            with self.assertRaises(teardown.CommandFailure):
                teardown.optional_aws("eks", "describe-cluster", missing=("ResourceNotFoundException",))


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for name in teardown.FILES + ("terraform.tfplan",):
            (self.directory / name).write_text(name)
        self.now = datetime.now(timezone.utc)
        self.sha = "a" * 40
        self.digest = plan_contract.checksum(self.directory / "terraform.tfplan")
        self.metadata = {"schema_version": 1, "operation": "lab-teardown", "project": "retail-lab",
                         "repository": plan_contract.REPOSITORY, "source_commit": self.sha,
                         "terraform_version": plan_contract.VERSION, "aws_account_id": ACCOUNT, "role_arn": ROLE,
                         "created_at": self.now.isoformat(), "execution": "github", "run_id": "123", "run_attempt": "1",
                         "sha256": self.digest, "files_sha256": {name: plan_contract.checksum(self.directory / name) for name in teardown.FILES}}
        self.run = {"id": 123, "run_attempt": 1, "head_sha": self.sha, "head_branch": "main", "event": "workflow_dispatch",
                    "conclusion": "success", "path": plan_contract.WORKFLOW}
        self.jobs = [{"name": "validate", "conclusion": "success"}, {"name": "destroy-plan", "conclusion": "success"},
                     {"name": "destroy-apply", "conclusion": "skipped"}]

    def verify(self, **changes):
        values = {"metadata": self.metadata, "directory": self.directory, "account": ACCOUNT, "role": ROLE,
                  "approved": self.digest, "commit": self.sha, "run": self.run, "jobs": self.jobs, "now": self.now}
        values.update(changes)
        return teardown.verify_metadata(**values)

    def test_reviewed_same_source_account_run_attempt_and_hash_pass(self):
        self.verify()

    def test_changed_account_source_run_age_or_gate_fails(self):
        variants = [{"account": "999988887777"}, {"commit": "b" * 40}, {"approved": "0" * 64},
                    {"run": dict(self.run, run_attempt=2)}, {"now": self.now + timedelta(days=2)},
                    {"jobs": [{"name": "validate", "conclusion": "success"}, {"name": "destroy-plan", "conclusion": "cancelled"}]}]
        for values in variants:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.verify(**values)

    def test_modified_retention_manifest_or_added_configuration_fails(self):
        (self.directory / "retained-resources.json").write_text("changed")
        with self.assertRaisesRegex(ValueError, "manifest changed"):
            self.verify()
        (self.directory / "surprise.tf").write_text("resource")
        with self.assertRaisesRegex(ValueError, "Unexpected files"):
            teardown.check_directory(self.directory, recorded=True)


class PlatformSecretTests(unittest.TestCase):
    def test_immediate_delete_requires_lab_container_proof(self):
        with self.assertRaisesRegex(ValueError, "lab secret-container"):
            teardown.platform_scope(ACCOUNT, 0, {"values": {}})

    def test_platform_finalizer_refuses_live_backend(self):
        with patch.object(teardown, "aws", return_value={"Account": ACCOUNT}), \
                patch.object(teardown, "optional_aws", return_value={"cluster": {}}), self.assertRaisesRegex(ValueError, "EKS must"):
            teardown.finalize_platform(scope())

    def test_default_is_seven_days_and_old_pending_deletes_are_never_forced(self):
        target = scope()
        name = "/retail/platform/mq/admin"
        arn = f"arn:aws:secretsmanager:ap-southeast-1:{ACCOUNT}:secret:{name}-Abc123"
        target["external_platform_secrets"] = [{"name": name, "arn": arn, "already_scheduled": False}]
        for pending in (False, True):
            def optional(service, operation, *args, **kwargs):
                if service != "secretsmanager":
                    return None
                return {"Name": name, "ARN": arn, **({"DeletedDate": "date"} if pending else {})}
            def api(service, operation, *args):
                return {"Account": ACCOUNT} if service == "sts" else {}
            with self.subTest(pending=pending), patch.object(teardown, "optional_aws", side_effect=optional), \
                    patch.object(teardown, "aws", side_effect=api) as calls:
                teardown.finalize_platform(target)
            deletions = [call for call in calls.call_args_list if call.args[1] == "delete-secret"]
            if pending:
                self.assertEqual(deletions, [])
            else:
                self.assertEqual(deletions[0].args[-2:], ("--recovery-window-in-days", "7"))


class PolicyTests(unittest.TestCase):
    def test_object_delete_is_limited_to_two_data_buckets_never_state(self):
        parts, _ = render_terraform_policy.render(ACCOUNT, "ap-southeast-1", "retail-state")
        statements = [value for part in parts for value in part["Statement"]]
        statement = next(value for value in statements if value["Sid"] == "ReviewedLabDataObjectCleanup")
        self.assertEqual(set(statement["Resource"]), {f"arn:aws:s3:::retail-lab-{ACCOUNT}-logs/*", f"arn:aws:s3:::retail-lab-{ACCOUNT}-backup/*"})
        self.assertEqual(set(statement["Action"]), {"s3:DeleteObject", "s3:DeleteObjectVersion"})


@unittest.skipUnless(shutil.which("terraform"), "Pinned Terraform CLI required for local-only forget/delete proof")
class TerraformSemanticsTests(unittest.TestCase):
    def test_saved_plan_forgets_retained_resource_and_deletes_disposable_without_aws(self):
        with tempfile.TemporaryDirectory(prefix="retail-teardown-test-") as directory:
            root = Path(directory)
            config = root / "main.tf"
            config.write_text('terraform { required_version = "= 1.17.1" }\nresource "terraform_data" "retained" { input = "keep" }\nresource "terraform_data" "disposable" { input = "delete" }\n')
            def run(*args):
                return subprocess.run(["terraform", "-chdir=" + directory, *args], check=True, capture_output=True, text=True, timeout=90).stdout
            run("init", "-input=false", "-no-color")
            run("apply", "-input=false", "-auto-approve", "-no-color")
            # A previous interrupted apply may have tainted the preserved SLR.
            run("taint", "terraform_data.retained")
            keep = teardown.removed_hcl({"resources": [{"address": "aws_iam_role.retained"}]}).replace("aws_iam_role.retained", "terraform_data.retained")
            config.write_text('terraform { required_version = "= 1.17.1" }\n' + keep)
            run("plan", "-input=false", "-no-color", "-out=reviewed.tfplan")
            value = json.loads(run("show", "-json", "reviewed.tfplan"))
            self.assertEqual({change["address"]: change["change"]["actions"] for change in value["resource_changes"]},
                             {"terraform_data.retained": ["forget"], "terraform_data.disposable": ["delete"]})
            run("apply", "-input=false", "-no-color", "reviewed.tfplan")
            self.assertFalse(run("state", "list").strip())


if __name__ == "__main__":
    unittest.main()

"""Restore plans only create isolated targets and require the reviewed identity."""
import copy
from datetime import datetime, timezone
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import ops_acceptance as ops
import restore_backends as restore
from test_runner import outputs


def target():
    return SimpleNamespace(account="111122223333", outputs=outputs(), metadata={"vpc_id": "vpc-0123456789abcdef0"},
                           context="retail-lab-eks", kubeconfig="private-config")


def rds_fixture():
    return {"DBInstanceIdentifier": "retail-lab-mysql", "DBInstanceArn": "arn:aws:rds:ap-southeast-1:111122223333:db:retail-lab-mysql",
            "Endpoint": {"Address": outputs()["mysql_endpoint"]}, "PubliclyAccessible": False, "StorageEncrypted": True,
            "Engine": "mysql", "DBInstanceClass": "db.t4g.small", "MasterUsername": "never-export-this-field",
            "DBParameterGroups": [{"DBParameterGroupName": "retail-mysql84-fixture"}],
            "DBSubnetGroup": {"DBSubnetGroupName": "retail-lab-mysql", "VpcId": "vpc-0123456789abcdef0",
                              "Subnets": [{"SubnetIdentifier": "subnet-0123456789abcdef0"}]}}


def snapshot():
    return {"DBSnapshotIdentifier": "rds:retail-lab-mysql-fixture",
            "DBSnapshotArn": "arn:aws:rds:ap-southeast-1:111122223333:snapshot:rds:retail-lab-mysql-fixture",
            "DBInstanceIdentifier": "retail-lab-mysql", "Status": "available", "Engine": "mysql", "Encrypted": True,
            "SnapshotCreateTime": datetime.now(timezone.utc).isoformat()}


def rds_api(database=None, saved=None):
    def invoke(service, action, *arguments):
        if action == "describe-db-instances":
            return {"DBInstances": [database or rds_fixture()]}
        if action == "describe-db-snapshots":
            return {"DBSnapshots": [saved or snapshot()]}
        if action == "describe-db-parameters":
            return {"Parameters": [{"ParameterName": "require_secure_transport", "ParameterValue": "ON"}]}
        raise AssertionError((service, action, arguments))
    return invoke


def reviewed_plan():
    with patch.object(ops, "aws", side_effect=rds_api()), patch.object(restore, "private_subnets"):
        return restore.plan_drill(target(), "rds", "lab20261104", "rds:retail-lab-mysql-fixture")


class RestorePlanTests(unittest.TestCase):
    def test_plan_is_read_only_isolated_and_contains_no_credentials(self):
        with patch.object(ops, "aws", side_effect=rds_api()) as api, patch.object(restore, "private_subnets"):
            plan = restore.plan_drill(target(), "rds", "lab20261104", "rds:retail-lab-mysql-fixture")
        self.assertEqual(plan["target_name"], "retail-drill-lab20261104")
        self.assertEqual(plan["plan_sha256"], restore.fingerprint(plan))
        self.assertFalse(plan["active_endpoints_changed"])
        self.assertNotIn("never-export-this-field", json.dumps(plan))
        self.assertTrue(all(call.args[1].startswith("describe-") for call in api.call_args_list))

    def test_target_cannot_be_an_active_backend_or_path(self):
        self.assertNotEqual(restore.identity("retail-lab-mysql"), "retail-lab-mysql")
        for operation in ("RETAIL-LAB-MYSQL", "../retail-dev", "-bad", "bad--id", "too-long-operation-identifier-for-drill", "abc"):
            with self.subTest(operation=operation), self.assertRaises(ops.OpsError):
                restore.identity(operation)

    def test_foreign_snapshot_is_rejected_before_any_network_creation(self):
        value = snapshot()
        value["DBSnapshotArn"] = value["DBSnapshotArn"].replace("111122223333", "999988887777")
        with patch.object(ops, "aws", side_effect=rds_api(saved=value)) as api, self.assertRaises(ops.OpsError):
            restore.plan_drill(target(), "rds", "lab20261104", value["DBSnapshotIdentifier"])
        self.assertTrue(all(not call.args[1].startswith("create-") for call in api.call_args_list))

    def test_public_database_or_wrong_endpoint_cannot_seed_restore(self):
        for change in ({"PubliclyAccessible": True}, {"Endpoint": {"Address": "some-other-rds.amazonaws.com"}}, {"StorageEncrypted": False}):
            database = dict(rds_fixture(), **change)
            with self.subTest(change=change), patch.object(ops, "aws", side_effect=rds_api(database=database)), self.assertRaises(ops.OpsError):
                restore.plan_drill(target(), "rds", "lab20261104", "snapshot")

    def test_subnet_with_default_route_is_not_an_isolated_data_subnet(self):
        responses = [{"Subnets": [{"SubnetId": "subnet-0123", "VpcId": "vpc-0123", "MapPublicIpOnLaunch": False}]},
                     {"RouteTables": [{"VpcId": "vpc-0123", "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-0123"}]}]}]
        with patch.object(ops, "aws", side_effect=responses), self.assertRaises(ops.OpsError):
            restore.private_subnets(["subnet-0123"], "vpc-0123")

    def test_edited_cross_account_or_active_target_plan_is_rejected(self):
        plan = reviewed_plan()
        restore.validate_plan(plan, target(), plan["plan_sha256"])
        for field, value in (("target_name", "retail-lab-mysql"), ("account_id", "999988887777"),
                             ("active_endpoints_changed", True), ("cluster_endpoint", "https://other.invalid")):
            changed = copy.deepcopy(plan)
            changed[field] = value
            changed["plan_sha256"] = restore.fingerprint(changed)
            with self.subTest(field=field), self.assertRaises(ops.OpsError):
                restore.validate_plan(changed, target(), changed["plan_sha256"])
        changed = dict(plan, instance_class="db.large")
        with self.assertRaises(ops.OpsError):
            restore.validate_plan(changed, target(), plan["plan_sha256"])

    def test_backend_request_preserves_isolation_and_uses_only_new_identifier(self):
        plan = reviewed_plan()
        with patch.object(ops, "aws", return_value={}) as api:
            restore.create_backend(plan, "sg-drill")
        body = json.loads(api.call_args.args[-1])
        self.assertFalse(body["PubliclyAccessible"])
        self.assertEqual(body["VpcSecurityGroupIds"], ["sg-drill"])
        self.assertEqual(body["DBInstanceIdentifier"], plan["target_name"])
        self.assertEqual(body["DBSnapshotIdentifier"], plan["source_arn"])
        # RestoreDBInstanceFromDBSnapshot documents this flag for Oracle only;
        # a MySQL drill must not assume it can initialize a new managed secret here.
        self.assertNotIn("ManageMasterUserPassword", body)
        self.assertNotIn("MasterUserPassword", body)

    def test_source_drift_stops_before_security_group_or_restore_write(self):
        plan = reviewed_plan()
        live = dict(plan, instance_class="db.other")
        report = {"changed": False}
        with patch.object(restore, "plan_drill", return_value=live), \
             patch.object(restore, "isolated_group") as group, patch.object(restore, "create_backend") as create, self.assertRaises(ops.OpsError):
            restore.execute(target(), plan, plan["plan_sha256"], 60, report)
        group.assert_not_called()
        create.assert_not_called()
        self.assertFalse(report["changed"])

    def test_resume_reuses_owned_backend_without_another_restore_request(self):
        plan = reviewed_plan()
        report = {"changed": False}
        with patch.object(restore, "plan_drill", return_value=plan), patch.object(restore, "isolated_group", return_value="sg-drill"), \
             patch.object(restore, "existing_backend", return_value={"DBInstanceIdentifier": plan["target_name"]}), \
             patch.object(restore, "wait_backend"), patch.object(restore, "create_backend") as create:
            restore.execute(target(), plan, plan["plan_sha256"], 60, report)
        create.assert_not_called()
        self.assertEqual(report["status"], "restored")
        self.assertFalse(report["changed"])
        self.assertEqual(report["functional_data_validation"], "not_run")

    def test_unowned_or_ingress_enabled_group_is_never_reused(self):
        plan = reviewed_plan()
        base = {"GroupId": "sg-other", "VpcId": plan["vpc_id"], "IpPermissions": [], "Tags": []}
        for group in (base, dict(base, Tags=restore.tags(plan), IpPermissions=[{"IpProtocol": "-1"}])):
            with self.subTest(group=group), patch.object(ops, "aws", return_value={"SecurityGroups": [group]}), self.assertRaises(ops.OpsError):
                restore.isolated_group(plan, create=True)

    def test_cleanup_requires_exact_operation_and_plan_tags(self):
        plan = reviewed_plan()
        with self.assertRaises(ops.OpsError):
            restore.verify_tags([{"Key": "RetailDrill", "Value": "someone-else"}], plan)
        restore.verify_tags(restore.tags(plan), plan)

    def test_redis_request_keeps_tls_acl_and_no_live_identifier(self):
        plan = reviewed_plan()
        plan.update(kind="redis", engine_version="7.1", user_groups=["retail-checkout"], instance_class="cache.t4g.small")
        with patch.object(ops, "aws", return_value={}) as api:
            restore.create_backend(plan, "sg-drill")
        body = json.loads(api.call_args.args[-1])
        self.assertTrue(body["TransitEncryptionEnabled"])
        self.assertTrue(body["AtRestEncryptionEnabled"])
        self.assertEqual(body["UserGroupIds"], ["retail-checkout"])
        self.assertNotEqual(body["ReplicationGroupId"], "retail-lab-redis")
        self.assertNotIn("AuthToken", body)

    def test_velero_restore_only_creates_inert_configmaps_in_new_namespace(self):
        selected = target()
        plan = reviewed_plan()
        configmap = {"kind": "ConfigMap", "metadata": {"name": "fixture"}, "data": {"key": "value"}}
        plan.update(kind="velero", source_backup="retail-daily-fixture", source_uid="backup-uid", source_archive="archive-sha",
                    source_etag="etag", configmap_digests={"fixture": ops.configmap_digest(configmap)})
        def kube(arguments, namespace=None):
            if arguments[1] == "namespace":
                return {}
            if arguments[1] == "backup.velero.io":
                return {"metadata": {"uid": "backup-uid"}, "status": {"phase": "Completed"}}
            if arguments[1] == "restore.velero.io":
                return {} if "--ignore-not-found" in arguments else {"status": {"phase": "Completed", "errors": 0}}
            if arguments[1] == "configmaps":
                return {"items": [configmap, {"kind": "ConfigMap", "metadata": {"name": "kube-root-ca.crt"},
                                             "data": {"ca.crt": "controller-generated"}}]}
            raise AssertionError(arguments)
        selected.kube = kube
        with patch.object(ops, "backup_archive", return_value={"sha256": "archive-sha", "etag": "etag"}), \
             patch.object(restore, "kube_create") as create:
            restore.velero_drill(selected, plan, 60)
        resource = create.call_args_list[-1].args[1]
        self.assertEqual(resource["spec"]["includedResources"], ["configmaps"])
        self.assertFalse(resource["spec"]["restorePVs"])
        self.assertFalse(resource["spec"]["includeClusterResources"])
        self.assertEqual(resource["spec"]["namespaceMapping"], {"retail-dev": plan["target_name"]})

    def test_velero_cleanup_never_deletes_unowned_namespace(self):
        selected = target()
        selected.kube = Mock(return_value={"metadata": {"annotations": {"retail.aws/drill": "other"}}})
        plan = dict(reviewed_plan(), kind="velero")
        with patch.object(restore, "kube_delete") as delete, self.assertRaises(ops.OpsError):
            restore.velero_drill(selected, plan, 60, cleanup=True)
        delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Plan and execute isolated restore drills; never replace an active backend."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time

import ops_acceptance as ops


def identity(operation_id):
    ops.require(re.fullmatch(r"[a-z][a-z0-9-]{3,19}", operation_id or "") is not None
                and not operation_id.endswith("-") and "--" not in operation_id,
                "invalid_operation", "Use a unique operation ID of 4-20 lowercase letters/digits/hyphens")
    return "retail-drill-" + operation_id


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def fingerprint(plan):
    return hashlib.sha256(canonical({key: value for key, value in plan.items() if key != "plan_sha256"}).encode()).hexdigest()


def own_arn(value, account, service, resource):
    ops.require(isinstance(value, str) and value.startswith(f"arn:aws:{service}:{ops.REGION}:{account}:{resource}"),
                "foreign_snapshot", "The selected resource belongs to another account, region or resource type")


def private_subnets(subnets, vpc):
    ops.require(subnets and re.fullmatch(r"vpc-[0-9a-f]+", vpc or "") is not None,
                "invalid_network", "Reviewed data subnets and VPC metadata are required")
    values = ops.aws("ec2", "describe-subnets", "--subnet-ids", *subnets).get("Subnets", [])
    ops.require({item.get("SubnetId") for item in values} == set(subnets)
                and all(item.get("VpcId") == vpc and item.get("MapPublicIpOnLaunch") is False for item in values),
                "invalid_network", "Restore subnets are outside the reviewed private VPC")
    for subnet in subnets:
        tables = ops.aws("ec2", "describe-route-tables", "--filters", "Name=association.subnet-id,Values=" + subnet).get("RouteTables", [])
        if not tables:
            tables = ops.aws("ec2", "describe-route-tables", "--filters", "Name=vpc-id,Values=" + vpc,
                             "Name=association.main,Values=true").get("RouteTables", [])
        ops.require(tables and all(table.get("VpcId") == vpc for table in tables), "invalid_network", "Cannot verify the private subnet route table")
        for table in tables:
            ops.require(not any(route.get("DestinationCidrBlock") == "0.0.0.0/0"
                                or route.get("DestinationIpv6CidrBlock") == "::/0" for route in table.get("Routes", [])),
                        "invalid_network", "Restore drills require the isolated data subnets without a default Internet/NAT route")


def plan_drill(target, kind, operation_id, source_backup):
    name = identity(operation_id)
    plan = {"schema_version": 1, "kind": kind, "operation_id": operation_id, "target_name": name,
            "account_id": target.account, "region": ops.REGION, "cluster_endpoint": target.outputs["cluster_endpoint"],
            "source_backup": source_backup, "created_at": datetime.now(timezone.utc).isoformat(),
            "active_endpoints_changed": False}
    if kind == "rds":
        source = ops.aws("rds", "describe-db-instances", "--db-instance-identifier", "retail-lab-mysql").get("DBInstances", [])
        ops.require(len(source) == 1, "missing_source", "Expected exactly the active lab database")
        database = source[0]
        own_arn(database.get("DBInstanceArn"), target.account, "rds", "db:retail-lab-mysql")
        ops.require(database.get("Endpoint", {}).get("Address") == target.outputs["mysql_endpoint"]
                    and database.get("PubliclyAccessible") is False and database.get("StorageEncrypted") is True
                    and database.get("Engine") == "mysql", "wrong_source", "Active RDS identity/encryption/network differs from Terraform outputs")
        snapshots = ops.aws("rds", "describe-db-snapshots", "--db-snapshot-identifier", source_backup).get("DBSnapshots", [])
        ops.require(len(snapshots) == 1, "invalid_snapshot", "Expected one selected RDS snapshot")
        snapshot = snapshots[0]
        own_arn(snapshot.get("DBSnapshotArn"), target.account, "rds", "snapshot:")
        ops.require(snapshot.get("DBInstanceIdentifier") == "retail-lab-mysql" and snapshot.get("Status") == "available"
                    and snapshot.get("Engine") == "mysql" and snapshot.get("Encrypted") is True,
                    "invalid_snapshot", "RDS snapshot is not an available encrypted snapshot of the lab database")
        group = database["DBSubnetGroup"]
        private_subnets([item["SubnetIdentifier"] for item in group["Subnets"]], target.metadata.get("vpc_id"))
        ops.require(group.get("VpcId") == target.metadata.get("vpc_id"), "invalid_network", "RDS subnet group differs from the reviewed VPC")
        parameter_group = database["DBParameterGroups"][0]["DBParameterGroupName"]
        parameters = ops.aws("rds", "describe-db-parameters", "--db-parameter-group-name", parameter_group).get("Parameters", [])
        ops.require(any(item.get("ParameterName") == "require_secure_transport" and str(item.get("ParameterValue", "")).upper() in {"ON", "1", "TRUE"}
                        for item in parameters), "insecure_parameters", "The source parameter group must require secure MySQL transport")
        plan.update(source_arn=snapshot["DBSnapshotArn"], source_created_at=snapshot["SnapshotCreateTime"],
                    source_resource="retail-lab-mysql", vpc_id=group["VpcId"], subnet_group=group["DBSubnetGroupName"],
                    instance_class=database["DBInstanceClass"], parameter_group=parameter_group)
    elif kind == "redis":
        source = ops.aws("elasticache", "describe-replication-groups", "--replication-group-id", "retail-lab-redis").get("ReplicationGroups", [])
        ops.require(len(source) == 1, "missing_source", "Expected the active lab Redis replication group")
        cache = source[0]
        own_arn(cache.get("ARN"), target.account, "elasticache", "replicationgroup:retail-lab-redis")
        ops.require(cache.get("Status") == "available" and cache.get("TransitEncryptionEnabled") is True
                    and cache.get("AtRestEncryptionEnabled") is True and cache.get("UserGroupIds") == ["retail-checkout"]
                    and cache.get("NodeGroups", [{}])[0].get("PrimaryEndpoint", {}).get("Address")
                        == target.outputs["backend_endpoints"]["RedisEndpoint"],
                    "wrong_source", "Redis identity, TLS, encryption or ACL differs from the reviewed lab")
        members = cache.get("MemberClusters", [])
        ops.require(len(members) == 1, "unsupported_topology", "This lab drill expects one Redis member")
        cluster = ops.aws("elasticache", "describe-cache-clusters", "--cache-cluster-id", members[0]).get("CacheClusters", [])[0]
        groups = ops.aws("elasticache", "describe-cache-subnet-groups", "--cache-subnet-group-name", cluster["CacheSubnetGroupName"]).get("CacheSubnetGroups", [])
        ops.require(len(groups) == 1 and groups[0].get("VpcId") == target.metadata.get("vpc_id"),
                    "invalid_network", "Redis subnet group differs from the reviewed VPC")
        private_subnets([item["SubnetIdentifier"] for item in groups[0]["Subnets"]], target.metadata.get("vpc_id"))
        snapshots = ops.aws("elasticache", "describe-snapshots", "--snapshot-name", source_backup).get("Snapshots", [])
        ops.require(len(snapshots) == 1, "invalid_snapshot", "Expected one selected Redis snapshot")
        snapshot = snapshots[0]
        own_arn(snapshot.get("ARN"), target.account, "elasticache", "snapshot:")
        ops.require(snapshot.get("SnapshotStatus") == "available" and snapshot.get("Engine") == "redis"
                    and (snapshot.get("ReplicationGroupId") == "retail-lab-redis" or snapshot.get("CacheClusterId") == members[0]),
                    "invalid_snapshot", "Redis snapshot does not belong to the reviewed lab cache")
        node_times = [ops.timestamp(item["SnapshotCreateTime"]) for item in snapshot.get("NodeSnapshots", [])]
        ops.require(node_times, "invalid_snapshot", "Redis snapshot has no immutable source timestamp")
        plan.update(source_arn=snapshot["ARN"], source_resource="retail-lab-redis", vpc_id=groups[0]["VpcId"],
                    subnet_group=cluster["CacheSubnetGroupName"], instance_class=cluster["CacheNodeType"],
                    engine_version=cluster["EngineVersion"], user_groups=cache["UserGroupIds"],
                    source_created_at=max(node_times).isoformat())
    elif kind == "velero":
        backups = target.kube(["get", "backups.velero.io", source_backup, "-o", "json"], "velero")
        backup = ops.latest_velero([backups], 168)
        archive = ops.backup_archive(target, backup, include_configmaps=True)
        expected = {key.split("/", 1)[1]: value for key, value in archive["configmap_digests"].items()
                    if key.startswith("retail-dev/") and key.split("/", 1)[1] != "kube-root-ca.crt"}
        ops.require(expected, "empty_backup", "The selected backup contains no dev ConfigMaps to verify")
        plan.update(source_uid=backup["metadata"]["uid"], source_resource="retail-dev", source_archive=archive["sha256"],
                    source_etag=archive["etag"], configmap_digests=expected)
    else:
        raise ops.OpsError("invalid_kind", "Supported drills are rds, redis and velero")
    plan["plan_sha256"] = fingerprint(plan)
    return plan


def validate_plan(plan, target, approved_hash, cleanup=False):
    ops.require(plan.get("schema_version") == 1 and plan.get("target_name") == identity(plan.get("operation_id")),
                "invalid_plan", "Restore plan identity is invalid")
    ops.require(re.fullmatch(r"[0-9a-f]{64}", approved_hash or "") is not None
                and approved_hash == plan.get("plan_sha256") == fingerprint(plan),
                "checksum_mismatch", "Review and approve the exact restore plan SHA256")
    ops.require(plan.get("account_id") == target.account and plan.get("region") == ops.REGION
                and plan.get("cluster_endpoint") == target.outputs["cluster_endpoint"]
                and plan.get("active_endpoints_changed") is False and plan.get("kind") in {"rds", "redis", "velero"},
                "wrong_target", "Restore plan differs from the current target")
    if not cleanup:
        ops.recent(plan.get("created_at"), 24)


def tags(plan):
    return [{"Key": "Project", "Value": "retail-lab"}, {"Key": "RetailDrill", "Value": plan["operation_id"]},
            {"Key": "RetailPlan", "Value": plan["plan_sha256"]}]


def verify_tags(values, plan):
    actual = {item["Key"]: item["Value"] for item in values}
    ops.require(all(actual.get(item["Key"]) == item["Value"] for item in tags(plan)),
                "ownership_mismatch", "Refusing a drill resource with different ownership tags")


def isolated_group(plan, create=False, report=None, missing_ok=False):
    groups = ops.aws("ec2", "describe-security-groups", "--filters", "Name=vpc-id,Values=" + plan["vpc_id"],
                     "Name=group-name,Values=" + plan["target_name"]).get("SecurityGroups", [])
    if not groups:
        if missing_ok and not create:
            return None
        ops.require(create, "missing_drill", "The drill security group does not exist")
        if report is not None:
            report["changed"] = True
        created = ops.aws("ec2", "create-security-group", "--vpc-id", plan["vpc_id"], "--group-name", plan["target_name"],
                          "--description", "Isolated retail restore drill", "--tag-specifications",
                          json.dumps([{"ResourceType": "security-group", "Tags": tags(plan)}]))
        groups = ops.aws("ec2", "describe-security-groups", "--group-ids", created["GroupId"]).get("SecurityGroups", [])
    ops.require(len(groups) == 1 and groups[0].get("VpcId") == plan["vpc_id"] and not groups[0].get("IpPermissions"),
                "unsafe_security_group", "Restore drill security group must have no ingress rules")
    verify_tags(groups[0].get("Tags", []), plan)
    return groups[0]["GroupId"]


def existing_backend(plan):
    if plan["kind"] == "rds":
        values = ops.aws("rds", "describe-db-instances").get("DBInstances", [])
        values = [item for item in values if item.get("DBInstanceIdentifier") == plan["target_name"]]
        if values:
            verify_tags(ops.aws("rds", "list-tags-for-resource", "--resource-name", values[0]["DBInstanceArn"]).get("TagList", []), plan)
    else:
        values = ops.aws("elasticache", "describe-replication-groups").get("ReplicationGroups", [])
        values = [item for item in values if item.get("ReplicationGroupId") == plan["target_name"]]
        if values:
            verify_tags(ops.aws("elasticache", "list-tags-for-resource", "--resource-name", values[0]["ARN"]).get("TagList", []), plan)
    ops.require(len(values) <= 1, "ambiguous_target", "Drill identity is ambiguous")
    return values[0] if values else None


def create_backend(plan, group):
    if plan["kind"] == "rds":
        request = {"DBInstanceIdentifier": plan["target_name"], "DBSnapshotIdentifier": plan["source_arn"],
                   "DBInstanceClass": plan["instance_class"], "DBSubnetGroupName": plan["subnet_group"],
                   "VpcSecurityGroupIds": [group], "DBParameterGroupName": plan["parameter_group"],
                   "PubliclyAccessible": False, "MultiAZ": False, "DeletionProtection": False,
                   "AutoMinorVersionUpgrade": False, "Tags": tags(plan)}
        ops.aws("rds", "restore-db-instance-from-db-snapshot", "--cli-input-json", json.dumps(request))
    else:
        request = {"ReplicationGroupId": plan["target_name"], "ReplicationGroupDescription": "Isolated retail restore drill",
                   "SnapshotName": plan["source_backup"], "Engine": "redis", "EngineVersion": plan["engine_version"],
                   "CacheNodeType": plan["instance_class"], "NumCacheClusters": 1, "CacheSubnetGroupName": plan["subnet_group"],
                   "SecurityGroupIds": [group], "AtRestEncryptionEnabled": True, "TransitEncryptionEnabled": True,
                   "TransitEncryptionMode": "required", "UserGroupIds": plan["user_groups"], "Tags": tags(plan)}
        ops.aws("elasticache", "create-replication-group", "--cli-input-json", json.dumps(request))


def wait_backend(plan, group, timeout, deleted=False):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resource = existing_backend(plan)
        if deleted and resource is None:
            return
        if resource:
            ready = resource.get("DBInstanceStatus" if plan["kind"] == "rds" else "Status")
            if ready == "available" and not deleted:
                if plan["kind"] == "rds":
                    ops.require(resource.get("PubliclyAccessible") is False and resource.get("StorageEncrypted") is True
                                and {item["VpcSecurityGroupId"] for item in resource.get("VpcSecurityGroups", [])} == {group},
                                "unsafe_restore", "Restored database is not isolated/private/encrypted")
                else:
                    ops.require(resource.get("TransitEncryptionEnabled") is True and resource.get("AtRestEncryptionEnabled") is True
                                and set(resource.get("UserGroupIds", [])) == set(plan["user_groups"]),
                                "unsafe_restore", "Restored Redis TLS/encryption/ACL differs from the plan")
                    members = resource.get("MemberClusters", [])
                    clusters = [ops.aws("elasticache", "describe-cache-clusters", "--cache-cluster-id", name).get("CacheClusters", [])[0] for name in members]
                    ops.require(clusters and all({item["SecurityGroupId"] for item in cache.get("SecurityGroups", [])} == {group} for cache in clusters),
                                "unsafe_restore", "Restored Redis is not isolated by its drill security group")
                return
        time.sleep(min(15, max(0, deadline - time.monotonic())))
    raise ops.OpsError("restore_timeout", "Restore readiness/deletion exceeded the deadline; inspect the retained drill resources")


def kube_create(target, document):
    arguments = ["kubectl", "--kubeconfig", target.kubeconfig, "--context", target.context,
                 "--request-timeout=30s", "create", "-f", "-", "-o", "json"]
    result = subprocess.run(arguments, input=json.dumps(document), text=True, capture_output=True, check=False, timeout=60)
    ops.require(result.returncode == 0, "kubernetes_write_failed", "A drill Kubernetes create failed; raw output was withheld")


def kube_delete(target, kind, name, metadata):
    # UID/resourceVersion preconditions prevent deleting a replaced resource.
    ops.require(metadata.get("uid") and metadata.get("resourceVersion"), "missing_identity", "Deletion requires an exact resource snapshot")
    from kubernetes import client, config
    config.load_kube_config(config_file=target.kubeconfig, context=target.context, persist_config=False)
    body = client.V1DeleteOptions(preconditions=client.V1Preconditions(uid=metadata["uid"], resource_version=metadata["resourceVersion"]),
                                 propagation_policy="Foreground")
    if kind == "namespace":
        client.CoreV1Api().delete_namespace(name, body=body, _request_timeout=30)
    else:
        client.CustomObjectsApi().delete_namespaced_custom_object("velero.io", "v1", "velero", "restores", name,
                                                                  body=body, _request_timeout=30)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        remaining = target.kube(["get", "namespace" if kind == "namespace" else "restore.velero.io", name,
                                 "--ignore-not-found", "-o", "json"], None if kind == "namespace" else "velero")
        if not remaining:
            return
        time.sleep(2)
    raise ops.OpsError("cleanup_timeout", "Owned Kubernetes resource deletion exceeded 120 seconds")


def velero_drill(target, plan, timeout, cleanup=False, report=None):
    name = plan["target_name"]
    namespaces = target.kube(["get", "namespace", name, "--ignore-not-found", "-o", "json"])
    expected = {"retail.aws/drill": plan["operation_id"], "retail.aws/plan": plan["plan_sha256"]}
    if namespaces:
        ops.require(all(namespaces.get("metadata", {}).get("annotations", {}).get(key) == value for key, value in expected.items()),
                    "ownership_mismatch", "The drill namespace already exists with different ownership")
    if cleanup:
        restore = target.kube(["get", "restore.velero.io", name, "--ignore-not-found", "-o", "json"], "velero")
        if restore:
            ops.require(all(restore.get("metadata", {}).get("annotations", {}).get(key) == value for key, value in expected.items()),
                        "ownership_mismatch", "The Restore belongs to another drill")
            ops.require(restore.get("status", {}).get("phase") in {"Completed", "Failed", "PartiallyFailed", "FailedValidation"},
                        "restore_active", "Wait for the restore controller to stop before cleanup")
            if report is not None:
                report["changed"] = True
            kube_delete(target, "restore", name, restore["metadata"])
        if namespaces:
            if report is not None:
                report["changed"] = True
            kube_delete(target, "namespace", name, namespaces["metadata"])
        return
    backup = target.kube(["get", "backup.velero.io", plan["source_backup"], "-o", "json"], "velero")
    ops.require(backup.get("metadata", {}).get("uid") == plan["source_uid"] and backup.get("status", {}).get("phase") == "Completed",
                "backup_changed", "The selected Velero backup identity/status changed after planning")
    archive = ops.backup_archive(target, backup, include_configmaps=True)
    ops.require(archive["sha256"] == plan["source_archive"] and archive["etag"] == plan["source_etag"],
                "backup_changed", "The selected S3 backup archive changed after review")
    if not namespaces:
        if report is not None:
            report["changed"] = True
        kube_create(target, {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": name, "annotations": expected}})
    # Reconcile isolation independently of Namespace creation: a previous attempt
    # may have stopped between creating the namespace and creating its policy.
    isolation = target.kube(["get", "networkpolicy", "deny-all", "--ignore-not-found", "-o", "json"], name)
    if isolation:
        spec = isolation.get('spec', {})
        ops.require(spec.get('podSelector') == {} and set(spec.get('policyTypes', [])) == {'Ingress', 'Egress'}
                    and not spec.get('ingress') and not spec.get('egress'),
                    'unsafe_isolation', 'The drill namespace isolation policy has changed')
    else:
        if report is not None:
            report['changed'] = True
        kube_create(target, {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                             "metadata": {"name": "deny-all", "namespace": name, "annotations": expected},
                             "spec": {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}})
    current = target.kube(["get", "restore.velero.io", name, "--ignore-not-found", "-o", "json"], "velero")
    document = {"apiVersion": "velero.io/v1", "kind": "Restore", "metadata": {"name": name, "namespace": "velero", "annotations": expected},
                "spec": {"backupName": plan["source_backup"], "includedNamespaces": ["retail-dev"], "includedResources": ["configmaps"],
                         "includeClusterResources": False, "namespaceMapping": {"retail-dev": name}, "restorePVs": False,
                         "existingResourcePolicy": "none"}}
    if current:
        ops.require(current.get("spec") == document["spec"] and current.get("metadata", {}).get("annotations", {}) == expected,
                    "ownership_mismatch", "An existing Restore differs from the reviewed plan")
    else:
        if report is not None:
            report["changed"] = True
        kube_create(target, document)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = target.kube(["get", "restore.velero.io", name, "-o", "json"], "velero")
        state = result.get("status", {})
        ops.require(state.get("phase") not in {"Failed", "PartiallyFailed", "FailedValidation"}, "restore_failed", "Velero restore failed")
        if state.get("phase") == "Completed":
            ops.require(state.get("errors", 0) == 0, "restore_failed", "Velero restore completed with errors")
            maps = target.kube(["get", "configmaps", "-o", "json"], name).get("items", [])
            hashes = {item["metadata"]["name"]: ops.configmap_digest(item) for item in maps
                      if item["metadata"]["name"] != "kube-root-ca.crt"}
            ops.require(hashes == plan["configmap_digests"], "restore_content_mismatch", "Restored ConfigMap content differs from the backup archive")
            return
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise ops.OpsError("restore_timeout", "Velero restore exceeded the deadline")


def execute(target, plan, approved, timeout, report, cleanup=False):
    validate_plan(plan, target, approved, cleanup)
    started = time.monotonic()
    report.update(operation_id=plan["operation_id"], target_name=plan["target_name"], kind=plan["kind"],
                  account_id=target.account, plan_sha256=approved)
    if plan["kind"] == "velero":
        velero_drill(target, plan, timeout, cleanup, report)
    else:
        if not cleanup:
            # Re-query the selected source and network before any drill writes.
            live = plan_drill(target, plan["kind"], plan["operation_id"], plan["source_backup"])
            ops.require(all(live.get(key) == value for key, value in plan.items() if key not in {"created_at", "plan_sha256"}),
                        "source_changed", "Restore source or topology changed after plan review")
        group = isolated_group(plan, create=not cleanup, report=report, missing_ok=cleanup)
        existing = existing_backend(plan)
        if cleanup:
            if existing:
                status = existing.get('DBInstanceStatus' if plan['kind'] == 'rds' else 'Status')
                if status != 'deleting':
                    report["changed"] = True
                    if plan["kind"] == "rds":
                        ops.aws("rds", "delete-db-instance", "--db-instance-identifier", plan["target_name"], "--skip-final-snapshot", "--delete-automated-backups")
                    else:
                        ops.aws("elasticache", "delete-replication-group", "--replication-group-id", plan["target_name"], "--no-retain-primary-cluster")
                wait_backend(plan, group, timeout, deleted=True)
            if group:
                report["changed"] = True
                ops.aws("ec2", "delete-security-group", "--group-id", group)
        else:
            if not existing:
                report["changed"] = True
                create_backend(plan, group)
            wait_backend(plan, group, timeout)
    report.update(status="cleaned" if cleanup else "restored", elapsed_seconds=round(time.monotonic() - started, 3),
                  active_endpoints_changed=False, functional_data_validation="not_run" if plan["kind"] != "velero" else "configmap_hashes_matched")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    ops.add_target_arguments(parser)
    parser.add_argument("--kind", choices=("rds", "redis", "velero"))
    parser.add_argument("--operation-id")
    parser.add_argument("--source-backup")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--cleanup", action="store_true")
    parser.add_argument("--approved-sha256")
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "operation": "restore_drill", "status": "failed", "changed": False}
    try:
        ops.require(60 <= args.timeout <= 7200, "invalid_timeout", "Drill timeout must be between 60 and 7200 seconds")
        target = ops.Target(args.terraform_output, args.account_id, args.context, args.kubeconfig, args.terraform_metadata, args.gitops_dir)
        if args.execute:
            plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
            execute(target, plan, args.approved_sha256, args.timeout, report, args.cleanup)
        else:
            ops.require(not args.cleanup and args.kind and args.source_backup and args.operation_id,
                        "plan_required", "Planning needs kind, operation ID and explicit source backup; cleanup always requires --execute")
            plan = plan_drill(target, args.kind, args.operation_id, args.source_backup)
            ops.write_report(args.plan, plan)
            report.update(status="planned", plan_sha256=plan["plan_sha256"], target_name=plan["target_name"], kind=plan["kind"],
                          account_id=target.account, operation_id=plan["operation_id"], created_at=plan["created_at"])
    except (ops.OpsError, ops.runner.RunnerError) as error:
        report.update(error_code=error.code, error=str(error))
    except Exception:
        report.update(error_code="restore_api_failed", error="Restore drill could not complete; inspect its retained owned resources; raw output was withheld")
    ops.write_report(args.report, report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] in {"planned", "restored", "cleaned"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Prepare and verify a saved lab teardown plan that retains all IAM and state."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import uuid

import plan_contract
from bootstrap_backends import PLATFORM

ROOT = Path(__file__).resolve().parents[2]
TF_ROOT = ROOT / "infra/retail-lab"
PROJECT, CLUSTER, REGION, STATE_BUCKET = "retail-lab", "retail-lab-eks", "ap-southeast-1", "manhnguyen1131"
FILES = ("versions.tf", ".terraform.lock.hcl", "retained.tf", "retained-resources.json", "scope.json")
ARTIFACT_FILES = set(FILES) | {"terraform.tfplan", "terraform-plan.txt", "metadata.json"}
ADDRESS = re.compile(r'^(aws_[a-z0-9_]+)\.([a-z][a-z0-9_]*)(?:\[(?:[0-9]+|"(?:[^"\\]|\\.)*")\])?$')


def require(condition, message):
    if not condition:
        raise ValueError(message)


class CommandFailure(RuntimeError):
    def __init__(self, operation, stderr):
        super().__init__("Read/validation command failed: " + operation)
        self.stderr = stderr  # Used only for known not-found codes; never printed.


def invoke(command, operation, timeout=120):
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=timeout)
    if result.returncode:
        raise CommandFailure(operation, result.stderr)
    return result.stdout


def aws(service, operation, *arguments):
    result = invoke(["aws", service, operation, "--region", REGION, "--output", "json",
                     "--no-cli-pager", *arguments], service + ":" + operation)
    return json.loads(result) if result.strip() else {}


def optional_aws(service, operation, *arguments, missing=()):
    try:
        return aws(service, operation, *arguments)
    except CommandFailure as error:
        if any("(" + code + ")" in error.stderr for code in missing):
            return None
        raise


def terraform(directory, *arguments):
    return invoke(["terraform", "-chdir=" + str(directory), *arguments], "terraform " + arguments[0])


def private_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)


def resources(module):
    result = list(module.get("resources", []))
    for child in module.get("child_modules", []):
        result.extend(resources(child))
    return [item for item in result if item.get("mode") == "managed"]


def family(address):
    match = ADDRESS.fullmatch(address)
    require(match is not None, "Only explicit resources in the reviewed root module can be torn down")
    return match.group(1) + "." + match.group(2)


def source_families():
    pattern = re.compile(r'^resource\s+"(aws_[a-z0-9_]+)"\s+"([a-z][a-z0-9_]*)"', re.MULTILINE)
    return {kind + "." + name for path in TF_ROOT.glob("*.tf")
            for kind, name in pattern.findall(path.read_text(encoding="utf-8-sig"))}


def retained(item):
    return item["type"].startswith("aws_iam_") or family(item["address"]) == "aws_s3_bucket_versioning.state"


def import_id(item):
    value = item["values"]
    if item["type"] == "aws_iam_role_policy_attachment":
        return value["role"] + "/" + value["policy_arn"]
    if item["type"] == "aws_iam_role_policy":
        return value["role"] + ":" + value["name"]
    if item["type"] in {"aws_iam_openid_connect_provider", "aws_iam_service_linked_role", "aws_iam_policy"}:
        return value["arn"]
    return value["id"]


def assert_owned(items, account, allowed=None):
    """Use exact source addresses plus tags/links/ARNs, never a regional delete."""
    require(re.fullmatch(r"[0-9]{12}", account) and account not in {"000000000000", "123456789012"},
            "Supply the actual AWS account ID")
    allowed = source_families() if allowed is None else allowed
    by_address = {item["address"]: item for item in items}
    require(len(by_address) == len(items), "Duplicate state addresses")
    owned_ids = set()
    for item in items:
        require(family(item["address"]) in allowed and item.get("type") == item["address"].split(".", 1)[0],
                "State contains a resource outside the reviewed retail root")
        value = item.get("values", {})
        require(isinstance(value, dict) and isinstance(value.get("id"), str) and value["id"],
                "Resource has no immutable state identity")
        arn = value.get("arn", "")
        if arn.startswith("arn:aws:"):
            require(arn.split(":")[4] in ("", account), "A resource ARN belongs to another account")
        if retained(item):
            if item["type"].startswith("aws_iam_"):
                name = value.get("role", "") if item["type"] in {
                    "aws_iam_role_policy", "aws_iam_role_policy_attachment"} else value.get("name", "")
                require(name.startswith("retail-lab-") or item["type"] in
                        {"aws_iam_openid_connect_provider", "aws_iam_service_linked_role"},
                        "Unexpected IAM identity in the lab state")
            else:
                require(value.get("bucket") == STATE_BUCKET, "Unexpected state foundation bucket")
            continue
        if item["type"].startswith("aws_s3_"):
            require(value.get("bucket", value["id"]) in {
                f"retail-lab-{account}-logs", f"retail-lab-{account}-backup"},
                "Unexpected data bucket identity")
        if item["type"] == "aws_cloudformation_stack":
            template = json.loads(value.get("template_body") or "{}")
            require(template.get("Resources") and not any(
                resource.get("Type", "").startswith("AWS::IAM::")
                for resource in template["Resources"].values()),
                "A stack contains IAM or has no inspectable template; teardown must retain ALL IAM")
        tags = value.get("tags_all") or value.get("tags") or {}
        if tags.get("Project") == PROJECT:
            owned_ids.add(value["id"])
    for item in items:
        if retained(item):
            continue
        value, kind = item["values"], item["type"]
        if value["id"] in owned_ids:
            if value.get("vpc_id"):
                require(value["vpc_id"] == by_address.get("aws_vpc.retail", {}).get("values", {}).get("id"),
                        "A tagged resource is outside the lab VPC")
            continue
        if kind.startswith("aws_s3_"):
            require(value.get("bucket") in {f"retail-lab-{account}-logs", f"retail-lab-{account}-backup"},
                    "Unexpected S3 bucket configuration")
        elif kind == "aws_route_table_association":
            require(value.get("route_table_id") in owned_ids and value.get("subnet_id") in owned_ids,
                    "Route association does not link owned lab resources")
        elif kind.startswith("aws_eks_"):
            require(value.get("cluster_name") == CLUSTER, "Unexpected EKS cluster resource")
        elif kind == "aws_autoscaling_group_tag":
            require(value.get("autoscaling_group_name", "").startswith(("eks-management-", "eks-dev-", "eks-prod-")),
                    "Unexpected Auto Scaling group tag")
        elif kind == "aws_acm_certificate_validation":
            require(value.get("certificate_arn") in {
                other["values"].get("arn") for other in items if other["type"] == "aws_acm_certificate"},
                "Certificate validation does not refer to the owned certificate")
        elif kind == "aws_route53_record":
            records = [option for other in items if other["type"] == "aws_acm_certificate"
                       for option in other["values"].get("domain_validation_options", [])]
            require(any(value.get("name", "").rstrip(".") == option.get("resource_record_name", "").rstrip(".")
                        and value.get("type") == option.get("resource_record_type")
                        and set(value.get("records", [])) == {option.get("resource_record_value")}
                        for option in records), "DNS record is not owned certificate validation")
        else:
            raise ValueError("Resource lacks project ownership proof: " + item["address"])
    return by_address


def retained_manifest(items, account):
    entries = [{"address": item["address"], "type": item["type"], "import_id": import_id(item)}
               for item in items if retained(item)]
    return {"schema_version": 1, "project": PROJECT, "account_id": account,
            "state_bucket": STATE_BUCKET, "resources": sorted(entries, key=lambda value: value["address"])}


def removed_hcl(manifest):
    # A complete resource family handles all count/for_each instances.
    families = sorted({family(item["address"]) for item in manifest["resources"]})
    return "\n".join("removed {\n  from = " + name +
                     "\n  lifecycle { destroy = false }\n}\n" for name in families)


def kube_drained(cluster, kubeconfig, context):
    require(kubeconfig is not None and isinstance(context, str) and bool(context.strip()),
            "Cluster still exists. Use the operator controller with explicit --kubeconfig/--context after draining GitOps; hosted CI cannot bypass this gate")
    command = ["kubectl", "--kubeconfig", str(kubeconfig), "--context", context, "--request-timeout=30s"]
    selected = json.loads(invoke(command + ["config", "view", "--minify", "-o", "json"], "kube target"))
    require(len(selected.get("clusters", [])) == 1 and selected["clusters"][0]["cluster"]["server"] == cluster["endpoint"],
            "Kubeconfig does not target the checked retail EKS endpoint")
    def get(kind):
        return json.loads(invoke(command + ["get", kind, "--all-namespaces", "-o", "json"], "drain " + kind)).get("items", [])
    crds = get("customresourcedefinitions")
    if any(item["metadata"]["name"] == "applications.argoproj.io" for item in crds):
        require(not get("applications.argoproj.io"), "Remove all Argo CD Applications after disabling GitOps reconciliation")
    require(not get("ingresses"), "Delete Ingress resources and wait for controller cleanup first")
    require(not any(item.get("spec", {}).get("type") == "LoadBalancer" for item in get("services")),
            "Delete LoadBalancer Services and wait for controller cleanup first")
    require(not get("persistentvolumeclaims") and not get("persistentvolumes"),
            "Archive/drain PVCs and retained PVs before deleting the cluster")
    require(not any(item["metadata"].get("namespace") not in {"kube-system", "kube-public", "kube-node-lease"}
                    for item in get("pods")), "Application/platform pods are still present; drain the lab first")


def readiness(scope, kubeconfig=None, context=None):
    """Only read AWS/Kubernetes. No account-plan gate applies to cleanup."""
    account = scope["account_id"]
    require(aws("sts", "get-caller-identity").get("Account") == account, "Wrong AWS account for teardown")
    cluster_result = optional_aws("eks", "describe-cluster", "--name", CLUSTER,
                                  missing=("ResourceNotFoundException",))
    if cluster_result:
        cluster = cluster_result["cluster"]
        require(cluster.get("arn") == f"arn:aws:eks:{REGION}:{account}:cluster/{CLUSTER}", "Wrong cluster identity")
        kube_drained(cluster, kubeconfig, context)
    vpc_ids = {item["id"] for item in scope["resources"] if item["type"] == "aws_vpc"}
    load_balancers = aws("elbv2", "describe-load-balancers").get("LoadBalancers", [])
    require(not any(item.get("VpcId") in vpc_ids for item in load_balancers),
            "Load balancers still exist in the lab VPC; wait for Kubernetes/controller cleanup")
    volumes = aws("ec2", "describe-volumes", "--filters", "Name=tag-key,Values=ebs.csi.aws.com/cluster").get("Volumes", [])
    instance_ids = set()
    if vpc_ids:
        reservations = aws("ec2", "describe-instances", "--filters",
                           "Name=vpc-id,Values=" + ",".join(sorted(vpc_ids))).get("Reservations", [])
        instance_ids = {item["InstanceId"] for reservation in reservations for item in reservation.get("Instances", [])}
    # Unattached CSI volumes lack a VPC identity: refuse ambiguity, never bulk-delete.
    require(not any(not item.get("Attachments") or
                    any(attachment.get("InstanceId") in instance_ids for attachment in item.get("Attachments", [])) or
                    any(tag.get("Key") == "kubernetes.io/cluster/" + CLUSTER for tag in item.get("Tags", []))
                    for item in volumes),
            "CSI volumes remain or are unassigned; inspect exact PV/volume ownership and archive them first (never bulk-delete)")
    mysql = optional_aws("rds", "describe-db-instances", "--db-instance-identifier", "retail-lab-mysql",
                         missing=("DBInstanceNotFound", "DBInstanceNotFoundFault"))
    if mysql and mysql.get("DBInstances"):
        require(mysql["DBInstances"][0].get("DeletionProtection") is False,
                "RDS deletion protection is enabled; review a normal lab configuration update first")
    if any(item["type"] == "aws_cloudformation_stack" for item in scope["resources"]):
        try:
            stacks = aws("cloudformation", "describe-stacks", "--stack-name", "retail-lab-managed-backends").get("Stacks", [])
        except CommandFailure as error:
            if "(ValidationError)" not in error.stderr or "does not exist" not in error.stderr:
                raise
            stacks = []
        require(not any(item.get("EnableTerminationProtection") for item in stacks),
                "CloudFormation termination protection must be disabled by a reviewed lab configuration update")
    for item in scope["resources"]:
        if item["type"] != "aws_s3_bucket":
            continue
        result = optional_aws("s3api", "list-object-versions", "--bucket", item["id"],
                              "--expected-bucket-owner", account, "--max-keys", "1", missing=("NoSuchBucket",))
        if result is not None:
            require(not result.get("Versions") and not result.get("DeleteMarkers") and not result.get("IsTruncated"),
                    "A data bucket is not empty; archive and explicitly clear its versions before teardown")
    return {"account_id": account, "cluster_absent": cluster_result is None, "drained": True,
            "checked_at": datetime.now(timezone.utc).isoformat()}


def scope_from_state(state, account):
    items = resources(state.get("values", {}).get("root_module", {}))
    assert_owned(items, account)
    return {"schema_version": 1, "project": PROJECT, "account_id": account,
            "resources": [{"address": item["address"], "type": item["type"], "id": item["values"]["id"],
                           "retain": retained(item)} for item in items]}, retained_manifest(items, account)


def platform_scope(account, recovery_days, state):
    require(recovery_days in (0, 7), "Platform deletion must use seven days or explicit lab-immediate mode")
    if recovery_days == 0:
        containers = [item for item in resources(state.get("values", {}).get("root_module", {}))
                      if item["type"] == "aws_secretsmanager_secret"]
        require(containers and all(item["values"].get("recovery_window_in_days") == 0 for item in containers),
                "Immediate platform deletion requires captured lab secret-container recovery=0 proof")
    result = []
    for name, _ in PLATFORM.values():
        metadata = optional_aws("secretsmanager", "describe-secret", "--secret-id", name,
                                missing=("ResourceNotFoundException",))
        if metadata is None:
            continue
        arn = metadata.get("ARN", "")
        require(metadata.get("Name") == name and re.fullmatch(
            r"arn:aws:secretsmanager:" + REGION + ":" + account + ":secret:" + re.escape(name) + r"-[A-Za-z0-9]{6}", arn),
            "Unexpected platform secret name/ARN; no credential values were read")
        result.append({"name": name, "arn": arn, "already_scheduled": bool(metadata.get("DeletedDate"))})
    return result


def finalize_platform(scope):
    """Only four reviewed platform metadata identities, after all backends are gone."""
    account = scope["account_id"]
    require(aws("sts", "get-caller-identity").get("Account") == account, "Wrong finalization account")
    require(optional_aws("eks", "describe-cluster", "--name", CLUSTER,
                         missing=("ResourceNotFoundException",)) is None, "EKS must be absent before platform credential deletion")
    require(optional_aws("rds", "describe-db-instances", "--db-instance-identifier", "retail-lab-mysql",
                         missing=("DBInstanceNotFound", "DBInstanceNotFoundFault")) is None,
            "MySQL must be absent before platform credential deletion")
    require(not any(item.get("BrokerName") == "retail-lab-rabbitmq" for item in
                    aws("mq", "list-brokers").get("BrokerSummaries", [])), "RabbitMQ must be absent before platform credential deletion")
    require(optional_aws("opensearch", "describe-domain", "--domain-name", "retail-lab-search",
                         missing=("ResourceNotFoundException",)) is None, "OpenSearch must be absent before platform credential deletion")
    require(optional_aws("elasticache", "describe-replication-groups", "--replication-group-id", "retail-lab-redis",
                         missing=("ReplicationGroupNotFoundFault",)) is None, "Redis must be absent before platform credential deletion")
    recovery = scope.get("platform_secret_recovery_days", 7)
    require(recovery in (0, 7), "Unexpected platform credential deletion mode")
    names = {name for name, _ in PLATFORM.values()}
    results = []
    for item in scope.get("external_platform_secrets", []):
        name, arn = item["name"], item["arn"]
        require(name in names and re.fullmatch(
            r"arn:aws:secretsmanager:" + REGION + ":" + account + ":secret:" + re.escape(name) + r"-[A-Za-z0-9]{6}", arn),
            "External deletion is limited to the four reviewed platform identities")
        current = optional_aws("secretsmanager", "describe-secret", "--secret-id", arn,
                               missing=("ResourceNotFoundException",))
        if current is None or current.get("DeletedDate"):
            # Never force an existing recovery window or silently restore anything.
            results.append({"name": name, "status": "absent_or_already_scheduled"})
            continue
        require(current.get("Name") == name and current.get("ARN") == arn, "Platform identity changed")
        if recovery == 0:
            aws("secretsmanager", "delete-secret", "--secret-id", arn, "--force-delete-without-recovery")
        else:
            aws("secretsmanager", "delete-secret", "--secret-id", arn, "--recovery-window-in-days", "7")
        results.append({"name": name, "status": "force_deleted_lab" if recovery == 0 else "scheduled_seven_days"})
    return results


def validate_plan(plan, scope):
    require(plan.get("terraform_version") == plan_contract.VERSION and plan.get("errored") is not True,
            "Unexpected or errored teardown plan")
    require(plan.get("complete") is not False and not plan.get("deferred_changes"), "Deferred/incomplete teardown plan")
    expected = {item["address"]: item for item in scope["resources"]}
    require(len(expected) == len(scope["resources"]), "Duplicate teardown scope identities")
    summary = {"delete": [], "retain": []}
    accounted = set()
    for change in plan.get("resource_changes", []):
        if change.get("mode") == "data":
            continue
        address = change["address"]
        require(address in expected and change.get("type") == expected[address]["type"],
                "Plan addresses are outside the reviewed lab scope")
        actions = change["change"]["actions"]
        keep = change["type"].startswith("aws_iam_") or family(address) == "aws_s3_bucket_versioning.state"
        require(expected[address]["retain"] is keep, "Scope cannot override IAM/state retention")
        wanted = ["forget"] if expected[address]["retain"] else ["delete"]
        require(actions == wanted, "Teardown may only delete owned lab resources and forget retained IAM/state")
        before = change["change"].get("before") or {}
        require(before.get("id") == expected[address]["id"], "Plan resource identity changed after scope capture")
        summary["retain" if expected[address]["retain"] else "delete"].append(address)
        accounted.add(address)
    for drift in plan.get("resource_drift", []):
        require(drift["address"] in expected and drift["change"]["actions"] == ["delete"],
                "Unexpected infrastructure drift; reconcile before teardown")
        accounted.add(drift["address"])
    require(accounted == set(expected), "Plan omits a captured resource; create a fresh complete teardown plan")
    require(not plan.get("checks") or all(item.get("status") == "pass" for item in plan["checks"]),
            "Teardown plan contains failed/unknown checks")
    return summary


def check_directory(directory, recorded=False):
    require(directory.is_dir() and not directory.is_symlink(), "Use a dedicated teardown directory")
    allowed = ARTIFACT_FILES | {".terraform"} if recorded else set(FILES) | {".terraform"}
    require(not any(path.name not in allowed or path.is_symlink() for path in directory.iterdir()),
            "Unexpected files in the isolated teardown root")


def prepare(directory, account, confirmation, kubeconfig=None, context=None, platform_recovery_days=7):
    require(confirmation == PROJECT, "Confirm the exact project retail-lab")
    require(not directory.exists(), "Use a new empty teardown directory; preserve previous evidence")
    state = json.loads(terraform(TF_ROOT, "show", "-json"))
    scope, manifest = scope_from_state(state, account)
    readiness(scope, kubeconfig, context)
    scope["external_platform_secrets"] = platform_scope(account, platform_recovery_days, state)
    scope["platform_secret_recovery_days"] = platform_recovery_days
    directory.mkdir(parents=True)
    for name in ("versions.tf", ".terraform.lock.hcl"):
        shutil.copyfile(TF_ROOT / name, directory / name)
    (directory / "retained.tf").write_text(removed_hcl(manifest), encoding="utf-8")
    private_json(directory / "scope.json", scope)
    private_json(directory / "retained-resources.json", manifest)
    check_directory(directory)
    print("Prepared isolated teardown root. All IAM and the state foundation will be retained; no AWS writes occurred.")


def source_commit(local=False):
    if not local:
        require(os.environ.get("GITHUB_REPOSITORY") == plan_contract.REPOSITORY and
                os.environ.get("GITHUB_REF") == "refs/heads/main", "Manual main workflow required")
        return os.environ["GITHUB_SHA"]
    require(not invoke(["git", "-C", str(ROOT), "status", "--porcelain"], "source status").strip(),
            "Commit/reconcile the source checkout before recording a local plan")
    return invoke(["git", "-C", str(ROOT), "rev-parse", "HEAD"], "source commit").strip()


def record(directory, account, role, local=False):
    check_directory(directory, recorded=True)
    scope = json.loads((directory / "scope.json").read_text())
    require(scope["account_id"] == account and role.startswith(f"arn:aws:iam::{account}:role/retail-lab-"),
            "Wrong reviewed account/operator role")
    summary = validate_plan(json.loads(terraform(directory, "show", "-json", "terraform.tfplan")), scope)
    metadata = {"schema_version": 1, "operation": "lab-teardown", "project": PROJECT,
                "repository": plan_contract.REPOSITORY, "source_commit": source_commit(local),
                "terraform_version": plan_contract.VERSION, "aws_account_id": account, "role_arn": role,
                "created_at": datetime.now(timezone.utc).isoformat(), "execution": "local" if local else "github",
                "run_id": str(uuid.uuid4()) if local else os.environ["GITHUB_RUN_ID"],
                "run_attempt": "1" if local else os.environ["GITHUB_RUN_ATTEMPT"],
                "sha256": plan_contract.checksum(directory / "terraform.tfplan"),
                "files_sha256": {name: plan_contract.checksum(directory / name) for name in FILES},
                "summary": summary}
    private_json(directory / "metadata.json", metadata)
    text = ("Teardown for " + account + " / retail-lab: " + str(len(summary["delete"])) +
            " owned resources to delete; ALL IAM/state retained.\n\nPlan run: " + metadata["run_id"] +
            "; attempt: " + metadata["run_attempt"] + "; SHA256: " + metadata["sha256"] +
            ".\n\nDownload retained-resources.json before apply; it is required for the next create/import.\n")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as target:
            target.write(text)
    print(text)


def verify_metadata(metadata, directory, account, role, approved, commit, local=False, run=None, jobs=None, now=None):
    require(metadata.get("schema_version") == 1 and metadata.get("operation") == "lab-teardown" and
            metadata.get("project") == PROJECT and metadata.get("repository") == plan_contract.REPOSITORY,
            "Wrong teardown contract")
    require(metadata.get("source_commit") == commit and re.fullmatch(r"[0-9a-f]{40}", commit),
            "Teardown source commit changed")
    require(metadata.get("aws_account_id") == account and metadata.get("role_arn") == role and
            role.startswith(f"arn:aws:iam::{account}:role/retail-lab-"), "Teardown account/role changed")
    require(metadata.get("terraform_version") == plan_contract.VERSION, "Wrong Terraform version")
    require(re.fullmatch(r"[0-9a-f]{64}", approved) and
            metadata.get("sha256") == approved == plan_contract.checksum(directory / "terraform.tfplan"),
            "Approved teardown checksum differs")
    require(set(metadata.get("files_sha256", {})) == set(FILES) and all(
        metadata["files_sha256"][name] == plan_contract.checksum(directory / name) for name in FILES),
        "Teardown configuration/scope/recovery manifest changed")
    created = datetime.fromisoformat(metadata["created_at"])
    require(created.tzinfo is not None, "Receipt time requires timezone")
    require(0 <= ((now or datetime.now(timezone.utc)) - created).total_seconds() <= 86400,
            "Teardown plan must be reviewed/applied within 24 hours")
    require(metadata.get("execution") == ("local" if local else "github"), "Wrong execution provenance")
    if local:
        require(re.fullmatch(r"[0-9a-f-]{36}", metadata.get("run_id", "")), "Invalid local operation identity")
        return
    require(run and jobs is not None, "GitHub run evidence required")
    require(str(run.get("id")) == metadata["run_id"] and str(run.get("run_attempt")) == metadata["run_attempt"],
            "Wrong/superseded teardown plan run")
    require(run.get("head_sha") == commit and run.get("head_branch") == "main" and
            run.get("event") == "workflow_dispatch" and run.get("conclusion") == "success" and
            run.get("path") == plan_contract.WORKFLOW, "Untrusted teardown plan workflow")
    results = {item["name"]: item.get("conclusion") for item in jobs}
    require(len(results) == len(jobs) and results.get("validate") == "success" and
            results.get("destroy-plan") == "success" and results.get("destroy-apply") in (None, "skipped"),
            "Teardown plan gates did not pass")


def verify(directory, account, role, confirmation, approved, local=False):
    require(confirmation == PROJECT, "Confirm the exact project retail-lab")
    check_directory(directory, recorded=True)
    metadata = json.loads((directory / "metadata.json").read_text())
    run = jobs = None
    if not local:
        require(metadata["run_id"] == os.environ["PLAN_RUN_ID"] and metadata["run_attempt"] == os.environ["PLAN_ATTEMPT"],
                "Wrong requested teardown artifact identity")
        require(re.fullmatch(r"[1-9][0-9]*", metadata["run_id"]) and
                re.fullmatch(r"[1-9][0-9]*", metadata["run_attempt"]), "Invalid workflow identity")
        run = plan_contract.github("/actions/runs/" + metadata["run_id"])
        jobs = plan_contract.github("/actions/runs/" + metadata["run_id"] + "/attempts/" +
                                    metadata["run_attempt"] + "/jobs?per_page=100")["jobs"]
        require(plan_contract.github("/git/ref/heads/main")["object"]["sha"] == os.environ["GITHUB_SHA"],
                "Main advanced; prepare a fresh teardown plan")
    verify_metadata(metadata, directory, account, role, approved, source_commit(local), local, run, jobs)
    print("Teardown source/account/run/attempt/checksum and recovery manifest verified; no AWS writes occurred.")


def reimport(manifest_path, output, account, include_service_linked_role=False):
    manifest = json.loads(manifest_path.read_text())
    require(manifest.get("schema_version") == 1 and manifest.get("account_id") == account and
            manifest.get("project") == PROJECT and manifest.get("state_bucket") == STATE_BUCKET,
            "Wrong recovery manifest target")
    require(not output.exists(), "Do not overwrite an existing import file")
    declarations = []
    for item in manifest["resources"]:
        require(item["type"].startswith("aws_iam_") or item["address"] == "aws_s3_bucket_versioning.state",
                "Recovery manifest may import only retained IAM/state")
        require(family(item["address"]) in source_families(), "Recovery address no longer exists in the reviewed source")
        require(isinstance(item["import_id"], str) and item["import_id"], "Missing import identity")
        if item["type"] == "aws_iam_service_linked_role" and not include_service_linked_role:
            # Normal fresh lab inputs reuse the account-wide role (resource count=0).
            # Importing its old instance would conflict with that configuration.
            continue
        declarations.append("import {\n  to = " + item["address"] + "\n  id = " + json.dumps(item["import_id"]) + "\n}\n")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(declarations), encoding="utf-8")
    output.chmod(0o600)
    print("Generated explicit import blocks only. Review a normal plan before any future create/apply.")


def guard_create(plan, account):
    """Prevent forgotten retained IAM from turning a later create into collisions."""
    require(aws("sts", "get-caller-identity").get("Account") == account, "Wrong create account")
    creates = [change for change in plan.get("resource_changes", [])
               if "create" in change.get("change", {}).get("actions", [])]
    new_roles = {(change["change"].get("after") or {}).get("name") for change in creates
                 if change.get("type") == "aws_iam_role"}
    for change in creates:
        kind = change.get("type", "")
        if not kind.startswith("aws_iam_") or "create" not in change.get("change", {}).get("actions", []):
            continue
        value = change["change"].get("after") or {}
        existing = None
        if kind == "aws_iam_role":
            name = value.get("name", "")
            require(name.startswith("retail-lab-"), "Unexpected role create identity")
            existing = optional_aws("iam", "get-role", "--role-name", name, missing=("NoSuchEntity",))
        elif kind == "aws_iam_service_linked_role":
            require(value.get("aws_service_name") == "opensearchservice.amazonaws.com", "Unknown service-linked role")
            existing = optional_aws("iam", "get-role", "--role-name", "AWSServiceRoleForAmazonOpenSearchService",
                                    missing=("NoSuchEntity",))
        elif kind == "aws_iam_role_policy":
            if not value.get("role"):
                require(new_roles, "Unknown policy owner without a newly-created role; import retained IAM first")
            elif not value.get("name"):
                require(value["role"] in new_roles,
                        "Generated inline-policy identity on an existing role requires recovery-manifest import")
            else:
                existing = optional_aws("iam", "get-role-policy", "--role-name", value["role"],
                                        "--policy-name", value["name"], missing=("NoSuchEntity",))
        elif kind == "aws_iam_role_policy_attachment":
            if not value.get("role"):
                require(new_roles, "Unknown attachment owner without a newly-created role; import retained IAM first")
            else:
                result = optional_aws("iam", "list-attached-role-policies", "--role-name", value["role"],
                                      missing=("NoSuchEntity",))
                existing = result and any(item.get("PolicyArn") == value.get("policy_arn")
                                          for item in result.get("AttachedPolicies", []))
        elif kind == "aws_iam_openid_connect_provider":
            # A newly-created EKS issuer can be unknown until cluster creation;
            # it has a fresh URL and does not collide with the retained old issuer.
            if value.get("url"):
                arn = f"arn:aws:iam::{account}:oidc-provider/" + value["url"].removeprefix("https://")
                existing = optional_aws("iam", "get-open-id-connect-provider", "--open-id-connect-provider-arn", arn,
                                        missing=("NoSuchEntity",))
        else:
            raise ValueError("Review/import the unsupported IAM create type before applying")
        require(not existing, "Retained IAM already exists: re-import it (or reuse the shared service-linked role) before creating another lab")
    print("Planned IAM creates have no known retained-identity collision; no IAM changes were made.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "record", "verify", "check", "inspect-plan", "complete", "finalize-platform", "reimport", "guard-create"))
    parser.add_argument("--directory", type=Path, default=ROOT / ".ci-tools/lab-teardown")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--confirm-project", default="")
    parser.add_argument("--role-arn", default=os.environ.get("TERRAFORM_ROLE_ARN", ""))
    parser.add_argument("--approved-sha256", default=os.environ.get("APPROVED_SHA256", ""))
    parser.add_argument("--kubeconfig", type=Path)
    parser.add_argument("--context")
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--import-output", type=Path)
    parser.add_argument("--include-service-linked-role", action="store_true",
                        help="Include the retained shared role only when create_opensearch_service_linked_role=true is explicitly configured")
    parser.add_argument("--platform-secret-recovery-days", type=int, choices=(0, 7), default=7)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.operation == "prepare":
        prepare(directory, args.account_id, args.confirm_project, args.kubeconfig, args.context, args.platform_secret_recovery_days)
    elif args.operation == "record":
        record(directory, args.account_id, args.role_arn, args.local)
    elif args.operation == "verify":
        verify(directory, args.account_id, args.role_arn, args.confirm_project, args.approved_sha256, args.local)
    elif args.operation == "check":
        require(args.confirm_project == PROJECT, "Confirm the exact project retail-lab")
        scope = json.loads((directory / "scope.json").read_text())
        require(scope["account_id"] == args.account_id, "Wrong checked teardown account")
        validate_plan(json.loads(terraform(directory, "show", "-json", "terraform.tfplan")), scope)
        readiness(scope, args.kubeconfig, args.context)
        print("Live drain/ownership gates and saved binary actions passed; cleanup is allowed on FREE and PAID accounts.")
    elif args.operation == "inspect-plan":
        scope = json.loads((directory / "scope.json").read_text())
        require(scope["account_id"] == args.account_id, "Wrong teardown account")
        print(json.dumps(validate_plan(json.loads(terraform(directory, "show", "-json", "terraform.tfplan")), scope), indent=2))
    elif args.operation == "complete":
        remaining = resources(json.loads(terraform(directory, "show", "-json")).get("values", {}).get("root_module", {}))
        require(not remaining, "Managed lab resources remain in state; review interrupted teardown before retrying")
        print("Terraform teardown complete. IAM/state were retained; keep the recovery manifest. This is not a region-wide orphan audit.")
    elif args.operation == "finalize-platform":
        verify(directory, args.account_id, args.role_arn, args.confirm_project, args.approved_sha256, args.local)
        remaining = resources(json.loads(terraform(directory, "show", "-json")).get("values", {}).get("root_module", {}))
        require(not remaining, "Complete the reviewed Terraform teardown before deleting platform credentials")
        scope = json.loads((directory / "scope.json").read_text())
        print(json.dumps(finalize_platform(scope), indent=2))
    elif args.operation == "guard-create":
        require(args.plan is not None, "Supply the reviewed binary --plan")
        guard_create(json.loads(terraform(TF_ROOT, "show", "-json", str(args.plan.resolve()))), args.account_id)
    else:
        require(args.manifest is not None and args.import_output is not None, "Supply --manifest and --import-output")
        reimport(args.manifest, args.import_output, args.account_id, args.include_service_linked_role)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, (ValueError, CommandFailure)) else type(error).__name__
        raise SystemExit("Lab teardown refused: " + message)

#!/usr/bin/env python3
"""Read-only regional compatibility checks before Terraform plan/apply."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

REGION = "ap-southeast-1"
ADDONS = {"vpc_cni": "vpc-cni", "coredns": "coredns", "kube_proxy": "kube-proxy", "ebs_csi": "aws-ebs-csi-driver"}
SEARCH_SERVICE_ROLES = {
    "AWSServiceRoleForAmazonOpenSearchService": "opensearchservice.amazonaws.com",
    "AWSServiceRoleForAmazonElasticsearchService": "es.amazonaws.com",
}
# Keep this tuple aligned with Search in infra/retail-lab/backends.tf.
SEARCH_ENGINE_VERSION = "OpenSearch_3.3"
SEARCH_INSTANCE_TYPE = "r8g.medium.search"
SEARCH_INSTANCE_COUNT = 1
SEARCH_STORAGE_TYPE = "gp3"
SEARCH_VOLUME_GIB = 50


def aws(service, operation, *arguments):
    command = ["aws", service, operation, "--region", REGION, "--output", "json", "--no-cli-pager", *arguments]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def addon_builds(result, kubernetes="1.36"):
    return {entry["addonVersion"] for addon in result.get("addons", []) for entry in addon.get("addonVersions", [])
            if any(item.get("clusterVersion") == kubernetes for item in entry.get("compatibilities", []))}


def standard_support(version):
    # versionStatus is the current API field. The deprecated status uses hyphens.
    status = version.get("versionStatus", version.get("status"))
    return isinstance(status, str) and status.replace("-", "_").upper() == "STANDARD_SUPPORT"


def check_search_service_role(config):
    create = config.get("create_opensearch_service_linked_role", False)
    if type(create) is not bool:
        raise ValueError("create_opensearch_service_linked_role must be a boolean")
    for name, service in SEARCH_SERVICE_ROLES.items():
        try:
            role = aws("iam", "get-role", "--role-name", name).get("Role", {})
        except subprocess.CalledProcessError as error:
            if "(NoSuchEntity)" not in (error.stderr or ""):
                raise
            continue
        path = "/aws-service-role/" + service + "/"
        arn = role.get("Arn", "")
        if (role.get("RoleName") != name or role.get("Path") != path
                or re.fullmatch(r"arn:aws:iam::[0-9]{12}:role" + re.escape(path + name), arn) is None):
            raise ValueError("Unexpected OpenSearch service-linked role identity/path")
        admin = config.get("admin_role_arn", "")
        if admin:
            identity = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/.+", admin)
            if identity is None or identity.group(1) != arn.split(":")[4]:
                raise ValueError("OpenSearch service-linked role and admin_role_arn must belong to the same account")
        statements = role.get("AssumeRolePolicyDocument", {}).get("Statement", [])
        if isinstance(statements, dict):
            statements = [statements]
        trusted = False
        for statement in statements:
            principal = statement.get("Principal", {})
            services = principal.get("Service", []) if isinstance(principal, dict) else []
            actions = statement.get("Action", [])
            if isinstance(services, str):
                services = [services]
            if isinstance(actions, str):
                actions = [actions]
            if statement.get("Effect") == "Allow" and service in services and "sts:AssumeRole" in actions:
                trusted = True
        if not trusted:
            raise ValueError("OpenSearch service-linked role does not trust the expected service")
        return
    if not create:
        raise ValueError("OpenSearch VPC service-linked role is missing. Set "
                         "create_opensearch_service_linked_role=true for Terraform to create it, "
                         "or provision it before planning.")


def check_search_instance_limits(result):
    """Reject unknown limits rather than assuming the selected domain is supported."""
    roles = result.get("LimitsByRole") if isinstance(result, dict) else None
    data = roles.get("data") if isinstance(roles, dict) else None
    instance = data.get("InstanceLimits") if isinstance(data, dict) else None
    counts = instance.get("InstanceCountLimits") if isinstance(instance, dict) else None
    minimum = counts.get("MinimumInstanceCount") if isinstance(counts, dict) else None
    maximum = counts.get("MaximumInstanceCount") if isinstance(counts, dict) else None
    if (type(minimum) is not int or type(maximum) is not int
            or not 1 <= minimum <= maximum):
        raise ValueError("Missing or invalid OpenSearch data-node count limits")
    if not minimum <= SEARCH_INSTANCE_COUNT <= maximum:
        raise ValueError("Selected OpenSearch instance count is outside the regional data-node limits")
    additional = data.get("AdditionalLimits", [])
    if not isinstance(additional, list):
        raise ValueError("Invalid OpenSearch additional instance limits")
    standalone = [item for item in additional if isinstance(item, dict)
                  and item.get("LimitName") == "MaximumNumberOfDataNodesWithoutMasterNode"]
    if standalone:
        values = standalone[0].get("LimitValues")
        if (len(standalone) != 1 or not isinstance(values, list) or len(values) != 1
                or not isinstance(values[0], str) or re.fullmatch(r"[0-9]+", values[0]) is None):
            raise ValueError("Invalid OpenSearch data-node limit without dedicated master nodes")
        if SEARCH_INSTANCE_COUNT > int(values[0]):
            raise ValueError("Selected OpenSearch data nodes require dedicated master nodes")
    storage_types = data.get("StorageTypes", [])
    if not isinstance(storage_types, list):
        raise ValueError("Missing or invalid OpenSearch storage types")
    storage = [item for item in storage_types if isinstance(item, dict)
               and item.get("StorageTypeName") == "ebs" and item.get("StorageSubTypeName") == SEARCH_STORAGE_TYPE]
    if len(storage) != 1:
        raise ValueError("OpenSearch " + SEARCH_INSTANCE_TYPE + " / " + SEARCH_ENGINE_VERSION
                         + " must offer one unambiguous EBS " + SEARCH_STORAGE_TYPE + " limit entry")
    limits = storage[0].get("StorageTypeLimits")
    if not isinstance(limits, list):
        raise ValueError("Missing OpenSearch EBS volume-size limits")
    sizes = {}
    for item in limits:
        if not isinstance(item, dict) or item.get("LimitName") not in {"MinimumVolumeSize", "MaximumVolumeSize"}:
            continue
        name = item["LimitName"]
        values = item.get("LimitValues")
        if (name in sizes or not isinstance(values, list) or len(values) != 1
                or not isinstance(values[0], str) or re.fullmatch(r"[0-9]+", values[0]) is None):
            raise ValueError("Missing or ambiguous OpenSearch EBS volume-size limits")
        sizes[name] = int(values[0])
    minimum = sizes.get("MinimumVolumeSize", 0)
    maximum = sizes.get("MaximumVolumeSize", 0)
    if not 1 <= minimum <= maximum:
        raise ValueError("Missing or invalid OpenSearch EBS volume-size bounds")
    if not minimum <= SEARCH_VOLUME_GIB <= maximum:
        raise ValueError("Selected OpenSearch " + str(SEARCH_VOLUME_GIB)
                         + " GiB EBS volume is outside the regional " + SEARCH_STORAGE_TYPE + " limits")


def check(config):
    versions = aws("eks", "describe-cluster-versions", "--cluster-versions", "1.36").get("clusterVersions", [])
    if not any(item.get("clusterVersion") == "1.36" and standard_support(item) for item in versions):
        raise ValueError("EKS 1.36 must be offered in Singapore under standard support")
    for key, name in ADDONS.items():
        result = aws("eks", "describe-addon-versions", "--kubernetes-version", "1.36", "--addon-name", name)
        selected = config["addon_versions"][key]
        if selected not in addon_builds(result):
            raise ValueError("Pinned " + name + " build is not compatible with EKS 1.36 in Singapore")
        if key == "vpc_cni" and tuple(int(x) for x in selected.removeprefix("v").split("-", 1)[0].split(".")[:2]) < (1, 21):
            raise ValueError("Native network policy plus Pod SG requires VPC CNI 1.21 or newer")
    mysql = aws("rds", "describe-db-engine-versions", "--engine", "mysql", "--engine-version", "8.4.11")
    if not any(item.get("EngineVersion") == "8.4.11" and item.get("Status") == "available" for item in mysql.get("DBEngineVersions", [])):
        raise ValueError("RDS MySQL 8.4.11 is not available in Singapore")
    options = aws("rds", "describe-orderable-db-instance-options", "--engine", "mysql", "--engine-version", "8.4.11", "--db-instance-class", config.get("mysql_instance_class", "db.t4g.small"))
    if not options.get("OrderableDBInstanceOptions"):
        raise ValueError("Selected MySQL instance class cannot run 8.4.11 in Singapore")
    redis = aws("elasticache", "describe-cache-engine-versions", "--engine", "redis", "--engine-version", "7.1")
    if not redis.get("CacheEngineVersions"):
        raise ValueError("ElastiCache Redis OSS 7.1 is unavailable in Singapore")
    mq = aws("mq", "describe-broker-engine-types", "--engine-type", "RABBITMQ")
    supported_mq = [version.get("name", "") for engine in mq.get("brokerEngineTypes", []) for version in engine.get("engineVersions", [])]
    if not any(version == "4.2" or version.startswith("4.2.") for version in supported_mq):
        raise ValueError("Amazon MQ RabbitMQ 4.2 is unavailable in Singapore")
    broker_options = aws("mq", "describe-broker-instance-options", "--engine-type", "RABBITMQ", "--host-instance-type", "mq.m7g.large")
    if not any("CLUSTER_MULTI_AZ" in item.get("supportedDeploymentModes", []) for item in broker_options.get("brokerInstanceOptions", [])):
        raise ValueError("mq.m7g.large does not offer CLUSTER_MULTI_AZ in Singapore")
    search = aws("opensearch", "list-versions")
    if SEARCH_ENGINE_VERSION not in search.get("Versions", []):
        raise ValueError("Amazon OpenSearch 3.3 is unavailable in Singapore")
    try:
        limits = aws("opensearch", "describe-instance-type-limits", "--engine-version", SEARCH_ENGINE_VERSION,
                     "--instance-type", SEARCH_INSTANCE_TYPE)
    except subprocess.CalledProcessError as error:
        raise ValueError("Cannot verify OpenSearch " + SEARCH_INSTANCE_TYPE + " / " + SEARCH_ENGINE_VERSION
                         + " in Singapore: DescribeInstanceTypeLimits failed; check regional support and IAM permissions") from error
    check_search_instance_limits(limits)
    check_search_service_role(config)
    aws("s3api", "head-bucket", "--bucket", "manhnguyen1131")
    # Only metadata is read; preflight never retrieves credential values.
    for arn in config["platform_secret_arns"].values():
        secret = aws("secretsmanager", "describe-secret", "--secret-id", arn)
        if not any("AWSCURRENT" in stages for stages in secret.get("VersionIdsToStages", {}).values()):
            raise ValueError("A platform secret has no AWSCURRENT value; run init-platform first")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tfvars", type=Path, required=True, help="JSON tfvars containing ARNs and configuration only")
    args = parser.parse_args()
    check(json.loads(args.tfvars.read_text(encoding="utf-8-sig")))
    print("Regional EKS/add-on/backend compatibility and bootstrap prerequisites passed.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, subprocess.SubprocessError) as error:
        print("Infrastructure preflight failed: " + (str(error) if isinstance(error, (ValueError, KeyError)) else type(error).__name__), file=sys.stderr)
        raise SystemExit(1)

#!/usr/bin/env python3
"""Read-only regional compatibility checks before Terraform plan/apply."""
import argparse
import ipaddress
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
STATE_BUCKET = "manhnguyen1131"
STATE_KEY = "retail/lab/terraform.tfstate"
PLATFORM_SECRET_KEYS = {"mq_admin", "search_admin", "redis_dev", "redis_prod"}
BLOCKED_ADMIN_CIDRS = (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.88.99.0/24", "192.168.0.0/16",
    "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)
WORKER_OFFERINGS = {"m6i.xlarge": {"ap-southeast-1a"},
                    "m6i.large": {"ap-southeast-1a", "ap-southeast-1b", "ap-southeast-1c"}}


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


def validate_inputs(config):
    """Reject placeholders and account/routing mistakes before making an AWS call."""
    if not isinstance(config, dict):
        raise ValueError("Terraform inputs must be a JSON object")
    cidrs = config.get("admin_cidrs")
    if not isinstance(cidrs, list) or not cidrs:
        raise ValueError("admin_cidrs requires at least one real public IPv4 /32")
    for cidr in cidrs:
        try:
            network = ipaddress.ip_network(cidr, strict=True)
        except (ValueError, TypeError) as error:
            raise ValueError("admin_cidrs must contain real public IPv4 /32 addresses") from error
        if (network.version != 4 or network.prefixlen != 32 or str(network) != cidr
                or not network.network_address.is_global or network.network_address.is_multicast
                or any(network.subnet_of(ipaddress.ip_network(blocked)) for blocked in BLOCKED_ADMIN_CIDRS)):
            raise ValueError("admin_cidrs must contain real public IPv4 /32 addresses, not private/documentation/reserved ranges")
    role = config.get("admin_role_arn", "")
    match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/[A-Za-z0-9+=,.@_/-]+", role) if isinstance(role, str) else None
    if match is None or match.group(1) in {"000000000000", "123456789012"}:
        raise ValueError("Supply the real administration IAM role ARN and AWS account ID")
    account = match.group(1)
    expected_oidc = "arn:aws:iam::" + account + ":oidc-provider/token.actions.githubusercontent.com"
    if config.get("github_oidc_provider_arn") != expected_oidc:
        raise ValueError("GitHub OIDC provider must belong to the administration account and token.actions.githubusercontent.com")
    policy = config.get("terraform_policy_arn", "")
    if not isinstance(policy, str) or re.fullmatch(r"arn:aws:iam::" + account + r":policy/[A-Za-z0-9+=,.@_/-]+", policy) is None:
        raise ValueError("Terraform policy ARN must belong to the administration account")
    additional = config.get("terraform_additional_policy_arns", [])
    if (not isinstance(additional, list) or len(additional) > 9
            or any(not isinstance(value, str) or re.fullmatch(r"arn:aws:iam::" + account + r":policy/[A-Za-z0-9+=,.@_/-]+", value) is None
                   or re.search(r"REPLACE|CHANGE_ME|YOUR_", value, re.IGNORECASE) for value in additional)
            or len(set(additional)) != len(additional) or policy in additional):
        raise ValueError("Supply up to nine distinct additional Terraform policy ARNs in the same account, excluding the primary policy")
    secrets = config.get("platform_secret_arns")
    if not isinstance(secrets, dict) or set(secrets) != PLATFORM_SECRET_KEYS:
        raise ValueError("Supply exactly the four documented platform secret ARNs")
    pattern = r"arn:aws:secretsmanager:" + REGION + ":" + account + r":secret:[A-Za-z0-9/_+=.@-]+"
    if any(not isinstance(value, str) or re.fullmatch(pattern, value) is None for value in secrets.values()):
        raise ValueError("Platform secret ARNs must belong to the administration account in Singapore")
    if any(re.search(r"REPLACE|CHANGE_ME|YOUR_", value, re.IGNORECASE)
           for value in [role, policy, *secrets.values()]):
        raise ValueError("Replace all example/placeholder IAM and secret values before planning")
    versions = config.get("addon_versions")
    if not isinstance(versions, dict) or set(versions) != set(ADDONS) or any(
            not isinstance(value, str) or re.fullmatch(r"v[0-9].*-eksbuild\.[0-9]+", value) is None
            for value in versions.values()):
        raise ValueError("Pin complete compatible eksbuild versions for all four managed add-ons")
    for key in ("mysql_multi_az", "create_opensearch_service_linked_role"):
        if key in config and type(config[key]) is not bool:
            raise ValueError(key + " must be a boolean")
    domain, zone = config.get("domain", ""), config.get("hosted_zone_id", "")
    if not isinstance(domain, str) or not isinstance(zone, str) or bool(domain) != bool(zone):
        raise ValueError("domain and hosted_zone_id must either both be empty or both be supplied")
    if domain:
        labels = domain.split(".")
        if (len(labels) < 2 or len(domain) > 253 or any(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) is None for label in labels)
                or domain.lower().endswith((".example", ".test", ".invalid", ".localhost", ".local"))
                or any(domain.lower() == example or domain.lower().endswith("." + example) for example in ("example.com", "example.net", "example.org"))
                or re.fullmatch(r"Z[A-Z0-9]+", zone) is None):
            raise ValueError("Supply a real controlled DNS domain and Route 53 hosted zone ID")
    return account


def check_state_bucket(account):
    """Check metadata only. Actual .tflock write/delete is verified by Terraform plan."""
    arguments = ("--bucket", STATE_BUCKET, "--expected-bucket-owner", account)

    def metadata(operation):
        try:
            return aws("s3api", operation, *arguments)
        except subprocess.CalledProcessError as error:
            raise ValueError("State bucket " + operation + " failed; check ownership, configuration and metadata permissions") from error

    head = metadata("head-bucket")
    region = head.get("BucketRegion")
    if not region:
        # Older AWS CLI response models omit BucketRegion from HeadBucket JSON.
        region = metadata("get-bucket-location").get("LocationConstraint") or "us-east-1"
        if region == "EU":
            region = "eu-west-1"
    if region != REGION:
        raise ValueError("Terraform state bucket must be in " + REGION)
    if metadata("get-bucket-versioning").get("Status") != "Enabled":
        raise ValueError("Enable state bucket Versioning before Terraform init")
    access = metadata("get-public-access-block").get("PublicAccessBlockConfiguration", {})
    if any(access.get(key) is not True for key in ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")):
        raise ValueError("Enable all four state bucket Block Public Access settings before Terraform init")
    rules = metadata("get-bucket-encryption").get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
    if not rules or any(rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
                        not in {"AES256", "aws:kms", "aws:kms:dsse"} for rule in rules):
        raise ValueError("State bucket default encryption is missing or unsupported")


def check_worker_offerings():
    """Check advertised type/AZ combinations, not live spare EC2 capacity."""
    result = aws("ec2", "describe-instance-type-offerings", "--location-type", "availability-zone",
                 "--filters", "Name=instance-type,Values=" + ",".join(sorted(WORKER_OFFERINGS)),
                 "Name=location,Values=ap-southeast-1a,ap-southeast-1b,ap-southeast-1c")
    available = {(item.get("InstanceType"), item.get("Location")) for item in result.get("InstanceTypeOfferings", [])
                 if item.get("LocationType") == "availability-zone"}
    missing = sorted(instance + " in " + zone for instance, zones in WORKER_OFFERINGS.items()
                     for zone in zones if (instance, zone) not in available)
    if missing:
        raise ValueError("Required worker instance/AZ offering is unavailable: " + ", ".join(missing))


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


def check_iam_prerequisites(config):
    """Read metadata for identities that must already exist before EKS is created."""
    try:
        role = aws("iam", "get-role", "--role-name", config["admin_role_arn"].rsplit("/", 1)[1]).get("Role", {})
        if role.get("Arn") != config["admin_role_arn"]:
            raise ValueError("The configured administration IAM role does not exist in this account")
        provider = aws("iam", "get-open-id-connect-provider", "--open-id-connect-provider-arn", config["github_oidc_provider_arn"])
        if (str(provider.get("Url", "")).removeprefix("https://") != "token.actions.githubusercontent.com"
                or "sts.amazonaws.com" not in provider.get("ClientIDList", [])):
            raise ValueError("GitHub OIDC provider must have the expected URL and STS audience")
        for arn in [config["terraform_policy_arn"], *config.get("terraform_additional_policy_arns", [])]:
            policy = aws("iam", "get-policy", "--policy-arn", arn).get("Policy", {})
            if policy.get("Arn") != arn or policy.get("IsAttachable") is not True:
                raise ValueError("A reviewed Terraform IAM policy does not exist or cannot be attached")
    except subprocess.CalledProcessError as error:
        raise ValueError("Cannot verify administration role, GitHub OIDC or Terraform policy metadata; check bootstrap and IAM read permissions") from error


def check(config):
    account = validate_inputs(config)
    identity = aws("sts", "get-caller-identity")
    if identity.get("Account") != account:
        raise ValueError("AWS caller account differs from the configured administration account")
    check_iam_prerequisites(config)
    check_state_bucket(account)
    check_worker_offerings()
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
    print("Account, state bucket metadata, worker AZ offerings and regional EKS/add-on/backend prerequisites passed.")
    print("Native state-lock write/delete permissions are verified when Terraform plan acquires the .tflock; metadata checks do not prove them.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, subprocess.SubprocessError) as error:
        print("Infrastructure preflight failed: " + (str(error) if isinstance(error, (ValueError, KeyError)) else type(error).__name__), file=sys.stderr)
        raise SystemExit(1)

#!/usr/bin/env python3
"""Read-only regional compatibility checks before Terraform plan/apply."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

REGION = "ap-southeast-1"
ADDONS = {"vpc_cni": "vpc-cni", "coredns": "coredns", "kube_proxy": "kube-proxy", "ebs_csi": "aws-ebs-csi-driver"}


def aws(service, operation, *arguments):
    command = ["aws", service, operation, "--region", REGION, "--output", "json", "--no-cli-pager", *arguments]
    result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=120)
    return json.loads(result.stdout) if result.stdout.strip() else {}


def addon_builds(result, kubernetes="1.36"):
    return {entry["addonVersion"] for addon in result.get("addons", []) for entry in addon.get("addonVersions", [])
            if any(item.get("clusterVersion") == kubernetes for item in entry.get("compatibilities", []))}


def check(config):
    versions = aws("eks", "describe-cluster-versions", "--cluster-versions", "1.36").get("clusterVersions", [])
    if not any(item.get("clusterVersion") == "1.36" and item.get("status") == "STANDARD_SUPPORT" for item in versions):
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
    if "OpenSearch_3.3" not in search.get("Versions", []):
        raise ValueError("Amazon OpenSearch 3.3 is unavailable in Singapore")
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
    check(json.loads(args.tfvars.read_text()))
    print("Regional EKS/add-on/backend compatibility and bootstrap prerequisites passed.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, subprocess.SubprocessError) as error:
        print("Infrastructure preflight failed: " + (str(error) if isinstance(error, (ValueError, KeyError)) else type(error).__name__), file=sys.stderr)
        raise SystemExit(1)

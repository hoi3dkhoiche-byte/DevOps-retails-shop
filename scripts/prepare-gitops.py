#!/usr/bin/env python3
"""Fill the GitOps seed with non-secret Terraform outputs, then validate it."""
import argparse
import ipaddress
import json
from pathlib import Path
import re
import sys

import yaml

ROOT = Path(__file__).resolve().parents[1]
GITOPS = ROOT / "gitops"
ROLES = ("lb", "autoscaler", "rollouts", "eso-dev", "eso-prod", "eso-monitoring", "eso-argocd", "grafana", "loki", "velero")


class ReadableDumper(yaml.SafeDumper):
    def ignore_aliases(self, value):
        return True


def multiline_string(dumper, value):
    return dumper.represent_scalar("tag:yaml.org,2002:str", value, style="|" if "\n" in value else None)


ReadableDumper.add_representer(str, multiline_string)


def read(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(yaml.dump(value, Dumper=ReadableDumper, sort_keys=False), encoding="utf-8", newline="\n")
    temporary.replace(path)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def validate(config):
    require(config["region"] == "ap-southeast-1" and config["clusterName"] == "retail-lab-eks", "Unexpected target region/cluster")
    account = str(config["accountId"])
    require(re.fullmatch(r"[0-9]{12}", account) is not None and account != "123456789012", "Supply the actual AWS account ID")
    for name, prefix in (("vpcId", "vpc-"), ("nodeSecurityGroupId", "sg-"), ("ordersSecurityGroupId", "sg-")):
        require(re.fullmatch(prefix + r"[0-9a-f]{8,17}", config[name]) is not None, "Invalid " + name)
    require(bool(config.get("adminCidrs")), "At least one administration /32 is required")
    examples = [ipaddress.ip_network(value) for value in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]
    for cidr in config["adminCidrs"]:
        network = ipaddress.ip_network(cidr)
        require(network.version == 4 and network.prefixlen == 32 and network.network_address.is_global
                and not any(network.subnet_of(example) for example in examples), "Use your actual public administration IPv4 /32")
    for name in ROLES:
        require(re.fullmatch(r"arn:aws:iam::" + account + r":role/.+", config["roles"].get(name, "")) is not None,
                "Missing or wrong-account IAM role: " + name)
    for name in ("logs", "backup"):
        require(config["buckets"].get(name) == "retail-lab-" + account + "-" + name, "Unexpected " + name + " bucket")
    if config.get("domain"):
        require(re.fullmatch(r"arn:aws:acm:ap-southeast-1:" + account + r":certificate/[0-9a-f-]+", config.get("certificateArn", "")) is not None,
                "HTTPS requires a validated ACM certificate in this account/region")


def prepare(outputs, domain=None, alerting=None):
    values = {key: value["value"] if isinstance(value, dict) and "value" in value else value for key, value in outputs.items()}
    config = read(GITOPS / "config.yaml")
    mapping = {"accountId": "account_id", "vpcId": "vpc_id", "nodeSecurityGroupId": "node_security_group_id",
               "ordersSecurityGroupId": "orders_security_group_id", "adminCidrs": "admin_cidrs", "roles": "workload_roles",
               "buckets": "buckets", "certificateArn": "certificate_arn"}
    for target, source in mapping.items():
        require(source in values, "Missing Terraform output: " + source)
        config[target] = values[source]
    config["domain"] = domain if domain is not None else values.get("domain", "")
    if alerting is not None:
        config["alerting"]["enabled"] = alerting
    validate(config)
    write(GITOPS / "config.yaml", config)
    for name, role in (("aws-load-balancer-controller", "lb"), ("argo-rollouts", "rollouts"), ("loki", "loki")):
        path = GITOPS / "platform" / (name + ".yaml")
        content = read(path)
        content["serviceAccount"]["annotations"]["eks.amazonaws.com/role-arn"] = config["roles"][role]
        if name == "aws-load-balancer-controller":
            content["vpcId"] = config["vpcId"]
        if name == "loki":
            content["loki"]["storage"]["bucketNames"] = dict.fromkeys(("chunks", "ruler", "admin"), config["buckets"]["logs"])
        write(path, content)
    path = GITOPS / "platform/cluster-autoscaler.yaml"
    content = read(path)
    content["rbac"]["serviceAccount"]["annotations"]["eks.amazonaws.com/role-arn"] = config["roles"]["autoscaler"]
    write(path, content)
    path = GITOPS / "platform/kube-prometheus-stack.yaml"
    content = read(path)
    content["grafana"]["serviceAccount"]["annotations"]["eks.amazonaws.com/role-arn"] = config["roles"]["grafana"]
    if config["alerting"]["enabled"]:
        content["alertmanager"]["alertmanagerSpec"]["configSecret"] = "alertmanager-notification-config"
    else:
        content["alertmanager"]["alertmanagerSpec"].pop("configSecret", None)
    write(path, content)
    path = GITOPS / "platform/velero.yaml"
    content = read(path)
    content["serviceAccount"]["server"]["annotations"]["eks.amazonaws.com/role-arn"] = config["roles"]["velero"]
    content["configuration"]["backupStorageLocation"][0]["bucket"] = config["buckets"]["backup"]
    write(path, content)
    return config


def check():
    validate(read(GITOPS / "config.yaml"))
    for path in (GITOPS / "platform").glob("*.yaml"):
        require("REPLACE_" not in path.read_text(), "Unfilled platform value in " + str(path))
    for name in ("rds-ca.pem", "rds-truststore.p12"):
        require((GITOPS / "applications/files" / name).stat().st_size > 0, "Missing public RDS trust material: " + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terraform-output", type=Path, help="File generated by terraform output -json")
    parser.add_argument("--domain")
    parser.add_argument("--enable-alerting", action="store_true", default=None)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    require(args.terraform_output or args.check, "Use --terraform-output FILE or --check")
    if args.terraform_output:
        prepare(json.loads(args.terraform_output.read_text(encoding="utf-8-sig")), args.domain, args.enable_alerting)
    check()
    print("GitOps target configuration is complete; no credentials were copied into Git.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError) as error:
        sys.exit(str(error))

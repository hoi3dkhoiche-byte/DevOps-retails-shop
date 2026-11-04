#!/usr/bin/env python3
"""Render reviewable IAM policy parts/trust for the fixed Retail Terraform role."""
import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = ROOT / "infra/retail-lab/policies/terraform-infra.json"
MAX_POLICY_SIZE = 6144


def require(condition, message):
    if not condition:
        raise ValueError(message)


def compact(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def render(account, region, bucket):
    require(re.fullmatch(r"[0-9]{12}", account) is not None and account != "123456789012",
            "Supply the actual AWS account ID")
    require(region == "ap-southeast-1", "This lab targets ap-southeast-1")
    require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) is not None
            and ".." not in bucket and re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", bucket) is None,
            "Supply a valid state bucket name")
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in (("__AWS_ACCOUNT_ID__", account), ("__AWS_REGION__", region), ("__STATE_BUCKET__", bucket)):
        text = text.replace(key, value)
    require("__" not in text, "Unresolved policy template placeholder")
    template = json.loads(text)
    parts = []
    current = {"Version": template["Version"], "Statement": []}
    for statement in template["Statement"]:
        candidate = {"Version": template["Version"], "Statement": current["Statement"] + [statement]}
        if len(compact(candidate)) > MAX_POLICY_SIZE:
            require(current["Statement"], "An individual statement exceeds the IAM policy limit")
            parts.append(current)
            current = {"Version": template["Version"], "Statement": [statement]}
            require(len(compact(current)) <= MAX_POLICY_SIZE, "An individual statement exceeds the IAM policy limit")
        else:
            current = candidate
    if current["Statement"]:
        parts.append(current)
    require(0 < len(parts) <= 10, "The default role attachment quota cannot hold this policy")
    trust = {"Version": "2012-10-17", "Statement": [{
        "Effect": "Allow", "Principal": {"Federated": "arn:aws:iam::" + account + ":oidc-provider/token.actions.githubusercontent.com"},
        "Action": "sts:AssumeRoleWithWebIdentity", "Condition": {"StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": "repo:hoi3dkhoiche-byte/DevOps-retails-shop:ref:refs/heads/main"}}}]}
    return parts, trust


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--state-bucket", default="manhnguyen1131")
    parser.add_argument("--output-dir", type=Path, default=ROOT / ".ci-tools/aws-bootstrap")
    args = parser.parse_args()
    parts, trust = render(args.account_id, args.region, args.state_bucket)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    policies = []
    for number, part in enumerate(parts, 1):
        name = "retail-lab-terraform" + ("" if number == 1 else "-" + str(number).zfill(2))
        path = args.output_dir / (name + ".json")
        path.write_text(json.dumps(part, indent=2) + "\n", encoding="utf-8")
        policies.append({"name": name, "file": path.name,
                         "arn": "arn:aws:iam::" + args.account_id + ":policy/" + name,
                         "canonical_size": len(compact(part))})
    (args.output_dir / "trust.json").write_text(json.dumps(trust, indent=2) + "\n", encoding="utf-8")
    manifest = {"schema_version": 1, "aws_account_id": args.account_id, "region": args.region,
                "state_bucket": args.state_bucket, "role_name": "retail-lab-terraform", "policies": policies,
                "terraform_policy_arn": policies[0]["arn"],
                "terraform_additional_policy_arns": [item["arn"] for item in policies[1:]]}
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print("Rendered " + str(len(parts)) + " IAM policy parts and main-branch OIDC trust. Review before attaching; no AWS calls were made.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Export only reviewed, non-secret Terraform metadata for bootstrap/GitOps."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


REQUIRED = {
    "account_id", "cluster_name", "cluster_endpoint", "vpc_id", "mysql_endpoint",
    "mysql_master_secret_arn", "backend_endpoints", "workload_roles", "node_security_group_id",
    "orders_security_group_id", "buckets", "certificate_arn", "admin_cidrs", "domain",
    "application_secret_arns", "platform_secret_arns", "monitoring_secret_arns", "terraform_role_arn",
}
ALLOWED = REQUIRED | {"public_subnet_ids", "s3_gateway_endpoint_id", "deployment_profile"}
FORBIDDEN = {"password", "secretstring", "secret_string", "secret_access_key", "aws_access_key_id",
             "aws_secret_access_key", "aws_session_token", "session_token", "token", "private_key",
             "tls_private_key", "authorization", ".dockerconfigjson"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def secret_free(value):
    if isinstance(value, dict):
        for key, item in value.items():
            require(str(key).lower() not in FORBIDDEN, "Credential fields are not deployment metadata")
            secret_free(item)
    elif isinstance(value, list):
        for item in value:
            secret_free(item)
    elif isinstance(value, str):
        if "://" in value:
            parsed = urlsplit(value)
            require(parsed.username is None and parsed.password is None, "Credential-bearing URLs cannot be exported")
        require("PRIVATE KEY-----" not in value, "Private key material cannot be exported")


def export(values):
    require(isinstance(values, dict), "Expected Terraform output mapping")
    require(REQUIRED <= values.keys(), "Required Terraform deployment outputs are missing")
    result = {}
    for key in sorted(ALLOWED & values.keys()):
        entry = values[key]
        require(isinstance(entry, dict) and "value" in entry and "type" in entry,
                "Expected native Terraform output entry: " + key)
        require(entry.get("sensitive") is False, "Sensitive output cannot be exported: " + key)
        secret_free(entry["value"])
        result[key] = entry
    account = result["account_id"]["value"]
    require(isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account) is not None,
            "Invalid deployment account ID")
    require(result["cluster_name"]["value"] == "retail-lab-eks", "Unexpected deployment cluster")
    if "deployment_profile" in result:
        profile = result["deployment_profile"]
        require(profile["type"] == "string" and isinstance(profile["value"], str)
                and profile["value"] in {"lab", "architecture"}, "Invalid deployment profile")
    endpoint = urlsplit(result["cluster_endpoint"]["value"])
    require(endpoint.scheme == "https" and endpoint.hostname is not None and endpoint.path in ("", "/")
            and not endpoint.query and not endpoint.fragment
            and re.fullmatch(r"[A-Za-z0-9.-]+\.ap-southeast-1\.eks\.amazonaws\.com", endpoint.hostname) is not None,
            "Unexpected EKS API endpoint")
    return result


def write_private(path, contents):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as target:
        path.chmod(0o600)
        target.write(contents)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--metadata-output", type=Path)
    args = parser.parse_args()
    result = export(json.loads(args.input.read_text(encoding="utf-8-sig")))
    contents = json.dumps(result, indent=2, sort_keys=True) + "\n"
    metadata = None
    if args.metadata_output:
        commit = os.environ.get("GITHUB_SHA", "")
        run = os.environ.get("GITHUB_RUN_ID", "")
        attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
        require(re.fullmatch(r"[0-9a-f]{40}", commit) is not None
                and re.fullmatch(r"[1-9][0-9]*", run) is not None
                and re.fullmatch(r"[1-9][0-9]*", attempt) is not None, "Missing trusted workflow identity")
        metadata = {"schema_version": 1, "repository": "hoi3dkhoiche-byte/DevOps-retails-shop",
                    "source_commit": commit, "run_id": run, "run_attempt": attempt,
                    "aws_account_id": result["account_id"]["value"],
                    "outputs_sha256": hashlib.sha256(contents.encode("utf-8")).hexdigest()}
    write_private(args.output, contents)
    if metadata is not None:
        write_private(args.metadata_output, json.dumps(metadata, indent=2, sort_keys=True) + "\n")
    print("Exported allowlisted deployment metadata; no state or credential values were included.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, TypeError, OSError):
        raise SystemExit("Deployment output export failed: verify output schema and workflow identity; no values were logged.")

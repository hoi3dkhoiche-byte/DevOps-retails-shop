#!/usr/bin/env python3
"""Read-only checks before Ansible is allowed to change the lab's AWS cluster."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

import yaml

REGION = "ap-southeast-1"
CLUSTER = "retail-lab-eks"
SOURCE_REPOSITORY = "hoi3dkhoiche-byte/DevOps-retails-shop"
GITOPS_REPOSITORY = "hoi3dkhoiche-byte/DevOps-retails-shop-gitops"


class TargetError(RuntimeError):
    """Only fixed, safe messages are emitted; command output is never forwarded."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(condition, code, message):
    if not condition:
        raise TargetError(code, message)


def invoke(command):
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False, timeout=45)
    except subprocess.TimeoutExpired as error:
        raise TargetError("command_timeout", "A read-only target check exceeded its timeout") from error
    except OSError as error:
        raise TargetError("tool_unavailable", "A required read-only command could not be started") from error
    require(result.returncode == 0, "command_failed", "A read-only target check failed; verify access and installed tools")
    return result.stdout


def json_object(contents, label):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "invalid_metadata", "Duplicate JSON keys are not accepted")
            result[key] = value
        return result

    try:
        value = json.loads(contents, object_pairs_hook=unique_pairs)
    except (ValueError, TypeError) as error:
        raise TargetError("invalid_metadata", label + " must contain valid JSON") from error
    require(isinstance(value, dict), "invalid_metadata", label + " must be an object")
    return value


def read_yaml(path):
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, yaml.YAMLError) as error:
        raise TargetError("invalid_metadata", "Cannot read GitOps YAML metadata") from error
    require(isinstance(value, dict), "invalid_metadata", "GitOps metadata must be a mapping")
    return value


def repository(value, expected):
    require(isinstance(value, str) and value.rstrip("/").removesuffix(".git") == "https://github.com/" + expected,
            "wrong_repository", "GitOps metadata must refer to the agreed source and GitOps repositories")


def offline(args):
    require(sys.version_info >= (3, 12), "python_version", "The control node requires Python 3.12 or newer")
    require(args.region == REGION and args.cluster_name == CLUSTER,
            "wrong_target", "This lab is configured for retail-lab-eks in ap-southeast-1")
    source = args.source_dir.resolve()
    gitops = args.gitops_dir.resolve()
    for marker in ("scripts/infra/run_bootstrap.py", "scripts/infra/bootstrap_backends.py", "scripts/infra/preflight.py"):
        require((source / marker).is_file(), "source_checkout", "The source checkout is missing an infrastructure helper")
    require(source != gitops and gitops != source / "gitops", "gitops_checkout",
            "Use the maintained GitOps repository checkout, not the source repository's historical gitops directory")
    for marker in ("scripts/prepare.py", "scripts/check-release.py", "argocd/retail-root.yaml",
                   "bootstrap/Chart.yaml", "argocd-secrets/Chart.yaml"):
        require((gitops / marker).is_file(), "gitops_checkout", "The maintained GitOps checkout is incomplete")
    config = read_yaml(gitops / "config.yaml")
    versions = read_yaml(gitops / "versions.yaml")
    repository(config.get("sourceRepo"), SOURCE_REPOSITORY)
    repository(config.get("gitopsRepo"), GITOPS_REPOSITORY)
    require(config.get("region") == REGION and config.get("clusterName") == CLUSTER,
            "wrong_target", "GitOps metadata names a different cluster or AWS region")
    require(isinstance(config.get("gitopsRevision"), str)
            and re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z/_.-]*", config["gitopsRevision"]) is not None,
            "invalid_metadata", "GitOps revision must be a nonempty string")
    for key in ("deployment", "repositoryCredentials"):
        require(isinstance(config.get(key), dict) and type(config[key].get("enabled")) is bool,
                "invalid_metadata", "GitOps feature switches must be explicit booleans")
    require({"argo-cd", "external-secrets"} <= versions.keys(),
            "invalid_metadata", "GitOps must pin the Argo CD and External Secrets chart versions")
    safe_versions = {}
    for name, chart in versions.items():
        require(isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9-]*", name) is not None,
                "invalid_metadata", "Platform chart names must be Helm chart names")
        require(isinstance(chart, dict) and isinstance(chart.get("version"), str)
                and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", chart["version"]) is not None
                and isinstance(chart.get("appVersion"), str)
                and re.fullmatch(r"v?[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?", chart["appVersion"]) is not None,
                "invalid_metadata", "Each platform chart needs a fixed chart version and appVersion")
        url = urlsplit(chart.get("repository", ""))
        require(url.scheme == "https" and bool(url.hostname) and not (url.username or url.password or url.query or url.fragment),
                "invalid_metadata", "Platform chart repositories must use HTTPS without credentials")
        safe_versions[name] = {key: chart[key] for key in ("repository", "version", "appVersion")}
    return {"schema_version": 1, "status": "success", "phase": args.phase,
            "source_dir": str(source), "gitops_dir": str(gitops), "region": REGION,
            "cluster_name": CLUSTER, "gitops_revision": config["gitopsRevision"],
            "versions": safe_versions}


def account_id(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9]{12}", value) is not None
            and value not in {"000000000000", "123456789012"},
            "account_required", "Set retail_account_id to the actual 12-digit AWS account ID")
    return value


def aws_json(arguments):
    return json_object(invoke(["aws", *arguments, "--region", REGION, "--output", "json", "--no-cli-pager"]), "AWS response")


def check_identity(expected):
    identity = aws_json(["sts", "get-caller-identity"])
    require(identity.get("Account") == expected, "wrong_account", "AWS credentials belong to a different account")


def load_outputs(source, path):
    # Reuse the backend runner's existing allowlist, ARN and endpoint validation.
    infra = source / "scripts/infra"
    spec = importlib.util.spec_from_file_location("retail_preflight_bootstrap", infra / "run_bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(infra))
    try:
        spec.loader.exec_module(module)
        return module.load_outputs(path)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as error:
        raise TargetError("invalid_outputs", "Terraform output failed the backend runner's account, role and endpoint validation") from error
    finally:
        sys.path.pop(0)


def verify_receipt(args, outputs):
    path = args.terraform_metadata
    if not path.is_file():
        require(args.allow_manual_outputs, "receipt_required", "Download metadata.json with the Terraform outputs artifact")
        return None
    try:
        receipt = json_object(path.read_text(encoding="utf-8-sig"), "Terraform output receipt")
        checksum = hashlib.sha256(args.terraform_output.read_bytes()).hexdigest()
    except OSError as error:
        raise TargetError("invalid_receipt", "Cannot read the Terraform output receipt or outputs file") from error
    require(type(receipt.get("schema_version")) is int and receipt["schema_version"] == 1
            and receipt.get("repository") == SOURCE_REPOSITORY
            and receipt.get("aws_account_id") == outputs["account_id"]
            and isinstance(receipt.get("source_commit"), str)
            and re.fullmatch(r"[0-9a-f]{40}", receipt["source_commit"]) is not None
            and all(isinstance(receipt.get(key), str) and re.fullmatch(r"[1-9][0-9]*", receipt[key]) is not None
                    for key in ("run_id", "run_attempt")),
            "invalid_receipt", "Terraform output receipt has an invalid identity or schema")
    require(receipt.get("outputs_sha256") == checksum, "checksum_mismatch", "Terraform outputs differ from their receipt checksum")
    head = invoke(["git", "-C", str(args.source_dir.resolve()), "rev-parse", "HEAD"]).strip()
    require(head == receipt["source_commit"], "source_commit_mismatch",
            "Check out the Terraform source commit recorded in metadata.json before bootstrapping")
    return receipt


def check(args):
    result = offline(args)
    if args.phase == "offline":
        return result
    expected = account_id(args.account_id)
    result["account_id"] = expected
    if args.phase == "account":
        check_identity(expected)
        return result
    require(args.terraform_output is not None and args.terraform_metadata is not None,
            "outputs_required", "Cluster preflight requires explicit Terraform output and receipt paths")
    require(isinstance(args.kube_context, str) and bool(args.kube_context.strip()) and args.kubeconfig is not None,
            "kube_target_required", "Cluster preflight requires an explicit kubeconfig and context")
    outputs = load_outputs(args.source_dir.resolve(), args.terraform_output)
    require(outputs["account_id"] == expected, "wrong_account", "Terraform outputs belong to a different AWS account")
    receipt = verify_receipt(args, outputs)
    check_identity(expected)
    response = aws_json(["eks", "describe-cluster", "--name", CLUSTER])
    cluster = response.get("cluster")
    require(isinstance(cluster, dict), "invalid_cluster", "EKS DescribeCluster returned no cluster metadata")
    require(cluster.get("arn") == f"arn:aws:eks:{REGION}:{expected}:cluster/{CLUSTER}"
            and cluster.get("name") == CLUSTER,
            "wrong_cluster", "EKS cluster identity differs from the configured AWS target")
    require(cluster.get("status") == "ACTIVE", "cluster_not_active", "The EKS cluster must be ACTIVE before bootstrapping")
    require(cluster.get("version") == "1.36", "cluster_version", "The lab requires the verified EKS Kubernetes version 1.36")
    require(cluster.get("endpoint") == outputs["cluster_endpoint"],
            "endpoint_mismatch", "The live EKS API endpoint differs from Terraform outputs")
    result.update(cluster_endpoint=outputs["cluster_endpoint"], kube_context=args.kube_context,
                  kubeconfig=str(args.kubeconfig.resolve()), outputs_receipt_verified=receipt is not None)
    if receipt is not None:
        result.update(source_commit=receipt["source_commit"], terraform_run_id=receipt["run_id"],
                      terraform_run_attempt=receipt["run_attempt"])
    return result


def parser():
    value = argparse.ArgumentParser(description=__doc__)
    value.add_argument("--phase", choices=("offline", "account", "cluster"), default="offline")
    value.add_argument("--source-dir", type=Path, required=True)
    value.add_argument("--gitops-dir", type=Path, required=True)
    value.add_argument("--region", default=REGION)
    value.add_argument("--cluster-name", default=CLUSTER)
    value.add_argument("--account-id", default="")
    value.add_argument("--terraform-output", type=Path)
    value.add_argument("--terraform-metadata", type=Path)
    value.add_argument("--kube-context")
    value.add_argument("--kubeconfig", type=Path)
    value.add_argument("--allow-manual-outputs", action="store_true")
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = check(args)
    except TargetError as error:
        print(json.dumps({"schema_version": 1, "status": "failed", "phase": args.phase,
                          "error_code": error.code, "error": str(error)}, sort_keys=True))
        return 1
    except (ValueError, TypeError, KeyError, OSError) as error:
        # A malformed URL or config must not produce a traceback containing its value.
        print(json.dumps({"schema_version": 1, "status": "failed", "phase": args.phase,
                          "error_code": "invalid_metadata", "error": "Target metadata is malformed; no values were logged"}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

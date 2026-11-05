#!/usr/bin/env python3
"""Validate the local GitOps bootstrap contract without contacting a cluster."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import yaml

GITOPS_REPO = "https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop-gitops.git"
SOURCE_REPO = "https://github.com/hoi3dkhoiche-byte/DevOps-retails-shop.git"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def read_mapping(path: Path) -> dict:
    value = yaml.safe_load(path.read_text(encoding="utf-8-sig"))
    require(isinstance(value, dict), f"Expected a YAML mapping: {path}")
    return value


def validate_root(root: dict, config: dict) -> None:
    require(root.get("apiVersion") == "argoproj.io/v1alpha1"
            and root.get("kind") == "Application", "Unexpected root resource kind")
    metadata = root.get("metadata", {})
    require(metadata.get("name") == "retail-root" and metadata.get("namespace") == "argocd",
            "Unexpected root Application name or namespace")
    spec = root.get("spec", {})
    expected_source = {
        "repoURL": config["gitopsRepo"], "targetRevision": config["gitopsRevision"],
        "path": "bootstrap", "helm": {"valueFiles": ["../config.yaml", "../versions.yaml"]},
    }
    require("sources" not in spec, "Root Application must use one source")
    require(spec.get("source") == expected_source,
            "Root source must use the configured repo/ref, bootstrap path and exact valueFiles; inline overrides are forbidden")
    require(spec.get("project") == "default", "Unexpected root Application project")
    require(spec.get("destination") == {
        "server": "https://kubernetes.default.svc", "namespace": "argocd",
    }, "Unexpected root Application destination")
    require(spec.get("syncPolicy", {}).get("automated") == {"selfHeal": True, "prune": False},
            "Root Application must self-heal without automatic pruning")


def validate_checkout(repo: Path) -> None:
    def git(*args: str) -> str:
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never", "GIT_ASKPASS": ""}
        result = subprocess.run(["git", "-c", "credential.interactive=never", "--no-optional-locks", "-C", str(repo), *args],
                                text=True, capture_output=True, check=False, timeout=30, env=environment)
        require(result.returncode == 0, "Cannot inspect the canonical GitOps checkout: " + result.stderr.strip())
        return result.stdout.strip()

    require(git("remote", "get-url", "origin") == GITOPS_REPO, "Unexpected GitOps origin URL")
    require(git("branch", "--show-current") == "main", "Bootstrap requires the canonical GitOps main branch")
    require(not git("status", "--porcelain", "--untracked-files=all"),
            "Bootstrap requires a clean, committed GitOps checkout; merge the candidate first")
    head = git("rev-parse", "HEAD")
    require(head == git("rev-parse", "refs/remotes/origin/main"),
            "Local GitOps main differs from origin/main; fetch and reconcile before bootstrap")
    remote_main = git("ls-remote", "--exit-code", "origin", "refs/heads/main").split()
    require(len(remote_main) == 2 and remote_main[1] == "refs/heads/main" and remote_main[0] == head,
            "GitOps checkout differs from the current remote main; fetch and reconcile before bootstrap")


def build_contract(repo: Path, account: str, region: str, cluster: str, deployment_profile: str | None = None) -> dict:
    config = read_mapping(repo / "config.yaml")
    versions = read_mapping(repo / "versions.yaml")
    root = read_mapping(repo / "argocd" / "retail-root.yaml")
    require(config.get("deployment", {}).get("enabled") is True,
            "GitOps deployment is disabled; prepare and activate reviewed Terraform outputs first")
    require(str(config.get("accountId")) == account and re.fullmatch(r"[0-9]{12}", account) is not None,
            "GitOps account does not match the checked AWS target")
    require(config.get("region") == region and config.get("clusterName") == cluster,
            "GitOps region/cluster does not match the checked AWS target")
    require(config.get("gitopsRepo") == GITOPS_REPO and config.get("sourceRepo") == SOURCE_REPO,
            "GitOps configuration points to an unexpected project repository")
    require(config.get("gitopsRevision") == "main", "Bootstrap requires the reviewed GitOps main revision")
    profile = config.get("deploymentProfile", "architecture")
    require(isinstance(profile, str) and profile in {"lab", "architecture"}, "Invalid GitOps deployment profile")
    require(deployment_profile is None or profile == deployment_profile,
            "GitOps deployment profile differs from the checked Terraform outputs")
    validate_root(root, config)
    charts = {}
    for chart in ("external-secrets", "argo-cd"):
        pin = versions.get(chart, {})
        require(isinstance(pin, dict) and set(("repository", "version", "appVersion")) <= pin.keys(),
                f"Missing chart/version pin: {chart}")
        require(str(pin["repository"]).startswith("https://")
                and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?", str(pin["version"])) is not None,
                f"Invalid chart pin: {chart}")
        values = repo / "platform" / f"{chart}.yaml"
        require(values.is_file(), f"Missing chart values: {values}")
        value_files = [str(values)]
        if profile == "lab":
            overlay = repo / "profiles/lab" / f"{chart}.yaml"
            require(overlay.is_file(), f"Missing lab chart overlay: {chart}")
            value_files.append(str(overlay))
        charts[chart] = {**pin, "values_file": str(values), "values_files": value_files}
    expected_secrets = ["argocd-admin", "argocd-redis"]
    if config.get("repositoryCredentials", {}).get("enabled") is True:
        expected_secrets.append("argocd-repositories")
    eso_sources = [
        {"repoURL": charts["external-secrets"]["repository"], "chart": "external-secrets",
         "targetRevision": str(charts["external-secrets"]["version"]),
         "helm": {"releaseName": "external-secrets", "skipTests": True,
                  "valueFiles": ["$values/" + Path(path).relative_to(repo).as_posix()
                                 for path in charts["external-secrets"]["values_files"]]}},
        {"repoURL": config["gitopsRepo"], "targetRevision": config["gitopsRevision"], "ref": "values"},
    ]
    platform_applications = ["argo-rollouts", "aws-load-balancer-controller", "external-secrets", "metrics-server",
                             "kube-prometheus-stack"]
    if profile == "architecture":
        platform_applications += ["cluster-autoscaler", "loki", "alloy", "velero"]
    return {"config": config, "versions": versions, "root": root, "charts": charts,
            "deployment_profile": profile, "platform_applications": platform_applications,
            "expected_external_secrets": expected_secrets, "eso_sources": eso_sources}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gitops-dir", type=Path, required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--cluster-name", required=True)
    parser.add_argument("--deployment-profile", choices=("lab", "architecture"))
    args = parser.parse_args()
    try:
        validate_checkout(args.gitops_dir.resolve())
        contract = build_contract(args.gitops_dir.resolve(), args.account_id, args.region, args.cluster_name,
                                  args.deployment_profile)
        print(json.dumps(contract))
    except (ValueError, OSError, subprocess.SubprocessError, yaml.YAMLError) as error:
        print(f"Bootstrap guard: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

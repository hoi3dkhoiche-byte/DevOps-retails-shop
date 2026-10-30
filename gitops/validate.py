#!/usr/bin/env python3
"""Render the GitOps graph and check routing, isolation and rollout invariants offline."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile

import yaml

GITOPS = Path(__file__).resolve().parent
SOURCE = GITOPS.parent / "retail-store-sample-app"
SERVICES = ("ui", "catalog", "cart", "checkout", "orders")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def render(chart, values=(), parameters=None, namespace="default", release="retail-check"):
    command = ["helm", "template", release, str(chart), "--namespace", namespace, "--kube-version", "1.36.0", "--skip-tests"]
    for value in values:
        command.extend(["-f", str(value)])
    for name, value in (parameters or {}).items():
        command.extend(["--set-string", name + "=" + value])
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=90)
    require(result.returncode == 0, result.stderr)
    return [document for document in yaml.safe_load_all(result.stdout) if document]


def validate_source_chart(service, environment):
    values = GITOPS / "environments" / environment / (service + ".yaml")
    docs = render(SOURCE / "src" / service / "chart", [values], {"image.repository": "docker.io/hmannnguye/devops-retails-shop" + ("" if service == "ui" else "-" + service), "image.digest": "sha256:" + "a" * 64})
    workload = next(document for document in docs if document["kind"] in ("Deployment", "Rollout"))
    pod = workload["spec"]["template"]["spec"]
    container = pod["containers"][0]
    require(container["image"].endswith("@sha256:" + "a" * 64), "Application chart lost the verified registry digest")
    ports = {port["name"]: port["containerPort"] for port in container["ports"]}
    for document in docs:
        if document["kind"] == "Service":
            for port in document["spec"]["ports"]:
                require(port["port"] == ports[port["targetPort"]] == 8080, "Native CNI network policies require service and container port 8080")
    selectors = pod["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    require(any({"key": "workload", "operator": "In", "values": [environment]} in term["matchExpressions"] for term in selectors), "Workload must require its own node group")
    if service == "ui":
        require(workload["kind"] == "Rollout", "UI must use Argo Rollouts")
        strategy = workload["spec"]["strategy"]["blueGreen"]
        require(strategy["activeService"] == "ui-active" and strategy["previewService"] == "ui-preview", "Blue/Green service routing mismatch")
        require(strategy["autoPromotionEnabled"] == (environment == "dev"), "Production UI promotion must stay manual")
        hpa = next(document for document in docs if document["kind"] == "HorizontalPodAutoscaler")
        require(hpa["spec"]["scaleTargetRef"]["kind"] == "Rollout", "UI HPA must target the Rollout")
        require(hpa["spec"]["maxReplicas"] == (3 if environment == "dev" else 7), "HPA limits differ from the design")
    else:
        secrets = [source["secretRef"]["name"] for source in container["envFrom"] if "secretRef" in source]
        require(service + "-runtime" in secrets, "Missing ESO runtime credential reference")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Application checkout; defaults to sibling retail-store-sample-app")
    parser.add_argument("--platform-cache", type=Path, help="Optional downloaded chart cache in <name>/<name> layout")
    args = parser.parse_args()
    global SOURCE
    if args.source:
        SOURCE = args.source.resolve()
    config = GITOPS / "config.yaml"
    bootstrap = render(GITOPS / "bootstrap", [config, GITOPS / "versions.yaml"])
    argocd_secrets = render(GITOPS / "argocd-secrets", [config])
    require({document["spec"]["target"]["name"] for document in argocd_secrets if document["kind"] == "ExternalSecret"}
            == {"argocd-secret", "argocd-redis"}, "Argo CD admin, TLS and Redis credentials must be supplied by ESO")
    storage = next(document for document in bootstrap if document["kind"] == "StorageClass")
    require(int(storage["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]) < 0, "StorageClass must exist before monitoring PVCs")
    grafana_secret = next(document for document in bootstrap if document["kind"] == "ExternalSecret" and document["metadata"]["name"] == "grafana-admin")
    require(int(grafana_secret["metadata"]["annotations"]["argocd.argoproj.io/sync-wave"]) < 0, "Grafana credentials must synchronize before the Grafana deployment")
    with tempfile.TemporaryDirectory() as directory:
        release = Path(directory) / "release.json"
        release.write_text(json.dumps({"release": {"enabled": True, "source_commit": "a" * 40, "backend_profile": "mysql-redis-opensearch"}, "images": {service: {"repository": "docker.io/hmannnguye/devops-retails-shop" + ("" if service == "ui" else "-" + service), "digest": "sha256:" + "b" * 64} for service in SERVICES}}))
        for environment in ("dev", "prod"):
            disabled = render(GITOPS / "applications", [config, GITOPS / "environments" / environment / "images.yaml"], {"environment": environment})
            seed = yaml.safe_load((GITOPS / "environments" / environment / "images.yaml").read_text())
            if not seed["release"]["enabled"]:
                require(not any(document["kind"] == "Application" for document in disabled), "Empty seed must not deploy unverified images")
            docs = render(GITOPS / "applications", [config, release], {"environment": environment})
            applications = [document for document in docs if document["kind"] == "Application"]
            require(len(applications) == 5, "A release must declare exactly five service Applications")
            for document in applications:
                require(document["spec"]["sources"][0]["targetRevision"] == "a" * 40, "Chart source must match the tested source commit")
                require(document["spec"]["destination"]["namespace"] == "retail-" + environment, "Service escaped its environment namespace")
                require(("automated" in document["spec"]["syncPolicy"]) == (environment == "dev"), "Production application sync must be manual")
            policy = next(document for document in docs if document["kind"] == "NetworkPolicy" and document["metadata"]["name"] == "default-deny")
            require(set(policy["spec"]["policyTypes"]) == {"Ingress", "Egress"}, "Both directions must default deny")
            domain = render(GITOPS / "applications", [config], {"environment": environment, "domain": "example.test", "certificateArn": "arn:aws:acm:ap-southeast-1:111111111111:certificate/test"})
            ingress = next(document for document in domain if document["kind"] == "Ingress")
            route = ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]
            require(route == {"name": "ui-active", "port": {"number": 8080}}, "ALB must route to the active UI on port 8080")
            for service in SERVICES:
                validate_source_chart(service, environment)
    render(GITOPS / "platform-resources", [config])
    if args.platform_cache:
        versions = yaml.safe_load((GITOPS / "versions.yaml").read_text())
        for name in versions:
            documents = render(args.platform_cache / name / name, [GITOPS / "platform" / (name + ".yaml")], release=name)
            if name == "argo-cd":
                require(not any(document["kind"] == "Secret" and document["metadata"]["name"] in {"argocd-secret", "argocd-redis"} for document in documents),
                        "Argo CD chart must not create or overwrite the ESO credential Secrets")
    print("GitOps graph passed: five digest-pinned applications in both environments, isolated routing, UI Rollout/HPA, bootstrap order and monitoring templates.")


if __name__ == "__main__":
    main()

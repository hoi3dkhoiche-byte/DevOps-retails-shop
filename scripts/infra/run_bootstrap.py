#!/usr/bin/env python3
"""Run backend initialization in a short-lived management-node Job using IRSA."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from bootstrap_backends import output_value

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "python:3.12-alpine@sha256:5789aa194f757888f41fa3d48e14b6feaa1b57c0af0b56fcb1372d6474d3bff0"


def resources(outputs, name):
    role = output_value(outputs, "workload_roles")["bootstrap"]
    if not re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/.+", role):
        raise ValueError("Terraform output has no valid backend-bootstrap IAM role")
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "retail-ops"}},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "backend-bootstrap", "namespace": "retail-ops", "annotations": {"eks.amazonaws.com/role-arn": role}}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": "retail-ops"}, "data": {
            "bootstrap_backends.py": (ROOT / "scripts/infra/bootstrap_backends.py").read_text(),
            "requirements.txt": (ROOT / "scripts/infra/requirements.txt").read_text(),
            "rds-ca.pem": (ROOT / "gitops/applications/files/rds-ca.pem").read_text(),
            "terraform-output.json": json.dumps(outputs),
        }},
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "retail-ops"}, "spec": {
            "backoffLimit": 0, "ttlSecondsAfterFinished": 3600, "activeDeadlineSeconds": 900,
            "template": {"metadata": {"labels": {"app.kubernetes.io/name": "backend-bootstrap"}}, "spec": {
                "restartPolicy": "Never", "serviceAccountName": "backend-bootstrap",
                "nodeSelector": {"workload": "management"}, "tolerations": [{"key": "workload", "operator": "Equal", "value": "management", "effect": "NoSchedule"}],
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "fsGroup": 1000},
                "containers": [{"name": "bootstrap", "image": IMAGE, "imagePullPolicy": "IfNotPresent",
                    "command": ["sh", "-ec"], "args": [
                        "python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes --target /tmp/dependencies -r /config/requirements.txt >/tmp/pip-install.log 2>&1 || { echo 'Dependency installation failed'; exit 1; }; "
                        "python /config/bootstrap_backends.py configure --terraform-output /config/terraform-output.json --rds-ca /config/rds-ca.pem"],
                    "env": [{"name": "PYTHONPATH", "value": "/tmp/dependencies"}, {"name": "AWS_REGION", "value": "ap-southeast-1"}, {"name": "AWS_STS_REGIONAL_ENDPOINTS", "value": "regional"}],
                    "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [{"name": "config", "mountPath": "/config", "readOnly": True}, {"name": "tmp", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "config", "configMap": {"name": name}}, {"name": "tmp", "emptyDir": {}}],
            }},
        }},
    ]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terraform-output", type=Path, required=True)
    parser.add_argument("--context", required=True, help="Explicit kubectl context for retail-lab-eks")
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args()
    name = "backend-bootstrap-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    outputs = json.loads(args.terraform_output.read_text())
    manifest = json.dumps(resources(outputs, name))
    if args.render_only:
        print(manifest)
        return
    command = ["kubectl", "--context", args.context, "--namespace", "retail-ops"]
    context = json.loads(subprocess.run(["kubectl", "--context", args.context, "config", "view", "--minify", "-o", "json"], text=True, check=True, capture_output=True).stdout)
    expected = output_value(outputs, "cluster_name")
    if expected != "retail-lab-eks" or not context.get("clusters"):
        raise ValueError("Explicit context must resolve to the Terraform retail-lab-eks cluster")
    # Bind to the actual API endpoint, not a user-controlled context alias alone.
    if context["clusters"][0]["cluster"]["server"] != output_value(outputs, "cluster_endpoint"):
        raise ValueError("kubectl context endpoint differs from the Terraform cluster endpoint")
    subprocess.run(command + ["apply", "-f", "-"], input=manifest, text=True, check=True)
    result = subprocess.run(command + ["wait", "--for=condition=complete", "job/" + name, "--timeout=900s"], check=False)
    subprocess.run(command + ["logs", "job/" + name], check=False)
    if result.returncode:
        raise RuntimeError("Private backend bootstrap Job did not complete")
    subprocess.run(command + ["delete", "configmap", name], check=True)


if __name__ == "__main__":
    main()

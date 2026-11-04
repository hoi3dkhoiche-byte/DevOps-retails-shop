#!/usr/bin/env python3
"""Run backend initialization in a short-lived management-node Job using IRSA."""
import argparse
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlsplit
import uuid
from bootstrap_backends import output_value

ROOT = Path(__file__).resolve().parents[2]
IMAGE = "python:3.12-alpine@sha256:5789aa194f757888f41fa3d48e14b6feaa1b57c0af0b56fcb1372d6474d3bff0"
DEPENDENCY_INSTALL_COMMAND = (
    "python -m pip install --no-cache-dir --disable-pip-version-check --require-hashes "
    "--target /tmp/dependencies -r /config/requirements.txt >/tmp/pip-install.log 2>&1 "
    "|| { echo 'Dependency installation failed'; exit 1; }"
)
REGION = "ap-southeast-1"
NAMESPACE = "retail-ops"
DEFAULT_TIMEOUT = 900
OUTPUT_KEYS = ("account_id", "cluster_name", "cluster_endpoint", "workload_roles", "mysql_endpoint",
               "mysql_master_secret_arn", "backend_endpoints", "platform_secret_arns")


def resources(outputs, name, timeout=DEFAULT_TIMEOUT):
    role = output_value(outputs, "workload_roles")["bootstrap"]
    if not re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/.+", role):
        raise ValueError("Terraform output has no valid backend-bootstrap IAM role")
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "retail-ops"}},
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "backend-bootstrap", "namespace": "retail-ops", "annotations": {"eks.amazonaws.com/role-arn": role}}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": "retail-ops"}, "data": {
            "bootstrap_backends.py": (ROOT / "scripts/infra/bootstrap_backends.py").read_text(encoding="utf-8"),
            "requirements.txt": (ROOT / "scripts/infra/requirements.txt").read_text(encoding="utf-8"),
            "rds-ca.pem": (ROOT / "gitops/applications/files/rds-ca.pem").read_text(encoding="utf-8"),
            "terraform-output.json": json.dumps({key: output_value(outputs, key) for key in OUTPUT_KEYS if key in outputs}),
        }},
        {"apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "namespace": "retail-ops",
            "labels": {"app.kubernetes.io/name": "backend-bootstrap", "retail.aws/run-id": name}}, "spec": {
            "backoffLimit": 0, "ttlSecondsAfterFinished": 3600, "activeDeadlineSeconds": timeout,
            "template": {"metadata": {"labels": {"app.kubernetes.io/name": "backend-bootstrap"}}, "spec": {
                "restartPolicy": "Never", "serviceAccountName": "backend-bootstrap",
                "nodeSelector": {"workload": "management"}, "tolerations": [{"key": "workload", "operator": "Equal", "value": "management", "effect": "NoSchedule"}],
                "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "fsGroup": 1000},
                "containers": [{"name": "bootstrap", "image": IMAGE, "imagePullPolicy": "IfNotPresent",
                    "command": ["sh", "-ec"], "args": [
                        DEPENDENCY_INSTALL_COMMAND + "; "
                        "python /config/bootstrap_backends.py configure --terraform-output /config/terraform-output.json --rds-ca /config/rds-ca.pem"],
                    "env": [{"name": "PYTHONPATH", "value": "/tmp/dependencies"}, {"name": "AWS_REGION", "value": "ap-southeast-1"}, {"name": "AWS_STS_REGIONAL_ENDPOINTS", "value": "regional"}],
                    "resources": {"requests": {"cpu": "100m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": "512Mi"}},
                    "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}},
                    "volumeMounts": [{"name": "config", "mountPath": "/config", "readOnly": True}, {"name": "tmp", "mountPath": "/tmp"}]}],
                "volumes": [{"name": "config", "configMap": {"name": name}}, {"name": "tmp", "emptyDir": {}}],
            }},
        }},
    ]}


class RunnerError(RuntimeError):
    """A safe error code/message; never wrap raw kubectl or SDK output."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(condition, message):
    if not condition:
        raise RunnerError("invalid_input", message)


def unique_name():
    return "backend-bootstrap-" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:12]


def load_outputs(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "Terraform output contains duplicate JSON keys")
            result[key] = value
        return result
    try:
        original = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_pairs)
    except (ValueError, OSError) as error:
        raise RunnerError("invalid_input", "Cannot read valid UTF-8 Terraform output JSON") from error
    return validate_outputs(original)


def validate_outputs(original):
    require(isinstance(original, dict), "Terraform output must be a JSON object")
    result = {}
    for key in OUTPUT_KEYS:
        require(key in original, "Missing Terraform output: " + key)
        field = original[key]
        require(not (isinstance(field, dict) and field.get("sensitive") is True),
                "Sensitive Terraform outputs are not accepted: " + key)
        result[key] = copy.deepcopy(output_value(original, key))
    account = result["account_id"]
    require(isinstance(account, str) and re.fullmatch(r"[0-9]{12}", account) is not None
            and account not in {"000000000000", "123456789012"}, "Supply the actual AWS account ID")
    require(result["cluster_name"] == "retail-lab-eks", "Unexpected Terraform cluster name")
    endpoint = result["cluster_endpoint"]
    require(isinstance(endpoint, str), "Invalid EKS API endpoint")
    parsed = urlsplit(endpoint)
    require(parsed.scheme == "https" and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            and parsed.path == "" and parsed.port is None and parsed.hostname is not None
            and parsed.hostname.endswith(("." + REGION + ".eks.amazonaws.com", "." + REGION + ".api.aws")),
            "EKS API endpoint must be the configured regional HTTPS endpoint")
    roles = result["workload_roles"]
    require(isinstance(roles, dict) and isinstance(roles.get("bootstrap"), str)
            and re.fullmatch(r"arn:aws:iam::" + account + r":role/retail-lab-bootstrap", roles["bootstrap"]) is not None,
            "Backend bootstrap role must match this account and lab")
    result["workload_roles"] = {"bootstrap": roles["bootstrap"]}

    def hostname(value, suffix, label):
        require(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9.-]+", value) is not None
                and value.lower().endswith(suffix) and "REPLACE" not in value.upper(), "Invalid " + label)

    def secret_arn(value):
        require(isinstance(value, str) and re.fullmatch(r"arn:aws:secretsmanager:" + REGION + ":" + account
                + r":secret:[A-Za-z0-9/_+=.@!-]+", value) is not None and "REPLACE" not in value.upper(),
                "Secret ARN must match the deployment account/region")

    hostname(result["mysql_endpoint"], "." + REGION + ".rds.amazonaws.com", "RDS endpoint")
    secret_arn(result["mysql_master_secret_arn"])
    endpoints = result["backend_endpoints"]
    require(isinstance(endpoints, dict) and set(endpoints) == {"RabbitMQEndpoint", "RedisEndpoint", "SearchEndpoint"},
            "Expected exactly the RabbitMQ, Redis and OpenSearch Terraform endpoints")
    rabbit = urlsplit(endpoints["RabbitMQEndpoint"]) if isinstance(endpoints["RabbitMQEndpoint"], str) else None
    require(rabbit is not None and rabbit.scheme == "amqps" and rabbit.port == 5671
            and not (rabbit.username or rabbit.password or rabbit.query or rabbit.fragment or rabbit.path),
            "RabbitMQ endpoint must use private AMQPS without embedded credentials")
    # Amazon MQ broker endpoints use amazonaws.com for IPv4 and on.aws for
    # dual-stack. Keep both restricted to this deployment's AWS region.
    hostname(rabbit.hostname, (".mq." + REGION + ".amazonaws.com", ".mq." + REGION + ".on.aws"),
             "RabbitMQ endpoint")
    hostname(endpoints["RedisEndpoint"], ".cache.amazonaws.com", "Redis endpoint")
    hostname(endpoints["SearchEndpoint"], "." + REGION + ".es.amazonaws.com", "OpenSearch endpoint")
    platform = result["platform_secret_arns"]
    require(isinstance(platform, dict) and set(platform) == {"mq_admin", "search_admin", "redis_dev", "redis_prod"},
            "Expected exactly four platform secret ARNs")
    for value in platform.values():
        secret_arn(value)
    return result


class Kubectl:
    def __init__(self, context, kubeconfig=None):
        require(isinstance(context, str) and bool(context.strip()), "An explicit Kubernetes context is required")
        self.base = ["kubectl", "--context", context, "--request-timeout=20s"]
        if kubeconfig:
            self.base.extend(["--kubeconfig", str(kubeconfig)])
        self.mutations = 0

    def call(self, arguments, document=None, namespaced=True):
        command = self.base + (["--namespace", NAMESPACE] if namespaced else []) + arguments
        try:
            result = subprocess.run(command, input=json.dumps(document) if document is not None else None,
                                    text=True, capture_output=True, check=False, timeout=45)
        except subprocess.TimeoutExpired as error:
            raise RunnerError("api_timeout", "Kubernetes command exceeded its timeout") from error
        except OSError as error:
            raise RunnerError("tool_unavailable", "kubectl could not be started") from error
        if result.returncode:
            text = result.stderr or ""
            code = next((code for marker, code in (("(AlreadyExists)", "already_exists"), ("(Conflict)", "conflict"),
                                                   ("(NotFound)", "not_found"), ("(Forbidden)", "forbidden")) if marker in text), "api_error")
            raise RunnerError(code, "Kubernetes command failed: " + code)
        if arguments[0] in {"create", "apply", "replace", "delete"}:
            self.mutations += 1
        return result.stdout

    def get(self, kind, name):
        value = self.call(["get", kind, name, "--ignore-not-found", "-o", "json"])
        try:
            result = json.loads(value) if value.strip() else None
        except ValueError as error:
            raise RunnerError("api_error", "Kubernetes returned invalid JSON") from error
        if result is not None and not isinstance(result, dict):
            raise RunnerError("api_error", "Kubernetes returned an unexpected object")
        return result


def validate_context(client, outputs):
    raw = client.call(["config", "view", "--minify", "-o", "json"], namespaced=False)
    try:
        context = json.loads(raw)
    except ValueError as error:
        raise RunnerError("wrong_target", "Cannot read the explicit Kubernetes context") from error
    require(isinstance(context, dict), "Unexpected Kubernetes context document")
    clusters = context.get("clusters", [])
    require(len(clusters) == 1 and clusters[0].get("cluster", {}).get("server") == outputs["cluster_endpoint"],
            "Kubernetes context endpoint differs from the Terraform cluster endpoint")


def ensure_namespace(client):
    raw = client.call(["get", "namespace", NAMESPACE, "--ignore-not-found", "-o", "json"], namespaced=False)
    if raw.strip():
        return
    try:
        client.call(["create", "-f", "-"], {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NAMESPACE}}, namespaced=False)
    except RunnerError as error:
        if error.code != "already_exists":
            raise


class Lease:
    """Fail-closed account-scoped mutex; stale holders require operator inspection."""
    def __init__(self, client, account, holder, timeout):
        self.client, self.holder = client, holder
        self.name = "backend-bootstrap-" + account
        self.duration = timeout + 300
        self.held = False

    def document(self, current=None, holder=None):
        metadata = {"name": self.name, "namespace": NAMESPACE}
        if current:
            require(bool(current.get("metadata", {}).get("resourceVersion")), "Lease has no resourceVersion")
            metadata.update({key: current["metadata"][key] for key in ("resourceVersion", "uid") if key in current["metadata"]})
        return {"apiVersion": "coordination.k8s.io/v1", "kind": "Lease", "metadata": metadata,
                "spec": {"holderIdentity": self.holder if holder is None else holder, "leaseDurationSeconds": self.duration,
                         "renewTime": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}}

    def acquire(self):
        current = self.client.get("lease", self.name)
        if current and current.get("spec", {}).get("holderIdentity"):
            raise RunnerError("lock_busy", "Another backend bootstrap owns the account lease; inspect its Job before recovering a stale lease")
        try:
            self.client.call(["replace" if current else "create", "-f", "-", "-o", "json"], self.document(current))
        except RunnerError as error:
            if error.code in {"conflict", "already_exists"}:
                raise RunnerError("lock_busy", "Another backend bootstrap acquired the account lease") from error
            raise
        self.held = True

    def release(self):
        if not self.held:
            return
        current = self.client.get("lease", self.name)
        if not current or current.get("spec", {}).get("holderIdentity") != self.holder:
            raise RunnerError("lock_lost", "Backend bootstrap no longer owns its lease")
        self.client.call(["replace", "-f", "-", "-o", "json"], self.document(current, holder=""))
        self.held = False


def job_result(job):
    conditions = job.get("status", {}).get("conditions", [])
    if any(value.get("type") == "Failed" and value.get("status") == "True" for value in conditions):
        return "failed"
    if any(value.get("type") == "Complete" and value.get("status") == "True" for value in conditions):
        return "complete"
    return "running"


def wait_job(client, name, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get("job", name)
        if not job:
            raise RunnerError("job_missing", "Submitted backend bootstrap Job is missing")
        state = job_result(job)
        if state == "failed":
            raise RunnerError("job_failed", "Backend bootstrap Job failed; inspect the safe report and retained Job")
        if state == "complete":
            return job
        time.sleep(min(5, max(0, deadline - time.monotonic())))
    raise RunnerError("job_timeout", "Backend bootstrap Job did not finish before its deadline")


def collect_diagnostics(client, name):
    result = {"safe_log_lines": [], "pod_reasons": []}
    try:
        logs = client.call(["logs", "job/" + name, "--tail=40", "--limit-bytes=8192"])
        patterns = (r"Configured six MySQL databases/users, RabbitMQ vhosts/users, OpenSearch roles, Redis URLs and platform credentials\.",
                    r"Set the optional Alertmanager notification configuration after selecting a recipient\.",
                    r"Dependency installation failed",
                    r"Backend bootstrap failed \([A-Za-z_][A-Za-z0-9_]*\)\. Check prerequisite configuration and private connectivity\.")
        result["safe_log_lines"] = [line for line in logs.splitlines() if any(re.fullmatch(pattern, line) for pattern in patterns)]
        result["omitted_log_lines"] = len(logs.splitlines()) - len(result["safe_log_lines"])
    except RunnerError:
        result["logs_available"] = False
    try:
        pods = json.loads(client.call(["get", "pods", "-l", "job-name=" + name, "-o", "json"]))
        allowed = {"ImagePullBackOff", "ErrImagePull", "CrashLoopBackOff", "CreateContainerConfigError",
                   "ContainerCreating", "PodInitializing", "InvalidImageName", "CreateContainerError", "Unschedulable"}
        for pod in pods.get("items", []):
            status = pod.get("status", {})
            for condition in status.get("conditions", []):
                if condition.get("reason") in allowed:
                    result["pod_reasons"].append(condition["reason"])
            for container in status.get("containerStatuses", []):
                reason = container.get("state", {}).get("waiting", {}).get("reason")
                if reason in allowed:
                    result["pod_reasons"].append(reason)
        result["pod_reasons"] = sorted(set(result["pod_reasons"]))
    except (RunnerError, ValueError):
        result["pod_status_available"] = False
    return result


def stop_owned_job(client, name):
    job = client.get("job", name)
    if not job:
        return True
    require(job.get("metadata", {}).get("labels", {}).get("retail.aws/run-id") == name,
            "Refusing to delete a Job without this bootstrap run identity")
    if job_result(job) in {"complete", "failed"}:
        return True
    client.call(["delete", "job", name, "--cascade=foreground", "--wait=true", "--timeout=30s"])
    return client.get("job", name) is None


def run(outputs, client, name, timeout, report):
    report["phase"] = "target"
    validate_context(client, outputs)
    report["phase"] = "namespace"
    ensure_namespace(client)
    lease = Lease(client, outputs["account_id"], name, timeout)
    report["lease_name"] = lease.name
    submitted = False
    terminal = False
    try:
        report["phase"] = "lease"
        lease.acquire()
        manifest = resources(outputs, name, timeout)
        report["phase"] = "submit"
        for item in manifest["items"][1:]:
            if item["kind"] == "Job":
                submitted = True  # The API may create it even if the response times out.
                client.call(["create", "-f", "-", "-o", "json"], item)
                report["job_created"] = True
            else:
                client.call(["apply", "-f", "-", "-o", "json"], item)
        report["phase"] = "wait"
        wait_job(client, name, timeout)
        terminal = True
        report["diagnostics"] = collect_diagnostics(client, name)
        report["phase"] = "cleanup"
        client.call(["delete", "configmap", name, "--ignore-not-found", "--wait=true", "--timeout=20s"])
    except RunnerError:
        if submitted:
            report["diagnostics"] = collect_diagnostics(client, name)
            try:
                terminal = stop_owned_job(client, name)
                report["job_terminal_or_stopped"] = terminal
            except RunnerError:
                report["job_terminal_or_stopped"] = False
        raise
    finally:
        primary_error = sys.exc_info()[0] is not None
        if lease.held and (not submitted or terminal):
            try:
                lease.release()
                report["lease_released"] = True
            except RunnerError:
                report["lease_released"] = False
                # A failed release must be visible; do not overwrite the first failure.
                if not primary_error:
                    raise RunnerError("lease_release_failed", "Bootstrap finished but its lease could not be released; inspect it before rerunning")
        elif lease.held:
            report["lease_released"] = False
            report["lease_retained_for_recovery"] = True
        report["changed"] = client.mutations > 0


def write_report(path, report):
    if path:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex)
        temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terraform-output", type=Path, required=True)
    parser.add_argument("--context", required=True, help="Explicit kubectl context for retail-lab-eks")
    parser.add_argument("--kubeconfig", type=Path, help="Explicit kubeconfig path; the user's default is never modified")
    parser.add_argument("--report", type=Path, help="Write a non-secret JSON operation report for automation")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="Job deadline in seconds, 30 through 3600")
    parser.add_argument("--render-only", action="store_true")
    args = parser.parse_args(argv)
    name = unique_name()
    report = {"schema_version": 1, "operation": "backend_bootstrap", "status": "failed", "changed": False,
              "job_name": name, "namespace": NAMESPACE, "job_created": False,
              "started_at": datetime.now(timezone.utc).isoformat(), "phase": "input"}
    code = 1
    try:
        require(30 <= args.timeout <= 3600, "Job timeout must be between 30 and 3600 seconds")
        outputs = load_outputs(args.terraform_output)
        report.update(account_id=outputs["account_id"], cluster_name=outputs["cluster_name"])
        if args.render_only:
            print(json.dumps(resources(outputs, name, args.timeout)))
            return 0
        client = Kubectl(args.context, args.kubeconfig)
        run(outputs, client, name, args.timeout, report)
        report.update(status="success", phase="complete")
        code = 0
    except RunnerError as error:
        report.update(error_code=error.code, error=str(error))
    except (ValueError, KeyError, TypeError, OSError):
        report.update(error_code="invalid_input", error="Invalid bootstrap input or local report configuration")
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    try:
        write_report(args.report, report)
    except OSError:
        report.update(status="failed", error_code="report_write_failed", error="Cannot write the operation report")
        code = 1
    print(json.dumps(report, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

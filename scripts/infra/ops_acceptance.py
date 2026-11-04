#!/usr/bin/env python3
"""Inspect live monitoring and backup evidence without reading credential values."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import io
import json
import math
from pathlib import Path, PurePosixPath
import re
import socket
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from urllib.parse import quote

from bootstrap_backends import output_value
import run_bootstrap as runner

REGION = "ap-southeast-1"
SERVICES = ("ui", "catalog", "carts", "checkout", "orders")
MAX_ARCHIVE = 64 * 1024 * 1024


class OpsError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def require(value, code, message):
    if not value:
        raise OpsError(code, message)


def command(arguments):
    try:
        result = subprocess.run(arguments, text=True, capture_output=True, check=False, timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise OpsError("command_unavailable", "A required operator command failed or exceeded its timeout") from error
    require(result.returncode == 0, "command_failed", "A required operator API command failed; raw output was withheld")
    try:
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except ValueError as error:
        raise OpsError("invalid_response", "An operator command returned invalid JSON") from error


def aws(service, operation, *arguments):
    return command(["aws", service, operation, "--region", REGION, "--output", "json", "--no-cli-pager", *arguments])


class Target:
    def __init__(self, outputs, account, context, kubeconfig, metadata=None, gitops_dir=None):
        require(re.fullmatch(r"[0-9]{12}", account or "") is not None, "invalid_target", "An actual account ID is required")
        self.outputs = runner.load_outputs(outputs)
        require(self.outputs["account_id"] == account, "wrong_account", "Output metadata belongs to another account")
        self.account, self.context, self.kubeconfig = account, context, str(kubeconfig)
        require(bool(context) and bool(self.kubeconfig), "invalid_target", "An explicit kubeconfig and context are required")
        root = Path(__file__).resolve().parents[2]
        receipt = command([sys.executable, str(root / "ansible/helpers/check_target.py"), "--phase", "cluster",
            "--source-dir", str(root), "--gitops-dir", str(gitops_dir or root / ".ci-tools/gitops-publish"),
            "--region", REGION, "--cluster-name", "retail-lab-eks", "--account-id", account,
            "--terraform-output", str(outputs), "--terraform-metadata", str(metadata or Path(outputs).parent / "metadata.json"),
            "--kube-context", context, "--kubeconfig", self.kubeconfig])
        require(receipt.get("outputs_receipt_verified") is True and receipt.get("account_id") == account,
                "receipt_required", "Operations require the verified Terraform deployment receipt")
        raw = json.loads(Path(outputs).read_text(encoding="utf-8-sig"))
        self.metadata = {key: output_value(raw, key) for key in ("buckets", "vpc_id") if key in raw}
        identity = aws("sts", "get-caller-identity")
        require(identity.get("Account") == account, "wrong_account", "The AWS session belongs to another account")
        cluster = aws("eks", "describe-cluster", "--name", "retail-lab-eks").get("cluster", {})
        require(cluster.get("arn") == f"arn:aws:eks:{REGION}:{account}:cluster/retail-lab-eks"
                and cluster.get("status") == "ACTIVE" and cluster.get("version") == "1.36"
                and cluster.get("endpoint") == self.outputs["cluster_endpoint"],
                "wrong_cluster", "The live EKS cluster differs from the reviewed outputs")
        configured = self.kube(["config", "view", "--minify", "-o", "json"])
        clusters = configured.get("clusters", [])
        require(len(clusters) == 1 and clusters[0].get("cluster", {}).get("server") == cluster["endpoint"],
                "wrong_context", "The explicit Kubernetes context points at another API endpoint")

    def kube(self, arguments, namespace=None):
        base = ["kubectl", "--kubeconfig", self.kubeconfig, "--context", self.context, "--request-timeout=30s"]
        if namespace:
            base += ["--namespace", namespace]
        return command(base + arguments)


def timestamp(value):
    require(isinstance(value, str), "invalid_timestamp", "Expected a timestamp in backup evidence")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(parsed.tzinfo is not None, "invalid_timestamp", "Backup timestamps must include a timezone")
        return parsed
    except ValueError as error:
        raise OpsError("invalid_timestamp", "Backup evidence has an invalid timestamp") from error


def recent(value, hours, now=None):
    age = ((now or datetime.now(timezone.utc)) - timestamp(value)).total_seconds()
    require(0 <= age <= hours * 3600, "stale_backup", "Backup evidence is stale or from the future")


def validate_targets(payload, environment="dev", now=None):
    require(payload.get("status") == "success", "prometheus_api", "Prometheus targets API did not succeed")
    targets = payload.get("data", {}).get("activeTargets", [])
    result = {}
    for service in SERVICES:
        pool = f"podMonitor/monitoring/retail-{environment}-{service}/"
        selected = [target for target in targets if target.get("scrapePool", "").startswith(pool)
                    and target.get("labels", {}).get("namespace") == "retail-" + environment]
        require(selected, "missing_scrape_target", "A configured retail PodMonitor has no active scrape target: " + service)
        for target in selected:
            require(target.get("health") == "up" and not target.get("lastError"),
                    "scrape_failed", "A retail scrape target is unhealthy: " + service)
            recent(target.get("lastScrape"), 0.1, now)
        result[service] = len(selected)
    return result


def validate_query(payload):
    result = payload.get("data", {}).get("result", [])
    require(payload.get("status") == "success" and payload.get("data", {}).get("resultType") == "vector" and len(result) == 1,
            "prometheus_query", "Prometheus query engine returned no dev scrape samples")
    try:
        count = float(result[0]["value"][1])
    except (ValueError, TypeError, KeyError, IndexError) as error:
        raise OpsError("prometheus_query", "Prometheus returned malformed scrape samples") from error
    require(math.isfinite(count) and count >= len(SERVICES), "prometheus_query", "Prometheus has fewer healthy dev samples than the expected service set")
    return int(count)


def latest_velero(backups, max_age_hours, now=None):
    selected = [item for item in backups if item.get("metadata", {}).get("labels", {}).get("velero.io/schedule-name") == "retail-daily"]
    require(selected, "missing_backup", "No retail-daily Velero backup exists")
    latest = max(selected, key=lambda item: timestamp(item.get("metadata", {}).get("creationTimestamp")))
    status = latest.get("status", {})
    require(status.get("phase") == "Completed" and status.get("errors", 0) == 0,
            "backup_failed", "The latest retail-daily backup is not a clean Completed backup")
    require(status.get("progress", {}).get("itemsBackedUp", 0) > 0, "empty_backup", "The backup contains no backed-up resources")
    recent(status.get("completionTimestamp"), max_age_hours, now)
    return latest


def inspect_archive(contents, include_configmaps=False):
    require(len(contents) <= MAX_ARCHIVE, "archive_too_large", "Backup archive exceeds the bounded inspection size")
    resources = configmaps = expanded = 0
    configmap_hashes = {}
    try:
        with tarfile.open(fileobj=io.BytesIO(contents), mode="r:gz") as archive:
            for index, member in enumerate(archive):
                require(index < 10000, "archive_too_large", "Backup archive contains too many entries")
                expanded += member.size
                require(expanded <= 512 * 1024 * 1024 and member.size <= 8 * 1024 * 1024,
                        "archive_too_large", "Backup archive expansion exceeds the inspection limit")
                path = PurePosixPath(member.name)
                require(not path.is_absolute() and ".." not in path.parts and not member.issym() and not member.islnk(),
                        "invalid_archive", "Backup archive contains unsafe member paths")
                if member.isfile() and member.name.endswith(".json") and "resources" in path.parts:
                    require(member.size <= 2 * 1024 * 1024, "archive_too_large", "A backup resource exceeds the inspection size")
                    data = json.loads(archive.extractfile(member).read())
                    require(data.get("kind") != "Secret", "backup_contains_secrets", "The backup unexpectedly contains Kubernetes Secrets")
                    resources += 1
                    configmaps += data.get("kind") == "ConfigMap"
                    if include_configmaps and data.get("kind") == "ConfigMap":
                        metadata = data.get("metadata", {})
                        identity = metadata.get("namespace", "") + "/" + metadata.get("name", "")
                        configmap_hashes[identity] = configmap_digest(data)
    except (tarfile.TarError, ValueError, OSError) as error:
        raise OpsError("invalid_archive", "The S3 backup is not a readable bounded resource archive") from error
    require(resources > 0 and configmaps > 0, "empty_backup", "The archive contains no inspectable resources/ConfigMaps")
    result = {"resources_inspected": resources, "configmaps": configmaps, "sha256": hashlib.sha256(contents).hexdigest()}
    if include_configmaps:
        result["configmap_digests"] = configmap_hashes
    return result


def configmap_digest(document):
    content = {key: document[key] for key in ("data", "binaryData", "immutable") if key in document}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def backup_archive(target, backup, include_configmaps=False):
    bucket = target.metadata.get("buckets", {}).get("backup")
    require(bucket == f"retail-lab-{target.account}-backup", "wrong_bucket", "Expected the account-scoped backup bucket from Terraform outputs")
    name = backup["metadata"]["name"]
    require(re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,251}", name) is not None, "invalid_backup", "Invalid backup identity")
    key = f"velero/backups/{name}/{name}.tar.gz"
    head = aws("s3api", "head-object", "--bucket", bucket, "--key", key, "--expected-bucket-owner", target.account)
    require(0 < head.get("ContentLength", 0) <= MAX_ARCHIVE, "empty_backup", "S3 backup archive is absent, empty or exceeds the inspection limit")
    import boto3
    response = boto3.client("s3", region_name=REGION).get_object(
        Bucket=bucket, Key=key, ExpectedBucketOwner=target.account, IfMatch=head["ETag"])
    body = response["Body"]
    try:
        contents = body.read(MAX_ARCHIVE + 1)
    finally:
        body.close()
    evidence = inspect_archive(contents, include_configmaps)
    return dict(evidence, bucket=bucket, key=key, etag=head["ETag"])


def snapshot_evidence(rds, redis, max_age_hours, now=None, account=None):
    require(rds, "missing_rds_backup", "No automated RDS snapshot is available")
    database = max(rds, key=lambda item: timestamp(item.get("SnapshotCreateTime")))
    require(database.get("DBInstanceIdentifier") == "retail-lab-mysql" and database.get("Status") == "available"
            and database.get("Encrypted") is True, "invalid_rds_backup", "RDS snapshot source/status/encryption is invalid")
    recent(database["SnapshotCreateTime"], max_age_hours, now)
    require(redis, "missing_redis_backup", "No automated Redis snapshot is available")
    def cache_time(item):
        return max(timestamp(node["SnapshotCreateTime"]) for node in item.get("NodeSnapshots", []))
    try:
        cache = max(redis, key=cache_time)
    except (ValueError, KeyError) as error:
        raise OpsError("invalid_redis_backup", "Redis snapshot has no node timestamp") from error
    require(cache.get("SnapshotStatus") == "available" and cache.get("SnapshotSource") == "automated"
            and (cache.get("ReplicationGroupId") == "retail-lab-redis"
                 or str(cache.get("CacheClusterId", "")).startswith("retail-lab-redis-")),
            "invalid_redis_backup", "Redis snapshot source/status is invalid")
    recent(cache_time(cache).isoformat(), max_age_hours, now)
    if account:
        require(str(database.get("DBSnapshotArn", "")).startswith(f"arn:aws:rds:{REGION}:{account}:snapshot:")
                and str(cache.get("ARN", "")).startswith(f"arn:aws:elasticache:{REGION}:{account}:snapshot:"),
                "foreign_snapshot", "Backup snapshot identity does not match this account/region")
    return {"rds_snapshot": database["DBSnapshotIdentifier"], "redis_snapshot": cache["SnapshotName"]}


def http_json(url, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(4 * 1024 * 1024)
            return json.loads(body) if body.strip() else {}
    except (OSError, ValueError, urllib.error.HTTPError) as error:
        raise OpsError("monitoring_api", "A local monitoring API request failed") from error


@contextmanager
def forwarded(target, services, port, marker):
    selected = [service for service in services if service.get("spec", {}).get("clusterIP") not in (None, "None")
                and any(item.get("port") == port for item in service.get("spec", {}).get("ports", []))
                and marker in json.dumps(service.get("metadata", {}).get("labels", {})).lower()]
    require(len(selected) == 1, "monitoring_service", "Expected one non-headless monitoring service for " + marker)
    with socket.socket() as allocation:
        allocation.bind(("127.0.0.1", 0))
        local = allocation.getsockname()[1]
    process = subprocess.Popen(["kubectl", "--kubeconfig", target.kubeconfig, "--context", target.context,
        "--namespace", "monitoring", "port-forward", "--address", "127.0.0.1",
        "service/" + selected[0]["metadata"]["name"], f"{local}:{port}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{local}"
    try:
        for _ in range(60):
            require(process.poll() is None, "port_forward", "The monitoring port-forward stopped")
            try:
                with urllib.request.urlopen(url + "/-/ready", timeout=1) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.5)
        else:
            raise OpsError("monitoring_not_ready", "Monitoring readiness exceeded 30 seconds")
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def accept(target, max_age_hours=36, require_alerting=False, send_test_alert=False, operation_id=None, report=None):
    services = target.kube(["get", "services", "-o", "json"], "monitoring").get("items", [])
    monitors = target.kube(["get", "podmonitors.monitoring.coreos.com", "-o", "json"], "monitoring").get("items", [])
    names = {item["metadata"]["name"] for item in monitors}
    require({"retail-dev-" + service for service in SERVICES} <= names,
            "missing_podmonitor", "Expected all five reviewed dev PodMonitors")
    with forwarded(target, services, 9090, "prometheus") as url:
        scrapes = validate_targets(http_json(url + "/api/v1/targets?state=active"))
        query_samples = validate_query(http_json(url + "/api/v1/query?query=" + quote('sum(up{namespace="retail-dev"})', safe="")))
    with forwarded(target, services, 9093, "alertmanager") as url:
        status = http_json(url + "/api/v2/status")
        import yaml
        config = yaml.safe_load(status.get("config", {}).get("original", "")) or {}
        receivers = [item for item in config.get("receivers", []) if any(key.endswith("_configs") and bool(value) for key, value in item.items())]
        configured = bool(receivers)
        require(not (require_alerting or send_test_alert) or configured,
                "alert_receiver_missing", "No external Alertmanager receiver is configured")
        if send_test_alert:
            require(re.fullmatch(r"[a-z0-9][a-z0-9-]{7,39}", operation_id or "") is not None,
                    "invalid_operation", "An explicit unique operation ID is required to send a test notification")
            now = datetime.now(timezone.utc)
            if report is not None:
                report.update(changed=True, operation_id=operation_id)
            http_json(url + "/api/v2/alerts", [{"labels": {"alertname": "RetailOpsAcceptance", "severity": "info",
                       "retail_operation": operation_id}, "annotations": {"summary": "Operator-requested retail lab notification test"},
                       "startsAt": now.isoformat(), "endsAt": datetime.fromtimestamp(now.timestamp() + 300, timezone.utc).isoformat()}])
    backups = target.kube(["get", "backups.velero.io", "-o", "json"], "velero").get("items", [])
    backup = latest_velero(backups, max_age_hours)
    archive = backup_archive(target, backup)
    rds = aws("rds", "describe-db-snapshots", "--db-instance-identifier", "retail-lab-mysql", "--snapshot-type", "automated").get("DBSnapshots", [])
    redis = aws("elasticache", "describe-snapshots", "--replication-group-id", "retail-lab-redis", "--snapshot-source", "automated").get("Snapshots", [])
    snapshots = snapshot_evidence(rds, redis, max_age_hours, account=target.account)
    return {"schema_version": 1, "operation": "operations_acceptance", "account_id": target.account,
            "cluster_name": "retail-lab-eks", "status": "notification_pending" if send_test_alert else "success",
            "checked_at": datetime.now(timezone.utc).isoformat(), "scrape_targets": scrapes,
            "healthy_dev_samples": query_samples,
            "alert_receiver_configured": configured, "notification_delivery": "operator_receipt_required" if send_test_alert else "not_tested",
            "operation_id": operation_id, "velero_backup": backup["metadata"]["name"], "archive": archive,
            "snapshots": snapshots, "changed": bool(send_test_alert), "restore_proven": False}


def add_target_arguments(parser):
    parser.add_argument("--terraform-output", type=Path, required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--context", required=True)
    parser.add_argument("--kubeconfig", type=Path, required=True)
    parser.add_argument("--terraform-metadata", type=Path)
    parser.add_argument("--gitops-dir", type=Path)
    parser.add_argument("--report", type=Path, required=True)


def write_report(path, report):
    runner.write_report(path, report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_target_arguments(parser)
    parser.add_argument("--max-backup-age-hours", type=int, default=36)
    parser.add_argument("--require-alerting", action="store_true")
    parser.add_argument("--send-test-alert", action="store_true")
    parser.add_argument("--operation-id")
    args = parser.parse_args(argv)
    report = {"schema_version": 1, "status": "failed", "changed": False, "operation": "operations_acceptance"}
    try:
        require(1 <= args.max_backup_age_hours <= 168, "invalid_input", "Backup freshness must be between 1 and 168 hours")
        target = Target(args.terraform_output, args.account_id, args.context, args.kubeconfig, args.terraform_metadata, args.gitops_dir)
        report = accept(target, args.max_backup_age_hours, args.require_alerting, args.send_test_alert, args.operation_id, report)
    except (OpsError, runner.RunnerError) as error:
        report.update(error_code=error.code, error=str(error))
    except Exception:
        report.update(error_code="operator_api_failed", error="An operator API or evidence check failed; no raw response was logged")
    write_report(args.report, report)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "success" else 2 if report["status"] == "notification_pending" else 1


if __name__ == "__main__":
    raise SystemExit(main())

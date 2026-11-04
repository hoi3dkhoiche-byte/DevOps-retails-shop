"""Operational evidence must fail closed without contacting AWS or Kubernetes."""
import copy
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import ops_acceptance as ops
from test_runner import outputs

NOW = datetime(2026, 11, 4, 8, tzinfo=timezone.utc)


def targets():
    return {"status": "success", "data": {"activeTargets": [
        {"scrapePool": f"podMonitor/monitoring/retail-dev-{service}/0", "labels": {"namespace": "retail-dev"},
         "health": "up", "lastError": "", "lastScrape": NOW.isoformat()} for service in ops.SERVICES]}}


def backup(name="retail-daily-1", age=1):
    stamp = (NOW - timedelta(hours=age)).isoformat()
    return {"metadata": {"name": name, "uid": "uid-backup", "creationTimestamp": stamp,
                         "labels": {"velero.io/schedule-name": "retail-daily"}},
            "status": {"phase": "Completed", "errors": 0, "completionTimestamp": stamp,
                       "progress": {"itemsBackedUp": 12}}}


def archive(documents):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as target:
        for index, document in enumerate(documents):
            payload = json.dumps(document).encode()
            entry = tarfile.TarInfo(f"resources/{document['kind'].lower()}s/namespaces/retail-dev/item-{index}.json")
            entry.size = len(payload)
            target.addfile(entry, io.BytesIO(payload))
    return stream.getvalue()


class OperationsEvidenceTests(unittest.TestCase):
    def test_query_engine_must_have_real_finite_samples(self):
        valid = {"status": "success", "data": {"resultType": "vector", "result": [{"value": [0, "5"]}]}}
        self.assertEqual(ops.validate_query(valid), 5)
        for count in ("0", "4", "NaN", "Infinity", "bad"):
            value = copy.deepcopy(valid)
            value["data"]["result"][0]["value"][1] = count
            with self.subTest(count=count), self.assertRaises(ops.OpsError):
                ops.validate_query(value)

    def test_all_five_actual_podmonitor_pools_are_required(self):
        self.assertEqual(set(ops.validate_targets(targets(), now=NOW)), set(ops.SERVICES))
        for change in ("missing", "wrong_namespace", "wrong_pool", "down", "stale"):
            value = targets()
            entry = value["data"]["activeTargets"][0]
            if change == "missing":
                value["data"]["activeTargets"].pop(0)
            elif change == "wrong_namespace":
                entry["labels"]["namespace"] = "retail-prod"
            elif change == "wrong_pool":
                entry["scrapePool"] = "some-other-monitor"
            elif change == "down":
                entry["health"] = "down"
            else:
                entry["lastScrape"] = (NOW - timedelta(hours=1)).isoformat()
            with self.subTest(change=change), self.assertRaises(ops.OpsError):
                ops.validate_targets(value, now=NOW)

    def test_latest_failed_backup_cannot_be_hidden_by_an_older_success(self):
        failed = backup("latest", 0.1)
        failed["status"]["phase"] = "PartiallyFailed"
        with self.assertRaisesRegex(ops.OpsError, "latest"):
            ops.latest_velero([backup(), failed], 36, NOW)

    def test_empty_stale_or_error_backups_fail(self):
        for change in ("missing", "empty", "stale", "errors"):
            values = [backup()]
            if change == "missing":
                values = []
            elif change == "empty":
                values[0]["status"]["progress"]["itemsBackedUp"] = 0
            elif change == "stale":
                values = [backup(age=48)]
            else:
                values[0]["status"]["errors"] = 1
            with self.subTest(change=change), self.assertRaises(ops.OpsError):
                ops.latest_velero(values, 36, NOW)

    def test_archive_inspection_checks_real_contents_and_never_returns_values(self):
        document = {"kind": "ConfigMap", "metadata": {"namespace": "retail-dev", "name": "fixture"}, "data": {"value": "DO_NOT_REPORT"}}
        report = ops.inspect_archive(archive([document]), True)
        self.assertEqual(report["configmaps"], 1)
        self.assertEqual(report["configmap_digests"]["retail-dev/fixture"], ops.configmap_digest(document))
        self.assertNotIn("DO_NOT_REPORT", json.dumps(report))

    def test_secret_or_empty_archive_is_rejected(self):
        for docs in ([], [{"kind": "Secret", "data": {"password": "never-print"}}]):
            with self.subTest(docs=docs), self.assertRaises(ops.OpsError):
                ops.inspect_archive(archive(docs))

    def test_archive_rejects_traversal_without_extracting(self):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as target:
            entry = tarfile.TarInfo("../unsafe.json")
            target.addfile(entry, io.BytesIO())
        with self.assertRaises(ops.OpsError):
            ops.inspect_archive(stream.getvalue())

    def test_snapshot_sources_must_be_ready_encrypted_and_fresh(self):
        rds = [{"DBInstanceIdentifier": "retail-lab-mysql", "Status": "available", "Encrypted": True,
                "SnapshotCreateTime": NOW.isoformat(), "DBSnapshotIdentifier": "rds-backup"}]
        redis = [{"ReplicationGroupId": "retail-lab-redis", "SnapshotStatus": "available", "SnapshotSource": "automated",
                  "SnapshotName": "redis-backup", "NodeSnapshots": [{"SnapshotCreateTime": NOW.isoformat()}]}]
        self.assertEqual(ops.snapshot_evidence(rds, redis, 36, NOW)["redis_snapshot"], "redis-backup")
        for value in ([], [dict(rds[0], Encrypted=False)], [dict(rds[0], SnapshotCreateTime=(NOW - timedelta(days=4)).isoformat())]):
            with self.assertRaises(ops.OpsError):
                ops.snapshot_evidence(value, redis, 36, NOW)
        with self.assertRaises(ops.OpsError):
            ops.snapshot_evidence(rds, [], 36, NOW)

    def test_wrong_account_stops_before_cluster_or_kubernetes_access(self):
        with patch.object(ops.runner, "load_outputs", return_value=outputs()), \
             patch.object(ops, "command", return_value={"outputs_receipt_verified": True, "account_id": "111122223333"}), \
             patch.object(ops, "aws", return_value={"Account": "999988887777"}) as api, \
             tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "outputs.json"
            path.write_text(json.dumps(outputs()), encoding="utf-8")
            with self.assertRaisesRegex(ops.OpsError, "another account"):
                ops.Target(path, "111122223333", "context", "kubeconfig")
            self.assertEqual(api.call_count, 1)

    def test_missing_receipt_stops_before_live_operator_calls(self):
        with patch.object(ops.runner, "load_outputs", return_value=outputs()), \
             patch.object(ops, "command", return_value={"outputs_receipt_verified": False}), \
             patch.object(ops, "aws") as api:
            with self.assertRaisesRegex(ops.OpsError, "receipt"):
                ops.Target(Path("unused"), "111122223333", "context", "kubeconfig")
            api.assert_not_called()

    def test_partial_write_failure_reports_changed_and_never_raw_errors(self):
        def partial(*args):
            args[-1]["changed"] = True
            raise ValueError("SECRET_FROM_SDK")
        with tempfile.TemporaryDirectory() as directory, patch.object(ops, "Target"), patch.object(ops, "accept", side_effect=partial):
            report = Path(directory) / "report.json"
            with patch("sys.stdout", new_callable=io.StringIO) as output:
                result = ops.main(["--terraform-output", "unused", "--account-id", "111122223333", "--context", "context",
                                   "--kubeconfig", "config", "--report", str(report)])
            self.assertEqual(result, 1)
            self.assertTrue(json.loads(report.read_text())["changed"])
            self.assertNotIn("SECRET_FROM_SDK", output.getvalue() + report.read_text())


if __name__ == "__main__":
    unittest.main()

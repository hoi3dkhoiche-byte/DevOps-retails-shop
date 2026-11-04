"""Backend runner contracts: fail before wrong-target writes and retain safe evidence."""
from collections import deque
import copy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import run_bootstrap as runner


def outputs():
    account = "111122223333"
    return {"account_id": account, "cluster_name": "retail-lab-eks",
            "cluster_endpoint": "https://test.gr7.ap-southeast-1.eks.amazonaws.com",
            "workload_roles": {"bootstrap": "arn:aws:iam::" + account + ":role/retail-lab-bootstrap"},
            "mysql_endpoint": "retail.test.ap-southeast-1.rds.amazonaws.com",
            "mysql_master_secret_arn": "arn:aws:secretsmanager:ap-southeast-1:" + account + ":secret:rds!db-test-Ab1234",
            "backend_endpoints": {"RabbitMQEndpoint": "amqps://b-test-1.mq.ap-southeast-1.amazonaws.com:5671",
                                  "RedisEndpoint": "retail.test.apse1.cache.amazonaws.com",
                                  "SearchEndpoint": "vpc-retail-test.ap-southeast-1.es.amazonaws.com"},
            "platform_secret_arns": {key: "arn:aws:secretsmanager:ap-southeast-1:" + account + ":secret:/retail/platform/" + key
                                      for key in ("mq_admin", "search_admin", "redis_dev", "redis_prod")}}


class Cluster:
    def __init__(self, states=("complete",)):
        self.objects = {}
        self.calls = []
        self.mutations = 0
        self.states = deque(states)
        self.server = outputs()["cluster_endpoint"]
        self.fail_job_response = False
        self.refuse_cancel = False
        self.refuse_release = False
        self.lease_race = False
        self.logs = ("Configured six MySQL databases/users, RabbitMQ vhosts/users, OpenSearch roles, Redis URLs and platform credentials.\n"
                     "password=NEVER_PRINT_THIS\n")

    def get(self, kind, name):
        self.calls.append((["get", kind, name], None))
        item = copy.deepcopy(self.objects.get((kind, name)))
        if kind == "job" and item:
            state = self.states[0] if self.states else "running"
            if len(self.states) > 1:
                self.states.popleft()
            item["status"] = {"conditions": [{"type": "Complete" if state == "complete" else "Failed",
                                               "status": "True"}]} if state in {"complete", "failed"} else {}
        return item

    def call(self, args, document=None, namespaced=True):
        self.calls.append((args, copy.deepcopy(document)))
        if args[:2] == ["config", "view"]:
            return json.dumps({"clusters": [{"cluster": {"server": self.server}}]})
        if args[:2] == ["get", "namespace"]:
            return json.dumps({"kind": "Namespace"})
        if args[:2] == ["get", "pods"]:
            return json.dumps({"items": [{"status": {"containerStatuses": [{"state": {
                "waiting": {"reason": "ImagePullBackOff", "message": "SECRET_TOKEN"}}}]}}]})
        if args[0] == "logs":
            return self.logs
        if args[0] in {"apply", "create", "replace"}:
            kind, name = document["kind"].lower(), document["metadata"]["name"]
            if kind == "lease" and self.lease_race:
                raise runner.RunnerError("conflict", "fixture race")
            if kind == "lease" and self.refuse_release and document["spec"]["holderIdentity"] == "":
                raise runner.RunnerError("api_error", "fixture release error")
            item = copy.deepcopy(document)
            item["metadata"].update(resourceVersion=str(self.mutations + 1), uid="fixture-uid")
            self.objects[kind, name] = item
            self.mutations += 1
            if kind == "job" and self.fail_job_response:
                raise runner.RunnerError("api_timeout", "fixture response lost after create")
            return json.dumps(item)
        if args[0] == "delete":
            if args[1] == "job" and self.refuse_cancel:
                raise runner.RunnerError("api_timeout", "fixture cancellation unavailable")
            self.objects.pop((args[1], args[2]), None)
            self.mutations += 1
            return "deleted"
        raise AssertionError("Unexpected fixture command: " + repr(args))


class RunnerInputTests(unittest.TestCase):
    def test_native_and_flat_outputs_accept_windows_bom_and_drop_unneeded_values(self):
        for native in (False, True):
            value = outputs()
            if native:
                value = {key: {"value": item, "sensitive": False} for key, item in value.items()}
            value["unused_secret"] = {"value": "NOT_FOR_CONFIGMAP", "sensitive": True}
            with self.subTest(native=native), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "outputs.json"
                path.write_text(json.dumps(value), encoding="utf-8-sig")
                accepted = runner.load_outputs(path)
                self.assertEqual(outputs(), accepted)
                self.assertNotIn("NOT_FOR_CONFIGMAP", json.dumps(runner.resources(accepted, "fixture")))

    def test_duplicate_keys_and_sensitive_forwarded_fields_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "outputs.json"
            path.write_text('{"account_id":"111122223333","account_id":"999988887777"}', encoding="utf-8")
            with self.assertRaises(runner.RunnerError):
                runner.load_outputs(path)
        value = outputs()
        value["mysql_master_secret_arn"] = {"value": value["mysql_master_secret_arn"], "sensitive": True}
        with self.assertRaises(runner.RunnerError):
            runner.validate_outputs(value)

    def test_wrong_account_cluster_endpoints_and_embedded_credentials_are_rejected(self):
        invalid = [("account_id", "123456789012"), ("account_id", "999988887777"),
                   ("cluster_name", "other-cluster"), ("cluster_endpoint", "http://test.ap-southeast-1.eks.amazonaws.com"),
                   ("cluster_endpoint", "https://admin:token@test.ap-southeast-1.eks.amazonaws.com"),
                   ("mysql_master_secret_arn", "arn:aws:secretsmanager:us-east-1:111122223333:secret:test")]
        for key, value in invalid:
            candidate = outputs()
            candidate[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(runner.RunnerError):
                runner.validate_outputs(candidate)
        candidate = outputs()
        candidate["backend_endpoints"]["RabbitMQEndpoint"] = "amqps://user:secret@b-test-1.mq.ap-southeast-1.amazonaws.com:5671"
        with self.assertRaises(runner.RunnerError):
            runner.validate_outputs(candidate)

    def test_render_only_never_executes_kubectl_or_aws(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "outputs.json"
            path.write_text(json.dumps(outputs()), encoding="utf-8-sig")
            with patch.object(runner.subprocess, "run") as command, patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = runner.main(["--terraform-output", str(path), "--context", "offline", "--render-only"])
                self.assertEqual(result, 0)
                self.assertEqual(json.loads(stdout.getvalue())["kind"], "List")
            command.assert_not_called()

    def test_job_names_do_not_collide_within_one_second(self):
        names = {runner.unique_name() for _ in range(20)}
        self.assertEqual(len(names), 20)
        self.assertTrue(all(len(name) <= 63 for name in names))


class RunnerExecutionTests(unittest.TestCase):
    def test_wrong_context_stops_before_any_mutation(self):
        client = Cluster()
        client.server = "https://wrong.gr7.ap-southeast-1.eks.amazonaws.com"
        with self.assertRaises(runner.RunnerError):
            runner.run(outputs(), client, "fixture", 30, {})
        self.assertEqual(client.mutations, 0)

    def test_success_creates_unique_job_removes_own_config_and_releases_lease(self):
        client, report = Cluster(), {}
        runner.run(outputs(), client, "fixture", 30, report)
        self.assertTrue(report["job_created"])
        self.assertTrue(report["changed"])
        self.assertTrue(report["lease_released"])
        self.assertNotIn(("configmap", "fixture"), client.objects)
        self.assertIn(("job", "fixture"), client.objects)
        self.assertEqual(client.objects["lease", "backend-bootstrap-111122223333"]["spec"]["holderIdentity"], "")
        self.assertNotIn("NEVER_PRINT_THIS", json.dumps(report))
        self.assertNotIn("SECRET_TOKEN", json.dumps(report))

    def test_held_or_stale_lease_is_never_stolen(self):
        for renew in ("2000-01-01T00:00:00Z", datetime.now(timezone.utc).isoformat()):
            client = Cluster()
            client.objects["lease", "backend-bootstrap-111122223333"] = {
                "metadata": {"resourceVersion": "1"}, "spec": {"holderIdentity": "another-job", "renewTime": renew}}
            with self.subTest(renew=renew), self.assertRaisesRegex(runner.RunnerError, "Another backend bootstrap"):
                runner.run(outputs(), client, "fixture", 30, {})
            self.assertEqual(client.mutations, 0)
            self.assertNotIn(("job", "fixture"), client.objects)

    def test_compare_and_swap_race_does_not_submit_work(self):
        client = Cluster()
        client.lease_race = True
        with self.assertRaisesRegex(runner.RunnerError, "Another backend bootstrap acquired"):
            runner.run(outputs(), client, "fixture", 30, {})
        self.assertNotIn(("job", "fixture"), client.objects)

    def test_failed_job_is_reported_without_waiting_for_timeout_and_retains_evidence(self):
        client, report = Cluster(("failed",)), {}
        with patch.object(runner.time, "sleep") as sleep, self.assertRaises(runner.RunnerError) as error:
            runner.run(outputs(), client, "fixture", 30, report)
        self.assertEqual(error.exception.code, "job_failed")
        sleep.assert_not_called()
        self.assertIn(("job", "fixture"), client.objects)
        self.assertTrue(report["lease_released"])
        self.assertEqual(report["diagnostics"]["pod_reasons"], ["ImagePullBackOff"])
        self.assertNotIn("SECRET_TOKEN", json.dumps(report))

    def test_timeout_cancels_only_owned_job_before_unlocking(self):
        client, report = Cluster(("running",)), {}
        with patch.object(runner, "wait_job", side_effect=runner.RunnerError("job_timeout", "fixture timeout")), \
                self.assertRaises(runner.RunnerError):
            runner.run(outputs(), client, "fixture", 30, report)
        self.assertNotIn(("job", "fixture"), client.objects)
        self.assertTrue(report["lease_released"])
        delete_index = next(i for i, (args, _) in enumerate(client.calls) if args[:2] == ["delete", "job"])
        release_index = next(i for i, (args, doc) in enumerate(client.calls)
                             if args[0] == "replace" and doc["spec"]["holderIdentity"] == "")
        self.assertLess(delete_index, release_index)

    def test_cancellation_failure_keeps_lease_for_operator_recovery(self):
        client, report = Cluster(("running",)), {}
        client.refuse_cancel = True
        with patch.object(runner, "wait_job", side_effect=runner.RunnerError("job_timeout", "fixture timeout")), \
                self.assertRaises(runner.RunnerError):
            runner.run(outputs(), client, "fixture", 30, report)
        self.assertTrue(report["lease_retained_for_recovery"])
        self.assertEqual(client.objects["lease", "backend-bootstrap-111122223333"]["spec"]["holderIdentity"], "fixture")

    def test_lost_job_creation_response_is_not_assumed_to_mean_no_job(self):
        client, report = Cluster(("running",)), {}
        client.fail_job_response = True
        with self.assertRaises(runner.RunnerError):
            runner.run(outputs(), client, "fixture", 30, report)
        self.assertNotIn(("job", "fixture"), client.objects)
        self.assertTrue(report["lease_released"])

    def test_release_failure_is_an_error_even_after_job_success(self):
        client, report = Cluster(), {}
        client.refuse_release = True
        with self.assertRaises(runner.RunnerError) as error:
            runner.run(outputs(), client, "fixture", 30, report)
        self.assertEqual(error.exception.code, "lease_release_failed")
        self.assertFalse(report["lease_released"])

    def test_wait_deadline_is_bounded(self):
        with patch.object(runner.time, "monotonic", side_effect=[0, 31]), self.assertRaises(runner.RunnerError) as error:
            runner.wait_job(Cluster(), "fixture", 30)
        self.assertEqual(error.exception.code, "job_timeout")


class KubectlBoundaryTests(unittest.TestCase):
    def test_every_command_uses_explicit_context_kubeconfig_and_timeouts(self):
        result = subprocess.CompletedProcess([], 0, stdout="{}", stderr="")
        with patch.object(runner.subprocess, "run", return_value=result) as command:
            client = runner.Kubectl("retail-lab-eks", Path("fixture-kubeconfig.yaml"))
            client.get("job", "fixture")
        args = command.call_args.args[0]
        self.assertIn("--context", args)
        self.assertIn("--kubeconfig", args)
        self.assertIn("--request-timeout=20s", args)
        self.assertEqual(command.call_args.kwargs["timeout"], 45)
        self.assertTrue(command.call_args.kwargs["capture_output"])

    def test_cli_error_text_never_exposes_raw_credentials(self):
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="token=SHOULD_NOT_ESCAPE (Forbidden)")
        with patch.object(runner.subprocess, "run", return_value=result), self.assertRaises(runner.RunnerError) as error:
            runner.Kubectl("retail-lab-eks").get("job", "fixture")
        self.assertEqual(error.exception.code, "forbidden")
        self.assertNotIn("SHOULD_NOT_ESCAPE", str(error.exception))

    def test_subprocess_timeout_is_converted_to_safe_error(self):
        with patch.object(runner.subprocess, "run", side_effect=subprocess.TimeoutExpired(["kubectl"], 45)), \
                self.assertRaises(runner.RunnerError) as error:
            runner.Kubectl("retail-lab-eks").get("job", "fixture")
        self.assertEqual(error.exception.code, "api_timeout")


if __name__ == "__main__":
    unittest.main()

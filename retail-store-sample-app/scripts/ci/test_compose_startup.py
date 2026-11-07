"""Keep fixture downloads outside the tested-image Compose readiness budget."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import e2e


class ComposeStartupTests(unittest.TestCase):
    def stack(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        stack = e2e.E2E.__new__(e2e.E2E)
        stack.report = Path(temporary.name)
        stack.mode, stack.name, stack.env = "search", "unit-compose", {}
        stack.started = False
        stack.manifest = {"images": {service: {"local_tag": "docker.io/example/" + service + ":tested"}
                                     for service in e2e.SERVICES}}
        stack.verify_compose = mock.Mock()
        config = {"services": {service: {"image": image["local_tag"], "pull_policy": "never"}
                               for service, image in stack.manifest["images"].items()}}
        self.fixtures = {"mysql": "mysql:8.4.11", "checkout-redis": "redis:7.4.2-alpine",
                         "rabbitmq": "rabbitmq:4.2.0-management",
                         "opensearch": "opensearchproject/opensearch:3.5.0"}
        config["services"].update({service: {"image": reference} for service, reference in self.fixtures.items()})
        self.config = config
        stack.run = mock.Mock(side_effect=lambda command, **kwargs: json.dumps(self.config)
                              if "config" in command else "")
        return stack

    def test_only_backend_images_are_pulled_before_bounded_no_pull_startup(self):
        for mode in ("compose", "search"):
            with self.subTest(mode=mode):
                stack = self.stack()
                stack.mode = mode
                stack.start_compose()
                calls = stack.run.call_args_list
                pulls = [call.args[0] for call in calls if call.args[0][:2] == ["docker", "pull"]]
                self.assertEqual(pulls, [["docker", "pull", "--platform", "linux/amd64", reference]
                                         for reference in self.fixtures.values()])
                app_references = {image["local_tag"] for image in stack.manifest["images"].values()}
                self.assertTrue(all(command[-1] not in app_references for command in pulls))
                startup = calls[-1]
                self.assertEqual(startup.args[0], stack.compose("up", "--detach", "--no-build",
                    "--pull", "never", "--wait", "--wait-timeout", "360"))
                self.assertEqual(startup.kwargs["timeout"], 480)
                self.assertTrue(stack.started)
                stack.verify_compose.assert_called_once_with()

    def test_transient_throttle_or_timeout_retries_only_the_fixture(self):
        for error in (RuntimeError("HTTP 429 fixture throttle"),
                      subprocess.TimeoutExpired(["docker", "pull"], 180)):
            with self.subTest(error=type(error).__name__):
                stack = self.stack()
                first = True

                def run(command, **kwargs):
                    nonlocal first
                    if "config" in command:
                        return json.dumps(self.config)
                    if command[:2] == ["docker", "pull"] and first:
                        first = False
                        raise error
                    return ""

                stack.run.side_effect = run
                with mock.patch.object(e2e.time, "sleep") as sleep:
                    stack.start_compose()
                pulls = [call for call in stack.run.call_args_list
                         if call.args[0][:2] == ["docker", "pull"]]
                self.assertEqual(len(pulls), 5)
                self.assertEqual(pulls[0], pulls[1])
                sleep.assert_called_once_with(5)
                stack.verify_compose.assert_called_once_with()

    def test_persistent_fixture_failure_stops_before_starting_containers(self):
        stack = self.stack()

        def run(command, **kwargs):
            if "config" in command:
                return json.dumps(self.config)
            raise RuntimeError("Registry unavailable")

        stack.run.side_effect = run
        with mock.patch.object(e2e.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts: mysql:8.4.11"):
                stack.start_compose()
        pulls = [call.args[0] for call in stack.run.call_args_list
                 if call.args[0][:2] == ["docker", "pull"]]
        self.assertEqual(len(pulls), 3)
        self.assertEqual(sleep.call_args_list, [mock.call(5), mock.call(10)])
        self.assertFalse(any("up" in call.args[0] for call in stack.run.call_args_list))
        self.assertFalse(stack.started)
        stack.verify_compose.assert_not_called()

    def test_invalid_app_or_fixture_configuration_fails_before_pulling(self):
        for mutation in ("app-image", "app-pull", "fixture-alias", "fixture-latest",
                         "fixture-untagged", "fixture-build", "extra-service"):
            with self.subTest(mutation=mutation):
                stack = self.stack()
                self.config = copy.deepcopy(self.config)
                if mutation == "app-image":
                    self.config["services"]["ui"]["image"] = "example/ui:wrong"
                elif mutation == "app-pull":
                    self.config["services"]["ui"]["pull_policy"] = "always"
                elif mutation == "fixture-alias":
                    self.config["services"]["opensearch"]["image"] = stack.manifest["images"]["ui"]["local_tag"]
                elif mutation == "fixture-latest":
                    self.config["services"]["opensearch"]["image"] = "opensearchproject/opensearch:latest"
                elif mutation == "fixture-untagged":
                    self.config["services"]["opensearch"]["image"] = "registry.example:5000/opensearch"
                elif mutation == "fixture-build":
                    self.config["services"]["opensearch"]["build"] = {"context": "unexpected"}
                else:
                    self.config["services"]["unexpected"] = {"image": "example/extra:1.0"}
                with self.assertRaises(RuntimeError):
                    stack.start_compose()
                self.assertEqual(stack.run.call_count, 1)
                self.assertFalse(stack.started)
                stack.verify_compose.assert_not_called()


class TimeoutDiagnosticTests(unittest.TestCase):
    def stack(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        stack = e2e.E2E.__new__(e2e.E2E)
        stack.report, stack.env = Path(temporary.name), {}
        return stack

    def test_timeout_keeps_output_and_still_fails_the_command(self):
        stack = self.stack()
        failure = subprocess.TimeoutExpired(["docker", "pull", "fixture:1.0"], 180,
                                            output=b"partial progress\n", stderr=b"registry pending\n")
        process = mock.Mock(pid=1234)
        process.communicate.side_effect = [failure, ("partial progress\nfinal progress\n", "registry pending\n")]
        with mock.patch.object(e2e.subprocess, "Popen", return_value=process), \
                mock.patch.object(e2e.os, "killpg", create=True) as kill:
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                stack.run(failure.cmd, timeout=180)
        self.assertIs(caught.exception, failure)
        kill.assert_called_once_with(process.pid, e2e.signal.SIGTERM)
        log = (stack.report / "commands.log").read_text(encoding="utf-8")
        self.assertIn("+ docker pull fixture:1.0", log)
        self.assertIn("partial progress\nfinal progress", log)
        self.assertIn("registry pending", log)

    def test_exited_process_still_keeps_bytes_from_timeout(self):
        stack = self.stack()
        failure = subprocess.TimeoutExpired(["docker", "pull", "fixture:1.0"], 180,
                                            output=b"downloaded layers\n", stderr=b"slow registry\n")
        process = mock.Mock(pid=1234)
        process.communicate.side_effect = failure
        with mock.patch.object(e2e.subprocess, "Popen", return_value=process), \
                mock.patch.object(e2e.os, "killpg", create=True, side_effect=ProcessLookupError):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                stack.run(failure.cmd, timeout=180)
        self.assertIs(caught.exception, failure)
        log = (stack.report / "commands.log").read_text(encoding="utf-8")
        self.assertIn("downloaded layers", log)
        self.assertIn("slow registry", log)

    def test_process_ignoring_termination_is_killed_and_output_is_preserved(self):
        stack = self.stack()
        failure = subprocess.TimeoutExpired(["docker", "pull", "fixture:1.0"], 180,
                                            output=b"partial progress\n")
        process = mock.Mock(pid=1234)
        process.communicate.side_effect = [failure, subprocess.TimeoutExpired(failure.cmd, 10),
                                           ("partial progress\nshutdown progress\n", "terminated\n")]
        with mock.patch.object(e2e.subprocess, "Popen", return_value=process), \
                mock.patch.object(e2e.os, "killpg", create=True) as kill, \
                mock.patch.object(e2e.signal, "SIGKILL", 9, create=True):
            with self.assertRaises(subprocess.TimeoutExpired) as caught:
                stack.run(failure.cmd, timeout=180)
        self.assertIs(caught.exception, failure)
        self.assertEqual(kill.call_args_list, [mock.call(process.pid, e2e.signal.SIGTERM),
                                              mock.call(process.pid, 9)])
        log = (stack.report / "commands.log").read_text(encoding="utf-8")
        self.assertIn("partial progress\nshutdown progress", log)
        self.assertIn("terminated", log)


if __name__ == "__main__":
    unittest.main()

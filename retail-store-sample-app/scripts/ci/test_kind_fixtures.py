"""Keep Kind fixtures available without pod-time registry access."""
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest import mock

import e2e
import images


class KindFixtureTests(unittest.TestCase):
    def export(self, command, architecture="amd64", **kwargs):
        # Exercise the real archive/config/checksum readers using small Docker
        # exports; only the external Docker process is replaced in these tests.
        if command[:3] == ["docker", "image", "save"]:
            path = Path(command[command.index("--output") + 1])
            reference = command[-1]
            config = json.dumps({"os": "linux", "architecture": architecture,
                                 "config": {"Labels": {"fixture": reference}}}).encode()
            manifest = json.dumps([{"Config": "config.json", "RepoTags": [reference], "Layers": []}]).encode()
            with tarfile.open(path, "w") as archive:
                for name, content in (("config.json", config), ("manifest.json", manifest)):
                    member = tarfile.TarInfo(name)
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
        return ""

    def stack(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        stack = e2e.E2E.__new__(e2e.E2E)
        stack.report = directory / "reports"
        stack.report.mkdir()
        stack.kubeconfig = directory / "runtime" / "kind.yaml"
        stack.env = {"KIND_NODE_IMAGE": "kindest/node:v1.35.0@sha256:" + "a" * 64}
        stack.name, stack.context, stack.namespace = "unit-kind", "kind-unit-kind", "retail-ci"
        stack.started, stack.evidence = False, {}
        stack.run = mock.Mock(side_effect=self.export)
        stack.manifest = {"images": {service: {"repository": "docker.io/example/" + service,
                                               "tag": "unit-test"} for service in e2e.SERVICES}}
        return stack

    def test_transient_registry_throttle_or_timeout_recovers(self):
        for error in (RuntimeError("HTTP 429 Too Many Requests"),
                      subprocess.TimeoutExpired(["docker", "pull"], 180)):
            with self.subTest(error=type(error).__name__):
                stack = self.stack()
                throttled = False

                def run(command, **kwargs):
                    nonlocal throttled
                    if command[:2] == ["docker", "pull"] and not throttled:
                        throttled = True
                        raise error
                    return self.export(command, **kwargs)

                stack.run.side_effect = run
                with mock.patch.object(e2e.time, "sleep") as sleep:
                    stack.prepare_kind_fixtures()
                commands = [call.args[0] for call in stack.run.call_args_list]
                pulls = [command for command in commands if command[:2] == ["docker", "pull"]]
                exports = [command for command in commands if command[:3] == ["docker", "image", "save"]]
                self.assertEqual(len(pulls), 4)
                self.assertEqual(len(exports), 3)
                self.assertEqual(stack.run.call_args_list[0].args, stack.run.call_args_list[1].args)
                sleep.assert_called_once_with(5)
                recorded = json.loads((stack.report / "kind-fixtures.json").read_text(encoding="utf-8"))
                self.assertEqual(recorded, stack.evidence["kind_fixtures"])
                self.assertEqual(set(recorded), {"mysql", "rabbitmq", "redis"})
                for command in exports:
                    self.assertIn("--platform", command)
                    self.assertEqual(command[command.index("--platform") + 1], "linux/amd64")
                for value in recorded.values():
                    archive = stack.fixture_directory / value["archive"]
                    self.assertEqual(value["platform"], "linux/amd64")
                    self.assertEqual(value["config_digest"], images.archive_config_id(archive, value["reference"]))
                    self.assertEqual(value["archive_sha256"], images.sha256_file(archive))

    def test_persistent_throttle_fails_before_cluster_creation(self):
        stack = self.stack()
        stack.run.side_effect = RuntimeError("HTTP 429 Too Many Requests: Data limit exceeded")
        with mock.patch.object(e2e.time, "sleep") as sleep:
            with self.assertRaisesRegex(RuntimeError, "after 3 attempts:.*mysql") as caught:
                stack.start_kind()
        self.assertIn("429", str(caught.exception))
        self.assertIn("Data limit exceeded", str(caught.exception))
        self.assertEqual(stack.run.call_count, 3)
        attempted = [call.args[0] for call in stack.run.call_args_list]
        self.assertTrue(all(command == attempted[0] for command in attempted))
        self.assertEqual(attempted[0], ["docker", "pull", "--platform", "linux/amd64",
                                        "docker.io/library/mysql:8.4.11"])
        self.assertEqual(sleep.call_args_list, [mock.call(5), mock.call(10)])
        self.assertFalse(stack.started)
        self.assertNotIn("kind_fixtures", stack.evidence)
        self.assertEqual(list(stack.fixture_directory.iterdir()), [])

    def test_wrong_image_platform_fails_before_cluster_creation(self):
        stack = self.stack()
        stack.run.side_effect = lambda command, **kwargs: self.export(command, architecture="arm64", **kwargs)
        with mock.patch.object(e2e.time, "sleep") as sleep:
            with self.assertRaisesRegex(images.ContractError, "Wrong archive platform"):
                stack.start_kind()
        self.assertEqual(stack.run.call_count, 2)
        self.assertTrue(all(call.args[0][0] == "docker" for call in stack.run.call_args_list))
        sleep.assert_not_called()
        self.assertFalse(stack.started)

    def test_fixture_consumers_use_the_preloaded_docker_hub_versions(self):
        expected = {"mysql": "docker.io/library/mysql:8.4.11",
                    "rabbitmq": "docker.io/library/rabbitmq:4.2.0-management",
                    "redis": "docker.io/library/redis:7.4.2-alpine"}
        actual = {name: value["repository"] + ":" + value["tag"]
                  for name, value in e2e.KIND_FIXTURES.items()}
        self.assertEqual(actual, expected)
        fixture = json.loads((e2e.APP / "scripts/ci/fixtures/mysql.json").read_text(encoding="utf-8"))
        mysql = next(item for item in fixture["items"] if item["kind"] == "StatefulSet")
        container = mysql["spec"]["template"]["spec"]["containers"][0]
        self.assertEqual((container["image"], container["imagePullPolicy"]), (expected["mysql"], "Never"))
        stack = self.stack()
        for service, backend in (("orders", "rabbitmq"), ("checkout", "redis")):
            with self.subTest(service=service):
                image = stack.chart_values(service)[backend]["image"]
                self.assertEqual(image["repository"] + ":" + image["tag"], expected[backend])
                self.assertEqual(image["pullPolicy"], "Never")

    def test_all_fixtures_are_imported_before_the_first_pod_manifest(self):
        stack = self.stack()

        class StopAtApply(Exception):
            pass

        def run(command, **kwargs):
            if command[0] == "kubectl" and "apply" in command:
                raise StopAtApply()
            return self.export(command, **kwargs)

        stack.run.side_effect = run
        with self.assertRaises(StopAtApply):
            stack.start_kind()
        commands = [call.args[0] for call in stack.run.call_args_list]
        apply_index = next(index for index, command in enumerate(commands)
                           if command[0] == "kubectl" and "apply" in command)
        imports = [command for command in commands[:apply_index] if command[:3] == ["kind", "load", "image-archive"]]
        self.assertEqual({command[-1] for command in imports},
                         {stack.fixture_directory / value["archive"]
                          for value in stack.evidence["kind_fixtures"].values()})
        self.assertEqual(len(imports), 3)

    def test_modified_fixture_archive_or_config_stops_before_importing_pods(self):
        for mutation in ("checksum", "config"):
            with self.subTest(mutation=mutation):
                stack = self.stack()
                prepare = stack.prepare_kind_fixtures

                def tamper():
                    prepare()
                    fixture = stack.evidence["kind_fixtures"]["mysql"]
                    if mutation == "checksum":
                        with (stack.fixture_directory / fixture["archive"]).open("ab") as archive:
                            archive.write(b"modified after preparation")
                    else:
                        fixture["config_digest"] = "sha256:" + "f" * 64

                stack.prepare_kind_fixtures = tamper
                with self.assertRaisesRegex(RuntimeError, "Fixture archive " + mutation + " mismatch"):
                    stack.start_kind()
                commands = [call.args[0] for call in stack.run.call_args_list]
                self.assertFalse(any(command[:2] == ["kind", "load"] for command in commands))
                self.assertFalse(any(command[0] == "kubectl" for command in commands))


if __name__ == "__main__":
    unittest.main()

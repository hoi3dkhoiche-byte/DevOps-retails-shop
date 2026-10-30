"""Regression tests for failed/skipped gates and immutable image publication."""
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import tomllib
import unittest
from unittest import mock
import xml.etree.ElementTree as ET

import checks
import e2e
import gate
import images
import plan


class ToolchainConfigTests(unittest.TestCase):
    def test_scanner_install_and_runtime_version_are_the_same(self):
        tools = tomllib.loads((images.APP_ROOT / ".mise.toml").read_text(encoding="utf-8"))["tools"]
        workflow = (images.APP_ROOT.parent / ".github/workflows/ci.yml").read_text(encoding="utf-8")
        expected = '  TRIVY_VERSION: "' + tools["trivy"] + '"'
        self.assertEqual(workflow.count(expected), 1)


class SelectionTests(unittest.TestCase):
    def test_service_and_deleted_paths(self):
        selected = plan.select_paths(["retail-store-sample-app/src/cart/deleted.java",
                                      "retail-store-sample-app/src/orders/new.java"], "pull_request")
        self.assertEqual(selected["services"], ["cart", "orders"])

    def test_shared_and_unknown_changes_require_all(self):
        for path in (".github/workflows/ci.yml", ".gitattributes", "retail-store-sample-app/.mise.toml",
                     "retail-store-sample-app/yarn.lock", "retail-store-sample-app/scripts/ci/checks.py",
                     "retail-store-sample-app/samples/data/products.json"):
            with self.subTest(path=path):
                self.assertEqual(set(plan.select_paths([path], "pull_request")["services"]), set(images.SERVICES))

    def test_docs_only_and_terraform_are_explicit(self):
        self.assertEqual(plan.select_paths(["retail-store-sample-app/README.md"], "pull_request")["services"], [])
        value = plan.select_paths(["retail-store-sample-app/terraform/main.tf"], "pull_request")
        self.assertEqual(value["services"], [])
        self.assertTrue(value["run_iac"])

    def test_main_and_manual_always_full(self):
        for event in ("push", "workflow_dispatch"):
            self.assertEqual(set(plan.select_paths([], event)["services"]), set(images.SERVICES))

    def test_main_does_not_depend_on_an_available_previous_commit(self):
        with mock.patch.object(plan, "nx_projects") as nx:
            self.assertEqual(set(plan.affected_projects("0" * 40, "a" * 40, "push")), set(images.SERVICES))
            self.assertEqual(set(plan.affected_projects(None, "a" * 40, "workflow_dispatch")), set(images.SERVICES))
            nx.assert_not_called()


class TestEvidenceTests(unittest.TestCase):
    def test_zero_skipped_failures_and_bad_counts_fail(self):
        for value in (checks.counts(), checks.counts(skipped=5), checks.counts(passed=2, failed=1),
                      dict(checks.counts(passed=2), tests=3), {"tests": 1}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                checks.validate_counts(value)

    def test_native_junit_cases_and_suite_errors(self):
        value = checks.parse_junit(['<testsuite><testcase/><testcase><skipped/></testcase></testsuite>'])
        self.assertEqual(value["executed"], 1)
        checks.validate_counts(value)
        for document in ('<testsuite/>', '<testsuite errors="1"><testcase/></testsuite>',
                         '<testsuite><testcase><failure/></testcase></testsuite>'):
            with self.assertRaises(ValueError):
                checks.validate_counts(checks.parse_junit([document]))

    def test_go_package_success_does_not_prove_any_tests(self):
        with self.assertRaises(ValueError):
            checks.validate_counts(checks.parse_go(json.dumps({"Package": "catalog", "Action": "pass"})))

    def test_go_real_uncached_events_required(self):
        value = "\n".join(json.dumps({"Package": "catalog", "Test": "TestProducts", "Action": action})
                          for action in ("run", "pass"))
        checks.validate_counts(checks.parse_go(value))
        for event in ({"Package": "catalog", "Action": "output", "Output": "(cached)"},
                      {"Package": "catalog", "Test": "TestProducts", "Action": "run"}):
            with self.assertRaises(ValueError):
                checks.parse_go(json.dumps(event))

    def test_jest_zero_or_interrupted_is_not_success(self):
        for value in ({"success": True, "numTotalTests": 0, "testResults": []},
                      {"success": False, "numTotalTests": 1,
                       "testResults": [{"assertionResults": [{"status": "passed"}]}]}):
            with self.assertRaises(ValueError):
                checks.validate_counts(checks.parse_jest(value))

    def test_source_report_cannot_hide_missing_or_na_required_suite(self):
        specifications = checks.manifest()
        report = dict(schema=1, sha="a" * 40, service="ui", status="passed", checks=[])
        for suite, spec in specifications["ui"].items():
            result = dict(suite=suite, status="passed", returncode=0, report_files=["reports/command.log"])
            if spec.get("format"):
                result.update(checks.counts(passed=1))
                result["report_files"].append("reports/unit/native/TEST-a.xml")
            if not spec["required"]:
                result.update(status="not_applicable", reason=spec["reason"])
            report["checks"].append(result)
        checks.validate_source_report(report, "ui", "a" * 40)
        for mutation in ("missing", "stale", "na", "zero", "no_native"):
            changed = copy.deepcopy(report)
            unit = next(item for item in changed["checks"] if item["suite"] == "unit")
            if mutation == "missing":
                changed["checks"].pop()
            elif mutation == "stale":
                changed["sha"] = "b" * 40
            elif mutation == "na":
                unit["status"] = "not_applicable"
            elif mutation == "zero":
                unit.update(checks.counts())
            else:
                unit["report_files"] = ["reports/command.log"]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                checks.validate_source_report(changed, "ui", "a" * 40)


class RequiredGateTests(unittest.TestCase):
    def test_failed_cancelled_or_unexpected_skip_fails(self):
        needs = {job: {"result": "success"} for job in ("plan", "style", "source", "images", "e2e", "scan")}
        gate.validate_jobs(needs, {"any_source": True})
        for status in ("failure", "cancelled", "skipped"):
            changed = copy.deepcopy(needs)
            changed["e2e"]["result"] = status
            with self.subTest(status=status), self.assertRaises(images.ContractError):
                gate.validate_jobs(changed, {"any_source": True})

    def test_explicit_empty_selection_allows_only_expected_skips(self):
        needs = {job: {"result": "success" if job in ("plan", "style") else "skipped"}
                 for job in ("plan", "style", "source", "images", "e2e", "scan")}
        gate.validate_jobs(needs, {"any_source": False})
        needs["style"]["result"] = "skipped"
        with self.assertRaises(images.ContractError):
            gate.validate_jobs(needs, {"any_source": False})

    def test_selection_cannot_claim_docs_when_nx_or_git_selects_service(self):
        value = plan.select_paths(["retail-store-sample-app/README.md"], "pull_request")
        value.update(sha="a" * 40, paths=["retail-store-sample-app/README.md"],
                     nx_projects=list(images.SERVICES), nx_affected=[], any_source=False,
                     run_e2e=False, source_matrix={"include": []})
        with mock.patch.dict(os.environ, {"GITHUB_SHA": "a" * 40}):
            gate.validate_plan(value)
            changed = copy.deepcopy(value)
            changed["nx_affected"] = ["cart"]
            with self.assertRaises(images.ContractError):
                gate.validate_plan(changed)
            changed = copy.deepcopy(value)
            changed["paths"] = ["retail-store-sample-app/src/cart/main.java"]
            with self.assertRaises(images.ContractError):
                gate.validate_plan(changed)


class RuntimeIdentityTests(unittest.TestCase):
    def test_docker_29_manifest_identifier_resolves_to_config_digest(self):
        native, config = "sha256:" + "a" * 64, "sha256:" + "b" * 64
        inspected = {"Id": native, "Descriptor": {"digest": native,
                     "annotations": {"config.digest": config}}}
        self.assertEqual(images.config_id_from_inspect(inspected), config)
        inspected["Descriptor"]["digest"] = "sha256:" + "c" * 64
        with self.assertRaises(images.ContractError):
            images.config_id_from_inspect(inspected)

    def test_legacy_docker_config_id_remains_supported(self):
        config = "sha256:" + "b" * 64
        self.assertEqual(images.config_id_from_inspect({"Id": config}), config)
        self.assertEqual(images.config_id_from_inspect({"Id": config, "Descriptor": {
            "digest": "sha256:" + "a" * 64, "annotations": {"config.digest": config}}}), config)

    @staticmethod
    def blob(value):
        data = json.dumps(value).encode()
        return "sha256:" + hashlib.sha256(data).hexdigest(), data

    def test_config_manifest_and_index_resolve_same_runtime_image(self):
        config, config_blob = self.blob({"os": "linux", "architecture": "amd64", "rootfs": {}})
        manifest, manifest_blob = self.blob({"config": {"digest": config}})
        index, index_blob = self.blob({"manifests": [{"platform": {"os": "linux", "architecture": "amd64"},
                                                    "digest": manifest}]})
        blobs = {config: config_blob, manifest: manifest_blob, index: index_blob}
        for reference in (config, manifest, index, "docker.io/example/app@" + manifest):
            self.assertEqual(e2e.resolve_config_digest(blobs.__getitem__, reference), config)
        with self.assertRaises(RuntimeError):
            e2e.resolve_config_digest(lambda digest: b"tampered", manifest)

    def test_cypress_missing_zero_skipped_failures_and_missing_search_fail(self):
        with self.assertRaises(RuntimeError):
            e2e.junit_results([], False)
        for xml in ("<testsuite/>", "<testsuite><testcase><skipped/></testcase></testsuite>",
                    '<testsuite errors="1"><testcase/></testsuite>',
                    "<testsuite><testcase><failure/></testcase></testsuite>"):
            with mock.patch.object(e2e.ET, "parse", return_value=ET.ElementTree(ET.fromstring(xml))):
                with self.assertRaises(RuntimeError):
                    e2e.junit_results(["dummy"], False)
        with mock.patch.object(e2e.ET, "parse", return_value=ET.ElementTree(ET.fromstring("<testsuite><testcase/></testsuite>"))):
            self.assertEqual(e2e.junit_results(["dummy"], False)["passed"], 1)
            with self.assertRaises(RuntimeError):
                e2e.junit_results(["dummy"], True)


class CypressGateRevalidationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        first, last = directory / "junit-a.xml", directory / "junit-z.xml"
        first.write_text('<testsuite><testcase name="return results for a valid keyword"/>'
                         '<testcase name="shared name"/></testsuite>', encoding="utf-8")
        last.write_text('<testsuite><testcase name="shared name"/></testsuite>', encoding="utf-8")
        self.native = [first, last]
        self.metadata = {"source_commit": "a" * 40, "run": {"id": "123", "attempt": "1"},
                         "images": {service: {"image_id": "sha256:" + "b" * 64} for service in images.SERVICES}}
        snapshot = {service: {"image_id": image["image_id"], "config_digest": image["image_id"]}
                    for service, image in self.metadata["images"].items()}
        self.evidence = {"status": "passed", "mode": "compose", "source_commit": self.metadata["source_commit"],
                         "run": self.metadata["run"], "backend_profile": "mysql-redis-opensearch",
                         "cypress": e2e.junit_results(list(reversed(self.native)), False),
                         "identity_checks": [snapshot, copy.deepcopy(snapshot), copy.deepcopy(snapshot)],
                         "cart_persistence": {"items_before_restart": 1, "items_after_restart": 1,
                                              "database_quantity": 3, "quantity_after_restart": 3,
                                              "unit_price_after_restart": 123},
                         "orders_persistence": {"orders_before_restart": 1, "database_rows": 1,
                                                "orders_after_restart": 1}}

    def test_artifact_file_order_does_not_change_valid_test_evidence(self):
        for mode in ("compose", "search", "kind"):
            value = copy.deepcopy(self.evidence)
            value["mode"] = mode
            original = copy.deepcopy(value)
            with self.subTest(mode=mode):
                gate.validate_e2e(value, self.metadata, mode, self.native)
                self.assertEqual(value, original)

    def test_missing_or_changed_cart_persistence_proof_fails(self):
        for field in ("database_quantity", "quantity_after_restart", "unit_price_after_restart"):
            value = copy.deepcopy(self.evidence)
            value["cart_persistence"][field] = 0
            with self.subTest(field=field), self.assertRaises(ValueError):
                gate.validate_e2e(value, self.metadata, "compose", self.native)
        value = copy.deepcopy(self.evidence)
        del value["cart_persistence"]
        with self.assertRaises(ValueError):
            gate.validate_e2e(value, self.metadata, "compose", self.native)

    def test_missing_extra_or_duplicated_names_still_fail(self):
        for names in (["shared name"],
                      ["shared name", "shared name", "return results for a valid keyword", "extra"],
                      ["return results for a valid keyword", "return results for a valid keyword", "shared name"],
                      "shared name", ["shared name", None]):
            value = copy.deepcopy(self.evidence)
            value["cypress"]["passed_test_names"] = names
            with self.subTest(names=names), self.assertRaises(images.ContractError):
                gate.validate_e2e(value, self.metadata, "compose", self.native)

    def test_numeric_test_count_mismatches_still_fail(self):
        for field in ("passed", "executed", "skipped"):
            value = copy.deepcopy(self.evidence)
            value["cypress"][field] += 1
            with self.subTest(field=field), self.assertRaises(images.ContractError):
                gate.validate_e2e(value, self.metadata, "compose", self.native)

    def test_changed_native_results_still_fail(self):
        for result in ("<failure/>", "<skipped/>"):
            self.native[1].write_text('<testsuite><testcase name="shared name">' + result
                                      + '</testcase></testsuite>', encoding="utf-8")
            with self.subTest(result=result), self.assertRaises((images.ContractError, RuntimeError)):
                gate.validate_e2e(self.evidence, self.metadata, "compose", self.native)


class ImageContractTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        env = dict(GITHUB_SHA="a" * 40, GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2",
                   GITHUB_REPOSITORY="hoi3dkhoiche-byte/DevOps-retails-shop", GITHUB_SERVER_URL="https://github.com",
                   GITHUB_REF="refs/heads/main", GITHUB_EVENT_NAME="push",
                   DOCKERHUB_NAMESPACE="hmannnguye", TRIVY_VERSION="0.76.1")
        patch = mock.patch.dict(os.environ, env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.expected = images.context()
        self.metadata = dict(self.expected, images={})
        for service in images.SERVICES:
            repository, tag = images.repository_for(service), images.tag_for(self.expected)
            local_tag = repository + ":" + tag
            config = json.dumps({"os": "linux", "architecture": "amd64", "rootfs": {},
                                 "config": {"Labels": {"service": service}}}).encode()
            image_id = "sha256:" + hashlib.sha256(config).hexdigest()
            config_name = image_id.removeprefix("sha256:") + ".json"
            manifest = json.dumps([{"Config": config_name, "RepoTags": [local_tag.removeprefix("docker.io/")],
                                    "Layers": []}]).encode()
            path = self.directory / (service + ".tar")
            with tarfile.open(path, "w") as archive:
                for name, data in (("manifest.json", manifest), (config_name, config)):
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
            self.metadata["images"][service] = dict(repository=repository, tag=tag, local_tag=local_tag,
                archive=path.name, archive_sha256=images.sha256_file(path), image_id=image_id)
        self.save()

    def save(self):
        images.write_json_atomic(self.directory / "metadata.json", self.metadata)

    def test_valid_archive_set(self):
        self.assertEqual(set(images.verify(self.directory)["images"]), set(images.SERVICES))

    def test_wrong_commit_run_missing_service_or_unsafe_path_fails(self):
        for kind in ("commit", "run", "missing", "path", "repository"):
            changed = copy.deepcopy(self.metadata)
            if kind == "commit":
                changed["source_commit"] = "b" * 40
            elif kind == "run":
                changed["run"]["attempt"] = "999"
            elif kind == "missing":
                changed["images"].pop("orders")
            elif kind == "path":
                changed["images"]["ui"]["archive"] = "../../elsewhere"
            else:
                changed["images"]["ui"]["repository"] = "docker.io/other/image"
            with self.subTest(kind=kind), self.assertRaises(images.ContractError):
                images.validate_metadata(changed, self.expected)

    def test_byte_tampering_blocks_all_loads(self):
        with (self.directory / "orders.tar").open("ab") as stream:
            stream.write(b"tampered")
        with mock.patch.object(images, "run") as run:
            with self.assertRaises(images.ContractError):
                images.verify(self.directory, load=True)
            run.assert_not_called()

    def test_config_id_mismatch_and_duplicate_json_fail(self):
        self.metadata["images"]["ui"]["image_id"] = "sha256:" + "b" * 64
        self.save()
        with self.assertRaises(images.ContractError):
            images.verify(self.directory)
        with self.assertRaises(images.ContractError):
            images.parse_json('{"schema_version":1,"schema_version":2}')

    def test_scanner_failure_checks_all_five_and_fails(self):
        scans = []

        def fake_run(arguments, **kwargs):
            if arguments == ["trivy", "--version"]:
                return "Version: 0.76.1\n"
            scans.append(arguments)
            if len(scans) == 1:
                raise subprocess.CalledProcessError(1, arguments)
            return ""

        with mock.patch.object(images, "run", side_effect=fake_run):
            with self.assertRaises(images.ContractError):
                images.scan(self.directory)
        self.assertEqual(len(scans), 5)
        for command in scans:
            self.assertEqual(command[command.index("--pkg-types") + 1], "os,library")
            self.assertEqual(command[command.index("--severity") + 1], "HIGH,CRITICAL")
            self.assertEqual(command[command.index("--exit-code") + 1], "1")

    def test_pr_cannot_publish_or_touch_docker(self):
        with mock.patch.dict(os.environ, {"GITHUB_EVENT_NAME": "pull_request"}):
            with mock.patch.object(images, "run") as run:
                with self.assertRaises(images.ContractError):
                    images.publish(self.directory)
                run.assert_not_called()

    def test_partial_push_never_creates_complete_release(self):
        commands, pushes = [], []

        def fake_run(arguments, **kwargs):
            command = [str(arg) for arg in arguments]
            commands.append(command)
            if command[:3] == ["docker", "image", "push"]:
                pushes.append(command)
                if len(pushes) == 2:
                    raise subprocess.CalledProcessError(1, command)
            return ""

        with mock.patch.object(images, "assert_checkout"), mock.patch.object(images, "verify", return_value=self.metadata), \
             mock.patch.object(images, "inspect_image"), mock.patch.object(images, "remote_digest", return_value="sha256:" + "d" * 64), \
             mock.patch.object(images, "run", side_effect=fake_run):
            with self.assertRaises(subprocess.CalledProcessError):
                images.publish(self.directory)
        self.assertEqual(len(pushes), 2)
        self.assertFalse((self.directory / "images.json").exists())
        self.assertTrue(all("nx" not in cmd and "build" not in cmd for cmd in commands))

    def test_digest_pullback_must_match_tested_config(self):
        def inspect(reference, expected_id=None):
            if "@sha256:" in reference:
                raise images.ContractError("Wrong registry image")

        with mock.patch.object(images, "assert_checkout"), mock.patch.object(images, "verify", return_value=self.metadata), \
             mock.patch.object(images, "inspect_image", side_effect=inspect), \
             mock.patch.object(images, "remote_digest", return_value="sha256:" + "d" * 64), mock.patch.object(images, "run"):
            with self.assertRaises(images.ContractError):
                images.publish(self.directory)
        self.assertFalse((self.directory / "images.json").exists())

    def test_complete_release_contains_all_digest_references(self):
        with mock.patch.object(images, "assert_checkout"), mock.patch.object(images, "verify", return_value=self.metadata), \
             mock.patch.object(images, "inspect_image"), \
             mock.patch.object(images, "remote_digest", return_value="sha256:" + "d" * 64), mock.patch.object(images, "run"):
            images.publish(self.directory)
        result = images.read_json(self.directory / "images.json")
        images.validate_release(result, self.expected)
        for service, image in result["images"].items():
            self.assertEqual(image["reference"], image["repository"] + "@" + image["digest"])
            self.assertEqual(image["image_id"], self.metadata["images"][service]["image_id"])


class NativeReportRevalidationTests(unittest.TestCase):
    def test_passed_summary_cannot_hide_skipped_or_different_native_xml(self):
        with tempfile.TemporaryDirectory() as temporary:
            app = Path(temporary).resolve()
            reports = app / "reports/source"
            directory = reports / "ui"
            directory.mkdir(parents=True)
            report = dict(schema=1, sha="a" * 40, service="ui", status="passed", checks=[])
            for suite, spec in checks.manifest()["ui"].items():
                result = dict(suite=suite, status="passed", returncode=0, report_files=[])
                if not spec["required"]:
                    result.update(status="not_applicable", reason=spec["reason"])
                else:
                    target = directory / suite
                    target.mkdir()
                    log = target / "command.log"
                    log.write_text("command ran\n", encoding="utf-8")
                    result["report_files"].append(log.relative_to(app).as_posix())
                    if spec.get("format"):
                        native = target / "native/TEST-unit.xml"
                        native.parent.mkdir()
                        native.write_text("<testsuite><testcase/></testsuite>", encoding="utf-8")
                        result["report_files"].append(native.relative_to(app).as_posix())
                        result.update(checks.counts(passed=1))
                report["checks"].append(result)
            (directory / "report.json").write_text(json.dumps(report), encoding="utf-8")
            plan_file = app / "plan.json"
            plan_file.write_text(json.dumps(dict(schema=1, sha="a" * 40, services=["ui"], any_source=True)), encoding="utf-8")
            with mock.patch.object(checks, "APP", app), mock.patch.object(checks.subprocess, "check_output", return_value="a" * 40):
                checks.verify(plan_file, reports)
                for document in ("<testsuite><testcase><skipped/></testcase></testsuite>",
                                 "<testsuite><testcase/><testcase/></testsuite>"):
                    native = directory / "unit/native/TEST-unit.xml"
                    native.write_text(document, encoding="utf-8")
                    with self.subTest(document=document), self.assertRaises(ValueError):
                        checks.verify(plan_file, reports)


if __name__ == "__main__":
    unittest.main()

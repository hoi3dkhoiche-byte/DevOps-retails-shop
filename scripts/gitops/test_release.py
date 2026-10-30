import copy
import hashlib
import io
import json
import unittest
from unittest import mock
import zipfile

import accept_dev
import release


def fixture(run_id="123", attempt="1"):
    sha = "a" * 40
    result = {"schema_version": 1, "source_commit": sha,
              "run": {"id": run_id, "attempt": attempt,
                      "url": f"https://github.com/{release.SOURCE_REPO}/actions/runs/{run_id}/attempts/{attempt}"},
              "platform": "linux/amd64", "backend_profile": release.PROFILE, "images": {}}
    for name, repository in release.SERVICES.items():
        digest = "sha256:" + hashlib.sha256(name.encode()).hexdigest()
        result["images"][name] = {"repository": repository, "tag": f"sha-{sha}-run-{run_id}-{attempt}",
                                  "digest": digest, "reference": repository + "@" + digest,
                                  "image_id": "sha256:" + "b" * 64}
    return result


def acceptance(value):
    return {"schema_version": 1, "environment": "dev", "source_commit": value["source_commit"], "release_run": value["run"],
            "images": {service: image["reference"] for service, image in value["images"].items()},
            "checks": {"ready": True, "image_identity": True, "smoke": True}, "checked_at": "2026-10-29T00:00:00+00:00", "operator": "operator",
            "pods": {service: [{"name": service + "-pod", "ready": True, "image_reference": image["reference"],
                                "runtime_image_id": image["reference"], "verified_config_digest": image["image_id"]}]
                     for service, image in value["images"].items()}}


class ReleaseContractTests(unittest.TestCase):
    def test_complete_release_round_trips_into_helm_values_without_rebuilding(self):
        value = fixture()
        self.assertEqual(value, release.release_from_values(release.image_values(value)))
        self.assertEqual(value["images"], release.image_values(value)["images"])

    def test_missing_image_wrong_profile_wrong_mapping_and_mutable_reference_are_rejected(self):
        changes = []
        value = fixture(); value["images"].pop("cart"); changes.append(value)
        value = fixture(); value["backend_profile"] = "upstream"; changes.append(value)
        value = fixture(); value["images"]["ui"]["repository"] = "docker.io/someone/other"; changes.append(value)
        value = fixture(); value["images"]["ui"]["reference"] = value["images"]["ui"]["repository"] + ":latest"; changes.append(value)
        value = fixture(); value["schema_version"] = True; changes.append(value)
        for value in changes:
            with self.subTest(value=value), self.assertRaises(ValueError):
                release.validate_release(value)

    def test_release_must_match_exact_source_run_and_attempt(self):
        for expected in ({"run_id": 124}, {"attempt": 2}, {"source_commit": "c" * 40}):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, "Stale"):
                release.validate_release(fixture(), **expected)

    def test_duplicate_json_fields_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            release.parse_json('{"images": {}, "images": {}}')


class AttemptProofTests(unittest.TestCase):
    def setUp(self):
        self.run = {"id": 123, "run_attempt": 1, "name": "Retail CI", "path": ".github/workflows/ci.yml",
                    "repository": {"full_name": release.SOURCE_REPO}, "head_repository": {"full_name": release.SOURCE_REPO},
                    "head_branch": "main", "head_sha": "a" * 40, "event": "push", "status": "completed", "conclusion": "success"}
        self.jobs = [{"name": name, "status": "completed", "conclusion": "success", "run_id": 123, "run_attempt": 1}
                     for name in sorted(release.REQUIRED_JOBS)]

    def check(self):
        return release.validate_attempt(self.run, self.jobs, 123, 1, "a" * 40)

    def test_complete_trusted_attempt_is_accepted(self):
        self.assertEqual(self.run, self.check())

    def test_failed_only_rerun_cannot_reuse_prior_gate_results(self):
        self.jobs = [job for job in self.jobs if job["name"] in {"ci-required", "publish"}]
        with self.assertRaisesRegex(ValueError, "omitted"):
            self.check()

    def test_job_from_another_attempt_is_rejected_even_when_its_name_passes(self):
        self.jobs[0]["run_attempt"] = 2
        with self.assertRaisesRegex(ValueError, "another run/attempt"):
            self.check()

    def test_failed_cancelled_skipped_and_duplicate_jobs_fail(self):
        original = copy.deepcopy(self.jobs)
        for status in ("failure", "cancelled", "skipped"):
            self.jobs = copy.deepcopy(original)
            self.jobs[0]["conclusion"] = status
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.check()
        self.jobs = original + [original[0]]
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.check()

    def test_forks_prs_other_workflow_and_wrong_attempt_fail(self):
        original = copy.deepcopy(self.run)
        for key, value in (("head_repository", {"full_name": "someone/fork"}), ("event", "pull_request"),
                           ("head_branch", "feature"), ("path", ".github/workflows/other.yml"), ("run_attempt", 2)):
            self.run = dict(original, **{key: value})
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.check()


class ArtifactTests(unittest.TestCase):
    def archive(self, names=None):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            for name in names or ["images.json", "images.schema.json"]:
                bundle.writestr(name, json.dumps(fixture() if name == "images.json" else {"schema": 1}))
        return output.getvalue()

    def test_checksum_is_verified_before_reading_release(self):
        archive = self.archive()
        digest = "sha256:" + hashlib.sha256(archive).hexdigest()
        self.assertEqual(fixture(), release.read_archive(archive, digest)[0])
        with self.assertRaisesRegex(ValueError, "checksum"):
            release.read_archive(archive, "sha256:" + "0" * 64)

    def test_archive_cannot_contain_paths_or_extra_payloads(self):
        for names in (["../images.json", "images.schema.json"], ["images.json", "images.schema.json", "script.py"]):
            archive = self.archive(names)
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, "entries"):
                release.read_archive(archive, "sha256:" + hashlib.sha256(archive).hexdigest())

    def test_cross_host_redirect_drops_api_authorization(self):
        request = release.urllib.request.Request("https://api.github.com/file", headers={"Authorization": "Bearer secret"})
        redirected = release.SafeRedirect().redirect_request(request, None, 302, "Found", {}, "https://blob.example.com/file")
        self.assertIsNone(redirected.get_header("Authorization"))

    def test_full_ci_consumer_links_attempt_artifact_schema_and_source(self):
        value = fixture()
        schema = {"type": "object", "title": "release"}
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as bundle:
            bundle.writestr("images.json", json.dumps(value))
            bundle.writestr("images.schema.json", json.dumps(schema))
        archive = output.getvalue()
        attempt = AttemptProofTests()
        attempt.setUp()
        artifact = {"id": 42, "name": "gitops-images-123-1", "expired": False,
                    "digest": "sha256:" + hashlib.sha256(archive).hexdigest(),
                    "workflow_run": {"id": 123, "head_sha": value["source_commit"]}}
        source = mock.Mock()
        source.request.side_effect = [attempt.run, archive]
        source.pages.side_effect = [attempt.jobs, [artifact]]
        source.file.return_value = schema
        event = {"workflow_run": {"id": 123, "run_attempt": 1, "head_sha": value["source_commit"],
                                  "head_repository": {"full_name": release.SOURCE_REPO}, "head_branch": "main", "conclusion": "success"}}
        self.assertEqual(value, release.ci_release(source, event))
        source.file.assert_called_once_with("retail-store-sample-app/scripts/ci/images.schema.json", ref=value["source_commit"])


class PromotionTests(unittest.TestCase):
    def test_exact_dev_accepted_release_is_promoted_unchanged(self):
        value = fixture()
        promoted = release.validate_acceptance(acceptance(value), value, value["source_commit"])
        self.assertEqual(value, promoted)
        self.assertEqual(value["images"], release.image_values(promoted)["images"])

    def test_missing_stale_or_partial_dev_proof_blocks_promotion(self):
        value = fixture()
        proofs = [None]
        proof = acceptance(value); proof["source_commit"] = "c" * 40; proofs.append(proof)
        proof = acceptance(value); proof["release_run"] = fixture(attempt="2")["run"]; proofs.append(proof)
        proof = acceptance(value); proof["images"].pop("orders"); proofs.append(proof)
        proof = acceptance(value); proof["checks"]["smoke"] = False; proofs.append(proof)
        proof = acceptance(value); proof["checks"]["smoke"] = 1; proofs.append(proof)
        proof = acceptance(value); proof["pods"].pop("ui"); proofs.append(proof)
        proof = acceptance(value); proof["pods"]["ui"][0]["verified_config_digest"] = "sha256:" + "d" * 64; proofs.append(proof)
        for proof in proofs:
            with self.subTest(proof=proof), self.assertRaises(ValueError):
                release.validate_acceptance(proof, value, value["source_commit"])

    def test_newer_merged_release_is_not_overwritten(self):
        github = mock.Mock()
        github.file.return_value = release.image_values(fixture("124"))
        self.assertIsNone(release.propose(github, "dev", fixture("123")))
        github.request.assert_not_called()
        github.pages.assert_not_called()

    def test_newer_pending_release_is_not_overwritten(self):
        github = mock.Mock()
        github.file.side_effect = [release.image_values(fixture("122")), release.image_values(fixture("124"))]
        pull = {"number": 20, "body": "<!-- retail-gitops automation v1 -->\nGenerated",
                "head": {"repo": {"full_name": release.GITOPS_REPO}, "ref": "automation/retail-dev-images-124-1"}}
        github.pages.return_value = [pull]
        self.assertEqual(pull, release.propose(github, "dev", fixture("123")))
        github.request.assert_not_called()

    def test_empty_gitops_repo_requires_initialization(self):
        github = mock.Mock()
        github.file.return_value = None
        with self.assertRaisesRegex(ValueError, "Initialize"):
            release.propose(github, "dev", fixture())
        github.request.assert_not_called()


class RuntimeIdentityTests(unittest.TestCase):
    def test_runtime_manifest_is_linked_to_the_ci_config(self):
        value = fixture()["images"]["ui"]
        manifest = json.dumps({"config": {"digest": value["image_id"]}}, separators=(",", ":")).encode()
        child = "sha256:" + hashlib.sha256(manifest).hexdigest()
        index = json.dumps({"manifests": [{"digest": child, "platform": {"os": "linux", "architecture": "amd64"}}]}, separators=(",", ":")).encode()
        value["digest"] = "sha256:" + hashlib.sha256(index).hexdigest()
        value["reference"] = value["repository"] + "@" + value["digest"]
        with mock.patch.object(accept_dev, "run", side_effect=[index + b"\n", manifest + b"\n"]):
            self.assertEqual({value["digest"], child, value["image_id"]}, accept_dev.runtime_digests(value))

    def test_registry_manifest_tampering_is_rejected(self):
        with mock.patch.object(accept_dev, "run", return_value=b'{}\n'):
            with self.assertRaisesRegex(ValueError, "bytes"):
                accept_dev.runtime_digests(fixture()["images"]["ui"])


if __name__ == "__main__":
    unittest.main()

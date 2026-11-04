"""Verify readiness cannot succeed with missing/wrong runtime image evidence."""

import copy
import importlib.util
from pathlib import Path
import unittest

HELPER = Path(__file__).resolve().parents[1] / "helpers" / "verify_pods.py"
SPEC = importlib.util.spec_from_file_location("verify_pods", HELPER)
pods = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pods)


def fixture():
    images = {}
    results = []
    for index, service in enumerate(sorted(pods.SERVICES), 1):
        digest = "sha256:" + str(index) * 64
        image_id = "sha256:" + str(index + 4) * 64
        repository = f"docker.io/retail/{service}"
        reference = repository + "@" + digest
        images[service] = {"digest": digest, "image_id": image_id, "repository": repository, "reference": reference}
        results.append({"item": service, "resources": [{"metadata": {"name": service + "-abc"},
                       "spec": {"containers": [{"name": service, "image": reference}]},
                       "status": {"phase": "Running", "containerStatuses": [
                           {"name": service, "ready": True, "imageID": "docker-pullable://" + reference}]}}]})
    return {"images": images, "results": results}


class VerifyPodTests(unittest.TestCase):
    def test_pullable_registry_ids_pass(self):
        self.assertEqual(set(pods.verify(fixture())["verified_pods"]), pods.SERVICES)

    def test_cri_configuration_ids_pass(self):
        value = fixture()
        for result in value["results"]:
            result["resources"][0]["status"]["containerStatuses"][0]["imageID"] = "containerd://" + value["images"][result["item"]]["image_id"]
        self.assertEqual(set(pods.verify(value)["verified_pods"]), pods.SERVICES)

    def test_wrong_runtime_image_fails_even_when_spec_is_correct(self):
        value = fixture()
        value["results"][0]["resources"][0]["status"]["containerStatuses"][0]["imageID"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(ValueError, "runtime image ID differs"):
            pods.verify(value)

    def test_missing_image_status_fails(self):
        value = fixture()
        value["results"][0]["resources"][0]["status"]["containerStatuses"] = []
        with self.assertRaisesRegex(ValueError, "no ready runtime status"):
            pods.verify(value)

    def test_no_pods_fails(self):
        value = fixture()
        value["results"][0]["resources"] = []
        with self.assertRaisesRegex(ValueError, "No live pods"):
            pods.verify(value)

    def test_missing_service_fails(self):
        value = fixture()
        value["results"].pop()
        with self.assertRaisesRegex(ValueError, "all five"):
            pods.verify(value)

    def test_terminating_old_image_is_ignored(self):
        value = fixture()
        old = copy.deepcopy(value["results"][0]["resources"][0])
        old["metadata"]["deletionTimestamp"] = "2026-11-04T00:00:00Z"
        old["spec"]["containers"][0]["image"] = "old:image"
        value["results"][0]["resources"].append(old)
        pods.verify(value)


if __name__ == "__main__":
    unittest.main()

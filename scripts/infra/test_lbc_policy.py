"""Keep the controller IAM policy tied to its reviewed upstream release."""
import hashlib
import json
from pathlib import Path
import re
import unittest


POLICIES = Path(__file__).resolve().parents[2] / "infra/retail-lab/policies"


class LoadBalancerPolicyTests(unittest.TestCase):
    def test_policy_matches_reviewed_immutable_controller_release(self):
        policy = json.loads((POLICIES / "load-balancer-controller.json").read_text(encoding="utf-8"))
        provenance = json.loads((POLICIES / "load-balancer-controller.provenance.json").read_text(encoding="utf-8"))
        self.assertEqual(provenance["schema_version"], 1)
        self.assertRegex(provenance["controller_release"], r"^v[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertRegex(provenance["helm_chart_version"], r"^[0-9]+\.[0-9]+\.[0-9]+$")
        self.assertIsNotNone(re.fullmatch(r"[0-9a-f]{40}", provenance["upstream_commit"]))
        expected_url = ("https://raw.githubusercontent.com/kubernetes-sigs/aws-load-balancer-controller/"
                        + provenance["upstream_commit"] + "/docs/install/iam_policy.json")
        self.assertEqual(provenance["upstream_repository"], "kubernetes-sigs/aws-load-balancer-controller")
        self.assertEqual(provenance["upstream_path"], "docs/install/iam_policy.json")
        self.assertEqual(provenance["upstream_url"], expected_url)
        canonical = json.dumps(policy, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.assertEqual(hashlib.sha256(canonical).hexdigest(), provenance["canonical_json_sha256"],
                         "IAM policy differs from the reviewed release. Compare the complete upstream policy, "
                         "including resource conditions, before updating its provenance.")


if __name__ == "__main__":
    unittest.main()

"""Check generated infrastructure permissions are bounded and attachable."""
import unittest
import render_terraform_policy as renderer


class TerraformPolicyTests(unittest.TestCase):
    def setUp(self):
        self.parts, self.trust = renderer.render("111122223333", "ap-southeast-1", "retail-lab-111122223333-state")

    def test_policy_parts_fit_managed_policy_limit_and_preserve_statements(self):
        self.assertLessEqual(len(self.parts), 10)
        statements = [item for part in self.parts for item in part["Statement"]]
        self.assertEqual(len(statements), len({item["Sid"] for item in statements}))
        for part in self.parts:
            self.assertLessEqual(len(renderer.compact(part)), 6144)
            self.assertNotIn("__", renderer.compact(part))
        self.assertIn("StateLock", {item["Sid"] for item in statements})

    def test_oidc_is_limited_to_the_fixed_source_main_branch(self):
        statement = self.trust["Statement"][0]
        self.assertEqual(statement["Condition"]["StringEquals"], {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": "repo:hoi3dkhoiche-byte/DevOps-retails-shop:ref:refs/heads/main"})

    def test_passrole_and_dynamic_secrets_are_project_scoped(self):
        statements = {item["Sid"]: item for part in self.parts for item in part["Statement"]}
        self.assertTrue(statements["PassProjectRoles"]["Resource"].endswith(":role/retail-lab-*"))
        self.assertEqual(len(statements["CfnDynamicReferences"]["Resource"]), 4)
        self.assertNotIn("argocd", renderer.compact(statements["CfnDynamicReferences"]))
        self.assertEqual(statements["StateLock"]["Resource"],
                         "arn:aws:s3:::retail-lab-111122223333-state/retail/lab/terraform.tfstate.tflock")

    def test_fake_account_wrong_region_and_invalid_bucket_are_rejected(self):
        for args in (("123456789012", "ap-southeast-1", "valid-bucket"),
                     ("bad", "ap-southeast-1", "valid-bucket"),
                     ("111122223333", "us-east-1", "valid-bucket"),
                     ("111122223333", "ap-southeast-1", "invalid\nbucket")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                renderer.render(*args)

    def test_create_apis_without_resource_scoping_keep_regional_bounds(self):
        statements = [item for part in self.parts for item in part["Statement"]]
        for action in ("mq:CreateBroker", "acm:RequestCertificate"):
            matches = [item for item in statements if action in item["Action"]]
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0]["Resource"], "*")
            self.assertEqual(matches[0]["Condition"]["StringEquals"]["aws:RequestedRegion"], "ap-southeast-1")


if __name__ == "__main__":
    unittest.main()

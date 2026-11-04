"""Check generated infrastructure permissions are bounded and attachable."""
import fnmatch
import unittest
import render_terraform_policy as renderer


class TerraformPolicyTests(unittest.TestCase):
    def setUp(self):
        self.parts, self.trust = renderer.render("111122223333", "ap-southeast-1", "retail-lab-111122223333-state")

    def statements_for(self, action, effect):
        return [item for part in self.parts for item in part["Statement"]
                if item["Effect"] == effect and any(fnmatch.fnmatchcase(action.lower(), pattern.lower())
                                                   for pattern in item["Action"])]

    def test_node_launch_requires_a_template_in_the_deployment_account_and_region(self):
        statements = self.statements_for("ec2:RunInstances", "Allow")
        self.assertTrue(statements, "EKS validates RunInstances for launch-template node groups")
        for statement in statements:
            self.assertEqual(statement["Condition"]["StringEquals"]["aws:RequestedRegion"], "ap-southeast-1")
            template = statement["Condition"]["ArnLike"]["ec2:LaunchTemplate"]
            self.assertTrue(fnmatch.fnmatchcase("arn:aws:ec2:ap-southeast-1:111122223333:launch-template/lt-0123456789abcdef0", template))
            for other in ("arn:aws:ec2:us-east-1:111122223333:launch-template/lt-0123456789abcdef0",
                          "arn:aws:ec2:ap-southeast-1:999988887777:launch-template/lt-0123456789abcdef0"):
                self.assertFalse(fnmatch.fnmatchcase(other, template))

    def test_bucket_refresh_reads_cover_the_pinned_provider_without_other_bucket_access(self):
        for action in ("s3:GetAccelerateConfiguration", "s3:GetReplicationConfiguration"):
            statements = self.statements_for(action, "Allow")
            self.assertEqual(len(statements), 1)
            self.assertEqual(set(statements[0]["Resource"]), {
                "arn:aws:s3:::retail-lab-111122223333-logs", "arn:aws:s3:::retail-lab-111122223333-backup"})

    def test_self_role_mutation_is_explicitly_denied_but_metadata_can_refresh(self):
        role = "arn:aws:iam::111122223333:role/retail-lab-terraform"
        actions = ("iam:CreateRole", "iam:DeleteRole", "iam:UpdateRole", "iam:UpdateAssumeRolePolicy",
                   "iam:AttachRolePolicy", "iam:DetachRolePolicy", "iam:PutRolePolicy", "iam:DeleteRolePolicy",
                   "iam:PutRolePermissionsBoundary", "iam:DeleteRolePermissionsBoundary", "iam:TagRole", "iam:PassRole")
        for action in actions:
            with self.subTest(action=action):
                self.assertTrue(any(s["Resource"] == role for s in self.statements_for(action, "Deny")))
        for action in ("iam:GetRole", "iam:ListAttachedRolePolicies", "iam:ListRolePolicies"):
            self.assertFalse(self.statements_for(action, "Deny"))
            self.assertTrue(self.statements_for(action, "Allow"))

    def test_managed_policy_attachments_cannot_use_administrator_access(self):
        for action in ("iam:AttachRolePolicy", "iam:DetachRolePolicy"):
            statements = self.statements_for(action, "Allow")
            self.assertTrue(statements)
            for statement in statements:
                allowed = statement["Condition"]["ArnEquals"]["iam:PolicyARN"]
                self.assertNotIn("arn:aws:iam::aws:policy/AdministratorAccess", allowed)
                self.assertEqual(len(allowed), 7)
                self.assertTrue(all("*" not in arn for arn in allowed))

    def test_own_policy_versions_cannot_be_rewritten(self):
        for action in ("iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:DeletePolicy"):
            matches = self.statements_for(action, "Deny")
            for name in ("retail-lab-terraform", "retail-lab-terraform-02"):
                arn = "arn:aws:iam::111122223333:policy/" + name
                self.assertTrue(any(fnmatch.fnmatchcase(arn, s["Resource"]) for s in matches))

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

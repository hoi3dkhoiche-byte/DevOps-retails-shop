"""Check generated infrastructure permissions are bounded and attachable."""
import fnmatch
import json
from pathlib import Path
import subprocess
import sys
import tempfile
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
                self.assertTrue(any(fnmatch.fnmatchcase(arn, pattern)
                                    for s in matches for pattern in as_list(s["Resource"])))

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
        roles = statements["PassProjectRoles"]["Resource"]
        self.assertEqual(len(roles), 14)
        self.assertTrue(all(role.startswith("arn:aws:iam::111122223333:role/retail-lab-") and "*" not in role for role in roles))
        self.assertNotIn("arn:aws:iam::111122223333:role/retail-lab-terraform", roles)
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


def as_list(value):
    return value if isinstance(value, list) else [value]


def decision(statements, action, resource, context=None):
    """Evaluate only the operators used by these fixtures; not an AWS simulator."""
    context = context or {}
    effects = []
    for statement in statements:
        if not any(fnmatch.fnmatchcase(action.lower(), p.lower()) for p in as_list(statement["Action"])):
            continue
        if not any(fnmatch.fnmatchcase(resource, p) for p in as_list(statement["Resource"])):
            continue
        matched = True
        for operator, fields in statement.get("Condition", {}).items():
            for key, expected in fields.items():
                value = context.get(key)
                equal = value in as_list(expected)
                if operator in {"StringEquals", "ArnEquals"}: matched &= equal
                elif operator in {"ArnNotEquals", "StringNotEqualsIfExists"}: matched &= not equal
                elif operator == "ArnLike": matched &= value is not None and any(fnmatch.fnmatchcase(value, p) for p in as_list(expected))
                else: raise AssertionError("Fixture evaluator needs explicit support for " + operator)
        if matched:
            effects.append(statement["Effect"])
    return "explicitDeny" if "Deny" in effects else "allowed" if "Allow" in effects else "implicitDeny"


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        self.boundary = renderer.render_boundary("111122223333", "ap-southeast-1")
        self.parts, _ = renderer.render("111122223333", "ap-southeast-1", "retail-lab-111122223333-state")
        self.policy = [s for part in self.parts for s in part["Statement"]]
        self.arn = "arn:aws:iam::111122223333:policy/retail-lab-workload-boundary"
        self.role = "arn:aws:iam::111122223333:role/retail-lab-bootstrap"

    def test_role_creation_requires_the_protected_boundary_and_exact_known_role(self):
        for context in ({}, {"iam:PermissionsBoundary": self.arn + "-unreviewed"}):
            self.assertEqual(decision(self.policy, "iam:CreateRole", self.role, context), "explicitDeny")
        self.assertEqual(decision(self.policy, "iam:CreateRole", self.role, {"iam:PermissionsBoundary": self.arn}), "allowed")
        self.assertEqual(decision(self.policy, "iam:CreateRole", self.role + "-unexpected", {"iam:PermissionsBoundary": self.arn}), "implicitDeny")

    def test_renderer_emits_a_separate_admin_boundary_and_checksum(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, str(Path(renderer.__file__).resolve()), "--account-id", "111122223333",
                                     "--output-dir", temporary], text=True, capture_output=True, check=False, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            folder = Path(temporary)
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            boundary = json.loads((folder / manifest["workload_boundary"]["file"]).read_text(encoding="utf-8"))
            self.assertEqual(manifest["terraform_workload_boundary_arn"], self.arn)
            self.assertEqual(manifest["workload_boundary"]["canonical_sha256"], renderer.boundary_checksum(boundary))
            self.assertNotIn(self.arn, [entry["arn"] for entry in manifest["policies"]])

    def test_ci_cannot_remove_replace_or_rewrite_the_boundary(self):
        for action in ("iam:DeleteRolePermissionsBoundary", "iam:PutRolePermissionsBoundary"):
            self.assertEqual(decision(self.policy, action, self.role), "explicitDeny")
        for action in ("iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:DeletePolicy"):
            self.assertEqual(decision(self.policy, action, self.arn), "explicitDeny")

    def test_existing_unbounded_role_cannot_be_used_to_bypass_creation_guard(self):
        for action in ("iam:PutRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:AttachRolePolicy"):
            self.assertEqual(decision(self.policy, action, self.role), "explicitDeny")
        self.assertEqual(decision(self.policy, "iam:PutRolePolicy", self.role,
                                 {"iam:PermissionsBoundary": self.arn}), "allowed")

    def test_admin_inline_policy_cannot_break_the_workload_permission_ceiling(self):
        for action in ("iam:CreateRole", "iam:PutRolePolicy", "iam:UpdateAssumeRolePolicy", "iam:AttachRolePolicy",
                       "iam:PassRole", "sts:AssumeRole", "eks:CreateAccessEntry", "eks:AssociateAccessPolicy",
                       "s3:PutBucketPolicy", "secretsmanager:PutResourcePolicy", "ssm:SendCommand"):
            with self.subTest(action=action):
                self.assertEqual(decision(self.boundary["Statement"], action, "*"), "explicitDeny")

    def test_eks_and_lb_can_create_only_the_legitimate_elb_service_linked_role(self):
        role = "arn:aws:iam::111122223333:role/aws-service-role/elasticloadbalancing.amazonaws.com/AWSServiceRoleForElasticLoadBalancing"
        self.assertEqual(decision(self.boundary["Statement"], "iam:CreateServiceLinkedRole", role,
                                 {"iam:AWSServiceName": "elasticloadbalancing.amazonaws.com"}), "allowed")
        self.assertEqual(decision(self.boundary["Statement"], "iam:CreateServiceLinkedRole", role,
                                 {"iam:AWSServiceName": "other.amazonaws.com"}), "explicitDeny")

    def test_runtime_and_rotation_requests_remain_usable_but_outside_project_is_denied(self):
        context = {"aws:RequestedRegion": "ap-southeast-1"}
        for action in ("ec2:CreateVolume", "ec2:CreateNetworkInterface", "ec2:CreateNetworkInterfacePermission",
                       "ec2:CopyVolumes", "elasticloadbalancing:CreateListener"):
            self.assertEqual(decision(self.boundary["Statement"], action, "*", context), "allowed")
        self.assertEqual(decision(self.boundary["Statement"], "elasticache:ModifyUser",
                                 "arn:aws:elasticache:ap-southeast-1:111122223333:user:retail-checkout-dev"), "allowed")
        self.assertEqual(decision(self.boundary["Statement"], "elasticache:ModifyUser",
                                 "arn:aws:elasticache:ap-southeast-1:111122223333:user:other"), "implicitDeny")
        self.assertLessEqual(len(renderer.compact(self.boundary)), 6144)

    def test_repository_token_cannot_be_read_by_an_arbitrary_bounded_role(self):
        token = "arn:aws:secretsmanager:ap-southeast-1:111122223333:secret:/retail/platform/argocd/repositories-Abc123"
        self.assertEqual(decision(self.boundary["Statement"], "secretsmanager:GetSecretValue", token,
                                 {"aws:PrincipalArn": self.role}), "explicitDeny")
        self.assertEqual(decision(self.boundary["Statement"], "secretsmanager:GetSecretValue", token,
                                 {"aws:PrincipalArn": "arn:aws:iam::111122223333:role/retail-lab-eso-argocd"}), "allowed")


if __name__ == "__main__":
    unittest.main()

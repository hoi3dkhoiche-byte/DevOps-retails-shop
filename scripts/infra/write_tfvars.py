#!/usr/bin/env python3
"""Write only the non-secret variables accepted by the retail Terraform root."""
import json
import os
from pathlib import Path

ALLOWED = {"admin_cidrs", "admin_role_arn", "addon_versions", "platform_secret_arns", "domain", "hosted_zone_id", "mysql_instance_class", "mysql_multi_az", "github_oidc_provider_arn", "terraform_policy_arn"}

config = json.loads(os.environ["TFVARS_JSON"])
if not isinstance(config, dict) or set(config) - ALLOWED:
    raise ValueError("Terraform input must contain only the documented non-secret configuration keys")
path = Path("infra/retail-lab/terraform.tfvars.json")
path.write_text(json.dumps(config, indent=2) + "\n")
path.chmod(0o600)

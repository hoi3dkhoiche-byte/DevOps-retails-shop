#!/usr/bin/env python3
"""List explicit EKS 1.36 add-on candidates for review; never modifies tfvars."""
import json
from preflight import ADDONS, addon_builds, aws

result = {}
for key, name in ADDONS.items():
    response = aws("eks", "describe-addon-versions", "--kubernetes-version", "1.36", "--addon-name", name)
    result[key] = sorted(addon_builds(response))
print(json.dumps(result, indent=2))

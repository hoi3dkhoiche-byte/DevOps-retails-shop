"""Check the apply-to-bootstrap boundary does not publish state/credentials."""
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

import export_outputs


def outputs():
    values = {name: "metadata" for name in export_outputs.REQUIRED}
    values.update(account_id="111122223333", cluster_name="retail-lab-eks",
                  cluster_endpoint="https://offline.ap-southeast-1.eks.amazonaws.com")
    return {name: {"sensitive": False, "type": "string", "value": value} for name, value in values.items()}


class DeploymentOutputTests(unittest.TestCase):
    def test_optional_profile_is_preserved_and_rejects_unknown_or_sensitive_values(self):
        for profile in ("lab", "architecture"):
            value = outputs()
            value["deployment_profile"] = {"sensitive": False, "type": "string", "value": profile}
            self.assertEqual(export_outputs.export(value)["deployment_profile"], value["deployment_profile"])
        for entry in ({"sensitive": False, "type": "string", "value": "production"},
                      {"sensitive": True, "type": "string", "value": "lab"},
                      {"sensitive": False, "type": "number", "value": 1}):
            value = outputs()
            value["deployment_profile"] = entry
            with self.subTest(entry=entry), self.assertRaises(ValueError):
                export_outputs.export(value)

    def test_optional_gateway_endpoint_is_exported_with_the_real_terraform_name(self):
        value = outputs()
        value["s3_gateway_endpoint_id"] = {"sensitive": False, "type": "string", "value": "vpce-0123456789abcdef0"}
        value["s3_endpoint_id"] = {"sensitive": False, "type": "string", "value": "obsolete-alias"}
        result = export_outputs.export(value)
        self.assertEqual(result["s3_gateway_endpoint_id"], value["s3_gateway_endpoint_id"])
        self.assertNotIn("s3_endpoint_id", result)
        value["s3_gateway_endpoint_id"]["sensitive"] = True
        with self.assertRaises(ValueError):
            export_outputs.export(value)

    def test_export_contract_names_are_present_in_the_terraform_root(self):
        root = Path(__file__).resolve().parents[2] / "infra/retail-lab"
        declared = set()
        for path in root.glob("*.tf"):
            declared.update(re.findall(r'^output\s+"([^"\n]+)"\s*\{', path.read_text(encoding="utf-8"), re.M))
        self.assertLessEqual(export_outputs.ALLOWED, declared)

    def test_exports_allowlisted_native_entries_and_ignores_future_outputs(self):
        value = outputs()
        value["future_password"] = {"sensitive": True, "type": "string", "value": "never-export"}
        original = copy.deepcopy(value)
        result = export_outputs.export(value)
        self.assertEqual(set(result), export_outputs.REQUIRED)
        self.assertNotIn("never-export", json.dumps(result))
        self.assertEqual(value, original)

    def test_sensitive_or_incomplete_allowed_output_is_rejected(self):
        for change in ({"sensitive": True}, {"value": "metadata"}):
            value = outputs()
            value["domain"] = change
            with self.subTest(change=change), self.assertRaises(ValueError):
                export_outputs.export(value)
        value = outputs()
        value.pop("account_id")
        with self.assertRaises(ValueError):
            export_outputs.export(value)

    def test_nested_credentials_and_credential_urls_are_rejected(self):
        for bad in ({"password": "never-export"}, {"nested": [{"token": "never-export"}]},
                    "https://user:never-export@example.invalid", "-----BEGIN PRIVATE KEY-----"):
            value = outputs()
            value["backend_endpoints"]["value"] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                export_outputs.export(value)

    def test_wrong_cluster_or_api_is_rejected(self):
        for field, bad in (("cluster_name", "other-cluster"), ("account_id", "bad-account"),
                           ("cluster_endpoint", "https://localhost:6443"),
                           ("cluster_endpoint", "https://offline.ap-southeast-1.eks.amazonaws.com.evil.invalid")):
            value = outputs()
            value[field]["value"] = bad
            with self.subTest(field=field, bad=bad), self.assertRaises(ValueError):
                export_outputs.export(value)

    def test_cli_accepts_bom_and_binds_metadata_checksum_to_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, target, metadata = root / "input.json", root / "context.json", root / "metadata.json"
            source.write_text(json.dumps(outputs()), encoding="utf-8-sig")
            environment = dict(os.environ, GITHUB_SHA="a" * 40, GITHUB_RUN_ID="123", GITHUB_RUN_ATTEMPT="2")
            result = subprocess.run([sys.executable, str(Path(export_outputs.__file__).resolve()), "--input", str(source),
                                     "--output", str(target), "--metadata-output", str(metadata)],
                                    env=environment, text=True, capture_output=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = json.loads(metadata.read_text(encoding="utf-8"))
            self.assertEqual(recorded["outputs_sha256"], hashlib.sha256(target.read_bytes()).hexdigest())
            self.assertEqual((recorded["run_id"], recorded["run_attempt"]), ("123", "2"))


if __name__ == "__main__":
    unittest.main()

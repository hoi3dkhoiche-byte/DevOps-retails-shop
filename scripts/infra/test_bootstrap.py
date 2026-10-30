import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

import bootstrap_backends as bootstrap
import run_bootstrap


class SecretStore:
    def __init__(self):
        names = [f"/retail/{env}/{service}/mysql" for env in bootstrap.ENVIRONMENTS for service in bootstrap.SERVICES]
        names += [f"/retail/{env}/{service}/{backend}" for env in bootstrap.ENVIRONMENTS for service, backend in (("checkout", "redis"), ("orders", "rabbitmq"), ("catalog", "opensearch"))]
        self.values = {name: None for name in names}
        for _, (name, username) in bootstrap.PLATFORM.items():
            self.values[name] = {"username": username, "password": "existing-safe-password-0123456789"}

    def describe_secret(self, SecretId):
        return {"ARN": SecretId, "VersionIdsToStages": {"version": ["AWSCURRENT"]} if self.values[SecretId] else {}}

    def get_secret_value(self, SecretId):
        return {"SecretString": json.dumps(self.values[SecretId])}

    def put_secret_value(self, SecretId, SecretString):
        self.values[SecretId] = json.loads(SecretString)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.store = SecretStore()

    def test_six_mysql_schemas_and_users_remain_separate_and_idempotent(self):
        cursor = Mock()
        connection = Mock()
        connection.cursor.return_value.__enter__ = Mock(return_value=cursor)
        connection.cursor.return_value.__exit__ = Mock(return_value=False)
        bootstrap.mysql_accounts(self.store, connection, "private.rds.amazonaws.com")
        values = copy.deepcopy(self.store.values)
        bootstrap.mysql_accounts(self.store, connection, "private.rds.amazonaws.com")
        self.assertEqual(values, self.store.values)
        passwords = set()
        for env in bootstrap.ENVIRONMENTS:
            for service in bootstrap.SERVICES:
                value = self.store.values[f"/retail/{env}/{service}/mysql"]
                self.assertEqual(value["username"], service + "_" + env)
                self.assertEqual(value["database"], service + "_" + env)
                passwords.add(value["password"])
        self.assertEqual(6, len(passwords))
        for call in cursor.execute.call_args_list:
            sql = call.args[0]
            self.assertTrue(all(password not in sql for password in passwords))
            if "USER" in sql:
                self.assertIn("REQUIRE SSL", sql)

    def test_rabbit_vhosts_permissions_and_search_indices_do_not_cross_environments(self):
        put = Mock()
        admin = {"username": "admin", "password": "never-log-this-password"}
        bootstrap.rabbit_accounts(self.store, "amqps://private.mq.ap-southeast-1.amazonaws.com:5671", admin, put=put)
        paths = [call.args[1] for call in put.call_args_list]
        self.assertIn("/api/permissions/retail-dev/orders-dev", paths)
        self.assertIn("/api/permissions/retail-prod/orders-prod", paths)
        put.reset_mock()
        bootstrap.search_accounts(self.store, "vpc-private.ap-southeast-1.es.amazonaws.com", admin, put=put)
        roles = [call.args[3] for call in put.call_args_list if "/roles/" in call.args[1]]
        self.assertEqual([["products-dev"], ["products-prod"]], [role["index_permissions"][0]["index_patterns"] for role in roles])

    def test_redis_url_uses_tls_and_correct_environment_identity(self):
        platform = {key: name for key, (name, _) in bootstrap.PLATFORM.items()}
        bootstrap.redis_urls(self.store, "private.cache.amazonaws.com", platform)
        for env in bootstrap.ENVIRONMENTS:
            url = self.store.values[f"/retail/{env}/checkout/redis"]["url"]
            self.assertTrue(url.startswith("rediss://checkout-" + env + ":"))
            self.assertNotIn("checkout-" + ("prod" if env == "dev" else "dev"), url)

    def test_existing_foreign_username_is_rejected(self):
        self.store.values["/retail/dev/cart/mysql"] = {"username": "orders_prod", "password": "long-enough-existing-password"}
        with self.assertRaises(ValueError):
            bootstrap.credentials(self.store, "/retail/dev/cart/mysql", "cart_dev")

    def test_bootstrap_job_has_scoped_irsa_and_private_management_placement(self):
        outputs = {"workload_roles": {"value": {"bootstrap": "arn:aws:iam::123456789012:role/retail-lab-bootstrap"}}}
        manifest = run_bootstrap.resources(outputs, "backend-bootstrap-test")
        job = next(item for item in manifest["items"] if item["kind"] == "Job")
        pod = job["spec"]["template"]["spec"]
        self.assertEqual({"workload": "management"}, pod["nodeSelector"])
        self.assertEqual("backend-bootstrap", pod["serviceAccountName"])
        self.assertIn("@sha256:", pod["containers"][0]["image"])
        self.assertTrue(pod["containers"][0]["securityContext"]["readOnlyRootFilesystem"])
        self.assertNotIn("AWS_ACCESS_KEY_ID", json.dumps(pod))


if __name__ == "__main__":
    unittest.main()

"""Keep Compose and Kind on the same native MySQL initialization input."""
import json
from pathlib import Path
import unittest


APP = Path(__file__).resolve().parents[2]
INIT = APP / "src/app/mysql-init.sql"
FIXTURE = APP / "scripts/ci/fixtures/mysql.json"


class MySQLFixtureTests(unittest.TestCase):
    def test_compose_and_kind_run_the_same_native_sql(self):
        items = json.loads(FIXTURE.read_text(encoding="utf-8"))["items"]
        config = next(item for item in items if item.get("kind") == "ConfigMap"
                      and item["metadata"]["name"] == "ci-mysql-init")
        # A .sql suffix delegates socket selection and authentication to the
        # image entrypoint, including when the temporary server disables TCP.
        self.assertEqual(config["data"], {"10-retail.sql": INIT.read_text(encoding="utf-8")})


if __name__ == "__main__":
    unittest.main()

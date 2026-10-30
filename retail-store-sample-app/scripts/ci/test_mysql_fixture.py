"""Exercise init scripts as MySQL's Linux entrypoint sources them."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


APP = Path(__file__).resolve().parents[2]
INIT = APP / "src/app/mysql-init.sh"
FIXTURE = APP / "scripts/ci/fixtures/mysql.json"


class MySQLFixtureTests(unittest.TestCase):
    def test_compose_and_kind_run_the_same_init_script(self):
        items = json.loads(FIXTURE.read_text(encoding="utf-8"))["items"]
        config = next(item for item in items if item.get("kind") == "ConfigMap"
                      and item["metadata"]["name"] == "ci-mysql-init")
        self.assertEqual(config["data"]["10-retail.sh"], INIT.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "posix" and Path("/bin/sh").is_file(),
                         "Requires the Linux shell used by the MySQL entrypoint")
    def test_sourcing_initializes_all_databases_without_changing_parent_options(self):
        # Non-executable init scripts are sourced. A leaking `set -u` breaks the
        # entrypoint's later access to optional MYSQL_ONETIME_PASSWORD.
        shell = r'''
mysql() {
    printf 'CALL %s PASSWORD %s\n' "$*" "$MYSQL_PWD" >> "$MYSQL_TEST_LOG"
    cat >> "$MYSQL_TEST_LOG"
}
before=$-
. "$1"
test "$before" = "$-"
unset MYSQL_ONETIME_PASSWORD
printf 'entrypoint-continued:%s\n' "$MYSQL_ONETIME_PASSWORD"
'''
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "mysql.log"
            environment = dict(os.environ, MYSQL_ROOT_PASSWORD="fixture-root-password",
                               MYSQL_TEST_LOG=str(log))
            result = subprocess.run(["/bin/sh", "-e", "-c", shell, "fixture-test", str(INIT)],
                                    capture_output=True, text=True, env=environment, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("entrypoint-continued:", result.stdout)
            statements = log.read_text(encoding="utf-8")
        self.assertEqual(statements.count("CALL -uroot PASSWORD fixture-root-password"), 3)
        for service in ("catalog", "cart", "orders"):
            with self.subTest(service=service):
                self.assertIn(f"CREATE DATABASE IF NOT EXISTS {service}_ci;", statements)
                self.assertIn(f"CREATE USER IF NOT EXISTS '{service}_user'@'%'", statements)
                self.assertIn(f"GRANT ALL PRIVILEGES ON {service}_ci.* TO '{service}_user'@'%';",
                              statements)

    @unittest.skipUnless(os.name == "posix" and Path("/bin/sh").is_file(),
                         "Requires the Linux shell used by the MySQL entrypoint")
    def test_failed_database_initialization_stops_the_entrypoint(self):
        shell = r'''
mysql() { cat > /dev/null; return 42; }
. "$1"
printf 'unexpected-success\n'
'''
        result = subprocess.run(["/bin/sh", "-e", "-c", shell, "fixture-test", str(INIT)],
                                capture_output=True, text=True,
                                env=dict(os.environ, MYSQL_ROOT_PASSWORD="fixture-root-password"),
                                timeout=10)
        self.assertEqual(result.returncode, 42, result.stdout + result.stderr)
        self.assertNotIn("unexpected-success", result.stdout)


if __name__ == "__main__":
    unittest.main()
